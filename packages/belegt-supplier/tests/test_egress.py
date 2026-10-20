# Spec: 04 §2.15 出口错误码；08 §4.3 UPC-LIM-01…07；07 D7 DLP
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from belegt_supplier.egress import (
    ALL_CODES, DefaultEgressGateway, EgressDenied, InMemoryLedger, MemoryTransport, OutboundMessage, OutboxTransport,
    PayloadDatapoint,
)
from belegt_supplier.ids import new_id
from belegt_supplier.messages import TemplateRegistry
from belegt_supplier.models import ApprovalInfo, CounterpartyInfo
from belegt_supplier.workcal import BusinessCalendar

MON_11_IST = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)  # 11:00 Istanbul
SREQ = new_id("sreq")
CP = CounterpartyInfo(counterparty_id=new_id("cp"), name="Anadolu", domains=["anadolu-celik.example"],
                      confirmed_addresses=["ayse@anadolu-celik.example"])
CAL = BusinessCalendar("TR", "Europe/Istanbul")
CTX = {
    "supplier_name": "Anadolu", "customer_name": "Müller", "products": [{"name": "Warmband", "cn_code": "72083900"}],
    "datapoints": ["dp:prod.cbam.see_direct"], "period": {"start": "2026-01-01", "end": "2026-12-31"},
    "due_date": "2026-11-23", "link_expires_at": "2026-12-07", "portal_link": "https://supplier.example.org/r/slk_x",
    "sender_name": "Anna", "sender_role": "Einkauf", "sreq_id": SREQ, "privacy_note_link": "https://mueller.example/privacy",
}


@pytest.fixture
def reg():
    r = TemplateRegistry.load_builtin()
    return r


@pytest.fixture
def gw(reg):
    return DefaultEgressGateway(reg, MemoryTransport(), ledger=InMemoryLedger(),
                                allowed_link_domains=["supplier.example.org", "mueller.example"], internal_domains=["mueller.example"])


def approved():
    return ApprovalInfo(ref=f"{new_id('apr')}#v1", subject_ref=f"{SREQ}#v1", status="approved", decision="approved")


def msg(reg, *, tid="stpl:cbam_see_request.tr@1.0.0", variant=None, **kw):
    base = dict(org_id="org_X", sreq_id=SREQ, recipient="ayse@anadolu-celik.example",
                rendered=reg.render(tid, CTX, variant=variant), counterparty=CP, first_send=True, approval=approved(),
                kind="first", calendar=CAL)
    base.update(kw)
    return OutboundMessage(**base)


def denied(gw, m, now=MON_11_IST) -> EgressDenied:
    with pytest.raises(EgressDenied) as e:
        gw.send(m, now)
    assert e.value.code in ALL_CODES
    assert gw.audit_events[-1]["action"] == "egress.block" and gw.audit_events[-1]["code"] == e.value.code
    return e.value


def test_send_ok_writes_audit_and_ledger(gw, reg):
    r = gw.send(msg(reg, pause_link="https://supplier.example.org/pause/slk_x"), MON_11_IST)
    ev = gw.audit_events[-1]
    assert ev["action"] == "egress.send" and ev["template_id"] == "stpl:cbam_see_request.tr@1.0.0"
    assert ev["body_sha256"].startswith("sha256:") and "ayse" not in str(ev)  # 收件人只存哈希与域名
    em = gw.transport.sent[-1]
    assert em["X-Belegt-Template"].startswith("stpl:cbam_see_request.tr@1.0.0") and em["List-Unsubscribe"]
    assert r.audit_ref == ev["id"] and gw.ledger.last_to_recipient("ayse@anadolu-celik.example") == MON_11_IST


def test_halted(gw, reg):
    gw.halted = True
    assert denied(gw, msg(reg)).code == "EGRESS_DENIED_HALTED"


def test_template_unregistered(gw, reg):
    m = msg(reg)
    m.rendered.template_hash = "sha256:" + "0" * 64
    assert denied(gw, m).code == "EGRESS_DENIED_TEMPLATE_UNREGISTERED"


def test_template_unreviewed(gw, reg):
    d = denied(gw, msg(reg, tid="stpl:cbam_see_reminder.tr@1.0.0", variant="r1", first_send=False, kind="reminder"))
    assert d.code == "EGRESS_DENIED_TEMPLATE_UNREVIEWED" and d.retry_after is None


@pytest.mark.parametrize("recipient,cp", [
    ("x@evil.example", CP),
    ("other@anadolu-celik.example", CP),  # 域名对但地址未人工确认
    ("ayse@anadolu-celik.example", None),
])
def test_recipient_not_allowed(gw, reg, recipient, cp):
    d = denied(gw, msg(reg, recipient=recipient, counterparty=cp))
    assert d.code == "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED" and d.rule_id == "UPC-LIM-06"


def test_suppressed_recipient(gw, reg):
    gw.ledger.suppress("ayse@anadolu-celik.example", "hard_bounce")
    d = denied(gw, msg(reg))
    assert d.code == "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED" and d.rule_id == "UPC-LIM-07"


