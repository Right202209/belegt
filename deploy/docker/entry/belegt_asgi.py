# Spec: 10 §5.1（指标/日志）、§6.1（deploy-profile）、§7.2（合成探测）；03 §6.8（Graph API）；04 §1.3
"""belegt-api 容器入口（ASGI 工厂）：``uvicorn --factory belegt_asgi:create``。

- ``BELEGT_DB_URL`` 为 PostgreSQL 且 ``BELEGT_PG_COMMIT=on``（默认）时，用 ``PgCommitGraphStore``：每次写入在同一事务内
  调用 ``graph.commit_version()``（数据库层最后防线 + RLS + 审计哈希链）；SQLite 时退化为普通 ``GraphStore``。
- 挂载 06 审批/冲突路由（``review_router``）。
- ``/healthz``：进程存活（容器 HEALTHCHECK、K8s liveness）；``/readyz``：数据库可连（readiness）；
  ``/metrics``：Prometheus（10 §5.1 指标名；装了 prometheus_client 才启用）。三者不需要令牌。
- 标签只用低基数字段（禁止 org_id，10 §5.1）。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

log = logging.getLogger("belegt.api")


def _store() -> Any:
    url = os.environ.get("BELEGT_DB_URL", "sqlite+pysqlite:///:memory:")
    pg_commit = os.environ.get("BELEGT_PG_COMMIT", "on").lower() in ("1", "on", "true", "yes")
    if url.startswith("postgresql") and pg_commit:
        from belegt_core.store.pg_commit import PgCommitConfig, PgCommitGraphStore

        role = os.environ.get("BELEGT_PG_WRITER_ROLE", "belegt_writer") or None
        # 可移植表由迁移 Job 预先建好并授权，这里不做 DDL（应用账号无 CREATE 权限）
        return PgCommitGraphStore(url, pg_commit=PgCommitConfig(writer_role=role), create_tables=False)
    from belegt_core import GraphStore

    return GraphStore(url)


def _metrics(app: FastAPI) -> None:
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, Histogram, generate_latest
    except ImportError:  # pragma: no cover
        log.warning("prometheus_client not installed: /metrics disabled")
        return
    http = Histogram("belegt_http_request_duration_seconds", "API latency (SLO-01/02)",
                     ["method", "route", "status_class"],
                     buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 2.0, 5.0))
    write = Histogram("belegt_graph_write_duration_seconds", "graph write latency (10 §5.1)", ["type", "result"])
    query = Histogram("belegt_graph_query_duration_seconds", "graph query latency (10 §5.1)", ["query"])
    app.state.metric_write, app.state.metric_query = write, query

    @app.middleware("http")
    async def _observe(request: Request, call_next: Any) -> Response:
        t0 = time.perf_counter()
        resp = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if route not in ("/metrics", "/healthz", "/readyz"):
            dt = time.perf_counter() - t0
            http.labels(request.method, route, f"{resp.status_code // 100}xx").observe(dt)
            if route.startswith("/graph/lineage"):
                query.labels("lineage").observe(dt)
            elif route.startswith("/graph/impact"):
                query.labels("impact").observe(dt)
            elif route.startswith("/graph/objects") and request.method == "POST":
                write.labels("any", "ok" if resp.status_code < 400 else "rejected").observe(dt)
        return resp

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def create() -> FastAPI:
    from belegt_core.api import create_app

    routers = []
    try:
        from belegt_core.api.routes_review import review_router

        routers.append(review_router)
    except ImportError:  # pragma: no cover
        log.warning("review_router unavailable")
    store = _store()
    app = create_app(store, routers=routers)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> Response:
        from sqlalchemy import text

        try:
            with store.engine.connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"status": "db_unavailable", "error": type(e).__name__}, status_code=503)
        return JSONResponse({"status": "ready", "profile": os.environ.get("BELEGT_DEPLOY_PROFILE", "unknown")})

    _metrics(app)
    return app
