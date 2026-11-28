"""工序编排领域服务：编排修缮工序顺序，发布工序时检查前置工序完成状态、安全交底和工匠到岗。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.inspection_coverage import as_int, build_coverage
from app.store import store


class WorkSequenceService(BaseModuleService):
    MODULE = "work_sequence"
    ENTITY = "工序卡"
    OPERATION_TERM = "发布工序"
    LIST_FIELDS = ["工序名称", "前置工序", "所需工匠", "施工区域", "预计工时", "编排人", "工序状态", "备注说明"]
    REQUIRED_FIELDS = ["工序名称", "前置工序", "所需工匠"]
    STATUS_ORDER = ["待排期", "已发布", "施工中", "已完成"]
    ACTION_RULES = {"发布工序": "已发布", "开始施工": "施工中", "完成工序": "已完成"}
    NEGATIVE_ACTIONS = []
    IDEMPOTENT_EXEMPT_ACTIONS = {"工序交接"}

    def resolve_action(self, action: str, entry: dict[str, Any]) -> str | None:
        if action == "工序交接":
            process = str(entry.get("工序名称") or "")
            if self._signed_acceptance(process) is not None:
                # 凭已签结果继续：工序按完成论，交接结果单独记录
                return "已完成"
            if self._outstanding_defects(process):
                return "待补测"
            return "待验收"
        return super().resolve_action(action, entry)

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        process_name = entry.get("工序名称", "")
        if action == "工序交接":
            # 未签署时交接动作仍可点击：结果落到“待补测/待验收”，并记录未签原因
            return True, ""
        if action == "完成工序":
            # 已有验收单时，完成必须以已签署验收为依据；尚无验收单时保留既有手工完成路径。
            acceptances = [
                row for row in store.rows("acceptance_record")
                if str(row.get("验收工序") or "") == str(process_name or "")
            ]
            if acceptances:
                signed = self._signed_acceptance(process_name)
                if signed is None:
                    return False, f"工序「{process_name}」验收尚未签署，不能标记完成"
                if self._outstanding_defects(process_name):
                    return False, f"工序「{process_name}」仍有待整改缺陷，完成结果以复检为准"
        if action == "发布工序":
            # 1. 检查前置工序是否已完成
            prereq = entry.get("前置工序", "")
            if prereq and prereq != "无":
                matched = False
                for row in store.rows("work_sequence"):
                    if row.get("工序名称", "") == prereq and row.get("id") != entry_id:
                        matched = True
                        if row.get("status") != "已完成":
                            return False, f"前置工序「{prereq}」尚未完成，当前状态「{row.get('status', '')}」"
                        break
                if not matched and prereq != "无":
                    return False, f"前置工序「{prereq}」在系统中不存在"
            # 2. 检查关联安全交底是否已签认
            process_name = entry.get("工序名称", "")
            safety_records = store.query_by_field("safety_briefing", "交底工序", process_name)
            if not safety_records:
                return False, f"工序「{process_name}」缺少安全交底记录"
            unsigned = [r for r in safety_records if r.get("status") != "已签认"]
            if unsigned:
                return False, f"安全交底未完全签认，尚有{len(unsigned)}条待签认"
        if action == "开始施工":
            process_name = entry.get("工序名称", "")
            # 检查工匠是否已到岗
            craftsmen = store.query_by_field("craftsman_assign", "分配工序", process_name)
            if craftsmen and any(c.get("status") != "已到岗" for c in craftsmen):
                return False, "该工序的工匠尚未全部到岗"
            # 检查关联排期是否已发布
            schedules = store.query_by_field("construction_schedule", "计划工序", process_name)
            if schedules:
                unreleased = [s for s in schedules if s.get("status") == "编制中"]
                if unreleased:
                    return False, "关联施工排期还未发布"
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "工序交接":
            evaluation = self.evaluate_handover(entry_id)
            entry["交接判定"] = evaluation
            entry["交接依据验收"] = evaluation.get("依据验收")
            if evaluation["ok"]:
                entry["交接结果"] = "已交接"
                entry["pending"] = False
                entry["abnormal"] = False
                effects.append(dict(entry))
            else:
                entry["交接结果"] = entry.get("status")
                entry["pending"] = True
                entry["abnormal"] = True
                effects.append(dict(entry))
        if action == "完成工序":
            process_name = entry.get("工序名称", "")
            signed = self._signed_acceptance(process_name)
            if signed is not None:
                receipt = signed.get("签署回执") or {}
                # 完成结果注明依据哪张验收、采用了哪次复检，和签署回执对得上
                entry["完成依据验收"] = signed.get("id")
                entry["完成采用复检"] = dict(receipt.get("采用复检记录", {}))
                entry["完成待复检"] = bool(receipt.get("仍待复检的检查记录"))
            # 工序完成后联动更新关联施工排期
            for sched in store.rows("construction_schedule"):
                if sched.get("计划工序", "") == process_name:
                    if sched.get("status") == "执行中":
                        sched["里程碑节点"] = f"{process_name}已完成"
                        effects.append(dict(sched))
            # 联动更新修缮日志中关联日志
            for log in store.rows("restoration_log"):
                if log.get("对应工序", "") == process_name:
                    if log.get("status") in ("已审核", "已填写"):
                        log["status"] = "已归档"
                        log["pending"] = False
                        effects.append(dict(log))
        return effects

    # ── 工序交接 ──
    def _signed_acceptance(self, process: str) -> dict[str, Any] | None:
        candidates = [
            row for row in store.rows("acceptance_record")
            if str(row.get("验收工序") or "") == str(process or "")
            and row.get("status") in {"已签署", "已交接"}
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: int(r.get("id", 0) or 0))

    def _outstanding_defects(self, process: str) -> list[dict[str, Any]]:
        coverage = build_coverage(
            {"验收工序": process, "验收标准": ""}, store.rows("quality_inspect")
        )
        ids = {item["检查记录"] for item in coverage["pending_records"]}
        return [
            row for row in store.rows("quality_inspect")
            if as_int(row.get("id")) in ids
        ]

    def evaluate_handover(self, entry_id: int) -> dict[str, Any]:
        """工序交接判定：凭已签验收继续交接，否则按未签原因安排补测。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"工序卡 {entry_id} 不存在"}
        process = str(entry.get("工序名称") or "")
        signed = self._signed_acceptance(process)
        defects = self._outstanding_defects(process)
        acceptances = [
            row for row in store.rows("acceptance_record")
            if str(row.get("验收工序") or "") == process
        ]
        if signed is not None:
            receipt = signed.get("签署回执") or {}
            return {
                "ok": True,
                "can_handover": True,
                "工序": process,
                "依据验收": signed.get("id"),
                "验收状态": signed.get("status"),
                "采用检查记录": receipt.get("采用检查记录", {}),
                "采用复检记录": receipt.get("采用复检记录", {}),
                "仍待复检": [
                    {"检查记录": row.get("id"), "检查项目": row.get("检查项目")}
                    for row in defects
                ],
                "reason": f"验收#{signed.get('id')}已签署，可凭签署结果交接",
            }
        unsigned = [row for row in acceptances if row.get("status") != "已签署"]
        if defects:
            reason = "存在待整改/待复检项目，需安排补测后再交接"
        elif not acceptances:
            reason = "尚无验收记录，需先补做质量检查并签署验收"
        else:
            statuses = "、".join(str(row.get("status") or "") for row in unsigned)
            reason = f"验收尚未签署（当前：{statuses}），需补测或补签后再交接"
        return {
            "ok": False,
            "can_handover": False,
            "工序": process,
            "依据验收": None,
            "验收状态": "未签署" if acceptances else "无验收记录",
            "补测要求": [
                {"检查记录": row.get("id"), "检查项目": row.get("检查项目"),
                 "状态": row.get("status"), "不合格项": row.get("不合格项", "")}
                for row in defects
            ],
            "待签验收": [{"验收单": row.get("id"), "状态": row.get("status")} for row in unsigned],
            "reason": reason,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 返回工序依赖图
        all_rows = store.rows(self.MODULE)
        base["dependency_graph"] = [
            {"工序": r.get("工序名称"), "前置": r.get("前置工序"), "状态": r.get("status")}
            for r in all_rows
        ]
        base["completed"] = [r.get("工序名称") for r in all_rows if r.get("status") == "已完成"]
        return base