# Spec: 03 §6.8 校验流程第 2 步（合并 patch，补服务端字段，规范化 DEC-STR、NFC、浮动引用→钉住）
from __future__ import annotations

import copy
from typing import Any, Callable

from ..canonical import nfc_deep, normalize_dec
from ..errors import GraphError
from ..hashing import answer_key_of, conflict_key, normalize_customer_scope
from ..rules.refspec import iter_path, ref_fields

SERVER_FIELDS = ("version", "created_at", "content_hash", "created_by")

DEC_PATHS: dict[str, tuple[str, ...]] = {
    "Calculation": ("inputs[].value", "factors_used[].value", "result.value", "breakdown[].value"),
    "Conflict": ("detail.values[].value", "detail.delta", "detail.delta_ratio", "detail.threshold"),
    "ClosedQuestion": ("proposed_value.value", "answer.corrected_value"),
}


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7396 JSON Merge Patch（null 删除字段）。"""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def normalize_decimals(type_name: str, body: dict[str, Any]) -> None:
    if type_name == "Answer" and body.get("value_type") == "number" and body.get("value") is not None:
        body["value"] = normalize_dec(body["value"])
    if type_name == "Evidence" and body.get("value_type") == "number" and body.get("extracted_value") is not None:
        body["extracted_value"] = normalize_dec(body["extracted_value"])
    for path in DEC_PATHS.get(type_name, ()):
        for _, val, setter in list(iter_path(body, path)):
            setter(normalize_dec(val))


def pin_floating_refs(type_name: str, body: dict[str, Any], head_version: Callable[[str], int | None]) -> None:
    """浮动引用（无 ``#v``）→ 当前 head 的钉住引用（03 §4.2-2：数据库中只存钉住引用）。"""
    for f in ref_fields(type_name):
        if f.kind != "ref":
            continue
        for ptr, val, setter in list(iter_path(body, f.path)):
            if not isinstance(val, str) or "#" in val:
                continue
            v = head_version(val)
            if v is None:
                raise GraphError("GRAPH_REF_NOT_FOUND", f"floating reference {val} has no head", path=ptr)
            setter(f"{val}#v{v}")


def server_keys(type_name: str, body: dict[str, Any]) -> None:
    """服务端计算字段：``answer_key``（SSOT-5）、``conflict_key``（03 §3.9）；``customer_scope`` 升序去重。"""
    if "customer_scope" in body and isinstance(body["customer_scope"], list):
        body["customer_scope"] = normalize_customer_scope(body["customer_scope"])
    if type_name == "Answer":
        try:
            body["answer_key"] = answer_key_of(body)
        except (KeyError, TypeError, AttributeError):
            pass  # 缺字段交给 R-SCHEMA
    if type_name == "Conflict" and body.get("rule_id") and isinstance(body.get("subject_refs"), list):
        body["conflict_key"] = conflict_key(body["rule_id"], body["subject_refs"])


def normalize_body(type_name: str, body: dict[str, Any], head_version: Callable[[str], int | None]) -> dict[str, Any]:
    body = nfc_deep(body)
    normalize_decimals(type_name, body)
    pin_floating_refs(type_name, body, head_version)
    server_keys(type_name, body)
    return body
