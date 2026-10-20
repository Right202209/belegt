# Spec: 04 §3.2（T01…T23：主路径、T18/T19 awaiting_human、T07/T08 awaiting_supplier、T20/T21 halted、T22/T23 cancelled）、
#       §3.4（信号去重）、§4.3（Activity 结果只含 ObjectRef 与计数）、06 §1.3（gate unknown → fail-closed halt）
from __future__ import annotations

import json

import pytest

from agentkit import drive_to, new_env, upload_file
from belegt_core.demo import simulate_supplier_reply
from belegt_core.guard.policy import Halted
from belegt_core.statemachines import parse_status_reason

MAIN_PATH = ["received", "triaged", "parsed", "mapped", "evidence_gathering", "awaiting_supplier", "drafting", "auditing",
             "in_review", "approved", "filling", "submitted", "archived"]


def _statuses(env, req_id):
    out = []
    for v in env.store.history(req_id, org_id=env.org_id):
        if not out or out[-1] != v["status"]:
            out.append(v["status"])
    return out


def test_full_path_reaches_archived(csv_run):
    env, ref, res, trail = csv_run
    req_id = ref.split("#")[0]
    seen = _statuses(env, req_id)
    it = iter(seen)
    assert all(s in it for s in MAIN_PATH), seen  # 主路径按序出现（中间可夹侧路）
    assert res.result["status"] == "archived" and res.result["export_blob"].startswith("sha256:")
    # 每个 awaiting_human 都回到其 return_to
    hist = env.store.history(req_id, org_id=env.org_id)
    for i, v in enumerate(hist):
        if v["status"] == "awaiting_human":
            info = parse_status_reason(v["status_reason"])
            nxt = next(x for x in hist[i + 1:] if x["status"] != "awaiting_human")
            assert nxt["status"] in (info["return_to"], "halted"), (info, nxt["status"])
    # Activity 日志只含 ObjectRef / 计数 / 状态（无业务值）
    for e in env.runner.journal.entries(env.runner.workflow_id(req_id)):
        blob = json.dumps(e["result"])
        assert "481.92" not in blob and "Industriestraße" not in blob


def test_awaiting_human_then_resolution():
    env = new_env()
    ref = upload_file(env)
    res = env.runner.run(ref)
    assert res.state == "awaiting_human" and res.pending
    req = env.store.get(ref.split("#")[0])
    info = parse_status_reason(req["status_reason"])
    assert req["status"] == "awaiting_human" and info["return_to"] == "evidence_gathering" and info["reason"] == "cq"
    assert all(p.startswith(("cq_", "q_", "apr_", "cfl_")) for p in res.pending)
    again = env.runner.run(ref)  # 人没处理 → 重放仍停在同一人工节点，不重复写
    assert again.state == "awaiting_human" and sorted(again.pending) == sorted(res.pending)
    n_versions = len(env.store.history(req["id"], org_id=env.org_id))
    env.runner.run(ref)
    assert len(env.store.history(req["id"], org_id=env.org_id)) == n_versions


def test_awaiting_supplier_signal_and_dedupe():
    env = new_env()
    ref = upload_file(env)
    res, _ = drive_to(env, ref, stop=("awaiting_supplier",))
    assert res.state == "awaiting_supplier"
    req_id = ref.split("#")[0]
    assert env.store.get(req_id)["status"] == "awaiting_supplier"
    (sid,) = res.pending
    assert env.store.get(sid)["status"] == "sent"
    assert env.runner.run(ref).state == "awaiting_supplier"  # 无信号 → 继续等
    simulate_supplier_reply(env, [sid])
    simulate_supplier_reply(env, [sid])  # 重复信号（04 §3.4 去重）
    res2 = env.runner.run(ref)
    assert res2.state in ("awaiting_human", "in_review")
    reviews = [e for e in env.runner.journal.entries(env.runner.workflow_id(req_id)) if e["name"] == "review_replies"]
    assert len(reviews) == 1
    sup_ev = [e for e in env.store.query("Evidence", org_id=env.org_id) if e["source_type"] == "supplier_response"]
    assert len(sup_ev) == 1
    res3, _ = drive_to(env, ref)
    assert res3.state == "completed"


def test_supplier_deadline_expires_request():
    env = new_env()
    ref = upload_file(env)
    res, _ = drive_to(env, ref, stop=("awaiting_supplier",))
    (sid,) = res.pending
    env.clock.advance(days=40)  # 超过 SupplierRequest.due_date
    res2 = env.runner.run(ref)
    assert env.store.get(sid)["status"] == "expired"
    assert res2.state != "awaiting_supplier"


def test_halt_resume_via_policy_violation():
    env = new_env()
    ref = upload_file(env)
    fired = []

    def fault(name, seq, phase):
        if name == "audit" and phase == "before" and not fired:
            fired.append(seq)
            raise Halted("policy", "simulated GP violation")
    env.runner.fault = fault
    res, _ = drive_to(env, ref)
    assert res.state == "halted"
    req = env.store.get(ref.split("#")[0])
    info = parse_status_reason(req["status_reason"])
    assert req["status"] == "halted" and info == {**info, "reason": "policy", "return_to": "auditing"}
    with pytest.raises(Exception):  # T21 只能由人（org_admin/compliance_owner）恢复
        env.runner.resume(req["id"], env.user("sales"))
    res = env.runner.resume(req["id"], env.user("owner"))
    assert env.store.get(req["id"])["status"] != "halted"
    res, _ = drive_to(env, ref)
    assert res.state == "completed"


def test_gate_unknown_is_fail_closed(monkeypatch):
    from belegt_core.workflow import activities as acts_mod

    env = new_env()
    ref = upload_file(env)
    calls = []
    orig = acts_mod.Activities.audit

    def audit(self, idem, req_ref):
        out = orig(self, idem, req_ref)
        calls.append(1)
        return {**out, "gate": "unknown"} if len(calls) == 1 else out
    monkeypatch.setattr(acts_mod.Activities, "audit", audit)
    res, _ = drive_to(env, ref)
    assert res.state == "halted"
    req = env.store.get(ref.split("#")[0])
    assert parse_status_reason(req["status_reason"])["return_to"] == "auditing"
    env.runner.resume(req["id"], env.user("owner"))
    res, _ = drive_to(env, ref)
    assert res.state == "completed"


def test_cancel_while_awaiting_human():
    env = new_env()
    ref = upload_file(env)
    res = env.runner.run(ref)
    assert res.state == "awaiting_human"
    req_id = ref.split("#")[0]
    n_entries = len(env.runner.journal.entries(env.runner.workflow_id(req_id)))
    out = env.runner.cancel(req_id, env.user("owner"))
    assert out.state == "cancelled" and env.store.get(req_id)["status"] == "cancelled"
    # 人随后处理了卡片也不再推进
    from agentkit import human
    human(env).resolve(res.pending)
    again = env.runner.run(ref)
    assert again.state == "cancelled"
    acts = [e for e in env.runner.journal.entries(env.runner.workflow_id(req_id)) if e["kind"] == "activity"]
    assert len([e for e in env.runner.journal.entries(env.runner.workflow_id(req_id))]) - n_entries <= 1  # 只多一个 cancel 信号
    assert env.runner.journal.run_state(env.runner.workflow_id(req_id))["state"] == "cancelled"
    assert acts
