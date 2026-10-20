# Spec: 03 §1.2（节点类型）、§3（核心对象）
"""证据图对象的 pydantic v2 模型（字段名与 03 完全一致）。

``GRAPH_TYPES``：可经 GraphStore 写入的图对象类型名 → 模型类。
"""
from __future__ import annotations

from typing import Any

from .common import (
    SCHEMA_VERSION, Boundary, ConnectorLocator, CustomerScope, DataPointRef, DateStr, DateTimeStr, DecStr,
    GraphObject, I18nText, Locator, Model, ObjectRefStr, Period, Promotion, Sha256,
)
from .decisions import (
    Answer, Approval, Conflict, ConflictDetail, DetailValue, Divergence, DivergenceSide, Resolution, Submission,
)
from .facts import (
    BreakdownItem, CalcInput, CalcResult, Calculation, DataPoint, Evidence, FactorRow, FactorSet, FactorUsed,
    Plausibility, Rounding, Verification,
)
from .logs import AUDIT_ACTIONS, AuditEvent, Trace
from .master import Counterparty, Organization, Product, Site
from .workflow import (
    REQUEST_STATUSES, ClosedQuestion, CQAnswer, CQOption, Mapping, ProposedValue, Question, Request,
    SourceRef, SupplierRequest, Transform,
)

GRAPH_TYPES: dict[str, type[GraphObject]] = {
    m.TYPE: m
    for m in (
        Request, Question, Mapping, Evidence, Calculation, Answer, Approval, Conflict, SupplierRequest,
        ClosedQuestion, Organization, Counterparty, Site, Product, FactorSet,
    )
}

# 类型名 → JSON Schema 文件名
SCHEMA_FILES: dict[str, str] = {
    "Request": "request.json", "Question": "question.json", "Mapping": "mapping.json",
    "Evidence": "evidence.json", "Calculation": "calculation.json", "Answer": "answer.json",
    "Approval": "approval.json", "Conflict": "conflict.json", "SupplierRequest": "supplier_request.json",
    "ClosedQuestion": "closed_question.json", "Organization": "organization.json",
    "Counterparty": "counterparty.json", "Site": "site.json", "Product": "product.json",
    "FactorSet": "factor_set.json", "DataPoint": "datapoint.json", "AuditEvent": "audit_event.json",
    "Trace": "trace.json",
}


def model_for(type_name: str) -> type[GraphObject]:
    try:
        return GRAPH_TYPES[type_name]
    except KeyError as e:
        raise KeyError(f"unknown graph object type {type_name!r}") from e


def parse_object(type_name: str, body: dict[str, Any]) -> GraphObject:
    return model_for(type_name).model_validate(body)


__all__ = [
    "SCHEMA_VERSION", "GRAPH_TYPES", "SCHEMA_FILES", "model_for", "parse_object",
    "GraphObject", "Model", "Period", "Locator", "ConnectorLocator", "Promotion", "I18nText", "DecStr", "DateStr",
    "DateTimeStr", "Sha256", "ObjectRefStr", "DataPointRef", "Boundary", "CustomerScope",
    "Request", "SourceRef", "Question", "Mapping", "Transform", "SupplierRequest", "ClosedQuestion", "CQOption",
    "ProposedValue", "CQAnswer", "Evidence", "Verification", "Plausibility", "Calculation", "CalcInput",
    "FactorUsed", "CalcResult", "Rounding", "BreakdownItem", "FactorSet", "FactorRow", "DataPoint", "Answer",
    "Submission", "Approval", "Conflict", "ConflictDetail", "DetailValue", "Resolution", "Divergence",
    "DivergenceSide", "Organization", "Counterparty", "Site", "Product", "AuditEvent", "AUDIT_ACTIONS", "Trace",
    "REQUEST_STATUSES",
]
