# Spec: 03 §6.8（校验流程第 4–6 步：读引用对象与 head，再执行规则）
"""规则执行上下文。规则只通过 ``GraphReader`` 读图（同一事务内，批量写入中先写的对象可见）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..authz import Actor
from ..datapoints import DataPointRegistry
from ..units import UnitSystem


class GraphReader(Protocol):
    def get_version(self, org_id: str, obj_id: str, version: int) -> dict[str, Any] | None: ...
    def get_head_body(self, org_id: str, obj_id: str) -> dict[str, Any] | None: ...
    def orgs_of(self, obj_id: str) -> list[str]: ...
    def heads_where(self, org_id: str, type_name: str, **eq: Any) -> list[dict[str, Any]]: ...
    def versions_of(self, org_id: str, obj_id: str) -> list[dict[str, Any]]: ...
    def effective_answer(self, org_id: str, answer_key: str) -> tuple[str, int] | None: ...
    def has_audit(self, org_id: str, action: str, object_ref_prefix: str) -> bool: ...
    def factor_rows(self, factor_set_id: str) -> list[dict[str, Any]] | None: ...
    def now(self) -> str: ...


@dataclass
class RuleContext:
    reader: GraphReader
    actor: Actor
    type_name: str
    new: dict[str, Any]
    prev: dict[str, Any] | None  # 当前 head（新版本时）；新对象为 None
    datapoints: DataPointRegistry
    units: UnitSystem
    batch_ids: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    @property
    def org_id(self) -> str:
        return self.new["org_id"]

    @property
    def status_from(self) -> str | None:
        return self.prev.get("status") if self.prev else None

    @property
    def status_to(self) -> str | None:
        return self.new.get("status")

    def entering(self, status: str) -> bool:
        return self.status_to == status and self.status_from != status

    def resolve(self, ref: str) -> dict[str, Any] | None:
        """读钉住引用 ``id#vN``（本租户或 org_global）。"""
        obj_id, _, v = ref.partition("#v")
        if not v:
            return self.get_head(obj_id)
        for org in (self.org_id, "org_global"):
            o = self.reader.get_version(org, obj_id, int(v))
            if o is not None:
                return o
        return None

    def get_head(self, obj_id: str) -> dict[str, Any] | None:
        for org in (self.org_id, "org_global"):
            o = self.reader.get_head_body(org, obj_id)
            if o is not None:
                return o
        return None
