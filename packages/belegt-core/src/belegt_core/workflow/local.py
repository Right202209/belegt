# Spec: 04 §3（状态机执行器）、§3.4（幂等键、信号去重）、§3.5（断点续跑）、§4.3（RetryPolicy 2s/×2/5 次；非重试错误）、§5.2（halted →
#       resume 携带 budget_patch）、§5.3
"""``LocalRunner``：单进程持久化执行器（开发/测试/demo；生产用 Temporal，见 ``workflow.temporal``）。

确定性重放：工作流函数（``logic.request_workflow``）每次从头执行，``act`` 按序号查日志——已完成的 Activity 直接返回日志中的
结果（ObjectRef），未完成的才执行；人工节点未解决时抛 ``Suspend`` 挂起（进程可退出，之后由信号/``run`` 继续）。
同步驱动协程（工作流内没有真正的 await 点），因此在有/无事件循环的环境都可调用；``run_async`` 供 asyncio 调用方使用。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..authz import Actor
from ..errors import GraphError
from ..guard.budget import BudgetExceeded, BudgetState
from ..guard.loops import LoopDetected
from ..guard.policy import Halted
from ..statemachines import parse_status_reason
from .activities import NON_RETRYABLE, Activities
from .journal import Journal
from .logic import question_batch, request_workflow


class Suspend(Exception):
    def __init__(self, state: str, pending: list[str] | None = None, until: str | None = None) -> None:
        super().__init__(state)
        self.state, self.pending, self.until = state, list(pending or []), until


class Cancelled(Exception):
    pass


class WorkflowFailed(Exception):
    pass


class CrashSimulated(BaseException):
    """测试用：模拟进程在 Activity 中途崩溃（BaseException：不被重试逻辑捕获）。"""


@dataclass(frozen=True)
class RetryPolicy:
    initial_interval_s: float = 2.0
    backoff: float = 2.0
    maximum_attempts: int = 5
    non_retryable: tuple[str, ...] = NON_RETRYABLE


@dataclass
class RunResult:
    workflow_id: str
    request_id: str
    state: str                       # completed | awaiting_human | in_review | awaiting_supplier | halted | timer | cancelled | failed
    pending: list[str] = field(default_factory=list)
    until: str | None = None
    result: dict[str, Any] | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("workflow_id", "request_id", "state", "pending", "until", "result", "progress", "error")}


def _drive(coro: Any) -> Any:
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    coro.close()
    raise RuntimeError("workflow coroutine awaited a real awaitable; use LocalRunner.run_async")


class _LocalWF:
    def __init__(self, runner: "LocalRunner", req_ref: str) -> None:
        self.r, self.req_ref = runner, req_ref
        self.req_id = req_ref.split("#")[0]
        self.wf_id = runner.workflow_id(self.req_id)
        self.seq = 0
        self.halts = 0
        self.state: dict[str, Any] = {}

    # ---------------------------------------------------------------- helpers
    @property
    def j(self) -> Journal:
        return self.r.journal

    @property
    def acts(self) -> Activities:
        return self.r.acts

    def _resumes(self) -> int:
        return len(self.j.signals(self.wf_id, "resume"))

    def progress(self, **kw: Any) -> None:
        self.state.update(kw)

    def _current_status(self) -> str:
        return self.acts.request_status(self.req_ref)["status"]

    # ---------------------------------------------------------------- Activity
    async def act(self, name: str, *args: Any) -> Any:
        self.seq += 1
        key = str(self.seq)
        e = self.j.get(self.wf_id, key)
        if e is not None:
            return e["result"]
        # 本序号曾 halt：未 resume 前保持挂起；resume 后重试同一 Activity（04 §3.5）
        while self.j.get(self.wf_id, f"{key}:halt:{self.halts + 1}") is not None:
            self.halts += 1
            if self._resumes() < self.halts:
                raise Suspend("halted")
        if self._current_status() == "cancelled":
            raise Cancelled(self.req_id)
        idem = f"wf:{self.req_id}:act:{name}:{self.seq}"
        pol = self.r.retry
        attempt = 0
        while True:
            attempt += 1
            try:
                if self.r.fault:
                    self.r.fault(name, self.seq, "before")
                result = getattr(self.acts, name)(idem, *args)
                if self.r.fault:
                    self.r.fault(name, self.seq, "after")
                break
            except (BudgetExceeded, LoopDetected, Halted) as exc:
                reason = getattr(exc, "reason", "policy")
                cur = self._current_status()
                ret = cur if cur not in ("awaiting_human", "halted") else (
                    (parse_status_reason(self.acts.request_status(self.req_ref)["status_reason"]) or {}).get("return_to") or "parsed")
                self.halts += 1
                self.acts.halt(idem + ":halt", self.req_ref, reason, ret, str(exc))
                self.j.put(self.wf_id, f"{key}:halt:{self.halts}", "halt", name,
                           {"reason": reason, "return_to": ret, "error": type(exc).__name__, "detail": str(exc)[:300]})
                self.r._save(self, "halted", [])
                raise Suspend("halted") from exc
            except GraphError as exc:
                if exc.code in pol.non_retryable or attempt >= pol.maximum_attempts:
                    raise WorkflowFailed(f"{name}: {exc}") from exc
                self.r.sleep(pol.initial_interval_s * pol.backoff ** (attempt - 1))
            except (KeyError, TypeError, AttributeError, ValueError):
                raise
            except Exception as exc:  # 瞬时错误：退避重试
                if type(exc).__name__ in pol.non_retryable or attempt >= pol.maximum_attempts:
                    raise WorkflowFailed(f"{name}: {exc}") from exc
                self.r.sleep(pol.initial_interval_s * pol.backoff ** (attempt - 1))
        self.j.put(self.wf_id, key, "activity", name, result)
        self.r._save_budget(self)
        return result

    async def set_status(self, status: str, reason: str | None = None) -> None:
        await self.act("set_request_status", self.req_ref, status, reason)

    async def fan_out(self, kind: str, req_ref: str, batches: list[list[str]]) -> list[dict[str, Any]]:
        return [await question_batch(self, kind, req_ref, b) for b in batches]  # LocalRunner：顺序执行（确定性）

    def _marker(self) -> tuple[str, dict[str, Any] | None]:
        self.seq += 1
        key = f"{self.seq}"
        return key, self.j.get(self.wf_id, key)

    async def wait_human(self, return_to: str, refs: list[str], reason: str) -> None:
        if not refs:
            return
        await self.act("enter_awaiting", self.req_ref, reason, return_to)  # T18
        key, done = self._marker()
        if done is None:
            pending = self.acts.check_pending(refs)["pending"]
            if pending:
                self.state.update(waiting=reason, pending=len(pending))
                raise Suspend("awaiting_human", pending)
            self.j.put(self.wf_id, key, "wait", "human", {"resolved": refs})
        await self.act("leave_awaiting", self.req_ref)  # T19

    async def wait_approvals(self, refs: list[str]) -> str:
        key, done = self._marker()
        if done is not None:
            return done["result"]["outcome"]
        o = self.acts.approvals_outcome(refs)
        if o["outcome"] is None:
            raise Suspend("in_review", o["pending"])
        self.j.put(self.wf_id, key, "wait", "approvals", o)
        return o["outcome"]

    async def wait_supplier(self, sreq_refs: list[str], due: str | None) -> list[dict[str, Any]]:
        key, done = self._marker()
        if done is not None:
            return done["result"]["replies"]
        ids = {r.split("#")[0] for r in sreq_refs}
        replies = [s["result"] for s in self.j.signals(self.wf_id, "supplier_replied") if s["result"].get("sreq_id") in ids]
        got = {r["sreq_id"] for r in replies}
        if got >= ids or (due and self.r.today() > due):
            if not got >= ids:
                self.acts.expire_supplier_requests(f"wf:{self.req_id}:act:expire:{self.seq}", [r for r in sreq_refs if r.split("#")[0] not in got])
            self.j.put(self.wf_id, key, "wait", "supplier", {"replies": replies})
            return replies
        raise Suspend("awaiting_supplier", sorted(ids - got))

    async def sleep_until(self, until: str | None) -> None:
        key, done = self._marker()
        if done is not None or until is None:
            return
        if self.r.auto_advance_timers or self.r.now_iso() >= until:
            self.acts.now_override = dt.datetime.fromisoformat(until.replace("Z", "+00:00"))
            self.j.put(self.wf_id, key, "timer", "sleep", {"until": until})
            return
        raise Suspend("timer", until=until)

    async def halt(self, reason: str, return_to: str, detail: str = "") -> None:
        await self.act("halt", self.req_ref, reason, return_to, detail)
        key, done = self._marker()
        if done is not None:
            return
        self.halts += 1
        if self._resumes() < self.halts:
            raise Suspend("halted")
        self.j.put(self.wf_id, key, "wait", "resume", {"resumed": True})

    async def child(self, kind: str, *args: Any) -> Any:  # 兼容接口
        return await question_batch(self, kind, *args)


class LocalRunner:
    def __init__(self, svc: Any, journal: Journal | None = None, *, retry: RetryPolicy | None = None,
                 sleep: Callable[[float], None] = time.sleep, fault: Callable[[str, int, str], None] | None = None,
                 auto_advance_timers: bool = True) -> None:
        self.svc = svc
        self.acts = Activities(svc)
        self.acts.now_override = None
        self.journal = journal or Journal(svc.store.engine)
        self.retry = retry or RetryPolicy()
        self.sleep = sleep
        self.fault = fault
        self.auto_advance_timers = auto_advance_timers
        self.last_progress: dict[str, dict[str, Any]] = {}

    @staticmethod
    def workflow_id(req_id: str) -> str:
        return f"req-{req_id}"

    def now_iso(self) -> str:
        return self.svc.store.peek_now()[:19] + "Z"

    def today(self) -> str:
        return self.svc.store.peek_now()[:10]

    # ---------------------------------------------------------------- persistence
    def _save_budget(self, wf: _LocalWF) -> None:
        b = self.svc.guard.budgets.get(wf.req_id)
        if b is not None:
            st = self.journal.run_state(wf.wf_id)
            self.journal.save_run(wf.wf_id, request_id=wf.req_id, org_id=self.svc.org_id, state=(st or {}).get("state") or "running",
                                  pending=(st or {}).get("pending"), budget=b.to_dict(), summary=wf.state)

    def _save(self, wf: _LocalWF, state: str, pending: list[str], note: str | None = None) -> None:
        b = self.svc.guard.budgets.get(wf.req_id)
        self.journal.save_run(wf.wf_id, request_id=wf.req_id, org_id=self.svc.org_id, state=state, pending=pending,
                              budget=b.to_dict() if b else None, summary=wf.state, note=note)

    def _restore_budget(self, req_id: str) -> None:
        st = self.journal.run_state(self.workflow_id(req_id))
        if st and st.get("budget") and req_id not in self.svc.guard.budgets:
            self.svc.guard.budgets[req_id] = BudgetState.from_dict(st["budget"])
        if st and st.get("state") == "halted" and not self._resumed(req_id):
            # 进程重启后 guard 内存状态丢失：仍处 halted（尚未被 resume 覆盖）的运行要重新冻结；已 resume 的不能再冻结，
            # 否则 resume → run 会立刻被 guard.enter 拦下，永远恢复不了
            self.svc.guard.halted_requests.add(req_id)

    def _resumed(self, req_id: str) -> bool:
        wf_id = self.workflow_id(req_id)
        ents = self.journal.entries(wf_id)
        halts = sum(1 for e in ents if e["kind"] == "halt" or (e["kind"] == "activity" and e["name"] == "halt"))
        resumes = sum(1 for e in ents if e["kind"] == "signal" and e["name"] == "resume")
        return halts > 0 and resumes >= halts

    # ---------------------------------------------------------------- driving
    def run(self, req_ref: str) -> RunResult:
        req_id = req_ref.split("#")[0]
        self._restore_budget(req_id)
        wf = _LocalWF(self, req_ref)
        # T22/T23：取消信号或 Request 已 cancelled → 不再重放（否则会在人工节点处重新挂起为 awaiting_human）
        if self.journal.signals(wf.wf_id, "cancel") or self.acts.request_status(req_ref)["status"] == "cancelled":
            self._save(wf, "cancelled", [])
            out = RunResult(wf.wf_id, req_id, "cancelled")
            out.progress = {**self.acts.progress(req_id)}
            self.last_progress[req_id] = out.progress
            return out
        try:
            result = _drive(request_workflow(wf, req_id))
            self._save(wf, "completed", [])
            out = RunResult(wf.wf_id, req_id, "completed", result=result)
        except Suspend as s:
            self._save(wf, s.state, s.pending)
            out = RunResult(wf.wf_id, req_id, s.state, pending=s.pending, until=s.until)
        except Cancelled:
            self._save(wf, "cancelled", [])
            out = RunResult(wf.wf_id, req_id, "cancelled")
        except WorkflowFailed as e:
            self._save(wf, "failed", [], note=str(e)[:2000])
            out = RunResult(wf.wf_id, req_id, "failed", error=str(e))
        out.progress = {**self.acts.progress(req_id), **wf.state}
        self.last_progress[req_id] = out.progress
        return out

    start = run

    async def run_async(self, req_ref: str) -> RunResult:
        return await asyncio.to_thread(self.run, req_ref)

    def signal(self, req_id: str, name: str, payload: dict[str, Any] | None = None, *, drive: bool = True) -> RunResult | None:
        """信号：cq_answered / approval_decided / conflict_resolved / supplier_replied / resume / cancel（04 §4.3）。"""
        self.journal.add_signal(self.workflow_id(req_id), name, payload or {})
        return self.run(req_id) if drive else None

    def resume(self, req_id: str, actor: Actor, budget_patch: dict[str, Any] | None = None) -> RunResult:
        """T21：人恢复 halted Request（``user[org_admin,compliance_owner]``），可追加预算。"""
        st = self.svc.store
        req = st.get(req_id, org_id=self.svc.org_id)
        if req["status"] == "halted":
            info = parse_status_reason(req.get("status_reason")) or {}
            st.write("Request", {"id": req_id, "status": info.get("return_to", "parsed")}, expected_version=req["version"],
                     actor=actor, patch_mode="merge_from_head")
        self.svc.guard.release(req_id, budget_patch)
        return self.signal(req_id, "resume", {"by": actor.id, "budget_patch": budget_patch or {}})

    def cancel(self, req_id: str, actor: Actor) -> RunResult:
        st = self.svc.store
        req = st.get(req_id, org_id=self.svc.org_id)
        st.write("Request", {"id": req_id, "status": "cancelled"}, expected_version=req["version"], actor=actor,
                 patch_mode="merge_from_head")
        return self.signal(req_id, "cancel", {"by": actor.id})

    def progress(self, req_id: str) -> dict[str, Any]:
        st = self.journal.run_state(self.workflow_id(req_id)) or {}
        return {**self.acts.progress(req_id), "workflow_state": st.get("state"), "pending": st.get("pending") or [],
                "summary": st.get("summary") or {}, "budget": (st.get("budget") or {}).get("total")}
