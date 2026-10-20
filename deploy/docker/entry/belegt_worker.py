# Spec: 04 §1.3（worker 进程与 task queue）、§4.3；10 §5.1（指标）、§3.3（HPA 按积压伸缩）
"""belegt-worker 容器入口：在后台线程起一个极小的 HTTP 服务（``/healthz``、``/metrics``，默认 :9464），
然后运行 ``belegt_core.workflow.temporal_worker.main``（参数与环境变量同该模块：``BELEGT_TEMPORAL_ADDRESS`` 等）。

``/healthz`` 在 worker 主循环退出（异常或 Temporal 连接失败）后返回 503，让编排器重启容器。
"""
from __future__ import annotations

import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"alive": True}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            ok = STATE["alive"]
            body = b"ok" if ok else b"worker stopped"
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/metrics":
            try:
                from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
            except ImportError:
                self.send_response(404)
                self.end_headers()
                return
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a: object) -> None:  # 不写访问日志（10 §5.1：日志只含结构化事件）
        return


def _enable_pg_commit() -> None:
    """worker 的服务工厂（如 belegt_core.demo:build_env）自己构造 ``GraphStore``。为保证 worker 与 belegt-api 的写入都经
    ``graph.commit_version()``（否则物理分区表与可移植表的 head 会分叉，之后 API 写同一对象时数据库报 GRAPH_VERSION_CONFLICT），
    在 PostgreSQL 且 ``BELEGT_PG_COMMIT=on`` 时给 ``GraphStore`` 的每个实例挂上适配器（SQLite 实例原样不动）。"""
    if os.environ.get("BELEGT_PG_COMMIT", "on").lower() not in ("1", "on", "true", "yes"):
        return
    if not os.environ.get("BELEGT_DB_URL", "").startswith("postgresql"):
        return
    from belegt_core.store import graph_store
    from belegt_core.store.pg_commit import PgCommitConfig, attach_pg_commit

    role = os.environ.get("BELEGT_PG_WRITER_ROLE", "belegt_writer") or None
    orig_init = graph_store.GraphStore.__init__

    def __init__(self, *a, **kw):  # type: ignore[no-untyped-def]
        url = a[0] if a else kw.get("url_or_engine", "")
        if isinstance(url, str) and url.startswith("postgresql"):
            kw.setdefault("create_tables", False)  # 可移植表由迁移 Job 建好，应用账号无 CREATE 权限
        orig_init(self, *a, **kw)
        attach_pg_commit(self, PgCommitConfig(writer_role=role))

    graph_store.GraphStore.__init__ = __init__  # type: ignore[method-assign]


def main() -> int:
    _enable_pg_commit()
    port = int(os.environ.get("BELEGT_WORKER_HEALTH_PORT", "9464"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from belegt_core.workflow.temporal_worker import main as worker_main

    try:
        return int(worker_main(sys.argv[1:]) or 0)
    finally:
        STATE["alive"] = False


if __name__ == "__main__":
    sys.exit(main())