@pytest.mark.parametrize("appr", [None,
                                  ApprovalInfo(ref=f"{new_id('apr')}#v1", subject_ref=f"{SREQ}#v1", status="pending"),
                                  ApprovalInfo(ref=f"{new_id('apr')}#v1", subject_ref=f"{new_id('sreq')}#v1", status="approved", decision="approved")])
def test_approval_missing(gw, reg, appr):
    assert denied(gw, msg(reg, approval=appr)).code == "EGRESS_DENIED_APPROVAL_MISSING"


def test_restricted_payload_requires_apr_and_dlp(gw, reg):
    assert denied(gw, msg(reg, payload=[PayloadDatapoint("dp:x", "restricted")])).code == "EGRESS_DENIED_APPROVAL_MISSING"
    d = denied(gw, msg(reg, payload=[PayloadDatapoint("dp:x", "confidential", value_included=True)]))
    assert d.code == "EGRESS_DENIED_DLP"


def test_dlp_link_domain(reg):
    gw = DefaultEgressGateway(reg, MemoryTransport(), allowed_link_domains=["supplier.example.org"])
    assert denied(gw, msg(reg)).code == "EGRESS_DENIED_DLP"  # 隐私链接域名不在白名单


def test_lim01_total_cap_escalates(gw, reg):
    d = denied(gw, msg(reg, first_send=False, sreq_sent_count=4, reminder_cap=3))
    assert d.code == "EGRESS_DENIED_RATE_LIMITED" and d.rule_id == "UPC-LIM-01" and d.escalate and d.retry_after is None


def test_lim04_quiet_period_and_hours(gw, reg):
    sat = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)
    d = denied(gw, msg(reg), now=sat)
    assert d.code == "EGRESS_DENIED_QUIET_PERIOD" and d.retry_after == datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)  # 17:00 Istanbul
    assert denied(gw, msg(reg), now=evening).retry_after == datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
    holiday = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
    assert denied(gw, msg(reg), now=holiday).code == "EGRESS_DENIED_QUIET_PERIOD"


def test_lim02_same_contact_72h(gw, reg):
    gw.send(msg(reg), MON_11_IST)
    d = denied(gw, msg(reg, first_send=False, sreq_sent_count=1), now=MON_11_IST + timedelta(hours=24))
    assert d.code == "EGRESS_DENIED_RATE_LIMITED" and d.rule_id == "UPC-LIM-02"
    assert d.retry_after >= MON_11_IST + timedelta(hours=72)
    gw.send(msg(reg, first_send=False, sreq_sent_count=1), MON_11_IST + timedelta(hours=72))


def test_lim03_org_3_per_7_days(gw, reg):
    for i, addr in enumerate(["a@anadolu-celik.example", "b@anadolu-celik.example", "c@anadolu-celik.example"]):
        cp = CP.model_copy(update={"confirmed_addresses": [addr, "d@anadolu-celik.example"]})
        gw.send(msg(reg, recipient=addr, counterparty=cp), MON_11_IST + timedelta(hours=i))
    cp = CP.model_copy(update={"confirmed_addresses": ["d@anadolu-celik.example"]})
    d = denied(gw, msg(reg, recipient="d@anadolu-celik.example", counterparty=cp), now=MON_11_IST + timedelta(hours=4))
    assert d.rule_id == "UPC-LIM-03" and d.retry_after >= MON_11_IST + timedelta(days=7)


def test_lim05_tenant_daily_first_week(gw, reg):
    for i in range(50):
        gw.ledger.record(org_id="org_X", counterparty_id=f"cp_{i}", recipient=f"r{i}@x.example", sreq_id=None, at=MON_11_IST)
    d = denied(gw, msg(reg))
    assert d.rule_id == "UPC-LIM-05" and d.retry_after.date() == datetime(2026, 11, 3).date()


def test_internal_message_skips_supplier_limits(gw, reg):
    ctx = dict(CTX, reminder_count=3, calc={"result": "1", "low": "1", "high": "2", "ref": "calc_X#v1"})
    gw.registry.review("stpl:chase_escalation_internal.en@1.0.0", "user:qa")
    m = OutboundMessage(org_id="org_X", sreq_id=SREQ, recipient="einkauf@mueller.example",
                        rendered=reg.render("stpl:chase_escalation_internal.en@1.0.0", ctx), internal=True)
    gw.send(m, datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc))  # 周六夜间也可发内部通知
    m2 = OutboundMessage(org_id="org_X", sreq_id=SREQ, recipient="x@gmail.example",
                         rendered=reg.render("stpl:chase_escalation_internal.en@1.0.0", ctx), internal=True)
    assert denied(gw, m2).code == "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED"


def test_outbox_transport_writes_eml(tmp_path, reg):
    gw = DefaultEgressGateway(reg, OutboxTransport(tmp_path / "outbox"), allowed_link_domains=["supplier.example.org", "mueller.example"])
    gw.send(msg(reg), MON_11_IST)
    files = list((tmp_path / "outbox").glob("*.eml"))
    assert len(files) == 1 and b"Subject:" in files[0].read_bytes()
