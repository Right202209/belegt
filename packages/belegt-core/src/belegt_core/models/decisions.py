# Spec: 03 §3.7 Answer、§3.8 Approval、§3.9 Conflict（含 resolution.divergence，06 §3.3 ADR/1）
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from .common import (
    Actor, Boundary, Consolidation, CustomerScope, DataPointRef, DateStr, DateTimeStr, DecStr, GraphObject,
    I18nText, Model, ObjectRefStr, Period, Sha256, ValueType,
)

RiskLevel = Literal["L0", "L1", "L2", "L3"]


class Submission(Model):
    request_id: str
    channel: Literal["file_export", "portal", "api", "a2a"]
    submitted_at: DateTimeStr
    export_input_hash: Sha256


class Answer(GraphObject):
    TYPE: ClassVar[str] = "Answer"
    PREFIX: ClassVar[str] = "ans_"

    datapoint_id: DataPointRef
    value: Any
    value_type: ValueType
    unit: str | None = None
    text_lang: str | None = None
    period: Period
    boundary: Boundary
    consolidation: Consolidation | None = None
    customer_scope: CustomerScope
    question_refs: list[str] | None = None
    evidence_refs: list[ObjectRefStr] | None = None
    calculation_ref: ObjectRefStr | None = None
    reused_from: ObjectRefStr | None = None
    origin: Literal["generated", "imported", "human"]
    answer_key: Sha256 | None = None  # 服务端计算
    status: Literal["draft", "in_review", "approved", "rejected", "submitted", "superseded"]
    risk_level: RiskLevel | None = None
    is_estimate: bool
    estimate_method: str | None = Field(default=None, max_length=200)
    legal_commitment: bool
    approval_ref: ObjectRefStr | None = None
    submission: Submission | None = None
    drafting_note: I18nText | None = None


class Approval(GraphObject):
    TYPE: ClassVar[str] = "Approval"
    PREFIX: ClassVar[str] = "apr_"

    subject_ref: ObjectRefStr
    request_id: str | None = None
    risk_level: RiskLevel
    required_role: list[str]
    assignee: str | None = None
    decision: Literal["approved", "rejected", "modified"] | None = None
    decided_by: Actor | None = None
    decided_at: DateTimeStr | None = None
    comment: str | None = Field(default=None, max_length=4000)
    modified_answer_ref: ObjectRefStr | None = None
    batch_id: str | None = None
    policy_version: str
    status: Literal["pending", "approved", "rejected", "modified"]


class DetailValue(Model):
    ref: ObjectRefStr
    value: DecStr
    unit: str | None = None
    customer_scope: CustomerScope | None = None


class ConflictDetail(Model):
    model_config = {"extra": "allow"}
    values: list[DetailValue] = Field(min_length=1)
    delta: DecStr | None = None
    delta_ratio: DecStr | None = None
    threshold: DecStr | None = None


class DivergenceSide(Model):
    answer: ObjectRefStr
    customer_scope: CustomerScope
    basis: str = Field(max_length=200)


class Divergence(Model):
    format: Literal["ADR/1"] = "ADR/1"
    reason_code: Literal["customer_methodology", "consolidation", "period", "product_scope", "boundary", "timing_correction"]
    customer_requirement_refs: list[str] = Field(min_length=1)
    sides: list[DivergenceSide] = Field(min_length=2)
    disclosure_text: I18nText | None = None
    include_in_export: bool
    review_by: DateStr


class Resolution(Model):
    kind: Literal["corrected", "accepted_divergence", "dismissed", "auto_cleared"]
    note: str = Field(min_length=1, max_length=4000)
    approval_ref: ObjectRefStr | None = None
    resolved_by: Actor
    divergence: Divergence | None = None


class Conflict(GraphObject):
    TYPE: ClassVar[str] = "Conflict"
    PREFIX: ClassVar[str] = "cfl_"

    rule_id: str
    severity: Literal["blocking", "warning"]
    datapoint_id: DataPointRef
    subject_refs: list[ObjectRefStr] = Field(min_length=1)
    detail: ConflictDetail
    explanation: I18nText | None = None
    explanation_by: str | None = None
    request_id: str | None = None
    conflict_key: Sha256 | None = None  # 服务端计算
    resolution: Resolution | None = None
    status: Literal["open", "resolved", "accepted_divergence", "dismissed"]
