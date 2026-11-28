"""验收记录领域服务：签署验收前逐项核对检查覆盖，签署后登记回执并同步复检清单。

签署验收的判定口径：
1. 验收标准（或检查清单）里列出的每个检查项目，都必须有一条属于本工序的合格记录；
2. 同一检查项目的多条合格记录只算一次，不能拿来顶替另一个缺项；
3. 复检记录必须能追溯回本工序对应的整改记录，其他工序的同名检查不计入；
4. 已被后续更正推翻的合格结果不再作为签署依据。

兼容既有记录：验收标准没有逐项列举（例如只写标准号）时，沿用“本工序至少一条
质量检查通过”的口径，历史验收单仍可正常签署。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.inspection_coverage import (
    as_int,
    build_coverage,
    coverage_reason,
    is_recheck,
    resolved_by_recheck,
)
from app.store import store


class AcceptanceRecordService(BaseModuleService):
    MODULE = "acceptance_record"
    ENTITY = "验收报告"
    OPERATION_TERM = "开始验收"
    LIST_FIELDS = ["验收工序", "验收标准", "验收结论", "遗留问题", "整改期限", "验收人", "验收日期", "备注说明"]
    EXTRA_FIELDS = ["检查清单"]
    REQUIRED_FIELDS = ["验收工序", "验收标准", "验收结论"]
    STATUS_ORDER = ["待验收", "验收中", "已签署", "待整改"]
    ACTION_RULES = {"开始验收": "验收中", "签署验收": "已签署", "退回整改": "待整改"}
    NEGATIVE_ACTIONS = []

    # ── 签署回执 ──
    RECEIPT_FIELD = "签署回执"

    def resolve_action(self, action: str, entry: dict[str, Any]) -> str | None:
        if action == "签署验收":
            # 签署结论仍区分“合格签署”与“带遗留问题签署”，但两者都算已签
            return "已签署"
        return super().resolve_action(action, entry)

    def action_lock_keys(self, action: str, entry: dict[str, Any]) -> list[str]:
        if action not in {"签署验收", "退回整改"}:
            return []
        process = str(entry.get("验收工序") or "")
        keys = [f"quality_inspect:{process}"]
        # 再锁本工序的每条检查记录，与复检/更正保持一致
        for row in store.rows("quality_inspect"):
            if str(row.get("检查工序") or "") == process:
                keys.append(f"quality_inspect:{row.get('id')}")
        return keys

    def _pending_quality(self, process: str) -> list[dict[str, Any]]:
        """本工序仍待整改/待复检的质量检查记录（用于签署后继续复查）。"""
        coverage = build_coverage(
            {"验收工序": process, "验收标准": ""},
            store.rows("quality_inspect"),
        )
        ids = {item["检查记录"] for item in coverage["pending_records"]}
        return [
            row for row in store.rows("quality_inspect")
            if as_int(row.get("id")) in ids
        ]

    def _receipt(self, entry: dict[str, Any], entry_id: int, action: str) -> dict[str, Any]:
        """生成签署回执：说明这次签署实际采用了哪些检查、哪些复检。"""
        process = str(entry.get("验收工序") or "")
        coverage = build_coverage(entry, store.rows("quality_inspect"))
        adopted_checks = {
            str(item): row.get("id") for item, row in coverage["coverage"].items()
        }
        pending = self._pending_quality(process)
        return {
            "验收单": entry_id,
            "动作": action,
            "验收工序": process,
            "签署结果": str(entry.get("status") or ""),
            "判定口径": "逐项覆盖" if coverage["structured"] else "兼容既有合格记录",
            "要求检查项目": list(coverage["required_items"]),
            "已覆盖检查项目": list(coverage["covered_items"]),
            "采用检查记录": adopted_checks,
            "采用复检记录": dict(coverage["adopted_rechecks"]),
            "采用复检记录号": list(coverage["adopted_recheck_ids"]),
            "复检追溯": dict(coverage["trace_details"]),
            "仍待复检的检查记录": [
                {"检查记录": row.get("id"), "检查项目": row.get("检查项目"),
                 "状态": row.get("status"), "不合格项": row.get("不合格项", "")}
                for row in pending
            ],
            "签署后完成状态": "完成待复检" if pending else "已完成",
            "未计入的复检记录": list(coverage["ignored_rechecks"]),
            "验收日期": entry.get("验收日期", ""),
            "验收人": entry.get("验收人", ""),
        }

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        process = entry.get("验收工序", "")
        if action == "签署验收":
            # 1. 验收标准要求的每个检查项目都必须被本工序的合格记录覆盖
            coverage = build_coverage(entry, store.rows("quality_inspect"))
            if not coverage["ok"]:
                return False, coverage_reason(coverage)
            # 2. 关联排期中工期是否合理
            schedules = store.query_by_field("construction_schedule", "计划工序", process)
            for s in schedules:
                if s.get("status") == "已延期":
                    return False, "关联排期已延期，需先获取变更签证再验收"
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "签署验收":
            process = entry.get("验收工序", "")

            # 0. 写签署回执，并把本次签署采用的检查项目清单固化到验收单上
            receipt = self._receipt(entry, entry_id, action)
            entry[self.RECEIPT_FIELD] = receipt
            if receipt["要求检查项目"]:
                entry["检查清单"] = receipt["要求检查项目"]
            entry["覆盖检查项目"] = receipt["已覆盖检查项目"]
            entry["采用检查记录"] = receipt["采用检查记录"]

            # 1. 签署结果同步到待复检清单：本工序仍待整改的缺陷继续复查
            pending = self._pending_quality(process)
            for qi in pending:
                qi["待复检"] = True
                qi["复检来源验收"] = entry_id
                effects.append(dict(qi))

            # 2. 工序完成状态：还有未整改缺陷时不能算完成
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    if pending:
                        ws_row["abnormal"] = True
                        ws_row["完成待复检"] = True
                    else:
                        ws_row["status"] = "已完成"
                        ws_row["pending"] = False
                        ws_row["验收签署"] = entry_id
                    effects.append(dict(ws_row))

            # 3. 联动修缮日志归档
            for log in store.rows("restoration_log"):
                if log.get("对应工序", "") == process:
                    log["status"] = "已归档"
                    log["pending"] = False
                    effects.append(dict(log))

            # 4. 联动施工排期里程碑更新
            for sched in store.rows("construction_schedule"):
                if sched.get("计划工序", "") == process:
                    sched["里程碑节点"] = f"{process}-验收已签署"
                    effects.append(dict(sched))

            # 5. 触发档案归档：同一验收单只自动归档一次
            arch_rows = store.rows("archive_mgmt")
            marker = f"验收签署#{entry_id}自动触发"
            existing = None
            for row in arch_rows:
                if str(row.get("备注说明", "")).startswith(marker):
                    existing = row
                    break
            if existing is None:
                new_arch = {
                    "id": max((int(r.get("id", 0)) for r in arch_rows), default=0) + 1,
                    "档案名称": f"{process}-验收报告",
                    "档案类别": "验收档案",
                    "形成日期": entry.get("验收日期", ""),
                    "保管期限": "永久",
                    "密级等级": "内部",
                    "归档人": entry.get("验收人", ""),
                    "归档状态": "待整理",
                    "备注说明": marker,
                    "验收单": entry_id,
                    "status": "待整理",
                    "pending": True,
                    "abnormal": False,
                }
                arch_rows.append(new_arch)
                effects.append(new_arch)
            else:
                # 重复签署不再新增归档，只刷新关联的验收单号
                existing["验收单"] = entry_id
                effects.append(dict(existing))

            # 6. 更新竣工总结质量评定
            for summary in store.rows("completion_summary"):
                if summary.get("status") in ("编制中", "已提交"):
                    if summary.get("质量评定", "") == "":
                        summary["质量评定"] = f"工序{process}已验收"
                    effects.append(dict(summary))

        elif action == "退回整改":
            process = entry.get("验收工序", "")
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["abnormal"] = True
                    effects.append(dict(ws_row))
            # 退回整改同样把本工序缺陷送入复检清单
            for qi in self._pending_quality(process):
                qi["待复检"] = True
                qi["复检来源验收"] = entry_id
                effects.append(dict(qi))
        return effects

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["receipts"] = [
            row.get(self.RECEIPT_FIELD)
            for row in store.rows(self.MODULE)
            if row.get(self.RECEIPT_FIELD)
        ]
        return base


def recheck_queue(process: str | None = None) -> list[dict[str, Any]]:
    """质量检查待复检清单：签署验收后仍需继续复查的缺陷。"""
    queue: list[dict[str, Any]] = []
    for row in store.rows("quality_inspect"):
        if process and str(row.get("检查工序") or "") != process:
            continue
        status = str(row.get("status") or "")
        is_open = (
            row.get("待复检") or status in {"需整改", "待复检", "不合格"}
        )
        if not is_open:
            continue
        if status in {"需整改", "待复检", "不合格"} and resolved_by_recheck(
            row, store.rows("quality_inspect")
        ):
            continue
        queue.append(
            {
                "检查记录": row.get("id"),
                "检查工序": row.get("检查工序"),
                "检查项目": row.get("检查项目"),
                "状态": status,
                "待复检": bool(row.get("待复检")),
                "不合格项": row.get("不合格项", ""),
                "复检来源验收": row.get("复检来源验收"),
                "复检": is_recheck(row),
            }
        )
    return queue
