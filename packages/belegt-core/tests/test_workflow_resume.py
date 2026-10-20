# Spec: 04 §3.5（断点续跑：已完成的 Activity 不重跑；进行中的按幂等键重跑；结果是 ObjectRef）、§3.4（幂等键 wf:<req>:act:<name>:<seq>）
"""进程在 Activity 中途崩溃（``CrashSimulated``）→ 新的 LocalRunner 从 ``wf_journal`` 恢复 → 与不中断的运行结果一致、无重复写。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from agentkit import drive_to, new_env, request_answers, upload_file
from belegt_core.workflow.journal import Journal
from belegt_core.workflow.local import CrashSimulated, LocalRunner

CRASHES = [("gather_evidence", "after"), ("draft_answers_batch", "before"), ("route_approvals", "after"), ("fill_file", "after")]


def _fingerprint(env, ref):
    req_id = ref.split("#")[0]
    st, org = env.store, env.org_id
    ans = sorted((a["datapoint_id"], str(a.get("value")), a["status"], a.get("risk_level")) for a in request_answers(env, req_id))
    calcs = sorted((c["method_id"], c["input_hash"], c["result"]["value"]) for c in st.query("Calculation", org_id=org))
    fill = next(e for e in env.runner.journal.entries(env.runner.workflow_id(req_id)) if e["name"] == "fill_file")["result"]
    counts = {t: len(st.query(t, org_id=org)) for t in ("Question", "Mapping", "Evidence", "Answer", "Calculation", "SupplierRequest",
                                                         "ClosedQuestion")}
    return {"answers": ans, "calcs": calcs, "export_input_hash": fill["export_input_hash"], "written": fill["written"],
            "counts": counts, "emails": len(env.transport.sent), "status": st.get(req_id)["status"]}


def _restart(env, fault=None):
    """模拟进程重启：新的 Journal 句柄 + 新 LocalRunner，guard 的内存状态清空（预算从 wf_run 快照恢复）。"""
    env.svc.guard.budgets.clear()
    env.svc.guard.halted_requests.clear()
    env.runner = LocalRunner(env.svc, Journal(env.store.engine), sleep=lambda s: None, fault=fault)
    return env.runner


def test_crash_and_resume_matches_uninterrupted(csv_run, tmp_path):
    ref_env, ref_ref, *_ = csv_run
    expected = _fingerprint(ref_env, ref_ref)

    env = new_env(url=f"sqlite+pysqlite:///{tmp_path / 'wf.db'}")
    ref = upload_file(env)
    assert ref == ref_ref  # 确定性 ID（同一原件、同一幂等前缀）
    todo = list(CRASHES)
    crashed = []

    def fault(name, seq, phase):
        if todo and (name, phase) == todo[0]:
            todo.pop(0)
            crashed.append((name, seq, phase))
            raise CrashSimulated(f"{name}#{seq}:{phase}")

    _restart(env, fault)
    for _ in range(len(CRASHES) + 1):
        try:
            res, _ = drive_to(env, ref)
            break
        except CrashSimulated:
            _restart(env, fault)
    assert [c[0] for c in crashed] == [c[0] for c in CRASHES]
    assert res.state == "completed", (res.state, res.error)
    got = _fingerprint(env, ref)
    assert got == expected
    s1 = next(c for c in env.store.query("Calculation", org_id=env.org_id) if c["method_id"] == "ghg.scope1.total")
    assert Decimal(s1["result"]["value"]) == Decimal("481.92")


def test_completed_activities_are_not_rerun(tmp_path):
    env = new_env(url=f"sqlite+pysqlite:///{tmp_path / 'wf2.db'}")
    ref = upload_file(env)
    res = env.runner.run(ref)
    assert res.state == "awaiting_human"
    req_id = ref.split("#")[0]
    done = [e["name"] for e in env.runner.journal.entries(env.runner.workflow_id(req_id)) if e["kind"] == "activity"]
    calls = []

    def spy(name, seq, phase):
        if phase == "before":
            calls.append(name)
    _restart(env, spy)
    res2 = env.runner.run(ref)
    assert res2.state == "awaiting_human" and sorted(res2.pending) == sorted(res.pending)
    assert calls == []  # 重放全部命中日志：一个 Activity 都没有重新执行
    assert done


def test_crash_inside_activity_is_idempotent(tmp_path):
    """Activity 已写图、日志未落（after 崩溃）→ 重跑用同一幂等键，图中对象不重复。"""
    env = new_env(url=f"sqlite+pysqlite:///{tmp_path / 'wf3.db'}")
    ref = upload_file(env)

    def crash_after_parse(name, seq, phase):
        if name == "parse_questions" and phase == "after":
            raise CrashSimulated("after parse")
    env.runner.fault = crash_after_parse
    with pytest.raises(CrashSimulated):
        env.runner.run(ref)
    n_q = len(env.store.query("Question", org_id=env.org_id))
    n_m = len(env.store.query("Mapping", org_id=env.org_id))
    _restart(env)
    res = env.runner.run(ref)
    assert res.state == "awaiting_human"
    assert len(env.store.query("Question", org_id=env.org_id)) == n_q == 15
    assert len(env.store.query("Mapping", org_id=env.org_id)) == n_m
