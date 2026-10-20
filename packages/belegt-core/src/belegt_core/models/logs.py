# Spec: 03 §3.14 AuditEvent（action 枚举含 _baseline §M / 07 §7.1 并入项）、§3.15 Trace
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from .common import DateTimeStr, DecStr, Model, Sha256

AUDIT_ACTIONS = (
    "object.create", "object.version", "object.promote", "edge.derive", "export.create",
    "egress.send", "egress.block", "auth.login", "authz.change", "authz.deny", "key.rotate",
    "policy.halt", "crypto.erase", "dsr.fulfil",
)
AuditAction = Literal[
    "object.create", "object.version", "object.promote", "edge.derive", "export.create",
    "egress.send", "egress.block", "auth.login", "authz.change", "authz.deny", "key.rotate",
    "policy.halt", "crypto.erase", "dsr.fulfil",
]


class AuditEvent(Model):
    """只插入的哈希链日志。``event_hash = sha256(prev_hash ‖ JCS(event 去掉 event_hash))``。

    ``details`` 为本包补充字段（拒绝记录的错误码/rule_id/path，03 §6.8"拒绝也记审计"），参与哈希。
    """

    TYPE: ClassVar[str] = "AuditEvent"
    id: str = Field(pattern=r"^aud_[0-9A-HJKMNP-TV-Z]{26}$")
    org_id: str
    version: int = 1
    created_at: DateTimeStr
    created_by: str
    seq: int = Field(ge=1)
    action: AuditAction
    object_ref: str | None = None
    content_hash: Sha256 | None = None
    trace_id: str
    prev_hash: Sha256
    event_hash: Sha256
    ip: str | None = None
    user_agent: str | None = None
    details: dict[str, Any] | None = None


class Trace(Model):
    TYPE: ClassVar[str] = "Trace"
    id: str = Field(pattern=r"^trc_[0-9A-HJKMNP-TV-Z]{26}$")
    org_id: str
    span_id: str
    parent_span_id: str | None = None
    actor: str
    request_id: str | None = None
    model: str | None = None
    model_provider: str | None = None
    prompt_version: str | None = None
    input_hash: Sha256
    output_hash: Sha256
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_eur: DecStr | None = None
    started_at: DateTimeStr
    ended_at: DateTimeStr
    status: Literal["ok", "error", "halted"]
    payload_blob: str | None = None
