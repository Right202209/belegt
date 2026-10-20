# Spec: 04 §4.3（Temporal workflow / activity 划分：RequestWorkflow、QuestionBatchWorkflow；RetryPolicy 2s/×2/5 次与非重试错误；
#       signal 由 graph.outbox 转发；query 只含计数与 ObjectRef）、§3（问卷级状态机 T01…T23）、§3.4（幂等键 wf:<req>:act:<name>:<seq>）、
#       §3.5（断点续跑：Temporal 事件历史即日志）、§5.2（halted → resume）
"""Temporal 编排：问卷级持久工作流 ``RequestWorkflow``（与 ``LocalRunner`` 共用 ``logic.request_workflow``）。

本模块只依赖 ``temporalio`` 与标准库（工作流沙箱内导入）；Activity 实现在 ``temporal_worker``（复用 ``workflow.activities``）。

- Workflow：``RequestWorkflow``（一份问卷一个工作流，ID ``req-<req_id>``）、``QuestionBatchWorkflow``（每 25 题一个子工作流，
  映射/起草扇出，默认并行）。
- Signal：``human_answered``（人工项有变化 → 工作流重新核对图中状态；图是唯一事实来源，信号只是唤醒）、
  ``supplier_responded``（``{sreq_id, blob_sha256, kind}``）、``resume``（``{by, budget_patch}``，T21）、``cancel``。
  兼容 04 §4.3 名称：``cq_answered``/``approval_decided``/``conflict_resolved`` 等同 ``human_answered``，``supplier_replied`` 等同
  ``supplier_responded``。
- Query：``status``（``state``、``pending``、``revision``、``progress``…；``progress`` 为 04 §4.3 名称的别名）。
- Activity 调用统一经 ``act()``：幂等键 ``wf:<req_id>:act:<name>:<seq>``，RetryPolicy 2 s / ×2 / 5 次，``NON_RETRYABLE`` 不重试；
  guard 抛出的 ``BudgetExceeded``/``LoopDetected``/``Halted`` 在 Activity 中转为同名 ``ApplicationError``，工作流写 ``halted``（T20）
  并等待 ``resume`` 信号后重试同一 Activity。
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from .logic import question_batch, request_workflow

TASK_QUEUE = "belegt-requests"
WORKFLOW_NAME = "RequestWorkflow"
BATCH_WORKFLOW_NAME = "QuestionBatchWorkflow"

# 带幂等键的 Activity（第一个参数为 idem）——名称与 ``workflow.activities.Activities`` 方法一致
ACTIVITY_NAMES: tuple[str, ...] = (
    "set_request_status", "enter_awaiting", "leave_awaiting", "promote_request", "promote_request_confirmed", "build_hints",
    "parse_questions", "promote_questions", "question_batches", "promote_mappings_batch", "finalize_mappings",
    "prefetch_evidence", "gather_evidence", "confirm_gaps", "chase_plan", "chase_send", "review_replies",
    "expire_supplier_requests", "draft_answers_batch", "audit", "route_approvals", "escalate", "fill_file", "mark_submitted",
    "archive_learn", "halt",
)
# 只读核对类 Activity（无幂等键）；``advance_clock`` 在 timer 后推进外发时间（同 LocalRunner.sleep_until）
READ_ACTIVITY_NAMES: tuple[str, ...] = ("request_status", "check_pending", "approvals_outcome", "advance_clock")
ALL_ACTIVITY_NAMES = ACTIVITY_NAMES + READ_ACTIVITY_NAMES

NON_RETRYABLE = ("GRAPH_TRUST_ZONE_VIOLATION", "GRAPH_PROMOTION_DENIED", "GRAPH_NUM_VALUE_MISMATCH", "GRAPH_HALTED",
                 "BudgetExceeded", "LoopDetected", "Halted", "KeyError", "TypeError", "AttributeError", "ValueError")
HALT_ERRORS = ("BudgetExceeded", "LoopDetected", "Halted")
DEFAULT_RETRY = RetryPolicy(initial_interval=dt.timedelta(seconds=2), backoff_coefficient=2.0, maximum_attempts=5,
                            non_retryable_error_types=list(NON_RETRYABLE))
WAITING_STATES = ("awaiting_human", "in_review", "awaiting_supplier", "halted", "timer")
FINAL_STATES = ("completed", "cancelled", "failed")
HUMAN_SIGNAL_ALIASES = ("cq_answered", "approval_decided", "conflict_resolved")


def workflow_id(req_id: str) -> str:
    """与 ``LocalRunner.workflow_id`` 相同（04 §3.4：一个 Request 一个工作流）。"""
    return f"req-{req_id.split('#')[0]}"


def _return_to(status_reason: str | None) -> str | None:
    """``"<kind>:<reason>;return_to=<state>"`` → state（标准库实现，沙箱内可用）。"""
    if not status_reason or "return_to=" not in status_reason:
        return None
    return status_reason.rsplit("return_to=", 1)[1].split(";", 1)[0].strip() or None


@dataclass
class WorkflowOptions:
    """``RequestWorkflow`` 输入中的可调项（JSON：``{"req_ref", "options": {...}}``）。"""

    parallel_batches: bool = True
    human_timeout_s: float | None = 72 * 3600     # 人工节点超时 → escalate（升级，不改对象，永不自动批准）
    max_escalations: int = 3
    max_timer_s: float | None = None              # sleep_until 上限（演示时钟与真实时钟不一致时用 0）
    activity_timeout_s: float = 600.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "WorkflowOptions":
        d = d or {}
        return cls(**{k: d[k] for k in ("parallel_batches", "human_timeout_s", "max_escalations", "max_timer_s",
                                           "activity_timeout_s") if k in d})


class _Cancelled(Exception):
    pass


class _HaltSignal(Exception):
    def __init__(self, err_type: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.err_type, self.reason, self.message = err_type, reason, message


def _halt_cause(exc: BaseException) -> ApplicationError | None:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ApplicationError) and cur.type in HALT_ERRORS:
            return cur
        cur = cur.__cause__ or getattr(cur, "cause", None)
    return None


def _app_cause(exc: BaseException) -> ApplicationError | None:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ApplicationError):
            return cur
        cur = cur.__cause__ or getattr(cur, "cause", None)
    return None


class _TemporalWF:
    """``logic.request_workflow`` 所需的 ``wf`` 接口在 Temporal 中的实现（与 ``local._LocalWF`` 对应）。"""

    def __init__(self, owner: Any, req_ref: str, opts: WorkflowOptions, *, prefix: str = "", handle_halt: bool = True) -> None:
        self.o, self.req_ref, self.opts, self.prefix, self.handle_halt = owner, req_ref, opts, prefix, handle_halt
        self.req_id = req_ref.split("#")[0]
        self.seq = 0
        self.fan_rounds = 0

    # ------------------------------------------------------------------ activities
    async def _exec(self, name: str, idem: str | None, args: list[Any]) -> Any:
        return await workflow.execute_activity(
            name, {"idem": idem, "args": args}, start_to_close_timeout=dt.timedelta(seconds=self.opts.activity_timeout_s),
            retry_policy=DEFAULT_RETRY)

    async def read(self, name: str, *args: Any) -> Any:
        return await self._exec(name, None, list(args))

    async def act(self, name: str, *args: Any) -> Any:
        self.seq += 1
        idem = f"wf:{self.req_id}:act:{name}:{self.prefix}{self.seq}"
        while True:
            self.o._check_cancel()
            try:
                return await self._exec(name, idem, list(args))
            except ActivityError as exc:
                hc = _halt_cause(exc)
                if hc is None:
                    raise
                det = hc.details[0] if hc.details and isinstance(hc.details[0], dict) else {}
                if not self.handle_halt:  # 子工作流：上抛给父工作流处理（父工作流持有 resume 信号）
                    raise ApplicationError(hc.message, det, type=hc.type, non_retryable=True) from exc
                await self.o._halt_and_wait(self, idem, det.get("reason") or "policy", hc.message)

    async def set_status(self, status: str, reason: str | None = None) -> None:
        await self.act("set_request_status", self.req_ref, status, reason)

    def progress(self, **kw: Any) -> None:
        self.o._progress.update(kw)

    # ------------------------------------------------------------------ fan-out（QuestionBatchWorkflow）
    async def fan_out(self, kind: str, req_ref: str, batches: list[list[str]]) -> list[dict[str, Any]]:
        self.fan_rounds += 1
        rnd = self.fan_rounds
        base = workflow.info().workflow_id

        async def one(i: int, qids: list[str]) -> dict[str, Any]:
            attempt = 0
            while True:
                attempt += 1
                self.o._check_cancel()
                try:
                    return await workflow.execute_child_workflow(
                        BATCH_WORKFLOW_NAME, {"kind": kind, "req_ref": req_ref, "question_ids": qids,
                                              "prefix": f"{kind}{rnd}.{i}.{attempt}.", "options": self.opts.__dict__},
                        id=f"{base}:{kind}:{rnd}:{i}:{attempt}")
                except ChildWorkflowError as exc:
                    hc = _halt_cause(exc)
                    if hc is None:
                        raise
                    det = hc.details[0] if hc.details and isinstance(hc.details[0], dict) else {}
                    self.seq += 1
                    await self.o._halt_and_wait(self, f"wf:{self.req_id}:act:{kind}_batch:{self.seq}",
                                                det.get("reason") or "policy", hc.message)

        if self.opts.parallel_batches:
            return list(await asyncio.gather(*[one(i, b) for i, b in enumerate(batches)]))
        return [await one(i, b) for i, b in enumerate(batches)]

    async def child(self, kind: str, *args: Any) -> Any:  # 兼容接口
        return await question_batch(self, kind, *args)

    # ------------------------------------------------------------------ 人工 / 外部等待
    async def _wait_wakeup(self, counter: str, state: str, pending: list[str], extra: dict[str, Any] | None = None) -> bool:
        """等到计数器变化（信号）；人工节点超时返回 False（调用方升级）。"""
        self.o._set_wait(state, pending, extra)
        seen = getattr(self.o, counter)
        timeout = None
        if self.opts.human_timeout_s and self.o._escalations < self.opts.max_escalations and state != "awaiting_supplier":
            timeout = dt.timedelta(seconds=self.opts.human_timeout_s)
        try:
            await workflow.wait_condition(lambda: getattr(self.o, counter) > seen or self.o._cancelled, timeout=timeout)
        except asyncio.TimeoutError:
            return False
        self.o._check_cancel()
        return True

    async def wait_human(self, return_to: str, refs: list[str], reason: str) -> None:
        if not refs:
            return
        await self.act("enter_awaiting", self.req_ref, reason, return_to)  # T18
        while True:
            pending = (await self.read("check_pending", refs))["pending"]
            if not pending:
                break
            if not await self._wait_wakeup("_wakeups", "awaiting_human", pending, {"waiting": reason, "return_to": return_to}):
                self.o._escalations += 1
                await self.act("escalate", pending)  # 超时升级（不改对象，06 §4.3）
        self.o._set_running()
        await self.act("leave_awaiting", self.req_ref)  # T19

    async def wait_approvals(self, refs: list[str]) -> str:
        while True:
            o = await self.read("approvals_outcome", refs)
            if o["outcome"] is not None:
                self.o._set_running()
                return o["outcome"]
            if not await self._wait_wakeup("_wakeups", "in_review", o["pending"], {"waiting": "approvals"}):
                self.o._escalations += 1
                await self.act("escalate", o["pending"])

    async def wait_supplier(self, sreq_refs: list[str], due: str | None) -> list[dict[str, Any]]:
        ids = {r.split("#")[0] for r in sreq_refs}
        while True:
            replies = [r for r in self.o._replies if r.get("sreq_id") in ids]
            got = {r["sreq_id"] for r in replies}
            if got >= ids:
                break
            remaining = None
            if due:
                end = dt.datetime.fromisoformat(due[:10]).replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
                remaining = (end - workflow.now()).total_seconds()
            if remaining is not None and remaining <= 0:
                await self.act("expire_supplier_requests", [r for r in sreq_refs if r.split("#")[0] not in got])
                break
            self.o._set_wait("awaiting_supplier", sorted(ids - got), {"due_date": due})
            seen = self.o._supplier_signals
            try:
                await workflow.wait_condition(lambda: self.o._supplier_signals > seen or self.o._cancelled,
                                              timeout=dt.timedelta(seconds=remaining) if remaining is not None else None)
            except asyncio.TimeoutError:
                pass
            self.o._check_cancel()
        self.o._set_running()
        return [r for r in self.o._replies if r.get("sreq_id") in ids]

    async def sleep_until(self, until: str | None) -> None:
        if until is None:
            return
        target = dt.datetime.fromisoformat(until.replace("Z", "+00:00"))
        if target.tzinfo is None:
            target = target.replace(tzinfo=dt.timezone.utc)
        secs = max(0.0, (target - workflow.now()).total_seconds())
        if self.opts.max_timer_s is not None:
            secs = min(secs, self.opts.max_timer_s)
        if secs > 0:
            self.o._set_wait("timer", [], {"until": until})
            await workflow.sleep(secs)
            self.o._set_running()
        await self.read("advance_clock", until)

    async def halt(self, reason: str, return_to: str, detail: str = "") -> None:
        await self.act("halt", self.req_ref, reason, return_to, detail)
        self.o._halts += 1
        self.o._set_wait("halted", [], {"halt_reason": reason, "return_to": return_to})
        await workflow.wait_condition(lambda: self.o._resumes >= self.o._halts or self.o._cancelled)
        self.o._check_cancel()
        self.o._set_running()


class _Base:
    def __init__(self) -> None:
        self._state = "running"
        self._pending: list[str] = []
        self._extra: dict[str, Any] = {}
        self._progress: dict[str, Any] = {}
        self._revision = 0
        self._wakeups = 0
        self._supplier_signals = 0
        self._replies: list[dict[str, Any]] = []
        self._resumes = 0
        self._resume_payloads: list[dict[str, Any]] = []
        self._halts = 0
        self._escalations = 0
        self._cancelled = False
        self._result: dict[str, Any] | None = None
        self._error: str | None = None
        self._req_id = ""

    def _set_wait(self, state: str, pending: list[str], extra: dict[str, Any] | None = None) -> None:
        self._state, self._pending, self._extra = state, list(pending), dict(extra or {})
        self._revision += 1

    def _set_running(self) -> None:
        if self._state != "running":
            self._state, self._pending, self._extra = "running", [], {}
            self._revision += 1

    def _check_cancel(self) -> None:
        if self._cancelled:
            raise _Cancelled()


@workflow.defn(name=BATCH_WORKFLOW_NAME)
class QuestionBatchWorkflow(_Base):
    """每 25 题一个批次（map：PROMO-MAP-0.85 / CQ-MAP-BAND；draft：answer-drafter）。halt 上抛给父工作流。"""

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        wf = _TemporalWF(self, inp["req_ref"], WorkflowOptions.from_dict(inp.get("options")), prefix=inp.get("prefix", ""),
                         handle_halt=False)
        return await question_batch(wf, inp["kind"], inp["req_ref"], inp["question_ids"])


@workflow.defn(name=WORKFLOW_NAME)
class RequestWorkflow(_Base):
    """问卷级持久工作流（04 §3、§4.3）。输入 ``{"req_ref": "req_…#vN", "options": {...}}``。"""

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        req_ref = inp["req_ref"]
        self._req_id = req_ref.split("#")[0]
        wf = _TemporalWF(self, req_ref, WorkflowOptions.from_dict(inp.get("options")))
        try:
            result = await request_workflow(wf, self._req_id)
        except _Cancelled:
            self._state, self._pending, self._result = "cancelled", [], {"status": "cancelled"}
            self._revision += 1
            return self._result
        except (ActivityError, ChildWorkflowError, ApplicationError) as exc:
            cause = _app_cause(exc)
            self._state, self._pending = "failed", []
            self._error = f"{cause.type if cause else type(exc).__name__}: {cause.message if cause else exc}"[:2000]
            self._revision += 1
            raise ApplicationError(self._error, type="WorkflowFailed", non_retryable=True) from exc
        self._result = result
        self._state = "cancelled" if result.get("status") == "cancelled" else "completed"
        self._pending = []
        self._revision += 1
        return result

    async def _halt_and_wait(self, wf: _TemporalWF, idem: str, reason: str, detail: str) -> None:
        """T20：guard 熔断 → 写 halted（return_to=当前主状态）→ 等 resume 信号（T21 由人经 API 写回 return_to）。"""
        st = await wf.read("request_status", wf.req_ref)
        cur = st["status"]
        ret = cur if cur not in ("awaiting_human", "halted") else (_return_to(st.get("status_reason")) or "parsed")
        self._halts += 1
        if cur != "halted":
            await wf._exec("halt", f"{idem}:halt:{self._halts}", [wf.req_ref, reason, ret, detail[:300]])
        self._set_wait("halted", [], {"halt_reason": reason, "return_to": ret, "detail": detail[:300]})
        await workflow.wait_condition(lambda: self._resumes >= self._halts or self._cancelled)
        self._check_cancel()
        self._set_running()

    # ------------------------------------------------------------------ signals
    @workflow.signal
    def human_answered(self, payload: dict[str, Any] | None = None) -> None:
        """人工项（CQ/Approval/Conflict/Mapping）有变化：工作流用 ``check_pending`` 重新核对（不信任载荷）。"""
        self._wakeups += 1

    @workflow.signal
    def cq_answered(self, payload: dict[str, Any] | None = None) -> None:
        self._wakeups += 1

    @workflow.signal
    def approval_decided(self, payload: dict[str, Any] | None = None) -> None:
        self._wakeups += 1

    @workflow.signal
    def conflict_resolved(self, payload: dict[str, Any] | None = None) -> None:
        self._wakeups += 1

    @workflow.signal
    def supplier_responded(self, payload: dict[str, Any]) -> None:
        """``{sreq_id, blob_sha256, kind: submission|reply}``（门户提交 / 回复邮件，blob 已入库）。按 sreq_id 去重。"""
        if payload and payload.get("sreq_id") and not any(r.get("sreq_id") == payload["sreq_id"] for r in self._replies):
            self._replies.append({k: payload.get(k) for k in ("sreq_id", "blob_sha256", "kind") if payload.get(k) is not None})
        self._supplier_signals += 1

    @workflow.signal
    def supplier_replied(self, payload: dict[str, Any]) -> None:
        self.supplier_responded(payload)

    @workflow.signal
    def resume(self, payload: dict[str, Any] | None = None) -> None:
        self._resumes += 1
        self._resume_payloads.append(dict(payload or {}))
        self._wakeups += 1

    @workflow.signal
    def cancel(self, payload: dict[str, Any] | None = None) -> None:
        self._cancelled = True

    # ------------------------------------------------------------------ queries
    @workflow.query
    def status(self) -> dict[str, Any]:
        return {"workflow_id": workflow.info().workflow_id, "request_id": self._req_id, "state": self._state,
                "pending": list(self._pending), "revision": self._revision, "progress": dict(self._progress),
                "halts": self._halts, "resumes": self._resumes, "escalations": self._escalations,
                "supplier_replies": len(self._replies), "result": self._result, "error": self._error, **self._extra}

    @workflow.query
    def progress(self) -> dict[str, Any]:
        return self.status()


# ---------------------------------------------------------------------------- client 侧
class TemporalEngine:
    """API / CLI 使用的 Temporal 客户端封装（启动、信号、查询）。"""

    def __init__(self, client: Any, *, task_queue: str = TASK_QUEUE, options: WorkflowOptions | dict[str, Any] | None = None,
                 execution_timeout_s: float | None = None) -> None:
        self.client = client
        self.task_queue = task_queue
        self.options = options if isinstance(options, WorkflowOptions) else WorkflowOptions.from_dict(options)
        self.execution_timeout_s = execution_timeout_s

    @classmethod
    async def connect(cls, address: str = "localhost:7233", namespace: str = "default", **kw: Any) -> "TemporalEngine":
        from temporalio.client import Client

        return cls(await Client.connect(address, namespace=namespace), **kw)

    async def start(self, req_ref: str) -> str:
        from temporalio.common import WorkflowIDReusePolicy

        from temporalio.exceptions import WorkflowAlreadyStartedError

        wid = workflow_id(req_ref)
        try:
            await self.client.start_workflow(
                WORKFLOW_NAME, {"req_ref": req_ref, "options": self.options.__dict__}, id=wid, task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                execution_timeout=dt.timedelta(seconds=self.execution_timeout_s) if self.execution_timeout_s else None)
        except WorkflowAlreadyStartedError:  # 同一 Request 重复上传（intake 幂等）→ 沿用已有工作流（04 §3.4）
            pass
        return wid

    def handle(self, req_id: str) -> Any:
        return self.client.get_workflow_handle(workflow_id(req_id))

    async def signal(self, req_id: str, name: str, payload: dict[str, Any] | None = None) -> None:
        await self.handle(req_id).signal(name, payload or {})

    async def status(self, req_id: str) -> dict[str, Any]:
        return await self.handle(req_id).query("status")

    async def wait_for(self, req_id: str, *, after_revision: int = -1, states: tuple[str, ...] = WAITING_STATES + FINAL_STATES,
                       timeout_s: float = 60.0, poll_s: float = 0.05) -> dict[str, Any]:
        """轮询 ``status`` 直到工作流停在等待/终态且 ``revision`` 前进（测试与同步 API 用）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last: dict[str, Any] = {}
        while loop.time() < deadline:
            try:
                last = await self.status(req_id)
            except Exception:  # noqa: BLE001 - 工作流刚启动、首个 workflow task 尚未完成时 query 可能失败
                await asyncio.sleep(poll_s)
                continue
            if last.get("state") in states and last.get("revision", 0) > after_revision:
                return last
            await asyncio.sleep(poll_s)
        raise TimeoutError(f"workflow {workflow_id(req_id)} did not reach {states} (last={last.get('state')})")


__all__ = ["RequestWorkflow", "QuestionBatchWorkflow", "TemporalEngine", "WorkflowOptions", "TASK_QUEUE", "WORKFLOW_NAME",
           "BATCH_WORKFLOW_NAME", "ACTIVITY_NAMES", "READ_ACTIVITY_NAMES", "ALL_ACTIVITY_NAMES", "NON_RETRYABLE", "HALT_ERRORS",
           "DEFAULT_RETRY", "WAITING_STATES", "FINAL_STATES", "workflow_id"]
