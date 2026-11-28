"""模块服务基类：共享查询、登记、动作和统计骨架，及跨模块业务联动。

每个领域服务除了基础CRUD外，还可以通过 cross_module_verify / cross_module_effect
注入跨模块业务校验逻辑和副作用。
"""
from __future__ import annotations

from typing import Any

from app.store import store


class BaseModuleService:
    MODULE = ""
    ENTITY = ""
    LIST_FIELDS: list[str] = []
    # 不在列表列中、但登记时需要落库的辅助字段（如复检来源、检查清单）。
    EXTRA_FIELDS: list[str] = []
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER: list[str] = []
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []
    # 这些动作不按小时窗口做幂等短路：重复提交需要反映最新的跨模块状态
    # （例如工序交接：先按未签原因挂起，签署后再点要能凭已签结果继续）。
    IDEMPOTENT_EXEMPT_ACTIONS: set[str] = set()

    # ── 跨模块业务扩展点（子类可覆盖） ──
    def resolve_action(self, action: str, entry: dict[str, Any]) -> str | None:
        """把动作映射到目标状态；子类可按条目字段覆盖个别动作的去向。"""
        return self.ACTION_RULES.get(action)

    # 锁顺序：先跨模块共享锁（子类按模块名 + 工序/记录 id 返回），再本记录锁。
    def action_lock_keys(self, action: str, entry: dict[str, Any]) -> list[str]:
        """动作需要额外加的跨模块锁（子类按业务需要返回）。"""
        return []

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        """执行动作前的跨模块校验。返回 (通过, 失败原因)。"""
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        """动作执行后的跨模块副作用。返回受影响的联动条目列表。"""
        return []

    # 这些动作不按小时窗口做幂等短路：重复提交需要反映最新的跨模块状态
    # （例如工序交接：先按未签原因挂起，签署后再点要能凭已签结果继续）。
    IDEMPOTENT_EXEMPT_ACTIONS: set[str] = set()

    # ── 基础 CRUD ──
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        for field in self.EXTRA_FIELDS:
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作：幂等检查 -> 加锁 -> 前置校验 -> 状态变更 -> 跨模块联动。

        校验和副作用在同一把锁内完成，避免签署请求和并发的复检/更正交错，
        保证签署回执里记录的采用结果就是这次验收实际判定的结果。
        """
        # 幂等检查
        if (
            action not in self.IDEMPOTENT_EXEMPT_ACTIONS
            and store.check_idempotent(self.MODULE, entry_id, action)
        ):
            entry = store.find(self.MODULE, entry_id)
            if entry:
                return entry, f"操作「{action}」已执行过，当前状态：{entry.get('status', '')}"
            return None, "操作已执行但记录可能已变更"

        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        # 加锁顺序固定：先跨模块锁（含工序级锁），再本记录锁，所有动作保持一致。
        lock_keys = [*self.action_lock_keys(action, entry), f"{self.MODULE}:{entry_id}"]
        acquired: list[str] = []
        for key in dict.fromkeys(lock_keys):
            if not store.acquire_resource(key):
                for held in acquired:
                    store.release_resource(held)
                return None, "资源正在被其他操作处理，请稍后重试"
            acquired.append(key)

        try:
            # 拿到锁后再看一次幂等和最新记录，避免并发请求重复执行副作用
            if (
                action not in self.IDEMPOTENT_EXEMPT_ACTIONS
                and store.check_idempotent(self.MODULE, entry_id, action)
            ):
                latest = store.find(self.MODULE, entry_id)
                if latest:
                    return latest, f"操作「{action}」已执行过，当前状态：{latest.get('status', '')}"
                return None, "操作已执行但记录可能已变更"

            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

            target = self.resolve_action(action, entry)
            if target is None:
                return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

            # 跨模块前置校验（与副作用共用同一把锁）
            passed, reason = self.cross_module_verify(entry_id, action, entry)
            if not passed:
                return None, reason

            entry["status"] = target
            entry["pending"] = target != self.STATUS_ORDER[-1]
            entry["abnormal"] = action in self.NEGATIVE_ACTIONS

            # 执行跨模块副作用
            self.cross_module_effect(entry_id, action, entry)

            # 标记幂等
            store.mark_idempotent(self.MODULE, entry_id, action)
            return entry, f"{self.ENTITY}已{action}"
        finally:
            for key in reversed(acquired):
                store.release_resource(key)

    def domain_snapshot(self) -> dict[str, Any]:
        """返回本模块的状态统计与业务对象，供其他模块读取。"""
        rows = store.rows(self.MODULE)
        by_status = {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in self.STATUS_ORDER
        }
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}状态记录",
            "count": len(rows),
            "by_status": by_status,
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
            "rows": [dict(row) for row in rows],
        }