# Spec: 04 §2.7 answer-drafter（不写数字；每句 cites；GP-05 数字扫描；修正 1 次后转人工；legal 关键词双判；模型不可用只做 L0 复用）、
#       §2.8 approval-router 适配（core ApprovalRouter / 最小确定性回退；策略不可用 → 全部 L3）、
#       §2.6 consistency-auditor 适配（判定确定、fail-closed）
from __future__ import annotations

from typing import Any

import pytest

from agentkit import new_env, upload_file
from belegt_core.agents import AnswerDrafter, ApprovalRouterAgent, ConsistencyAuditorAgent, FallbackApprovalRouter
from belegt_core.agents import approval_router as ar_mod
from belegt_core.agents import consistency_auditor as ca_mod
from belegt_core.agents.base import latest_ref
from belegt_core.authz import Actor
from belegt_core.errors import GraphError
from belegt_core.llm.mock import MockLLM

P25 = {"start": "2025-01-01", "end": "2025-12-31"}


def _hist(env, dp: str) -> str:
    a = [x for x in env.store.query("Answer", org_id=env.org_id, status="submitted", datapoint_base=dp)][0]
    return f"{a['id']}#v{a['version']}"


def _target(env, dp: str, **kw: Any) -> dict[str, Any]:
    return {"datapoint_id": latest_ref(env.svc.registry, dp), "period": P25, "boundary": "organization", "question_ids": [],
            "question_refs": [], "reuse": [_hist(env, dp)] if kw.pop("reuse", True) else [], "evidence_refs": [], **kw}


def _env(llm=None):
    env = new_env(llm=llm)
    return env, upload_file(env)


# ---------------------------------------------------------------------------- answer-drafter
def test_drafter_reuses_history_l0_and_skips_numbers():
    env, ref = _env()
    ts = [_target(env, "dp:custom.org.address"), _target(env, "dp:custom.org.group_member"),
          {**_target(env, "dp:custom.org.address"), "datapoint_id": latest_ref(env.svc.registry, "dp:energy.consumption.electricity_total")}]
    out = AnswerDrafter(env.svc).run(ref, ts, idem_prefix="t:d1")
    assert len(out.refs) == 2 and out.details["pending_manual"] == []
    a = env.store.get(out.refs[0])
    assert a["created_by"] == "agent:answer-drafter" and a["value"] == "Industriestraße 12, 74072 Heilbronn"
    assert a["reused_from"] == ts[0]["reuse"][0] and a["value_type"] == "text"
    b = env.store.get(out.refs[1])
    assert b["value"] is False and b["value_type"] == "bool"


def test_drafter_number_in_text_revised_once_then_manual():
    calls = []

    def draft(req):
        calls.append(req)
        return {"value": "Industriestraße 12, 74072 Heilbronn", "legal_commitment": False, "is_estimate": False,
                "sentences": [{"text": "Wir beschäftigen 4711 Mitarbeitende am Standort.", "cites": [req.data["reused"][0]["ref"]]}]}
    env, ref = _env(MockLLM(script={"draft_answer": draft}))
    out = AnswerDrafter(env.svc).run(ref, [_target(env, "dp:custom.org.address")], idem_prefix="t:d2")
    assert out.refs == []
    (m,) = out.details["pending_manual"]
    assert "GRAPH_NUM_IN_TEXT" in m["reason"]
    assert len(calls) == 2 and calls[1].data.get("previous_errors") == ["GRAPH_NUM_IN_TEXT"]  # 修正 1 次


def test_drafter_uncited_sentence_rejected():
    env, ref = _env(MockLLM(script={"draft_answer": {"value": "Industriestraße 12, 74072 Heilbronn", "legal_commitment": False,
                                                     "is_estimate": False,
                                                     "sentences": [{"text": "Siehe Webseite.", "cites": []}]}}))
    out = AnswerDrafter(env.svc).run(ref, [_target(env, "dp:custom.org.address")], idem_prefix="t:d3")
    assert out.refs == [] and "SENTENCE_UNCITED" in out.details["pending_manual"][0]["reason"]


def test_drafter_legal_keyword_flags_commitment():
    def draft(req):
        return {"value": "Industriestraße 12, 74072 Heilbronn", "legal_commitment": False, "is_estimate": False,
                "sentences": [{"text": "Wir sichern zu, dass die Anschrift aktuell ist.", "cites": [req.data["reused"][0]["ref"]]}]}
    env, ref = _env(MockLLM(script={"draft_answer": draft}))
    out = AnswerDrafter(env.svc).run(ref, [_target(env, "dp:custom.org.address")], idem_prefix="t:d4")
    a = env.store.get(out.refs[0])
    assert a["legal_commitment"] is True and a["drafting_note"]["de"].startswith("Wir sichern zu")
    assert "reused_from" in a


def test_drafter_model_unavailable_only_l0_reuse():
    env, ref = _env(MockLLM(fail_tasks={"draft_answer"}))
    t_reuse = _target(env, "dp:custom.org.address")
    t_ev = _target(env, "dp:custom.org.website", reuse=False)
    out = AnswerDrafter(env.svc).run(ref, [t_reuse, t_ev], idem_prefix="t:d5")
    assert len(out.refs) == 1 and env.store.get(out.refs[0])["reused_from"] == t_reuse["reuse"][0]
    assert out.details["pending_manual"][0]["reason"] in ("no_source", "model_unavailable")


