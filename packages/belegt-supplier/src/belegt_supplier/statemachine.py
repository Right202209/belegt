# Spec: 03 §3.10 SupplierRequest 状态机与不变量；08 §2.3（含 reminded→expired、escalated→responded）
"""SupplierRequest 本地状态机（纯函数：输入旧版本，输出新版本，不做 I/O）。

不变量：
- 首次 `draft → sent` 必须带已批准 Approval 的引用（`approval_ref` 格式 + `ApprovalInfo.decision=approved`
  + `subject_ref` 指向本 sreq），并带 egress 放行审计引用；
- `sent`（含 `reviewed → sent` 补充请求）与 `reminded` 的版本必须有 egress 放行审计引用；
- `reminder_count` 只增不减，且 ≤ 租户上限；`sent → reminded`、`reminded → reminded`、`reviewed → sent` 计数 +1。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from .ids import APR_REF_RE, parse_ref
from .models import ApprovalInfo, SupplierRequest

Status = Literal["draft", "sent", "reminded", "responded", "reviewed", "closed", "escalated", "expired"]

TRANSITIONS: dict[tuple[str, str], str] = {
    ("draft", "sent"): "首次发送已确认 / user + egress-gateway",
    ("sent", "reminded"): "自动催办 / upstream-chaser + egress-gateway",
    ("reminded", "reminded"): "再次催办(未达上限) / upstream-chaser",
    ("sent", "responded"): "收到回复 / supplier-reply-reviewer",
    ("reminded", "responded"): "收到回复 / supplier-reply-reviewer",
    ("responded", "reviewed"): "合理性审核完成 / supplier-reply-reviewer",
    ("reviewed", "closed"): "证据已 verified / user 或 evidence-miner",
    ("reviewed", "sent"): "数据不全需补充 / upstream-chaser",
    ("reminded", "escalated"): "达催办上限 / upstream-chaser",
    ("sent", "expired"): "超过 due_date 且无回复 / service:workflow",
    ("reminded", "expired"): "超过 due_date 且未达升级条件 / service:workflow",
    ("escalated", "closed"): "采购介入后关闭 / user",
    ("escalated", "responded"): "升级后供应商仍提交 / supplier-reply-reviewer",
}
TERMINAL = frozenset({"closed", "expired"})
COUNTING = frozenset({("sent", "reminded"), ("reminded", "reminded"), ("reviewed", "sent")})
NEEDS_EGRESS = frozenset({("draft", "sent"), ("reviewed", "sent"), ("sent", "reminded"), ("reminded", "reminded")})


class TransitionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def allowed_targets(status: str) -> list[str]:
    return [t for (s, t) in TRANSITIONS if s == status]


def transition(sreq: SupplierRequest, to: Status, *, at: datetime, approval: ApprovalInfo | None = None,
               egress_audit_ref: str | None = None, reminder_cap: int = 3) -> SupplierRequest:
    """返回新版本（version+1）。违反规则抛 TransitionError(code=GRAPH_STATE_INVALID|GRAPH_APPROVAL_REQUIRED|...)。"""
    key = (sreq.status, to)
    if key not in TRANSITIONS:
        raise TransitionError("GRAPH_STATE_INVALID", f"{sreq.status} → {to} not allowed (03 §3.10)")
    upd: dict = {"status": to, "version": sreq.version + 1}

    if key == ("draft", "sent"):
        ref = approval.ref if approval else sreq.approval_ref
        if not ref or not APR_REF_RE.match(ref):
            raise TransitionError("GRAPH_APPROVAL_REQUIRED", "first send requires approval_ref apr_<ULID>#vN")
        if approval is None:
            raise TransitionError("GRAPH_APPROVAL_REQUIRED", "approved Approval object required for first send")
        if approval.status != "approved" or approval.decision != "approved":
            raise TransitionError("GRAPH_APPROVAL_REQUIRED", f"approval {approval.ref} is {approval.status}")
        subj_id, _ = parse_ref(approval.subject_ref)
        if subj_id != sreq.id:
            raise TransitionError("GRAPH_APPROVAL_REQUIRED", "approval.subject_ref does not point to this SupplierRequest")
        upd["approval_ref"] = approval.ref
        upd["first_sent_at"] = at

    if key in NEEDS_EGRESS:
        if not egress_audit_ref:
            raise TransitionError("GRAPH_EGRESS_AUDIT_MISSING", "sent/reminded version requires egress AuditEvent ref")
        upd["egress_audit_ref"] = egress_audit_ref
        upd["last_sent_at"] = at

    if key in COUNTING:
        new_count = sreq.reminder_count + 1
        if new_count > reminder_cap:
            raise TransitionError("GRAPH_REMINDER_CAP", f"reminder_count {new_count} > cap {reminder_cap}")
        upd["reminder_count"] = new_count

    return sreq.model_copy(update=upd)
