# Spec: 08 §2.1–§2.3、§4（首次发送需已批准 Approval；催办经 egress；升级；到期）、04 §2.15
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from belegt_supplier.service import PortalError
from belegt_supplier.web.app import create_app

from conftest import approval_for, counterparty_for, make_manifest

SENDER = {"name": "Anna Weber", "role": "Einkauf"}


@pytest.fixture
def draft(svc):
    m = make_manifest()
    svc.create_draft(m, sender=SENDER, counterparty=counterparty_for(m))
    return m


def review_reminders(svc):
    for loc in ("de", "en", "tr", "zh"):
        svc.review_template(f"stpl:cbam_see_reminder.{loc}@1.0.0", "user:native-reviewer")


def test_first_send_requires_approved_approval(svc, draft, transport):
    with pytest.raises(PortalError) as e:
        svc.send_first(draft.sreq_id, approval_for(draft.sreq_id, status="pending"))
    assert e.value.code == "GRAPH_APPROVAL_REQUIRED" and transport.sent == []
    with pytest.raises(PortalError):
        svc.send_first(draft.sreq_id, approval_for("sreq_01JD8Y3K5M2N4P6Q8R0S2T4V6Z"))
    res = svc.send_first(draft.sreq_id, approval_for(draft.sreq_id))
    assert res["ok"], res
    sreq, _, chase = svc.get_request(draft.sreq_id)
    assert sreq.status == "sent" and sreq.version == 2 and sreq.approval_ref and sreq.egress_audit_ref == res["audit_ref"]
    assert chase["sent_on"] == "2026-11-02"
    em = transport.sent[-1]
    assert em["To"] == draft.contact_email and "SKDM" in em["Subject"]
    body = em.get_body(("plain",)).get_content()
    assert "https://supplier.example.org/r/slk_" in body and "Sayın Ayşe Yılmaz" in body
    # 门户链接可用
    token = body.split("/r/")[1].split()[0]
    assert svc.resolve(token, check_device=False).sreq.status == "sent"


def test_first_send_denied_outside_hours_no_transition(svc, draft, clock):
    clock.now = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)  # Samstag
    res = svc.send_first(draft.sreq_id, approval_for(draft.sreq_id))
    assert not res["ok"] and res["denied"]["code"] == "EGRESS_DENIED_QUIET_PERIOD" and res["denied"]["retry_after"]
    sreq, _, _ = svc.get_request(draft.sreq_id)
    assert sreq.status == "draft"


def test_first_send_recipient_not_allowed(svc, clock):
    m = make_manifest(contact_email="someone@gmail.example")
    svc.create_draft(m, sender=SENDER, counterparty=counterparty_for(make_manifest()).model_copy(update={"counterparty_id": m.counterparty_id}))
    res = svc.send_first(m.sreq_id, approval_for(m.sreq_id))
    assert res["denied"]["code"] == "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED"


def test_unreviewed_reminder_template_blocks_until_reviewed(svc, draft, clock, core):
    svc.send_first(draft.sreq_id, approval_for(draft.sreq_id))
    clock.now = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)  # T+5（r1）
    res = svc.chase_tick()
    assert res[0].action == "denied" and res[0].detail["code"] == "EGRESS_DENIED_TEMPLATE_UNREVIEWED"
    assert any(e == "egress.denied" for e, _ in core.events)
    review_reminders(svc)
    res = svc.chase_tick()
    assert res[0].action == "reminded"
    sreq, _, _ = svc.get_request(draft.sreq_id)
    assert sreq.status == "reminded" and sreq.reminder_count == 1


def test_full_chase_cycle_to_escalation(svc, draft, clock, transport, core):
    review_reminders(svc)
    svc.send_first(draft.sreq_id, approval_for(draft.sreq_id))
    for day, expected in [(9, "reminded"), (16, "reminded"), (20, "escalated")]:
        clock.now = datetime(2026, 11, day, 8, 0, tzinfo=timezone.utc)
        res = svc.chase_tick()
        assert res and res[0].action == expected, res
    sreq, _, chase = svc.get_request(draft.sreq_id)
    assert sreq.status == "escalated" and sreq.reminder_count == 3 and chase["sent_count"] == 4
    subjects = [m["Subject"] for m in transport.sent]
    assert subjects[1].startswith("Hatırlatma") and subjects[2].startswith("Yardımcı") and subjects[3].startswith("Son tarih")
    assert any(e == "request.escalated" for e, _ in core.events)
    # 到期后 escalated 保持
    clock.now = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)
    assert svc.chase_tick() == []
    # 升级后供应商仍提交 → responded
    link = svc.current_link(draft.sreq_id)
    acc = svc.resolve(link["token"], check_device=False)
    svc.submit(acc, attestation=True, values={f"dp:prod.identity.cn_code|prod_01JD8Y3K5M2N4P6Q8R0S2T4V6W": {"value": "72083900"}})
    assert svc.get_request(draft.sreq_id)[0].status == "responded"


