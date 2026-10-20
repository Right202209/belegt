# Spec: 04 §4.3（Activity 清单；RetryPolicy 与非重试错误；task queue）、§1.3（部署形态：worker 进程）、§5.2（guard 熔断 → halted）
"""Temporal worker：把 ``workflow.activities.Activities`` 的方法包装为同名 ``@activity.defn``，注册 ``RequestWorkflow`` /
``QuestionBatchWorkflow`` 并轮询 task queue。

    python -m belegt_core.workflow.temporal_worker --address localhost:7233 --task-queue belegt-requests \\
        --services belegt_core.demo:build_env

``--services``（或 ``BELEGT_SERVICES_FACTORY``）指向返回 ``agents.base.Services``（或带 ``.svc`` 的对象，如 ``DemoEnv``）的工厂；
默认演示租户（``belegt_core.demo:build_env``，数据库取 ``BELEGT_DB_URL``）。

Activity 输入统一为 ``{"idem": str | None, "args": [...]}``；结果是 JSON 可序列化 dict（只含 ObjectRef、计数、状态）。异常映射：
``GraphError`` → ``ApplicationError(type=<code>)``（``NON_RETRYABLE`` 内不重试）；guard 的 ``BudgetExceeded``/``LoopDetected``/
``Halted`` → 同名 ``ApplicationError``（不重试，工作流转 halted 等待 resume）；编程错误（Key/Type/Attribute/ValueError）不重试。
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import datetime as dt
import importlib
import os
from typing import Any, Callable

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .temporal import (
    ACTIVITY_NAMES, NON_RETRYABLE, READ_ACTIVITY_NAMES, TASK_QUEUE, QuestionBatchWorkflow, RequestWorkflow,
)

PASSTHROUGH_MODULES = ("belegt_core", "belegt_dictionary", "belegt_calc", "sqlalchemy", "pydantic", "yaml", "ulid",
                       "jsonschema", "celpy", "httpx")


def _map_exception(exc: Exception) -> ApplicationError:
    from ..errors import GraphError
    from ..guard.budget import BudgetExceeded
    from ..guard.loops import LoopDetected
    from ..guard.policy import Halted

    if isinstance(exc, GraphError):
        return ApplicationError(str(exc), exc.to_dict(), type=exc.code, non_retryable=exc.code in NON_RETRYABLE)
    if isinstance(exc, (BudgetExceeded, LoopDetected, Halted)):
        return ApplicationError(str(exc)[:500], {"reason": getattr(exc, "reason", "policy")}, type=type(exc).__name__,
                                non_retryable=True)
    if isinstance(exc, (KeyError, TypeError, AttributeError, ValueError)):
        return ApplicationError(f"{type(exc).__name__}: {exc}"[:500], type=type(exc).__name__, non_retryable=True)
    return ApplicationError(f"{type(exc).__name__}: {exc}"[:500], type=type(exc).__name__,
                            non_retryable=type(exc).__name__ in NON_RETRYABLE)


def build_activities(svc: Any) -> list[Callable[..., Any]]:
    """为一个租户的 ``Services`` 构造全部 Activity（同一 ``Activities`` 实例；同步函数，需 ``activity_executor``）。"""
    from .activities import Activities

    acts = Activities(svc)
    acts.now_override = None
    out: list[Callable[..., Any]] = []

    def make(name: str, with_idem: bool) -> Callable[..., Any]:
        def fn(inp: dict[str, Any]) -> Any:
            args = list(inp.get("args") or [])
            try:
                if name == "advance_clock":
                    until = args[0]
                    acts.now_override = dt.datetime.fromisoformat(until.replace("Z", "+00:00")) if until else None
                    return {"now_override": until}
                method = getattr(acts, name)
                return method(inp.get("idem"), *args) if with_idem else method(*args)
            except ApplicationError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一映射为 ApplicationError
                raise _map_exception(exc) from exc

        fn.__name__ = f"belegt_{name}"
        fn.__qualname__ = fn.__name__
        return activity.defn(name=name)(fn)

    for n in ACTIVITY_NAMES:
        out.append(make(n, True))
    for n in READ_ACTIVITY_NAMES:
        out.append(make(n, False))
    return out


def sandbox_runner() -> Any:
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

    return SandboxedWorkflowRunner(restrictions=SandboxRestrictions.default.with_passthrough_modules(*PASSTHROUGH_MODULES))


def create_worker(client: Any, svc: Any, *, task_queue: str = TASK_QUEUE, max_concurrent_activities: int | None = None,
                  activity_executor: concurrent.futures.Executor | None = None, activities: list[Any] | None = None,
                  **worker_kw: Any) -> Any:
    """返回未启动的 ``temporalio.worker.Worker``。SQLite（单连接 StaticPool）时 Activity 串行执行。"""
    from temporalio.worker import Worker

    if max_concurrent_activities is None:
        url = str(getattr(getattr(getattr(svc, "store", None), "engine", None), "url", ""))
        max_concurrent_activities = 1 if url.startswith("sqlite") else 8
    executor = activity_executor or concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent_activities)
    worker_kw.setdefault("workflow_runner", sandbox_runner())
    return Worker(client, task_queue=task_queue, workflows=[RequestWorkflow, QuestionBatchWorkflow],
                  activities=activities if activities is not None else build_activities(svc), activity_executor=executor,
                  max_concurrent_activities=max_concurrent_activities, **worker_kw)


def load_services(spec: str | None = None) -> Any:
    """``module:attr`` → 调用得到 ``Services``（``DemoEnv`` 等带 ``.svc`` 的对象取其 ``.svc``）。"""
    spec = spec or os.environ.get("BELEGT_SERVICES_FACTORY") or "belegt_core.demo:build_env"
    mod, _, attr = spec.partition(":")
    fn = getattr(importlib.import_module(mod), attr or "build_services")
    kw: dict[str, Any] = {}
    if spec.startswith("belegt_core.demo") and os.environ.get("BELEGT_DB_URL"):
        kw["url"] = os.environ["BELEGT_DB_URL"]
    obj = fn(**kw) if callable(fn) else fn
    return getattr(obj, "svc", obj)


async def run_worker(*, address: str = "localhost:7233", namespace: str = "default", task_queue: str = TASK_QUEUE,
                     services: str | None = None) -> None:
    from temporalio.client import Client

    client = await Client.connect(address, namespace=namespace)
    svc = load_services(services)
    worker = create_worker(client, svc, task_queue=task_queue)
    print(f"belegt temporal worker: {address} ns={namespace} queue={task_queue} org={getattr(svc, 'org_id', '?')}", flush=True)
    await worker.run()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="belegt worker", description="Belegt Temporal worker（RequestWorkflow + Activities）")
    p.add_argument("--address", default=os.environ.get("BELEGT_TEMPORAL_ADDRESS", "localhost:7233"))
    p.add_argument("--namespace", default=os.environ.get("BELEGT_TEMPORAL_NAMESPACE", "default"))
    p.add_argument("--task-queue", default=os.environ.get("BELEGT_TEMPORAL_TASK_QUEUE", TASK_QUEUE))
    p.add_argument("--services", default=None, help="module:factory 返回 Services（默认 belegt_core.demo:build_env）")
    a = p.parse_args(argv)
    try:
        asyncio.run(run_worker(address=a.address, namespace=a.namespace, task_queue=a.task_queue, services=a.services))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


__all__ = ["build_activities", "create_worker", "sandbox_runner", "load_services", "run_worker", "main", "PASSTHROUGH_MODULES"]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
