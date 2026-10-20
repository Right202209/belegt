# Spec: 03 §6.8（Graph API）、01（审批台 UI 端点）、04 §4.3（工作流引擎）、§6.3（A2A）、07 §4.4/§4.6（认证；开发头仅开发用）、
#       10 §1.3（部署形态：API 进程 + worker 进程）
"""``create_full_app(settings)``：组装完整 HTTP 应用。

- ``api.create_app``（Graph API 03 §6.8 + 最小 UI 端点）
- ``api.routes_workflow.workflow_router``（/requests 上传/列表/详情/推进/恢复、/blobs 兼容 web）+ ``configure_workflow``
  （``workflow_engine = local | temporal``）
- ``api.routes_review.review_router``（收件箱/审批/卡片/冲突/有效期，06）
- ``a2a.routes.a2a_router``（Agent Card、订阅推送）
- ``web/dist`` 静态挂载（存在时，挂在 ``/ui``，``/`` 重定向）
- 开发：CORS（``cors_origins``）、``X-Belegt-Actor`` → 令牌映射（仅 ``BELEGT_DEV_AUTH=1`` / ``dev_auth=True``）

租户依赖（``Services``：LLM、guard、egress、连接器…）由 ``services_factory``（``module:callable``）提供，默认演示租户
``belegt_core.demo:build_env``（返回 ``DemoEnv`` 时一并合入其令牌表与 LocalRunner）。``services_factory="none"`` 时只有图 API、
审批台与 A2A，工作流端点返回 404 ``workflow engine not configured``。
"""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _default_web_dist() -> str | None:
    for p in (Path(__file__).resolve().parents[4] / "web" / "dist", Path.cwd() / "web" / "dist"):
        if (p / "index.html").exists():
            return str(p)
    return None


@dataclass
class Settings:
    db_url: str = "sqlite+pysqlite:///:memory:"
    services_factory: str | None = "belegt_core.demo:build_env"   # "none" → 无工作流
    workflow_engine: str = "local"                                 # local | temporal
    local_background: bool = True                                  # local：上传后后台推进
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "belegt-requests"
    temporal_options: dict[str, Any] = field(default_factory=dict)  # WorkflowOptions
    tokens_path: str | None = None                                 # 静态令牌表（JSON/YAML），默认 BELEGT_DEV_TOKENS
    dev_auth: bool = False                                         # X-Belegt-Actor 映射（BELEGT_DEV_AUTH=1）
    dev_default_actor: str | None = None                           # 无任何认证头时的默认执行者（仅 dev_auth）
    cors_origins: list[str] = field(default_factory=list)
    static_dir: str | None = None                                  # web/dist；None → 自动探测
    static_path: str = "/ui"
    public_base_url: str | None = None                             # Agent Card url
    a2a_allow_http_webhooks: bool = False
    a2a_webhook_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        dev = _env_bool("BELEGT_DEV_AUTH")
        cors = [o.strip() for o in e.get("BELEGT_CORS_ORIGINS", "").split(",") if o.strip()]
        if dev and not cors:
            cors = ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173"]
        return cls(
            db_url=e.get("BELEGT_DB_URL", cls.db_url),
            services_factory=e.get("BELEGT_SERVICES_FACTORY", cls.services_factory),
            workflow_engine=e.get("BELEGT_WORKFLOW_ENGINE", "local"),
            local_background=_env_bool("BELEGT_LOCAL_BACKGROUND", True),
            temporal_address=e.get("BELEGT_TEMPORAL_ADDRESS", cls.temporal_address),
            temporal_namespace=e.get("BELEGT_TEMPORAL_NAMESPACE", cls.temporal_namespace),
            temporal_task_queue=e.get("BELEGT_TEMPORAL_TASK_QUEUE", cls.temporal_task_queue),
            temporal_options=json.loads(e["BELEGT_TEMPORAL_OPTIONS"]) if e.get("BELEGT_TEMPORAL_OPTIONS") else {},
            tokens_path=e.get("BELEGT_DEV_TOKENS"),
            dev_auth=dev,
            dev_default_actor=e.get("BELEGT_DEV_DEFAULT_ACTOR") or None,
            cors_origins=cors,
            static_dir=e.get("BELEGT_WEB_DIST") or None,
            public_base_url=e.get("BELEGT_PUBLIC_BASE_URL") or None,
            a2a_allow_http_webhooks=_env_bool("BELEGT_A2A_ALLOW_HTTP"),
            a2a_webhook_hosts=[h.strip() for h in e.get("BELEGT_A2A_WEBHOOK_HOSTS", "").split(",") if h.strip()],
        )