def test_answer_drafter_actor_may_not_write_number_answer():
    env, ref = _env()
    ev_any = env.store.query("Evidence", org_id=env.org_id)
    body = {"datapoint_id": latest_ref(env.svc.registry, "dp:energy.consumption.electricity_total"), "value": "4820",
            "value_type": "number", "unit": "MWh", "period": P25, "boundary": "organization", "customer_scope": "all",
            "origin": "generated", "is_estimate": False, "legal_commitment": False, "status": "draft",
            "evidence_refs": [f"{e['id']}#v{e['version']}" for e in ev_any[:1]] or None}
    body = {k: v for k, v in body.items() if v is not None}
    with pytest.raises(GraphError) as ei:
        env.store.write("Answer", body, actor=Actor.of("agent:answer-drafter", env.org_id))
    assert ei.value.code in ("GRAPH_ACTOR_NOT_ALLOWED", "GRAPH_NUM_UNREFERENCED")


# ---------------------------------------------------------------------------- approval-router 适配
def _drafts(env, ref):
    ts = [_target(env, "dp:custom.org.address"), _target(env, "dp:custom.org.website"), _target(env, "dp:custom.org.group_member")]
    return AnswerDrafter(env.svc).run(ref, ts, idem_prefix="t:ar:draft").refs


def test_approval_router_uses_core_policy():
    env, ref = _env()
    refs = _drafts(env, ref)
    agent = ApprovalRouterAgent(env.svc)
    assert agent.implementation == "belegt_core.approval"
    out = agent.run(ref, refs, idem_prefix="t:ar1")
    assert len(out.details["approval_refs"]) == 3 and sum(out.details["levels"].values()) == 3
    for r in out.details["approval_refs"]:
        apr = env.store.get(r)
        assert apr["created_by"] == "agent:approval-router" and apr["status"] == "pending"
        assert apr["policy_version"] == "approval-policy@1.0.0" and "compliance_owner" in apr["required_role"]
        assert env.store.get(apr["subject_ref"])["status"] == "in_review"


def test_approval_router_fallback_when_core_missing(monkeypatch):
    env, ref = _env()
    refs = _drafts(env, ref)
    monkeypatch.setattr(ar_mod, "_CoreApprovalRouter", None)
    agent = ApprovalRouterAgent(env.svc)
    assert agent.implementation == "fallback"
    out = agent.run(ref, refs, idem_prefix="t:ar2")
    assert out.details["levels"] == {"L0": 3}  # 复用历史的非数字答案 → L0
    apr = env.store.get(out.details["approval_refs"][0])
    assert apr["policy_version"] == "approval-policy@1.0.0" and ar_mod.FALLBACK_TAG in apr["tags"]
    assert apr["batch_id"].startswith("apb_")


def test_approval_router_policy_unavailable_is_l3(monkeypatch):
    env, ref = _env()
    refs = _drafts(env, ref)

    def broken(store, *a, **k):
        raise RuntimeError("policy service down")
    monkeypatch.setattr(ar_mod, "_CoreApprovalRouter", broken)
    out = ApprovalRouterAgent(env.svc).run(ref, refs, idem_prefix="t:ar3")
    assert out.details["levels"] == {"L3": 3}  # 宁严勿宽（04 §2.8）
    for r in out.details["approval_refs"]:
        apr = env.store.get(r)
        assert set(apr["required_role"]) == {"compliance_owner", "executive"} and "batch_id" not in apr


def test_fallback_router_levels_unit():
    env, _ = _env()
    fr = FallbackApprovalRouter(env.store)
    assert fr._level({"legal_commitment": True}) == "L3"
    assert fr._level({"is_estimate": True}) == "L2"
    assert fr._level({"calculation_ref": "calc_x#v1"}) == "L2"
    assert fr._level({"reused_from": "ans_x#v1", "value_type": "text"}) == "L0"
    assert fr._level({"value_type": "number", "evidence_refs": ["ev_x#v1"]}) == "L1"
    assert FallbackApprovalRouter(env.store, policy_available=False)._level({}) == "L3"


# ---------------------------------------------------------------------------- consistency-auditor 适配
def test_consistency_auditor_fallback_and_engine(monkeypatch):
    env, ref = _env()
    out = ConsistencyAuditorAgent(env.svc).run(ref, idem_prefix="t:ca1")
    assert out.details["implementation"] == "belegt_core.consistency" and out.details["gate"] in ("pass", "block", "unknown")
    monkeypatch.setattr(ca_mod, "_Engine", None)
    out2 = ConsistencyAuditorAgent(env.svc).run(ref, idem_prefix="t:ca2")
    assert out2.details == {"implementation": "fallback", "gate": "pass", "blocking": []}


def test_consistency_auditor_reports_open_blocking_conflicts(monkeypatch):
    """引擎判 block → 只把本 Request 的 open blocking 冲突交给工作流（人处置后才继续）。"""
    env, ref = _env()
    req_id = ref.split("#")[0]

    class FakeRun:
        gate = "block"
        conflicts_written = ["cfl_X#v1"]
        results: list = []

        def summary(self):
            return {"counts": {"fail": 1}}

    class FakeEngine:
        def __init__(self, store, **kw):
            pass

        def run(self, trigger, **kw):
            assert trigger == "pre_submit_gate" and kw["request_id"] == req_id
            return FakeRun()

    monkeypatch.setattr(ca_mod, "_Engine", FakeEngine)
    out = ConsistencyAuditorAgent(env.svc).run(ref, idem_prefix="t:ca3")
    assert out.outcome == "block" and out.details["gate"] == "block" and out.refs == ["cfl_X#v1"]
    assert out.details["blocking"] == []  # 图中没有该 Request 的 open blocking 冲突
