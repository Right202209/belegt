# Spec: 03 §1.3（边类型与来源字段）、§4.2-2（浮动引用→钉住）、R-REF
"""引用字段规格：每个对象类型的哪些字段是引用、指向什么类型、派生什么边。

同一份规格同时用于：浮动引用解析（写入规范化）、R-REF 校验、边派生（SSOT-3）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ..ids import ObjectRef, type_of_id


@dataclass(frozen=True)
class RefField:
    path: str  # 点分路径，``[]`` 表示数组元素，如 ``inputs[].ref``
    kind: str  # "ref"（ObjectRef，可浮动→钉住）| "id"（逻辑 ID）| "dp"（DataPoint SemVer 引用）| "fs"（FactorSet ID）| "boundary" | "scope"
    targets: tuple[str, ...]  # 允许的目标类型
    edge: str | None = None  # 派生的边类型；None = 只做 R-REF
    reverse: bool = False  # True：边方向为 目标 → 本对象（如 resolves 来自 Conflict.resolution.approval_ref）
    pin_edge: bool = False  # 边是否钉住目标版本
    edge_by_target: tuple[tuple[str, str], ...] = ()  # 按目标类型选边：(("Evidence","cites"),("Calculation","derived_from"))


ANY_REFABLE = ("Request", "Question", "Mapping", "Evidence", "Calculation", "Answer", "Approval", "Conflict",
               "SupplierRequest", "ClosedQuestion", "Counterparty", "Site", "Product")

COMMON_REFS = (RefField("supersedes", "ref", ANY_REFABLE + ("Organization", "FactorSet"), edge="supersedes", pin_edge=True),)

REF_FIELDS: dict[str, tuple[RefField, ...]] = {
    "Request": (RefField("counterparty_id", "id", ("Counterparty",)),),
    "Question": (
        RefField("request_id", "id", ("Request",), edge="part_of"),
        RefField("parent_question_id", "id", ("Question",)),
    ),
    "Mapping": (
        RefField("question_id", "id", ("Question",), edge="part_of"),
        RefField("datapoint_id", "dp", ("DataPoint",), edge="maps_to"),
        RefField("decided_by_cq", "id", ("ClosedQuestion",)),
        RefField("boundary_hint", "boundary", ("Site", "Product")),
    ),
    "Evidence": (
        RefField("datapoint_id", "dp", ("DataPoint",)),
        RefField("supplier_request_ref", "id", ("SupplierRequest",), edge="fulfills"),
        RefField("corrects", "ref", ("Evidence",), edge="derived_from", pin_edge=True),
        RefField("boundary", "boundary", ("Site", "Product"), edge="scoped_to"),
        RefField("locator.cq_ref", "ref", ("ClosedQuestion",)),
        RefField("locator.approval_ref", "ref", ("Approval",)),
        RefField("verification.cq_ref", "ref", ("ClosedQuestion",)),
    ),
    "Calculation": (
        RefField("inputs[].ref", "ref", ("Evidence", "Calculation"), pin_edge=True,
                 edge_by_target=(("Evidence", "cites"), ("Calculation", "derived_from"))),
        RefField("factor_set_version", "fs", ("FactorSet",), edge="computed_with", pin_edge=True),
        RefField("boundary", "boundary", ("Site", "Product")),
    ),
    "Answer": (
        RefField("datapoint_id", "dp", ("DataPoint",)),
        RefField("question_refs[]", "id", ("Question",), edge="answers"),
        RefField("evidence_refs[]", "ref", ("Evidence",), edge="cites", pin_edge=True),
        RefField("calculation_ref", "ref", ("Calculation",), edge="derived_from", pin_edge=True),
        RefField("reused_from", "ref", ("Answer",), edge="derived_from", pin_edge=True),
        RefField("approval_ref", "ref", ("Approval",)),
        RefField("customer_scope[]", "scope", ("Counterparty",), edge="scoped_to"),
        RefField("boundary", "boundary", ("Site", "Product"), edge="scoped_to"),
        RefField("submission.request_id", "id", ("Request",)),
    ),
    "Approval": (
        RefField("subject_ref", "ref", ("Answer", "SupplierRequest", "Conflict"), pin_edge=True,
                 edge_by_target=(("Answer", "approves"), ("SupplierRequest", "approves"), ("Conflict", "resolves"))),
        RefField("modified_answer_ref", "ref", ("Answer",)),
        RefField("request_id", "id", ("Request",)),
    ),
    "Conflict": (
        RefField("datapoint_id", "dp", ("DataPoint",)),
        RefField("subject_refs[]", "ref", ("Answer", "Evidence", "Calculation"), edge="conflicts_with", pin_edge=True),
        RefField("detail.values[].ref", "ref", ANY_REFABLE),
        RefField("request_id", "id", ("Request",)),
        RefField("resolution.approval_ref", "ref", ("Approval",), edge="resolves", reverse=True, pin_edge=True),
        RefField("resolution.divergence.sides[].answer", "ref", ("Answer",)),
    ),
    "SupplierRequest": (
        RefField("counterparty_id", "id", ("Counterparty",), edge="requested_from"),
        RefField("datapoint_ids[]", "dp", ("DataPoint",)),
        RefField("product_ids[]", "id", ("Product",)),
        RefField("trigger_ref", "ref", ("Answer", "Question")),
        RefField("default_cost_estimate_ref", "ref", ("Calculation",)),
    ),
    "ClosedQuestion": (
        RefField("target_ref", "ref", ("Mapping", "Evidence", "Calculation", "Request", "Question", "Answer", "Conflict"),
                 edge="resolves", pin_edge=True),
        RefField("request_id", "id", ("Request",), edge="part_of"),
        RefField("context_refs[]", "ref", ANY_REFABLE),
        RefField("proposed_value.evidence_ref", "ref", ("Evidence",)),
        RefField("options[].ref", "ref", ANY_REFABLE),
    ),
    "Product": (RefField("site_ids[]", "id", ("Site",)),),
    "Organization": (),
    "Counterparty": (),
    "Site": (),
    "FactorSet": (),
}


def ref_fields(type_name: str) -> tuple[RefField, ...]:
    return REF_FIELDS.get(type_name, ()) + COMMON_REFS


def iter_path(obj: Any, path: str) -> Iterator[tuple[str, Any, Callable[[Any], None]]]:
    """遍历路径上的叶子值：产出 ``(json_pointer, value, setter)``。"""
    parts = path.split(".")

    def rec(node: Any, i: int, ptr: str) -> Iterator[tuple[str, Any, Callable[[Any], None]]]:
        if node is None:
            return
        part = parts[i]
        is_arr = part.endswith("[]")
        key = part[:-2] if is_arr else part
        if not isinstance(node, dict) or key not in node or node[key] is None:
            return
        val = node[key]
        last = i == len(parts) - 1
        if is_arr:
            if not isinstance(val, list):
                return
            for j, el in enumerate(val):
                p = f"{ptr}/{key}/{j}"
                if last:
                    def setter(v, _l=val, _j=j):
                        _l[_j] = v
                    yield p, el, setter
                else:
                    yield from rec(el, i + 1, p)
        else:
            p = f"{ptr}/{key}"
            if last:
                def setter(v, _n=node, _k=key):
                    _n[_k] = v
                yield p, val, setter
            else:
                yield from rec(val, i + 1, p)

    yield from rec(obj, 0, "")


def target_id(field: RefField, value: Any) -> tuple[str | None, int | None]:
    """把字段值解析为 (目标逻辑 ID, 版本)。boundary/scope 中非对象值返回 (None, None)。"""
    if not isinstance(value, str):
        return None, None
    if field.kind == "boundary":
        if value.startswith("site:") or value.startswith("product:"):
            return value.split(":", 1)[1], None
        return None, None
    if field.kind == "scope":
        return (value, None) if value.startswith("cp_") else (None, None)
    if field.kind in ("dp", "fs"):
        return value, None
    r = ObjectRef.parse(value)
    return r.id, r.version


def edge_type_for(field: RefField, tid: str) -> str | None:
    if field.edge_by_target:
        t = type_of_id(tid)
        for tt, et in field.edge_by_target:
            if tt == t:
                return et
        return None
    return field.edge
