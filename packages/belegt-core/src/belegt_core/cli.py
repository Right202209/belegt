# Spec: CLI（CONTRACTS：`belegt demo`）；04 §1.3（API / worker 进程）、§4.3（Temporal worker）、§6.2（MCP server）
"""``belegt`` 命令行。

    belegt serve  [--host 127.0.0.1] [--port 8000] [--engine local|temporal] [--dev-auth] [--reload]
    belegt demo   [path/to/questionnaire.xlsx] [--json]
    belegt worker [--address localhost:7233] [--task-queue belegt-requests] [--services module:factory]
    belegt mcp    [--transport stdio|streamable-http] [--token …] [--demo]

``serve`` 用 ``app_factory.create_full_app(Settings.from_env())``，命令行参数覆盖环境变量（``BELEGT_DB_URL``、
``BELEGT_WORKFLOW_ENGINE``、``BELEGT_DEV_AUTH`` …，见 ``app_factory.Settings``）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    if a.engine:
        os.environ["BELEGT_WORKFLOW_ENGINE"] = a.engine
    if a.dev_auth:
        os.environ["BELEGT_DEV_AUTH"] = "1"
    if a.db_url:
        os.environ["BELEGT_DB_URL"] = a.db_url
    if a.services:
        os.environ["BELEGT_SERVICES_FACTORY"] = a.services
    if not os.environ.get("BELEGT_DB_URL"):
        # 多线程服务下 SQLite :memory: + StaticPool 共享单连接会出现竞态；开发服务默认改用临时文件库。
        import tempfile

        dbf = os.path.join(tempfile.mkdtemp(prefix="belegt-dev-"), "dev.db")
        os.environ["BELEGT_DB_URL"] = f"sqlite+pysqlite:///{dbf}"
    uvicorn.run("belegt_core.cli:asgi_app", factory=True, host=a.host, port=a.port, reload=a.reload,
                log_level=a.log_level)
    return 0


def asgi_app() -> Any:
    """``uvicorn --factory belegt_core.cli:asgi_app``。"""
    from .app_factory import Settings, create_full_app

    return create_full_app(Settings.from_env())


def _run_demo(path: str | None, *, out: str | None = None, until: str | None = None, db_url: str | None = None) -> Any:
    """调用 ``belegt_core.demo.run_demo``（按其签名传参：``path``/``out``/``until``/``db_url`` 只传它支持的）；
    非 xlsx 问卷（csv…）用 demo 包的 ``build_env/upload/drive/summary`` 按扩展名给 MIME 跑到待审批。"""
    import inspect

    from . import demo

    if path and not path.lower().endswith(".xlsx") and all(hasattr(demo, n) for n in ("build_env", "upload", "drive", "summary")):
        import mimetypes
        from pathlib import Path

        p = Path(path)
        env = demo.build_env(url=db_url) if db_url else demo.build_env()
        mime = {".csv": "text/csv"}.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        req_ref = demo.upload(env, p.read_bytes(), p.name, mime=mime)
        human = demo.DemoHuman(env.store, env.user("owner"), executive=env.user("exec"))
        res, trail = demo.drive(env, req_ref, human=human)
        return demo.summary(env, req_ref, res, trail, human)
    fn = getattr(demo, "run_demo", None)
    if fn is None:  # 兼容：demo 包入口改名时退回 run 模块
        from .demo import run as demo_run

        fn = getattr(demo_run, "run_demo", None) or getattr(demo_run, "main")
    params = inspect.signature(fn).parameters
    kw: dict[str, Any] = {}
    for name, val in (("path", path), ("out", out), ("until", until), ("db_url", db_url)):
        if val is not None and name in params:
            kw[name] = val
    if path and "path" not in params:  # 旧签名 run_demo(path)
        return fn(path, **kw)
    return fn(**kw)


def _summary_dict(s: Any) -> Any:
    return s.to_dict() if hasattr(s, "to_dict") else s


def _cmd_demo(a: argparse.Namespace) -> int:
    s = _run_demo(a.path, out=a.out, until=a.until, db_url=a.db_url)
    if a.json:
        print(json.dumps(_summary_dict(s), ensure_ascii=False, indent=2, default=str))
        return 0
    from . import demo

    if hasattr(s, "format"):
        print(s.format())
        return 0
    fmt = getattr(demo, "format_summary", None)
    print(fmt(s) if fmt else s)
    return 0


def _cmd_worker(a: argparse.Namespace) -> int:
    from .workflow.temporal_worker import main as worker_main

    argv = ["--address", a.address, "--namespace", a.namespace, "--task-queue", a.task_queue]
    if a.services:
        argv += ["--services", a.services]
    return worker_main(argv)


def _cmd_mcp(a: argparse.Namespace) -> int:
    from .mcp.server import serve

    store = tokens = None
    if a.demo:  # 演示租户：内存库 + 演示令牌（demo-owner / demo-sales …）
        from .demo import build_env

        env = build_env()
        store, tokens = env.store, env.tokens
    serve(store, transport=a.transport, token=a.token, tokens=tokens, host=a.host, port=a.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="belegt", description="Belegt：证据图、问卷工作流、审批台 API、Temporal worker、MCP server")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="启动 HTTP API（uvicorn）")
    s.add_argument("--host", default=os.environ.get("BELEGT_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("BELEGT_PORT", "8000")))
    s.add_argument("--engine", choices=["local", "temporal"], default=None)
    s.add_argument("--dev-auth", action="store_true", help="允许 X-Belegt-Actor 演示头（BELEGT_DEV_AUTH=1）")
    s.add_argument("--db-url", default=None)
    s.add_argument("--services", default=None, help="module:factory（默认演示租户 belegt_core.demo:build_env；none=不启用工作流）")
    s.add_argument("--reload", action="store_true")
    s.add_argument("--log-level", default="info")
    s.set_defaults(fn=_cmd_serve)

    d = sub.add_parser("demo", help="跑演示：收件→解析映射→取证→计算→起草→审计→待审批，输出摘要")
    d.add_argument("path", nargs="?", default=None, help="问卷文件（默认 examples/oem_fragebogen_de_100.xlsx）")
    d.add_argument("--json", action="store_true")
    d.add_argument("--out", default=None, help="输出目录（回填文件、审计包、summary；run_demo 支持时）")
    d.add_argument("--until", default=None, choices=["in_review", "submitted"], help="停在哪一步（run_demo 支持时）")
    d.add_argument("--db-url", default=None)
    d.set_defaults(fn=_cmd_demo)

    w = sub.add_parser("worker", help="Temporal worker（RequestWorkflow + Activities）")
    w.add_argument("--address", default=os.environ.get("BELEGT_TEMPORAL_ADDRESS", "localhost:7233"))
    w.add_argument("--namespace", default=os.environ.get("BELEGT_TEMPORAL_NAMESPACE", "default"))
    w.add_argument("--task-queue", default=os.environ.get("BELEGT_TEMPORAL_TASK_QUEUE", "belegt-requests"))
    w.add_argument("--services", default=None)
    w.set_defaults(fn=_cmd_worker)

    m = sub.add_parser("mcp", help="Belegt MCP server（只读已批准数据）")
    m.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    m.add_argument("--token", default=None, help="stdio 执行者令牌（默认 BELEGT_MCP_TOKEN）")
    m.add_argument("--demo", action="store_true", help="用演示租户（内存库）")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8766)
    m.set_defaults(fn=_cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
