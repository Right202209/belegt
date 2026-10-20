# Spec: 04 §2.2–§2.11（Agent 契约：写入执行者、信任区、输出只含 ObjectRef）、§2.1（晋升只盖章）、NFR-03（数字只经 calc-service）
"""以一次完整的短问卷运行（会话 fixture ``csv_run``）核对每个 Agent 在证据图里留下的痕迹是否符合契约。"""
from __future__ import annotations

from decimal import Decimal

from agentkit import all_versions, request_answers, writers

ROUTED_WRITERS = {"agent:approval-router", "agent:filler"}


def test_intake_triage_writes_quarantine_request_v1(csv_run):
    env, ref, *_ = csv_run
    v1 = env.store.get(ref)
    assert v1["version"] == 1 and v1["created_by"] == "agent:intake-triage"
    assert v1["trust_zone"] == "quarantine" and v1["status"] == "received"
    assert v1["due_date"] == "2026-12-15"  # 主题可见文本"bis 15.12.2026"（RT-PI-02：只认可见日期）
    assert v1["reporting_period"] == {"start": "2025-01-01", "end": "2025-12-31"}
    v2 = env.store.get(ref.split("#")[0], version=2)
    assert v2["created_by"] == "agent:guard" and v2["trust_zone"] == "trusted"
    assert v2["promotion"]["rule"] == "PROMO-REQ-0.90" and v2["status"] == "triaged"


def test_question_parser_only_writes_quarantine_candidates(csv_run):
    env, ref, *_ = csv_run
    org = env.org_id
    for v in all_versions(env.store, org, "Mapping"):
        if v["created_by"] == "agent:question-parser":
            assert v["trust_zone"] == "quarantine" and v["status"] == "candidate" and v["version"] == 1
        else:  # 晋升/落选只由 guard 盖章，不改值
            assert v["created_by"] == "agent:guard" and v["trust_zone"] == "trusted"
            v1 = env.store.get(v["id"], version=1)
            assert v["body"]["datapoint_id"] == v1["datapoint_id"] and v["body"]["confidence"] == v1["confidence"]
    qs = env.store.query("Question", org_id=org, request_id=ref.split("#")[0])
    assert len(qs) == 15 and all(q["status"] == "mapped" and q["trust_zone"] == "trusted" for q in qs)
    assert writers(env.store, org, "Question") == {"agent:question-parser": 15, "agent:guard": 30}
    confirmed = env.store.query("Mapping", org_id=org, status="confirmed")
    assert {m["question_id"] for m in confirmed} >= {q["id"] for q in qs}
    assert all(m["promotion"]["rule"] == "PROMO-MAP-0.85" for m in confirmed if m["question_id"] in {q["id"] for q in qs})


def test_evidence_miner_evidence_is_located_and_verified_by_rule_or_human(csv_run):
    env, *_ = csv_run
    evs = [e for e in env.store.query("Evidence", org_id=env.org_id) if e["source_type"] in ("erp", "document")]
    assert evs
    for e in evs:
        assert e["trust_zone"] == "trusted"
        v1 = env.store.get(e["id"], version=1)
        assert v1["created_by"] == "agent:evidence-miner" and v1["status"] == "candidate"  # 先 candidate
        if e["source_type"] == "document":
            assert e["locator"]["blob_sha256"].startswith("sha256:")
            assert any(e["locator"].get(k) for k in ("page", "cell", "bbox"))
        else:
            assert set(e["locator"]["connector"]) >= {"connector_id", "query_hash", "record_key", "fetched_at"}
        if e["status"] == "verified":
            ver = e.get("verification") or {}
            assert ver.get("rule_id") in ("EV-AUTO-ERP-ATTEST", "EV-AUTO-0.90-LOC") or e["created_by"] == "service:graph-api"


def test_calc_service_is_the_only_number_channel(csv_run):
    env, ref, *_ = csv_run
    org = env.org_id
    nums = [v for v in all_versions(env.store, org, "Answer") if v["body"].get("value_type") == "number" and v["version"] == 1
            and v["body"].get("origin") != "imported"]
    assert nums and {v["created_by"] for v in nums} == {"agent:calc-service"}
    for a in request_answers(env, ref.split("#")[0]):
        if a["value_type"] == "number":
            assert a.get("calculation_ref") or a.get("evidence_refs")
            assert env.store.replay_answer(f"{a['id']}#v{a['version']}")["status"] in ("ok", "drift")
    scope = {c["method_id"]: c["result"]["value"] for c in env.store.query("Calculation", org_id=org)}
    assert Decimal(scope["ghg.scope1.total"]) == Decimal("481.92")
    assert Decimal(scope["ghg.scope2.market_based"]) == Decimal("2027.2")
    assert writers(env.store, org, "Calculation") == {"agent:calc-service": 2}
    # 方法论决策 CQ-METH-02（GWP 集）由 calc-service 出卡、人作答后写进 params
    s1 = next(c for c in env.store.query("Calculation", org_id=org) if c["method_id"] == "ghg.scope1.total")
    assert s1["params"]["gwp_set"] == "ar6"


