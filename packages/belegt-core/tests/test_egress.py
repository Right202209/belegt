# Spec: 04 §2.15（egress-gateway：唯一出站通道；EGRESS_DENIED_* 错误码表与调用方处理；retry_after；不排队重试）、§3.4（外发幂等）、
#       08 §4.3 UPC-LIM-01…07、§4.4 静默期、07 §3.2 D7、§7（审计 egress.send / egress.block）
from __future__ import annotations

import datetime as dt

import pytest

from agentkit import new_env
from belegt_core.egress import CoreEgressGateway, EgressDenied, MemoryTransport, StaticTemplateRegistry
from belegt_core.egress.gateway import EGRESS_CODES, EgressMessage, PayloadItem

TUE = dt.datetime(2026, 11, 3, 9, 30, tzinfo=dt.timezone.utc)   # 10:30 Europe/Berlin
SAT = dt.datetime(2026, 11, 7, 10, 0, tzinfo=dt.timezone.utc)
TO = "ayse.yilmaz@anadolu-celik.example"


@pytest.fixture
def gw():
    env = new_env()
    reg = StaticTemplateRegistry({"stpl:ok@1.0.0": ("sha256:ok", "reviewed"), "stpl:draft@1.0.0": ("sha256:dr", "draft")})
    tr = MemoryTransport()
    g = CoreEgressGateway(env.store, reg, tr, guard=env.svc.guard, allowed_link_domains=["supplier.belegt.example"],
                          internal_domains=["musterteile.example"])
    return env, g, tr


def msg(env, **kw) -> EgressMessage:
    base = dict(org_id=env.org_id, recipient=TO, template_id="stpl:ok@1.0.0", template_hash="sha256:ok", subject="Data request",
                body_text="Please provide the data.", counterparty_id=env.ids["supplier"])
    base.update(kw)
    return EgressMessage(**base)


def deny(env, g, m, now=TUE) -> EgressDenied:
    with pytest.raises(EgressDenied) as ei:
        g.send(m, now)
    e = ei.value
    blocks = [a for a in env.store.audit_events(env.org_id) if a["action"] == "egress.block"]
    assert blocks and blocks[-1]["details"]["code"] == e.code and blocks[-1]["details"]["rule_id"] == e.rule_id
    assert blocks[-1]["details"]["recipient_hash"].startswith("sha256:") and TO not in str(blocks[-1]["details"])
    return e


def test_allowed_send_is_audited_and_deduplicated(gw):
    env, g, tr = gw
    rc = g.send(msg(env, subject_ref="sreq_X#v1"), TUE)
    assert rc.message_id and not rc.deduplicated and len(tr.sent) == 1
    sends = [a for a in env.store.audit_events(env.org_id) if a["action"] == "egress.send"]
    assert len(sends) == 1 and sends[0]["details"]["message_id"] == rc.message_id
    rc2 = g.send(msg(env, subject_ref="sreq_X#v1"), TUE + dt.timedelta(days=5))  # 同一版本只发一次（04 §3.4）
    assert rc2.deduplicated and rc2.message_id == rc.message_id and len(tr.sent) == 1


def test_denied_halted(gw):
    env, g, tr = gw
    env.svc.guard.egress_halted = True
    e = deny(env, g, msg(env))
    assert e.code == "EGRESS_DENIED_HALTED" and e.handling == "wait_resume"
    env.svc.guard.egress_halted = False
    env.svc.guard.halted_requests.add("req_01JDEM00000000000000000001")
    assert deny(env, g, msg(env, request_id="req_01JDEM00000000000000000001")).code == "EGRESS_DENIED_HALTED"
    assert tr.sent == []


def test_denied_template_unregistered(gw):
    env, g, _ = gw
    e = deny(env, g, msg(env, template_id="stpl:nope@1.0.0"))
    assert e.code == "EGRESS_DENIED_TEMPLATE_UNREGISTERED" and e.rule_id == "GP-07"
    assert deny(env, g, msg(env, template_hash="sha256:tampered")).code == "EGRESS_DENIED_TEMPLATE_UNREGISTERED"


def test_denied_template_unreviewed(gw):
    env, g, _ = gw
    e = deny(env, g, msg(env, template_id="stpl:draft@1.0.0", template_hash="sha256:dr"))
    assert e.code == "EGRESS_DENIED_TEMPLATE_UNREVIEWED" and e.handling == "fail_review_queue"


