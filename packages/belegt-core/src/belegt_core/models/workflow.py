# Spec: 03 §3.2 Request、§3.3 Question、§3.4 Mapping、§3.10 SupplierRequest、§3.11 ClosedQuestion
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from .common import (
    Boundary, DataPointRef, DateStr, DateTimeStr, DecStr, GraphObject, I18nText, Locator, Model,
    ObjectRefStr, Period, Sha256,
)

REQUEST_STATUSES = (
    "received", "triaged", "parsed", "mapped", "evidence_gathering", "awaiting_supplier", "drafting",
    "auditing", "in_review", "approved", "filling", "submitted", "archived", "awaiting_human", "halted",
    "cancelled",
)
RequestStatus = Literal[
    "received", "triaged", "parsed", "mapped", "evidence_gathering", "awaiting_supplier", "drafting",
    "auditing", "in_review", "approved", "filling", "submitted", "archived", "awaiting_human", "halted",
    "cancelled",
]


class SourceRef(Model):
    message_id: str | None = None
    portal_task_id: str | None = None
    blob_sha256: list[Sha256] = Field(default_factory=list)


class Request(GraphObject):
    TYPE: ClassVar[str] = "Request"
    PREFIX: ClassVar[str] = "req_"
    DEFAULT_ZONE: ClassVar[str] = "quarantine"

    counterparty_id: str
    channel: Literal["email", "portal", "upload", "api", "a2a"]
    source_ref: SourceRef
    template_ref: str | None = None
    title: str = Field(max_length=300)
    language: str
    due_date: DateStr | None = None
    reporting_period: Period | None = None
    priority: Literal["low", "normal", "high", "urgent"]
    triage_confidence: float = Field(ge=0, le=1)
    status: RequestStatus
    status_reason: str | None = None
    workflow_id: str | None = None
    assignee: str | None = None


class Question(GraphObject):
    TYPE: ClassVar[str] = "Question"
    PREFIX: ClassVar[str] = "q_"
    DEFAULT_ZONE: ClassVar[str] = "quarantine"

    request_id: str
    ordinal: int = Field(ge=1)
    native_id: str | None = Field(default=None, max_length=64)
    text: I18nText
    answer_format: Literal["number", "text", "enum", "bool", "date", "file", "geo", "table"]
    options: list[str] | None = None
    unit_requested: str | None = None
    period_requested: Period | None = None
    locator: Locator
    parent_question_id: str | None = None
    status: Literal["parsed", "mapped", "unmapped", "answered"]


class Transform(Model):
    kind: Literal["unit_convert", "percent_to_ratio", "enum_map"]
    params: dict[str, Any] = Field(default_factory=dict)


class Mapping(GraphObject):
    TYPE: ClassVar[str] = "Mapping"
    PREFIX: ClassVar[str] = "map_"
    DEFAULT_ZONE: ClassVar[str] = "quarantine"

    question_id: str
    datapoint_id: DataPointRef
    confidence: float = Field(ge=0, le=1)
    rank: int = Field(ge=1, le=3)
    method: Literal["exact_template", "historical", "semantic", "human"]
    template_mapping_ref: str | None = None
    unit_hint: str | None = None
    period_hint: Period | None = None
    boundary_hint: Boundary | None = None
    transform: Transform | None = None
    status: Literal["candidate", "confirmed", "rejected"]
    decided_by_cq: str | None = None


class SupplierRequest(GraphObject):
    TYPE: ClassVar[str] = "SupplierRequest"
    PREFIX: ClassVar[str] = "sreq_"

    counterparty_id: str
    datapoint_ids: list[DataPointRef] = Field(min_length=1)
    product_ids: list[str] | None = None
    period: Period
    language: str
    template_id: str
    channel: Literal["email", "supplier_portal"]
    portal_token_hash: Sha256 | None = None
    due_date: DateStr
    reminder_count: int = Field(ge=0)
    last_sent_at: DateTimeStr | None = None
    trigger_ref: ObjectRefStr | None = None
    default_cost_estimate_ref: ObjectRefStr | None = None
    status: Literal["draft", "sent", "reminded", "responded", "reviewed", "closed", "escalated", "expired"]


class CQOption(Model):
    key: str
    label: I18nText
    ref: str | None = None


class ProposedValue(Model):
    value: DecStr
    unit: str
    evidence_ref: ObjectRefStr


class CQAnswer(Model):
    choice: str | None = None
    bool_: bool | None = Field(default=None, alias="bool")  # JSON 字段名 "bool"（与 03 一致）
    corrected_value: DecStr | None = None
    blob_sha256: list[Sha256] | None = None
    answered_by: str
    answered_at: DateTimeStr


class ClosedQuestion(GraphObject):
    TYPE: ClassVar[str] = "ClosedQuestion"
    PREFIX: ClassVar[str] = "cq_"

    target_ref: ObjectRefStr
    request_id: str | None = None
    kind: Literal["yes_no", "single_choice", "numeric_confirm", "file_request"]
    prompt: I18nText
    options: list[CQOption] | None = None
    proposed_value: ProposedValue | None = None
    context_refs: list[ObjectRefStr] | None = None
    assignee_role: str
    assignee: str | None = None
    expires_at: DateTimeStr
    answer: CQAnswer | None = None
    status: Literal["open", "answered", "expired"]
