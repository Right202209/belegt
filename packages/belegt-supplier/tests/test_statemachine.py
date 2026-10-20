# Spec: 03 §3.10、08 §2.3 SupplierRequest 状态机
from __future__ import annotations

from datetime import date, datetime, timezone
from itertools import product

import pytest

from belegt_supplier.ids import new_id
from belegt_supplier.models import ApprovalInfo, SupplierRequest
from belegt_supplier.statemachine import TRANSITIONS, TransitionError, allowed_targets, transition

NOW = datetime(2026, 11, 2, 8, tzinfo=timezone.utc)
STATUSES = ["draft", "sent", "reminded", "responded", "reviewed", "closed", "escalated", "expired"]


def sreq(status="draft", count=0) -> SupplierRequest:
    return SupplierRequest(id=new_id("sreq"), org_id=new_id("org"), counterparty_id=new_id("cp"),
                           datapoint_ids=["dp:prod.cbam.see_direct"], period={"start": "2026-01-01", "end": "2026-12-31"},
                           language="de", template_id="stpl:cbam_see_request.de@1.0.0", due_date=date(2026, 11, 23),
                           status=status, reminder_count=count)


def approval(s: SupplierRequest, status="approved", subject=None) -> ApprovalInfo:
    return ApprovalInfo(ref=f"{new_id('apr')}#v2", subject_ref=subject or s.ref, status=status,
                        decision=None if status == "pending" else status)


def test_transition_table_matches_spec():
    expected = {("draft", "sent"), ("sent", "reminded"), ("reminded", "reminded"), ("sent", "responded"),
                ("reminded", "responded"), ("responded", "reviewed"), ("reviewed", "closed"), ("reviewed", "sent"),
                ("reminded", "escalated"), ("sent", "expired"), ("reminded", "expired"), ("escalated", "closed"),
                ("escalated", "responded")}
    assert set(TRANSITIONS) == expected
    assert ("reminded", "expired") in TRANSITIONS and ("escalated", "responded") in TRANSITIONS  # 08 §2.3 补充


@pytest.mark.parametrize("src,dst", [p for p in product(STATUSES, STATUSES) if p not in TRANSITIONS])
def test_invalid_transitions_rejected(src, dst):
    with pytest.raises(TransitionError) as e:
        transition(sreq(src), dst, at=NOW, egress_audit_ref="aud_x")
    assert e.value.code == "GRAPH_STATE_INVALID"


def test_first_send_requires_approved_approval():
    s = sreq()
    with pytest.raises(TransitionError) as e:
        transition(s, "sent", at=NOW, egress_audit_ref="aud_1")
    assert e.value.code == "GRAPH_APPROVAL_REQUIRED"
    with pytest.raises(TransitionError):
        transition(s, "sent", at=NOW, approval=approval(s, "pending"), egress_audit_ref="aud_1")
    with pytest.raises(TransitionError):
        transition(s, "sent", at=NOW, approval=approval(s, "rejected"), egress_audit_ref="aud_1")
    with pytest.raises(TransitionError):  # subject 指向别的请求
        transition(s, "sent", at=NOW, approval=approval(s, subject=f"{new_id('sreq')}#v1"), egress_audit_ref="aud_1")
    with pytest.raises(TransitionError) as e:  # 缺 egress 审计
        transition(s, "sent", at=NOW, approval=approval(s))
    assert e.value.code == "GRAPH_EGRESS_AUDIT_MISSING"
    a = approval(s)
    out = transition(s, "sent", at=NOW, approval=a, egress_audit_ref="aud_1")
    assert out.status == "sent" and out.version == 2 and out.approval_ref == a.ref and out.last_sent_at == NOW
    assert out.reminder_count == 0 and out.first_sent_at == NOW


def test_reminder_count_increments_and_cap():
    s = transition(sreq("sent"), "reminded", at=NOW, egress_audit_ref="a")
    s = transition(s, "reminded", at=NOW, egress_audit_ref="b")
    s = transition(s, "reminded", at=NOW, egress_audit_ref="c")
    assert s.reminder_count == 3
    with pytest.raises(TransitionError) as e:
        transition(s, "reminded", at=NOW, egress_audit_ref="d", reminder_cap=3)
    assert e.value.code == "GRAPH_REMINDER_CAP"
    s = transition(s, "escalated", at=NOW)
    s = transition(s, "responded", at=NOW)  # escalated → responded
    assert s.reminder_count == 3 and s.status == "responded"


def test_supplement_request_counts():
    s = transition(sreq("reviewed", count=1), "sent", at=NOW, egress_audit_ref="aud")
    assert s.status == "sent" and s.reminder_count == 2


def test_reminded_to_expired_and_terminal():
    s = transition(sreq("reminded", 1), "expired", at=NOW)
    assert s.status == "expired" and allowed_targets("expired") == [] and allowed_targets("closed") == []


def test_approval_ref_format_validated_on_model():
    with pytest.raises(ValueError):
        sreq().model_copy(update={}).model_validate({**sreq().model_dump(), "approval_ref": "apr_bad"})