def test_reminder_uses_continue_variant_when_opened(svc, draft, clock, transport):
    review_reminders(svc)
    svc.send_first(draft.sreq_id, approval_for(draft.sreq_id))
    link = svc.current_link(draft.sreq_id)
    svc.resolve(link["token"], check_device=False)
    clock.now = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
    res = svc.chase_tick()
    assert res[0].detail["variant"] == "r1_continue"
    assert transport.sent[-1]["Subject"].startswith("Kaldığınız yerden")


def test_deferred_by_quiet_hours_then_sent(svc, draft, clock):
    review_reminders(svc)
    svc.send_first(draft.sreq_id, approval_for(draft.sreq_id))
    clock.now = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)  # 18:00 Istanbul
    res = svc.chase_tick()
    assert res[0].action == "deferred" and res[0].detail["code"] == "EGRESS_DENIED_QUIET_PERIOD"
    _, _, chase = svc.get_request(draft.sreq_id)
    assert chase["retry_after"] == "2026-11-10T06:00:00+00:00"
    clock.now = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
    assert svc.chase_tick() == []  # retry_after 之前不重试
    clock.now = datetime(2026, 11, 10, 6, 30, tzinfo=timezone.utc)
    assert svc.chase_tick()[0].action == "reminded"
    sreq, _, _ = svc.get_request(draft.sreq_id)
    assert sreq.last_sent_at == clock.now  # 延后不更新 last_sent_at，真正发出才更新


def test_expire_without_reply(svc, draft, clock):
    svc.send_first(draft.sreq_id, approval_for(draft.sreq_id))
    clock.now = datetime(2026, 11, 23, 8, 0, tzinfo=timezone.utc)
    res = svc.chase_tick()
    assert res[0].action == "expired"
    assert svc.get_request(draft.sreq_id)[0].status == "expired"


def test_pause_link_stops_external_reminders(svc, draft, clock, transport):
    review_reminders(svc)
    svc.send_first(draft.sreq_id, approval_for(draft.sreq_id))
    acc = svc.resolve(svc.current_link(draft.sreq_id)["token"], check_device=False)
    svc.pause_reminders(acc)
    n = len(transport.sent)
    clock.now = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
    res = svc.chase_tick()
    assert res[0].action == "remind" and len(transport.sent) == n  # 仅内部通知


def test_procurement_pause_14_days(svc, draft, clock, transport):
    review_reminders(svc)
    svc.send_first(draft.sreq_id, approval_for(draft.sreq_id))
    svc.pause_chasing(draft.sreq_id)
    n = len(transport.sent)
    clock.now = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
    svc.chase_tick()
    assert len(transport.sent) == n


def test_admin_api_flow(svc, settings, clock):
    client = TestClient(create_app(settings, service=svc), base_url="https://testserver")
    m = make_manifest(locale="de")
    H = {"Authorization": "Bearer admin-key"}
    assert client.post("/supplier-api/v1/admin/requests", json={"manifest": m.model_dump(mode="json")}).status_code == 401
    r = client.post("/supplier-api/v1/admin/requests", headers=H, json={"manifest": m.model_dump(mode="json"), "sender": SENDER,
                                                                         "counterparty": counterparty_for(m).model_dump()})
    assert r.status_code == 201 and r.json()["status"] == "draft"
    tl = client.get("/supplier-api/v1/admin/templates", headers=H).json()
    assert {t["id"]: t["status"] for t in tl}["stpl:cbam_see_reminder.de@1.0.0"] == "draft"
    r = client.post("/supplier-api/v1/admin/templates/stpl:cbam_see_reminder.de@1.0.0/review", headers=H, json={"reviewer": "user:qa"})
    assert r.json()["status"] == "reviewed"
    r = client.post(f"/supplier-api/v1/admin/requests/{m.sreq_id}/send", headers=H,
                    json={"approval": approval_for(m.sreq_id).model_dump(), "sender_name": "Anna Weber", "sender_role": "Einkauf"})
    assert r.status_code == 200 and r.json()["ok"]
    r = client.post(f"/supplier-api/v1/admin/requests/{m.sreq_id}/link", headers=H)
    assert "token" not in r.json() and r.json()["portal_link"].startswith("https://supplier.example.org/r/")
    assert client.post("/supplier-api/v1/admin/chase/tick", headers=H).json() == []


def test_review_persists_across_service_instances(svc, settings, core, transport, clock):
    from belegt_supplier.service import SupplierService

    svc.review_template("stpl:cbam_see_reminder.zh@1.0.0", "user:zh")
    svc2 = SupplierService(settings, core=core, transport=transport, clock=clock)
    svc2.engine = svc.engine  # 同一内存库
    svc2.sm = svc.sm
    svc2._load_reviews()
    assert svc2.registry.is_sendable("stpl:cbam_see_reminder.zh@1.0.0")