def test_answer_drafter_never_writes_numbers(csv_run):
    env, ref, *_ = csv_run
    drafted = [v for v in all_versions(env.store, env.org_id, "Answer") if v["created_by"] == "agent:answer-drafter"]
    assert drafted
    assert all(v["body"]["value_type"] != "number" for v in drafted)
    for v in drafted:
        if v["body"].get("drafting_note"):
            assert v["body"].get("evidence_refs") or v["body"].get("reused_from")


def test_approval_router_routes_every_draft_with_pinned_subject(csv_run):
    env, ref, *_ = csv_run
    org = env.org_id
    req_id = ref.split("#")[0]
    aprs = [a for a in env.store.query("Approval", org_id=org) if a.get("request_id") == req_id]
    ans = request_answers(env, req_id)
    assert len(aprs) == len(ans) == 14
    for a in aprs:
        v1 = env.store.get(a["id"], version=1)
        assert v1["created_by"] == "agent:approval-router" and v1["status"] == "pending"
        assert v1["policy_version"] == "approval-policy@1.0.0"
        subj = env.store.get(v1["subject_ref"])
        assert subj["status"] == "in_review" and subj["risk_level"] == v1["risk_level"]
        assert a["status"] == "approved" and a["decided_by"].startswith("user:")
    levels = {a["risk_level"] for a in aprs}
    assert levels <= {"L0", "L1", "L2", "L3"}


def test_consistency_auditor_gate_passed(csv_run):
    env, ref, res, trail = csv_run
    from belegt_core.agents import ConsistencyAuditorAgent

    out = ConsistencyAuditorAgent(env.svc).run(ref, idem_prefix="test:audit:again")
    assert out.details["implementation"] == "belegt_core.consistency"
    assert out.details["gate"] in ("pass", "block") and out.details["blocking"] == []


def test_upstream_chaser_and_egress(csv_run):
    env, *_ = csv_run
    org = env.org_id
    (s,) = env.store.query("SupplierRequest", org_id=org)
    assert env.store.get(s["id"], version=1)["created_by"] == "agent:upstream-chaser"
    assert env.store.get(s["id"], version=2)["created_by"] == "agent:egress-gateway"
    assert s["template_id"] == "stpl:cbam_see_request.en@1.0.0" and s["status"] == "reviewed"
    apr = [a for a in env.store.query("Approval", org_id=org) if a["subject_ref"].startswith(s["id"])]
    assert apr and apr[0]["status"] == "approved" and apr[0]["decided_by"] == "user:u_owner"
    assert len(env.transport.sent) == 1 and env.transport.sent[0].recipient == "ayse.yilmaz@anadolu-celik.example"
    acts = [e for e in env.store.audit_events(org) if e["action"] == "egress.send"]
    assert len(acts) == 1


def test_supplier_reply_reviewer_writes_quarantine_only(csv_run):
    env, *_ = csv_run
    vs = [v for v in all_versions(env.store, env.org_id) if v["created_by"] == "agent:supplier-reply-reviewer"]
    assert vs and {(v["type"], v["trust_zone"], v["status"]) for v in vs} == {("Evidence", "quarantine", "candidate")}
    for v in vs:
        assert v["body"]["source_type"] == "supplier_response" and v["body"]["plausibility"]["flag"] == "ok"
        promoted = env.store.get(v["id"], version=2)
        assert promoted["created_by"] == "agent:guard" and promoted["promotion"]["rule"] == "PROMO-EV-SUP"
        assert promoted["extracted_value"] == v["body"]["extracted_value"]  # 只盖章不改值


def test_filler_file_exports_and_submits_approved_only(csv_run):
    env, ref, *_ = csv_run
    req_id = ref.split("#")[0]
    fill = next(e for e in env.runner.journal.entries(env.runner.workflow_id(req_id)) if e["name"] == "fill_file")["result"]
    assert fill["written"] == 14 and fill["rejected"] == []
    assert env.store.get_blob(env.org_id, fill["export_blob"])[1] == "text/csv"
    for a in request_answers(env, req_id):
        assert a["status"] == "submitted" and a["created_by"] == "agent:filler"
        assert a["submission"]["export_input_hash"] == fill["export_input_hash"]
        prev = env.store.get(a["id"], version=a["version"] - 1)
        assert prev["status"] == "approved"
    assert env.store.get(req_id)["status"] == "archived"


def test_every_agent_span_recorded(csv_run):
    env, ref, *_ = csv_run
    spans = env.svc.guard.recorder.for_request(ref.split("#")[0])
    names = {s.actor for s in spans}
    for agent in ("question-parser", "evidence-miner", "calc-service", "answer-drafter", "consistency-auditor",
                  "approval-router", "upstream-chaser", "filler"):
        assert f"agent:{agent}" in names, (agent, names)
