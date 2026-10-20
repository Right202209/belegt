# Spec: 04 §4.3（RequestWorkflow：signals human_answered/supplier_responded/resume，query status；RetryPolicy；halted→resume）、
#       §3.4（幂等键）、§3.2（T07/T08、T18/T19、T20/T21）
"""Temporal 工作流测试（离线）。

使用 ``WorkflowEnvironment.start_time_skipping()``。测试服务器二进制只在本地已有时使用：``BELEGT_TEMPORAL_TEST_SERVER``
指定路径，或 temporalio 的缓存（``$TMPDIR/temporal-test-server-sdk-python-<ver>``）已存在；否则 ``pytest.skip``（不在测试中
下载，CONTRACTS：测试不得访问网络）。设 ``BELEGT_TEMPORAL_DOWNLOAD=1`` 可允许 temporalio 自行下载。
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

temporalio = pytest.importorskip("temporalio")

from temporalio import activity  # noqa: E402
from temporalio.exceptions import ApplicationError  # noqa: E402

from belegt_core.workflow.temporal import (  # noqa: E402
    ACTIVITY_NAMES, READ_ACTIVITY_NAMES, TemporalEngine, WorkflowOptions, workflow_id,
)
from belegt_core.workflow.temporal_worker import build_activities, create_worker  # noqa: E402


def _server_path() -> str | None:
    p = os.environ.get("BELEGT_TEMPORAL_TEST_SERVER")
    if p and Path(p).exists():
        return p
    cached = Path(tempfile.gettempdir()) / f"temporal-test-server-sdk-python-{importlib.metadata.version('temporalio')}"
    if cached.exists():
        return str(cached)
    return None


async def _env():
    from temporalio.testing import WorkflowEnvironment

    path = _server_path()
    if path is None and os.environ.get("BELEGT_TEMPORAL_DOWNLOAD") != "1":
        pytest.skip("Temporal 测试服务器二进制不在本地（离线无法获取）；设 BELEGT_TEMPORAL_TEST_SERVER 或 BELEGT_TEMPORAL_DOWNLOAD=1")
    try:
        return await WorkflowEnvironment.start_time_skipping(test_server_existing_path=path)
    except Exception as e:  # pragma: no cover - 二进制不可执行等
        pytest.skip(f"Temporal 测试服务器无法启动：{e}")


# ---------------------------------------------------------------------------- 伪 Activity（机制测试）
class FakeWorld:
    def __init__(self) -> None:
        self.status, self.reason = "received", None
        self.open: set[str] = set()
        self.idems: list[tuple[str, str]] = []
        self.statuses: list[str] = []
        self.replies: list[dict[str, Any]] = []
        self.audit_calls = 0
        self.chase_calls = 0
        self.escalated: list[str] = []
        self.clock: list[str] = []
        self.halt_on_audit = True

    def acts(self) -> list[Any]:
        w = self

        def mk(name: str, with_idem: bool):
            def fn(inp: dict[str, Any]) -> Any:
                args = inp.get("args") or []
                if with_idem:
                    w.idems.append((name, inp["idem"]))
                return getattr(w, name)(*args)

            fn.__name__ = f"fake_{name}"
            return activity.defn(name=name)(fn)

        return [mk(n, True) for n in ACTIVITY_NAMES] + [mk(n, False) for n in READ_ACTIVITY_NAMES]

    # 状态
    def set_request_status(self, ref, status, reason=None):
        self.status, self.reason = status, reason
        self.statuses.append(status)
        return {"status": status}

    def enter_awaiting(self, ref, reason, return_to):
        return self.set_request_status(ref, "awaiting_human", f"awaiting_human:{reason};return_to={return_to}")

    def leave_awaiting(self, ref):
        rt = (self.reason or "").rsplit("return_to=", 1)[-1]
        return self.set_request_status(ref, rt or "parsed")

    def request_status(self, ref):
        return {"status": self.status, "status_reason": self.reason}

    def check_pending(self, refs):
        return {"pending": [r for r in refs if r in self.open]}

    def approvals_outcome(self, refs):
        p = [r for r in refs if r in self.open]
        return {"outcome": None if p else "approved", "pending": p}

    def halt(self, ref, reason, return_to, detail=""):
        self.set_request_status(ref, "halted", f"halted:{reason};return_to={return_to}")
        return {"reason": reason, "return_to": return_to}

    def advance_clock(self, until):
        self.clock.append(until)
        return {}

    def escalate(self, refs):
        self.escalated += refs
        return {"escalated": len(refs)}

    # 主路径
    def promote_request(self, ref):
        self.open.add("cq_triage#v1")
        return {"promoted": False, "cq_refs": ["cq_triage#v1"], "flags": []}

    def promote_request_confirmed(self, ref, cqs):
        self.set_request_status(ref, "triaged")
        return {"promoted": True, "cancelled": False}

    def build_hints(self, ref):
        return {"hints_ref": None, "questions": 2}

    def parse_questions(self, ref, hints):
        return {"questions": 2}

    def promote_questions(self, ref):
        return {"promoted": 2, "failed": [], "cq_refs": []}

    def question_batches(self, ref):
        return {"batches": [["q_1"], ["q_2"]]}

    def promote_mappings_batch(self, ref, qids):
        return {"confirmed": len(qids), "cq_refs": [], "unmapped_refs": []}

    def finalize_mappings(self, ref):
        return {}

    def prefetch_evidence(self, ref):
        return {}

    def gather_evidence(self, ref, supplier_done=False):
        gaps = [] if supplier_done else [{"datapoint_id": "dp:prod.cbam.see_direct@1.0.0", "kind": "supplier"}]
        return {"cq_refs": [], "supplier_gaps": gaps, "internal_gaps": [], "gaps_confirmed": False, "counts": {"targets": 2}}

    def confirm_gaps(self, ref, gaps):
        return {"cq_refs": []}

    def chase_plan(self, ref, gaps):
        self.open.add("apr_first#v1")
        return {"sreq_refs": ["sreq_1#v1"], "approval_refs": ["apr_first#v1"], "due_date": "2099-12-31"}

    def chase_send(self, ref, sreqs):
        self.chase_calls += 1
        if self.chase_calls == 1:  # EGRESS_DENIED_QUIET_PERIOD → retry_after
            return {"sent": [], "deferred": sreqs, "retry_after": "2000-01-01T07:00:00Z", "denied": []}
        return {"sent": ["sreq_1#v2"], "deferred": [], "retry_after": None, "denied": []}

    def review_replies(self, ref, replies):
        self.replies += replies
        return {"evidence": [], "cq_refs": []}

    def expire_supplier_requests(self, refs):
        return {"expired": refs}

    def draft_answers_batch(self, ref, qids):
        return {"answers": len(qids), "pending_manual": 0}

    def audit(self, ref):
        self.audit_calls += 1
        if self.halt_on_audit and self.audit_calls == 1:
            raise ApplicationError("budget exceeded", {"reason": "budget"}, type="BudgetExceeded", non_retryable=True)
        return {"gate": "pass", "blocking": [], "conflicts": 0}

    def route_approvals(self, ref):
        self.open.add("apr_ans#v1")
        return {"approval_refs": ["apr_ans#v1"], "levels": {"L1": 1}, "blocked": 0}

    def fill_file(self, ref):
        self.open.add("cq_submit#v1")
        return {"export_blob": "sha256:" + "a" * 64, "export_input_hash": "sha256:" + "b" * 64, "cq_refs": ["cq_submit#v1"]}

    def mark_submitted(self, ref, h):
        self.set_request_status(ref, "submitted")
        return {"submitted": 2}

    def archive_learn(self, ref):
        return {"learned": 2}


def test_activity_registry_covers_logic():
    """``logic.request_workflow`` 用到的每个 Activity 名都在注册表里，且 worker 能为真实 Activities 全部包装。"""
    import re

    from belegt_core.workflow import activities as A, logic

    src = Path(logic.__file__).read_text("utf-8")
    used = set(re.findall(r'act\("([a-z_]+)"', src))
    assert used <= set(ACTIVITY_NAMES), used - set(ACTIVITY_NAMES)
    for n in ACTIVITY_NAMES + READ_ACTIVITY_NAMES:
        assert n == "advance_clock" or hasattr(A.Activities, n), n

    class _Svc:  # build_activities 只在调用时访问 svc
        store = None

    fns = build_activities(_Svc())
    names = {getattr(f, "__temporal_activity_definition").name for f in fns}
    assert names == set(ACTIVITY_NAMES + READ_ACTIVITY_NAMES)


def test_exception_mapping():
    from belegt_core.errors import GraphError
    from belegt_core.guard.budget import BudgetExceeded
    from belegt_core.workflow.temporal_worker import _map_exception

    e = _map_exception(GraphError("GRAPH_TRUST_ZONE_VIOLATION", "x"))
    assert e.type == "GRAPH_TRUST_ZONE_VIOLATION" and e.non_retryable
    e = _map_exception(GraphError("GRAPH_VERSION_CONFLICT", "x"))
    assert e.type == "GRAPH_VERSION_CONFLICT" and not e.non_retryable
    e = _map_exception(BudgetExceeded("budget_cost", "request", "5.1", "5"))
    assert e.type == "BudgetExceeded" and e.non_retryable and e.details[0] == {"reason": "budget_cost"}
    assert _map_exception(KeyError("k")).non_retryable
    assert not _map_exception(ConnectionError("down")).non_retryable


def test_workflow_signals_halt_resume_query():
    async def main() -> None:
        env = await _env()
        try:
            world = FakeWorld()
            tq = f"tq-{uuid.uuid4().hex[:8]}"
            worker = create_worker(env.client, None, task_queue=tq, activities=world.acts(), max_concurrent_activities=4)
            eng = TemporalEngine(env.client, task_queue=tq, options=WorkflowOptions(human_timeout_s=None, max_timer_s=0))
            async with worker:
                wid = await eng.start("req_01TEST#v1")
                assert wid == workflow_id("req_01TEST") == "req-req_01TEST"
                assert await eng.start("req_01TEST#v2") == wid  # 已在运行：幂等
                st = await eng.wait_for("req_01TEST")
                # T18：intake 未达 PROMO-REQ-0.90 → 等人确认
                assert st["state"] == "awaiting_human" and st["pending"] == ["cq_triage#v1"] and st["return_to"] == "received"
                assert world.status == "awaiting_human"
                # 信号不带载荷也只是唤醒：未解决前仍等待
                await eng.signal("req_01TEST", "human_answered", {"refs": ["cq_triage#v1"]})
                st2 = await eng.wait_for("req_01TEST", after_revision=st["revision"])
                assert st2["state"] == "awaiting_human" and st2["pending"] == ["cq_triage#v1"]
                world.open.discard("cq_triage#v1")
                await eng.signal("req_01TEST", "human_answered", {})
                # 首次外发人确认（apr_first）
                st = await eng.wait_for("req_01TEST", after_revision=st2["revision"])
                assert st["state"] == "awaiting_human" and st["pending"] == ["apr_first#v1"]
                world.open.discard("apr_first#v1")
                await eng.signal("req_01TEST", "approval_decided", {})  # 04 §4.3 别名
                # chase_send 第一次被静默期推迟 → timer（max_timer_s=0）→ 推进外发时钟 → 重提 → T07 awaiting_supplier
                st = await eng.wait_for("req_01TEST", after_revision=st["revision"])
                assert st["state"] == "awaiting_supplier" and st["pending"] == ["sreq_1"]
                assert world.clock == ["2000-01-01T07:00:00Z"] and world.status == "awaiting_supplier"
                reply = {"sreq_id": "sreq_1", "blob_sha256": "sha256:" + "c" * 64, "kind": "submission"}
                await eng.signal("req_01TEST", "supplier_responded", reply)
                await eng.signal("req_01TEST", "supplier_responded", reply)  # 重复信号去重
                # 审计时 guard 熔断 → T20 halted（return_to=auditing）
                st = await eng.wait_for("req_01TEST", after_revision=st["revision"])
                assert st["state"] == "halted" and st["return_to"] == "auditing" and st["halt_reason"] == "budget"
                assert world.status == "halted" and world.replies == [reply]
                world.status = "auditing"  # T21：人经 API 写回 return_to（LocalRunner.resume 同）
                await eng.signal("req_01TEST", "resume", {"by": "user:u_owner", "budget_patch": {"eur": "2"}})
                st = await eng.wait_for("req_01TEST", after_revision=st["revision"])
                assert st["state"] == "in_review" and st["pending"] == ["apr_ans#v1"] and world.audit_calls == 2
                world.open.discard("apr_ans#v1")
                await eng.signal("req_01TEST", "human_answered", {})
                st = await eng.wait_for("req_01TEST", after_revision=st["revision"])
                assert st["state"] == "awaiting_human" and st["pending"] == ["cq_submit#v1"]
                world.open.discard("cq_submit#v1")
                await eng.signal("req_01TEST", "human_answered", {})
                result = await eng.handle("req_01TEST").result()
                assert result == {"status": "archived", "export_blob": "sha256:" + "a" * 64}
                final = await eng.status("req_01TEST")
                assert final["state"] == "completed" and final["halts"] == 1 and final["resumes"] == 1
                assert final["supplier_replies"] == 1
            # 主路径状态顺序（04 §3.2）
            main_path = [s for s in world.statuses if s not in ("awaiting_human", "halted")]
            for s in ("parsed", "mapped", "evidence_gathering", "awaiting_supplier", "drafting", "auditing", "in_review",
                      "approved", "filling", "archived"):
                assert s in main_path, s
            # 幂等键 wf:<req_id>:act:<activity>:<seq>，主工作流内唯一；halt 后重试同一 Activity 用同一键
            keys = [k for _, k in world.idems]
            assert all(k.startswith("wf:req_01TEST:act:") for k in keys)
            audits = [k for n, k in world.idems if n == "audit"]
            assert len(audits) == 2 and audits[0] == audits[1]
            others = [k for n, k in world.idems if n != "audit"]
            assert len(others) == len(set(others))
            # 映射/起草按 25 题批次扇出到 QuestionBatchWorkflow（2 批 × 2 轮）
            assert len([n for n, _ in world.idems if n in ("promote_mappings_batch", "draft_answers_batch")]) == 4
        finally:
            await env.shutdown()

    asyncio.run(main())


def test_workflow_escalation_and_cancel():
    async def main() -> None:
        env = await _env()
        try:
            world = FakeWorld()
            tq = f"tq-{uuid.uuid4().hex[:8]}"
            worker = create_worker(env.client, None, task_queue=tq, activities=world.acts(), max_concurrent_activities=2)
            eng = TemporalEngine(env.client, task_queue=tq, options=WorkflowOptions(human_timeout_s=3600, max_escalations=2))
            async with worker:
                await eng.start("req_01ESC#v1")
                st = await eng.wait_for("req_01ESC")
                assert st["state"] == "awaiting_human"
                await env.sleep(3 * 3600)  # 时间跳跃：人工超时 → escalate（不改对象，永不自动批准）
                for _ in range(100):
                    if len(world.escalated) >= 2:
                        break
                    await asyncio.sleep(0.05)
                assert world.escalated[:2] == ["cq_triage#v1", "cq_triage#v1"]
                st = await eng.status("req_01ESC")
                assert st["state"] == "awaiting_human" and st["escalations"] == 2
                await eng.signal("req_01ESC", "cancel", {"by": "user:u_owner"})
                assert await eng.handle("req_01ESC").result() == {"status": "cancelled"}
                assert (await eng.status("req_01ESC"))["state"] == "cancelled"
        finally:
            await env.shutdown()

    asyncio.run(main())


def test_real_activities_demo_until_conflict_gate():
    """真实 Activities + 演示租户（短问卷）：Temporal 推进结果与 LocalRunner 一致（停在审计冲突的人工节点）。"""
    from belegt_core.demo import DemoHuman, build_env, find_example, upload

    try:
        csv = find_example("oem_fragebogen_kurz.csv")
    except FileNotFoundError:
        pytest.skip("examples/oem_fragebogen_kurz.csv 不在")

    def local_reference() -> tuple[str, str]:
        from belegt_core.demo import drive

        env = build_env()
        ref = upload(env, csv.read_bytes(), csv.name, mime="text/csv")
        res, _ = drive(env, ref, max_steps=12)
        return res.state, env.store.get(ref.split("#")[0], org_id=env.org_id)["status"]

    async def main() -> tuple[str, str]:
        env_t = await _env()
        try:
            denv = build_env()
            ref = upload(denv, csv.read_bytes(), csv.name, mime="text/csv")
            req_id = ref.split("#")[0]
            human = DemoHuman(denv.store, denv.user("owner"), executive=denv.user("exec"))
            tq = f"tq-{uuid.uuid4().hex[:8]}"
            worker = create_worker(env_t.client, denv.svc, task_queue=tq)
            eng = TemporalEngine(env_t.client, task_queue=tq, options=WorkflowOptions(human_timeout_s=None, max_timer_s=0))
            seen_states = []
            async with worker:
                await eng.start(ref)
                rev = -1
                st: dict[str, Any] = {}
                for _ in range(12):
                    st = await eng.wait_for(req_id, after_revision=rev, timeout_s=120)
                    rev = st["revision"]
                    seen_states.append(st["state"])
                    if st["state"] in ("in_review", "completed", "failed", "cancelled", "halted"):
                        break
                    if st["state"] == "awaiting_human":
                        n = human.resolve(st["pending"])
                        if n == 0:  # 只剩冲突（DemoHuman 不处置冲突）→ 与 LocalRunner 演示同样停住
                            break
                        await eng.signal(req_id, "human_answered", {"refs": st["pending"]})
                    elif st["state"] == "awaiting_supplier":
                        for sid in st["pending"]:
                            payload = {"format": "belegt-supplier-submission/1", "values": denv.tenant["supplier_submission"],
                                       "sreq_id": sid}
                            blob = denv.store.put_blob(denv.org_id, json.dumps(payload).encode(), "application/json")
                            await eng.signal(req_id, "supplier_responded", {"sreq_id": sid, "blob_sha256": blob, "kind": "submission"})
                assert st["state"] != "failed", st.get("error")
                temporal_status = denv.store.get(req_id, org_id=denv.org_id)["status"]
                assert "awaiting_supplier" in seen_states or "awaiting_human" in seen_states
                await eng.signal(req_id, "cancel", {})
        finally:
            await env_t.shutdown()
        return st["state"], temporal_status

    got = asyncio.run(main())
    assert got == local_reference()
