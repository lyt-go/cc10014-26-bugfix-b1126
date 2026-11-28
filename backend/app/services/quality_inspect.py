"""质量检查领域服务：逐项检查后联动验收创建，并承接整改复检与结果更正。

几个关键约束：
* 复检必须能追溯回本工序对应的那条整改记录，缺来源或指到别的工序都不算出结果；
* 更正只能针对真实存在的原记录，同一检查项目被更正后，该项目的合格结果以更正为准；
* 签署验收后仍待整改的缺陷会挂在待复检清单里，复检合格只解除该条记录。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.inspection_coverage import (
    as_int,
    build_coverage,
    correction_source_ids,
    is_correction,
    is_passed,
    is_recheck,
    normalize,
    record_trace_error,
    recheck_source_ids,
    resolved_by_recheck,
    result_passes,
)
from app.store import store


class QualityInspectService(BaseModuleService):
    MODULE = "quality_inspect"
    ENTITY = "检查记录"
    OPERATION_TERM = "开始检查"
    LIST_FIELDS = ["检查工序", "检查项目", "技术标准", "检查结果", "不合格项", "检查人", "检查日期", "备注说明"]
    EXTRA_FIELDS = [
        "复检来源", "复检轮次", "检查类型", "检查类别", "复检标志",
        "更正来源", "更正对象", "更正", "已出具", "原检查记录",
    ]
    REQUIRED_FIELDS = ["检查工序", "检查项目", "技术标准"]
    STATUS_ORDER = ["待检查", "检查中", "已通过", "需整改"]
    ACTION_RULES = {"开始检查": "检查中", "出具报告": "已通过", "退回整改": "需整改"}
    NEGATIVE_ACTIONS = []

    REPORT_ACTIONS = {
        "出具报告", "复检", "复检通过", "复检合格", "提交复检",
        "更正", "提交更正", "更正结果",
    }
    RECHECK_ACTIONS = {"复检", "复检通过", "复检合格", "提交复检"}
    CORRECTION_ACTIONS = {"更正", "提交更正", "更正结果"}

    def action_lock_keys(self, action: str, entry: dict[str, Any]) -> list[str]:
        if action not in self.REPORT_ACTIONS:
            return []
        process = str(entry.get("检查工序") or "")
        # 先锁工序级质量检查，再锁具体记录；与签署流程的加锁顺序一致，避免死锁。
        return [f"quality_inspect:{process}"]

    def resolve_action(self, action: str, entry: dict[str, Any]) -> str | None:
        if action == "出具报告" and (is_recheck(entry) or is_correction(entry)):
            return "已通过" if result_passes(entry) else "需整改"
        if action in self.RECHECK_ACTIONS or action in self.CORRECTION_ACTIONS:
            return "已通过" if result_passes(entry) else "需整改"
        return super().resolve_action(action, entry)

    # ── 追溯辅助 ──
    def _same_item_siblings(
        self, entry_id: int, process: str, item: str
    ) -> list[dict[str, Any]]:
        key = normalize(item)
        return [
            row for row in store.rows(self.MODULE)
            if row.get("id") != entry_id
            and str(row.get("检查工序") or "") == str(process or "")
            and normalize(row.get("检查项目")) == key
        ]

    def _is_latest_correction(
        self, entry_id: int, process: str, item: str
    ) -> tuple[bool, dict[str, Any] | None]:
        """条目是否是同一项目里最新的一条更正记录。"""
        siblings = self._same_item_siblings(entry_id, process, item)
        latest = None
        latest_id = -1
        for row in siblings:
            if not is_correction(row):
                continue
            rid = as_int(row.get("id")) or 0
            if rid > latest_id:
                latest_id = rid
                latest = row
        return (latest is None or latest_id < (as_int(entry_id) or 0)), latest

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        process = entry.get("检查工序", "")
        if action in self.REPORT_ACTIONS:
            # 0. 复检记录必须追溯到本工序对应的那条整改
            if is_recheck(entry):
                error = record_trace_error(
                    entry, process, str(entry.get("检查项目") or ""), store.rows(self.MODULE)
                )
                if error:
                    return False, error
            # 0.1 更正必须指向真实存在的原记录
            if is_correction(entry):
                targets = [
                    row for row in store.rows(self.MODULE)
                    if as_int(row.get("id")) in correction_source_ids(entry)
                ]
                if not targets:
                    return False, "更正记录未关联有效的原检查记录"
                if not any(
                    str(row.get("检查工序") or "") == str(process or "") for row in targets
                ):
                    return False, "更正记录关联的原检查不属于本工序"
                latest, newer = self._is_latest_correction(
                    entry_id, process, str(entry.get("检查项目") or "")
                )
                if not latest and newer is not None:
                    return False, f"已有更新的更正记录#{newer.get('id')}，不能覆盖"
            # 1. 出具报告前检查关联工序是否完成（复检与更正不重复卡工序）
            if action == "出具报告" and not is_recheck(entry) and not is_correction(entry):
                ws_rows = store.query_by_field("work_sequence", "工序名称", process)
                if ws_rows and any(
                    r.get("status") != "已完成" and r.get("完成工序") != "已完成"
                    for r in ws_rows
                ):
                    return False, f"关联工序「{process}」尚未完成"
        if action == "开始检查":
            # 必须有对应的已排期工序
            schedules = store.query_by_field("construction_schedule", "计划工序", process)
            if schedules and not any(s.get("status") in ("执行中", "已发布") for s in schedules):
                return False, f"关联排期「{process}」尚未进入可检查状态"
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action not in self.REPORT_ACTIONS:
            return effects
        process = entry.get("检查工序", "")
        recheck = is_recheck(entry)
        correction = is_correction(entry)

        if action in self.RECHECK_ACTIONS or (recheck and action == "出具报告"):
            entry["复检"] = True
        if action in self.CORRECTION_ACTIONS or (correction and action == "出具报告"):
            entry["已出具"] = True
            for target_id in correction_source_ids(entry):
                target = store.find(self.MODULE, target_id)
                if target is not None:
                    target["已更正"] = True
                    target["更正记录"] = entry_id
                    effects.append(dict(target))

        # 1. 本条记录出具后解除“待复检”标记
        if is_passed(entry):
            entry["待复检"] = False
            entry.pop("复检来源验收", None)
            entry["pending"] = False
            entry["abnormal"] = False
        # 仍待复检的缺陷由复检清单继续跟进，不因报告出具而消失
        for row in store.rows(self.MODULE):
            if str(row.get("检查工序") or "") == process and row.get("待复检"):
                if not is_passed(row):
                    row["pending"] = True
                    row["abnormal"] = True

        # 2. 自动创建/刷新验收记录：检查清单与签署采用的检查项目保持一致
        acc_rows = store.rows("acceptance_record")
        acc = self._find_acceptance_for_process(process)
        if acc is None:
            coverage = build_coverage(
                {"验收工序": process, "验收标准": entry.get("技术标准", "")},
                store.rows(self.MODULE),
            )
            new_acc = {
                "id": max((int(r.get("id", 0)) for r in acc_rows), default=0) + 1,
                "验收工序": process,
                "验收标准": entry.get("技术标准", ""),
                "验收结论": "质量检查已通过" if is_passed(entry) else "存在需整改项",
                "遗留问题": entry.get("不合格项", "无"),
                "整改期限": "",
                "验收人": "系统自动",
                "验收日期": entry.get("检查日期", ""),
                "备注说明": f"由质量检查#{entry_id}触发创建",
                "检查清单": coverage["required_items"],
                "status": "待验收",
                "pending": True,
                "abnormal": False,
            }
            acc_rows.append(new_acc)
            effects.append(new_acc)
        else:
            # 用该验收单自己的标准重算清单，保证清单和签署采用的检查项目一致；
            # 判定回退到兼容口径时保留原有清单，避免把显式清单清空。
            coverage = build_coverage(acc, store.rows(self.MODULE))
            if coverage["required_items"]:
                acc["检查清单"] = coverage["required_items"]
            acc["覆盖检查项目"] = coverage["covered_items"]
            acc["覆盖检查记录"] = coverage["adopted_checks"]
            effects.append(dict(acc))

        # 3. 更新修缮日志状态
        for log in store.rows("restoration_log"):
            if log.get("对应工序", "") == process and log.get("status") in ("已填写", "已审核"):
                log["status"] = "已审核"
                log["pending"] = False
                effects.append(dict(log))

        # 4. 本工序缺陷全部清除时，解除工序的“完成待复检”标记
        outstanding = [
            row for row in store.rows(self.MODULE)
            if str(row.get("检查工序") or "") == process
            and (
                row.get("待复检")
                or (
                    str(row.get("status") or "") in {"需整改", "待复检", "不合格"}
                    and not resolved_by_recheck(row, store.rows(self.MODULE))
                )
            )
        ]
        if not outstanding:
            signature = self._signed_acceptance_for(process)
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process and ws_row.get("完成待复检"):
                    ws_row.pop("完成待复检", None)
                    ws_row.pop("abnormal", None)
                    if signature is not None:
                        ws_row["status"] = "已完成"
                        ws_row["pending"] = False
                        ws_row["验收签署"] = signature
                        ws_row["完成依据验收"] = signature
                    effects.append(dict(ws_row))
        return effects

    def _signed_acceptance_for(self, process: str) -> int | None:
        signed = [
            row for row in store.rows("acceptance_record")
            if str(row.get("验收工序") or "") == str(process or "")
            and row.get("status") == "已签署"
        ]
        if not signed:
            return None
        return as_int(max(signed, key=lambda r: as_int(r.get("id")) or 0).get("id"))

    def _find_acceptance_for_process(self, process: str) -> dict[str, Any] | None:
        candidates = [
            row for row in store.rows("acceptance_record")
            if str(row.get("验收工序") or "") == str(process or "")
        ]
        if not candidates:
            return None
        # 优先未签署的；都已签署时取最新一条
        for row in sorted(candidates, key=lambda r: as_int(r.get("id")) or 0, reverse=True):
            if row.get("status") != "已签署":
                return row
        return candidates[-1]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["pending_recheck"] = [
            {"id": row.get("id"), "检查工序": row.get("检查工序"),
             "检查项目": row.get("检查项目"), "status": row.get("status")}
            for row in store.rows(self.MODULE)
            if row.get("待复检")
        ]
        base["rechecks"] = [
            {"id": row.get("id"), "检查项目": row.get("检查项目"),
             "复检来源": recheck_source_ids(row)}
            for row in store.rows(self.MODULE) if is_recheck(row)
        ]
        return base
