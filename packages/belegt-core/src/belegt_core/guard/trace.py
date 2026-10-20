# Spec: 04 §5.4（Trace 结构：每个 Agent 调用一个 span，模型/工具调用为子 span；新增 workflow_run_id、activity、attempt、
#       policy_checks[]、budget、loop_sig、cache、flags[]）、03 §3.15 Trace、10 §5.1（span 属性中不含业务值）
"""Trace 记录器。

03 §3.15 的核心字段写入 ``GraphStore.record_trace``（trace.json，``additionalProperties:false``）；04 §5.4 的扩展字段
序列化后作为 blob 存储，``Trace.payload_blob`` 记其 ``sha256:``（生产应加密，10 §5.1；本实现为明文 blob，见 README）。
扩展字段里只有计数、哈希、ObjectRef 与策略结果，不含题干/证据原文/数值。
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterator

from ..canonical import hash_obj
from ..ids import new_trace_id


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Span:
    trace_id: str
    span_id: str
    actor: str
    org_id: str
    parent_span_id: str | None = None
    request_id: str | None = None
    workflow_run_id: str | None = None
    activity: str | None = None
    attempt: int = 1
    model: str | None = None
    model_provider: str | None = None
    prompt_version: str | None = None
    input_hash: str = "sha256:" + "0" * 64
    output_hash: str = "sha256:" + "0" * 64
    tokens_in: int = 0
    tokens_out: int = 0
    cost_eur: Decimal = Decimal("0")
    cache: dict[str, Any] = field(default_factory=lambda: {"layer": "none", "key": None})
    policy_checks: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    loop_sig: str | None = None
    flags: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=_now)
    ended_at: str | None = None
    status: str = "ok"
    kind: str = "agent"  # agent | llm | tool | activity

    def check(self, gp_id: str, result: str, **extra: Any) -> None:
        self.policy_checks.append({"id": gp_id, "result": result, **{k: str(v) for k, v in extra.items()}})

    def core(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.trace_id, "org_id": self.org_id, "span_id": self.span_id, "actor": self.actor,
            "input_hash": self.input_hash, "output_hash": self.output_hash, "started_at": self.started_at,
            "ended_at": self.ended_at or _now(), "status": self.status,
        }
        if self.parent_span_id:
            d["parent_span_id"] = self.parent_span_id
        if self.request_id and self.request_id.startswith("req_"):
            d["request_id"] = self.request_id
        if self.model:
            d["model"] = self.model
            d["model_provider"] = self.model_provider or "unknown"
        if self.prompt_version:
            d["prompt_version"] = self.prompt_version
        if self.tokens_in or self.tokens_out:
            d["tokens_in"], d["tokens_out"] = self.tokens_in, self.tokens_out
            d["cost_eur"] = format(self.cost_eur.normalize(), "f") if self.cost_eur else "0"
        return d

    def extended(self) -> dict[str, Any]:
        return {"workflow_run_id": self.workflow_run_id, "activity": self.activity, "attempt": self.attempt,
                "policy_checks": self.policy_checks, "budget": self.budget, "loop_sig": self.loop_sig, "cache": self.cache,
                "flags": self.flags, "kind": self.kind}

    def to_dict(self) -> dict[str, Any]:
        return {**self.core(), **self.extended()}


class TraceRecorder:
    """收集 span；``store`` 给定时同步落库（03 §3.15，Trace 100% 保留）。"""

    def __init__(self, store: Any | None = None, *, persist: bool = True) -> None:
        self.store = store
        self.persist = persist and store is not None
        self.spans: list[Span] = []
        self._lock = threading.Lock()

    @staticmethod
    def new_span_id() -> str:
        return "sp_" + secrets.token_hex(4)

    @contextmanager
    def span(self, actor: str, org_id: str, *, trace_id: str | None = None, parent: Span | None = None,
             request_id: str | None = None, activity: str | None = None, attempt: int = 1, kind: str = "agent",
             workflow_run_id: str | None = None, input_obj: Any = None) -> Iterator[Span]:
        sp = Span(trace_id=trace_id or (parent.trace_id if parent else new_trace_id()), span_id=self.new_span_id(),
                  actor=actor, org_id=org_id, parent_span_id=parent.span_id if parent else None,
                  request_id=request_id or (parent.request_id if parent else None),
                  workflow_run_id=workflow_run_id or (parent.workflow_run_id if parent else None),
                  activity=activity or (parent.activity if parent else None), attempt=attempt, kind=kind)
        if input_obj is not None:
            sp.input_hash = hash_obj(_jsonable(input_obj))
        try:
            yield sp
        except Exception as e:
            sp.status = "halted" if type(e).__name__ in ("BudgetExceeded", "LoopDetected", "Halted") else "error"
            sp.flags.append(f"error:{type(e).__name__}")
            raise
        finally:
            sp.ended_at = _now()
            self.finish(sp)

    def finish(self, sp: Span) -> None:
        with self._lock:
            self.spans.append(sp)
        if self.persist:
            core = sp.core()
            try:
                blob = self.store.put_blob(sp.org_id, json.dumps(sp.extended(), sort_keys=True, default=str).encode("utf-8"),
                                           "application/vnd.belegt.trace-ext+json")
                core["payload_blob"] = blob
                self.store.record_trace(core)
            except Exception:  # pragma: no cover - Trace 失败不应中断业务（另有 OTel 旁路）
                pass

    def for_request(self, request_id: str) -> list[Span]:
        return [s for s in self.spans if s.request_id == request_id]


def _jsonable(o: Any) -> Any:
    return json.loads(json.dumps(o, default=str, sort_keys=True))