def load_services_env(settings: Settings) -> Any:
    """``services_factory`` → 返回值（``Services`` 或 ``DemoEnv``）；``none``/空 → None。"""
    spec = (settings.services_factory or "").strip()
    if not spec or spec.lower() == "none":
        return None
    mod, _, attr = spec.partition(":")
    fn = getattr(importlib.import_module(mod), attr or "build_services")
    if not callable(fn):
        return fn
    if spec.startswith("belegt_core.demo"):
        return fn(url=settings.db_url)
    return fn()


def _prepend_router(app: Any, router: Any) -> None:
    """把 ``router`` 放到全部已挂载路由之前（同路径时优先匹配）：core 的 ``POST /blobs`` 只返回 ``sha256``，
    ``routes_workflow.blobs_router`` 的版本同时返回 ``blob_sha256``（web 兼容）。"""
    n = len(app.router.routes)
    app.include_router(router)
    added = app.router.routes[n:]
    del app.router.routes[n:]
    first = next((i for i, r in enumerate(app.router.routes) if getattr(r, "path", None) not in
                  ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")), len(app.router.routes))
    app.router.routes[first:first] = added


def create_full_app(settings: Settings | None = None, *, services: Any = None, store: Any = None, tokens: Any = None,
                    temporal_engine: Any = None) -> Any:
    from fastapi.responses import RedirectResponse

    from .a2a.routes import a2a_router, configure_a2a
    from .api import create_app
    from .api.auth import StaticTokenTable
    from .api.routes_review import review_router
    from .api.routes_workflow import DevActorMiddleware, blobs_router, configure_workflow, workflow_router
    from .store import GraphStore

    settings = settings or Settings.from_env()
    env = services if services is not None else load_services_env(settings)
    svc = getattr(env, "svc", env)
    runner = getattr(env, "runner", None)
    if store is None:
        store = svc.store if svc is not None else GraphStore(settings.db_url)
    if tokens is None:
        tokens = StaticTokenTable()
        if settings.tokens_path:
            src = settings.tokens_path
            text = Path(src).read_text("utf-8")
            if src.endswith((".yaml", ".yml")):
                import yaml

                tokens.table.update(yaml.safe_load(text) or {})
            else:
                tokens.table.update(json.loads(text))
        env_tokens = getattr(env, "tokens", None)
        if env_tokens is not None and hasattr(env_tokens, "table"):
            for k, v in env_tokens.table.items():
                tokens.table.setdefault(k, v)

    app = create_app(store, tokens, routers=[workflow_router, review_router, a2a_router])
    _prepend_router(app, blobs_router)
    app.state.settings = settings

    if svc is not None:
        factory = None
        if settings.workflow_engine == "temporal" and temporal_engine is None:
            async def _connect() -> Any:
                from .workflow.temporal import TemporalEngine

                return await TemporalEngine.connect(settings.temporal_address, settings.temporal_namespace,
                                                    task_queue=settings.temporal_task_queue, options=settings.temporal_options)

            factory = _connect
        configure_workflow(app, svc, engine=settings.workflow_engine, runner=runner if settings.workflow_engine == "local" else None,
                           temporal_engine=temporal_engine, engine_factory=factory, background=settings.local_background)
    configure_a2a(app, org_id=getattr(svc, "org_id", None), base_url=settings.public_base_url,
                  allow_http=settings.a2a_allow_http_webhooks, allowed_hosts=settings.a2a_webhook_hosts)

    if settings.dev_auth:
        app.add_middleware(DevActorMiddleware, default_actor=settings.dev_default_actor)
    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                           allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Disposition", "X-Belegt-Pack-Hash"])

    static_dir = settings.static_dir or _default_web_dist()
    if static_dir and Path(static_dir, "index.html").exists():
        from fastapi.staticfiles import StaticFiles

        app.mount(settings.static_path, StaticFiles(directory=static_dir, html=True), name="web")
        target = settings.static_path.rstrip("/") + "/"

        @app.get("/", include_in_schema=False)
        def _root() -> Any:
            return RedirectResponse(target)

    @app.get("/healthz", include_in_schema=False)
    def _healthz() -> dict[str, Any]:
        ws = getattr(app.state, "workflow", None)
        return {"status": "ok", "workflow_engine": ws.mode if ws else None, "org_id": getattr(svc, "org_id", None)}

    return app


__all__ = ["Settings", "create_full_app", "load_services_env"]