def test_denied_recipient_not_allowed(gw):
    env, g, _ = gw
    assert deny(env, g, msg(env, recipient="x@evil.com")).code == "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED"
    e = deny(env, g, msg(env, recipient="unknown.person@anadolu-celik.example"))  # 域名对、地址未经确认
    assert e.code == "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED" and e.rule_id == "UPC-LIM-06"
    g.ledger.suppressed.add(TO)
    assert deny(env, g, msg(env)).rule_id == "UPC-LIM-07"


def test_denied_approval_missing(gw):
    env, g, _ = gw
    e = deny(env, g, msg(env, first_send=True, sreq_id="sreq_01JDEM0000000000000000000S", subject_ref="sreq_01JDEM0000000000000000000S#v1"))
    assert e.code == "EGRESS_DENIED_APPROVAL_MISSING" and e.handling == "fail_approval_inbox"
    e2 = deny(env, g, msg(env, payload=[PayloadItem("dp:x.y.z", "restricted", False)]))
    assert e2.code == "EGRESS_DENIED_APPROVAL_MISSING" and e2.rule_id == "06 §4.4"


def test_denied_dlp(gw):
    env, g, _ = gw
    e = deny(env, g, msg(env, payload=[PayloadItem("dp:fin.revenue.total", "confidential", True)]))
    assert e.code == "EGRESS_DENIED_DLP"
    assert deny(env, g, msg(env, links=["https://phish.example/login"])).code == "EGRESS_DENIED_DLP"
    canary = env.svc.guard.config.canaries[0]
    assert deny(env, g, msg(env, body_text=f"debug {canary}")).rule_id == "LLM07"


def test_denied_rate_limited_with_retry_after(gw):
    env, g, tr = gw
    g.send(msg(env, kind="first"), TUE)
    e = deny(env, g, msg(env, kind="reminder", sent_count=1), TUE + dt.timedelta(days=1))  # 周三工作时间，距上封 24 h
    assert e.code == "EGRESS_DENIED_RATE_LIMITED" and e.rule_id == "UPC-LIM-02" and e.handling == "defer_retry_after"
    assert e.retry_after is not None and e.retry_after >= TUE + dt.timedelta(hours=72)
    blocks = [a for a in env.store.audit_events(env.org_id) if a["action"] == "egress.block"]
    assert blocks[-1]["details"]["retry_after"] == e.to_dict()["retry_after"]
    # UPC-LIM-01：1 + N 封上限 → 升级（不给 retry_after）
    cap = deny(env, g, msg(env, kind="reminder", sent_count=4, recipient=TO), TUE + dt.timedelta(days=30))
    assert cap.rule_id == "UPC-LIM-01" and cap.escalate is True and cap.retry_after is None
    assert len(tr.sent) == 1


def test_denied_quiet_period_with_retry_after(gw):
    env, g, tr = gw
    e = deny(env, g, msg(env), SAT)
    assert e.code == "EGRESS_DENIED_QUIET_PERIOD" and e.rule_id == "UPC-LIM-04"
    assert e.retry_after == dt.datetime(2026, 11, 9, 7, 0, tzinfo=dt.timezone.utc)  # 周一 08:00 Europe/Berlin
    g.send(msg(env), e.retry_after)  # 到点重提即可（网关不排队）
    assert len(tr.sent) == 1


def test_all_eight_codes_covered():
    assert len(EGRESS_CODES) == 8 and len(set(EGRESS_CODES)) == 8


def test_workflow_defers_on_quiet_period_and_retries():
    """工作流对 QUIET_PERIOD 按 retry_after 设 timer 重提（04 §2.15 调用方处理）；LocalRunner auto_advance_timers。"""
    from agentkit import drive_to, upload_file
    from belegt_core.demo import SteppingClock

    env = new_env(clock=SteppingClock(dt.datetime(2026, 11, 7, 10, 0, tzinfo=dt.timezone.utc)))  # 周六
    ref = upload_file(env)
    res, _ = drive_to(env, ref, stop=("awaiting_supplier",))
    assert res.state == "awaiting_supplier"
    req_id = ref.split("#")[0]
    ent = [e for e in env.runner.journal.entries(env.runner.workflow_id(req_id)) if e["name"] in ("chase_send", "sleep")]
    first = next(e for e in ent if e["name"] == "chase_send")["result"]
    assert first["sent"] == [] and first["deferred"] and first["retry_after"].startswith("2026-11-09T07:00")
    assert any(e["name"] == "sleep" for e in ent)
    assert len(env.transport.sent) == 1
    blocks = [a for a in env.store.audit_events(env.org_id) if a["action"] == "egress.block"]
    assert blocks and blocks[0]["details"]["code"] == "EGRESS_DENIED_QUIET_PERIOD"
