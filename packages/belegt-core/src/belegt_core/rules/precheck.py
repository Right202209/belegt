# Spec: 03 §2.3（R-NUM、R-LOC、R-EST）、§5.2 说明（R-NUM 在 schema 层第一次拦截）
"""Schema 之前执行的语义预检：保证 R-NUM / R-LOC / R-EST 返回其专用错误码，而不是被
JSON Schema 的 anyOf/if-then 统一报成 ``GRAPH_SCHEMA_INVALID``。"""
from __future__ import annotations

from typing import Any

from ..errors import GraphError


def rule_num(type_name: str, obj: dict[str, Any]) -> None:
    """R-NUM（FR-401）：``value_type=number`` 时 ``evidence_refs`` 或 ``calculation_ref`` 至少一个非空。"""
    if type_name != "Answer" or obj.get("value_type") != "number":
        return
    if not (obj.get("evidence_refs") or obj.get("calculation_ref")):
        raise GraphError("GRAPH_NUM_UNREFERENCED",
                         "number answer requires evidence_refs or calculation_ref (FR-401)", path="/evidence_refs")


def rule_loc(type_name: str, obj: dict[str, Any]) -> None:
    """R-LOC：``source_type=document`` 必须有 ``locator.blob_sha256`` 与 page/cell/bbox 至少一种。"""
    if type_name != "Evidence" or obj.get("source_type") != "document":
        return
    loc = obj.get("locator") or {}
    if not loc.get("blob_sha256"):
        raise GraphError("GRAPH_LOCATOR_MISSING", "document evidence requires locator.blob_sha256", path="/locator/blob_sha256")
    if not any(loc.get(k) not in (None, "", []) for k in ("page", "cell", "bbox")):
        raise GraphError("GRAPH_LOCATOR_MISSING", "document evidence requires locator page, cell or bbox", path="/locator")


def rule_est(type_name: str, obj: dict[str, Any]) -> None:
    """R-EST：``is_estimate=true`` 必须有 ``estimate_method``；``legal_commitment=true`` 时 ``risk_level`` 必须为 L3。"""
    if type_name != "Answer":
        return
    if obj.get("is_estimate") is True and not obj.get("estimate_method"):
        raise GraphError("GRAPH_SCHEMA_INVALID", "is_estimate=true requires estimate_method", rule_id="R-EST",
                         path="/estimate_method")
    if obj.get("legal_commitment") is True and obj.get("risk_level") is not None and obj.get("risk_level") != "L3":
        raise GraphError("GRAPH_SCHEMA_INVALID", "legal_commitment=true requires risk_level=L3", rule_id="R-EST",
                         path="/risk_level")
    if obj.get("legal_commitment") is True and obj.get("status") in ("in_review", "approved", "submitted") \
            and obj.get("risk_level") != "L3":
        raise GraphError("GRAPH_SCHEMA_INVALID", "legal_commitment=true requires risk_level=L3", rule_id="R-EST",
                         path="/risk_level")


def rule_fs1_shape(type_name: str, obj: dict[str, Any]) -> None:
    """R-FS1（形状部分）：一个 Calculation 只能有一个 ``factor_set_version``（字符串）。"""
    if type_name != "Calculation":
        return
    fsv = obj.get("factor_set_version")
    if isinstance(fsv, (list, tuple)) or (isinstance(fsv, str) and ("," in fsv or " " in fsv.strip())):
        raise GraphError("GRAPH_SCHEMA_INVALID", "a Calculation uses exactly one factor_set_version; split into a "
                         "sub-calculation chain (R-FS1)", rule_id="R-FS1", path="/factor_set_version")


PRE_SCHEMA_RULES = (rule_num, rule_loc, rule_est, rule_fs1_shape)


def run_pre_schema(type_name: str, obj: dict[str, Any]) -> None:
    for r in PRE_SCHEMA_RULES:
        r(type_name, obj)
