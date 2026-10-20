# Spec: 03 §6.8（写入 API 端点与错误码）、§4.6（as-of）、§4.7（重放、影响分析）、§7（审计包）、CONTRACTS（UI 端点）
"""Graph API（FastAPI）。

``create_app(store=None, token_resolver=None, routers=())`` 构建应用；``app`` 为使用内存 SQLite 与
``BELEGT_DEV_TOKENS`` 令牌表的默认实例（``uvicorn belegt_core.api:app``）。

扩展点：后续模块（approval/、workflow/、agents/ …）以 ``routers=[APIRouter, ...]`` 挂载，路由内用
``Depends(current_actor)`` 取执行者、``Depends(get_store)`` 取 GraphStore。
"""
from __future__ import annotations

import os
from typing import Any, Iterable

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..authz import Actor
from ..errors import GraphError
from ..store import GraphStore, WriteItem
from .auth import StaticTokenTable, TokenResolver, actor_from_header


class WriteRequest(BaseModel):
    type: str
    object: dict[str, Any]
    expected_version: int | None = None
    idempotency_key: str | None = None
    patch_mode: str = "replace"


class BatchRequest(BaseModel):
    items: list[WriteRequest] = Field(max_length=500)


class ReplayCalcRequest(BaseModel):
    calc_ref: str
    engine_version: str | None = None


class ReplayAnswerRequest(BaseModel):
    answer_ref: str


def get_store(request: Request) -> GraphStore:
    return request.app.state.store


def current_actor(request: Request, authorization: str | None = Header(default=None),
                  x_trace_id: str | None = Header(default=None)) -> Actor:
    a = actor_from_header(request.app.state.tokens, authorization,
                          ip=request.client.host if request.client else None,
                          user_agent=request.headers.get("user-agent"))
    request.state.trace_id = x_trace_id
    return a


def _items(reqs: Iterable[WriteRequest], trace_id: str | None) -> list[WriteItem]:
    return [WriteItem(r.type, r.object, r.expected_version, r.idempotency_key, r.patch_mode, trace_id) for r in reqs]


graph_router = APIRouter()


@graph_router.post("/graph/objects", status_code=201)
def post_object(body: WriteRequest, request: Request, actor: Actor = Depends(current_actor),
                store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    res = store.write_batch_detailed(_items([body], getattr(request.state, "trace_id", None)), actor=actor)[0]
    return res


@graph_router.post("/graph/objects:batch", status_code=201)
def post_batch(body: BatchRequest, request: Request, actor: Actor = Depends(current_actor),
               store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    return {"results": store.write_batch_detailed(_items(body.items, getattr(request.state, "trace_id", None)), actor=actor)}


@graph_router.post("/graph/validate")
def post_validate(body: dict[str, Any], request: Request, actor: Actor = Depends(current_actor),
                  store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    reqs = [WriteRequest(**i) for i in body["items"]] if "items" in body else [WriteRequest(**body)]
    return {"valid": True, "results": store.validate(_items(reqs, getattr(request.state, "trace_id", None)), actor=actor)}


@graph_router.get("/graph/objects/{obj_id}")
def get_object(obj_id: str, version: int | None = None, as_of_tx: str | None = None, as_of_valid: str | None = None,
               actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    o = store.get(obj_id, version, as_of_tx=as_of_tx, as_of_valid=as_of_valid, actor=actor)
    if o is None:
        raise GraphError("GRAPH_NOT_FOUND", f"{obj_id} not found", path="/id")
    return o


@graph_router.get("/graph/objects/{obj_id}/history")
def get_history(obj_id: str, actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    return {"versions": store.history(obj_id, actor=actor)}


@graph_router.get("/graph/answers")
def get_answers(datapoint_id: str | None = None, as_of_tx: str | None = None, as_of_valid: str | None = None,
                status: str | None = None, actor: Actor = Depends(current_actor),
                store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    f: dict[str, Any] = {}
    if datapoint_id:
        f["datapoint_base"] = datapoint_id.split("@", 1)[0]
    if status:
        f["status"] = status
    return {"items": store.query("Answer", actor=actor, as_of_tx=as_of_tx, as_of_valid=as_of_valid, **f)}


@graph_router.get("/graph/lineage/{object_ref:path}")
def get_lineage(object_ref: str, direction: str = Query("up", pattern="^(up|down)$"), depth: int = Query(8, ge=1, le=32),
                actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    if direction == "down":
        return store.impact(object_ref, depth, actor=actor)
    return store.lineage(object_ref, depth, actor=actor)


@graph_router.get("/graph/impact/{object_ref:path}")
def get_impact(object_ref: str, depth: int = Query(8, ge=1, le=32), actor: Actor = Depends(current_actor),
               store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    return store.impact(object_ref, depth, actor=actor)


@graph_router.post("/graph/replay/calculation")
def replay_calc(body: ReplayCalcRequest, actor: Actor = Depends(current_actor),
                store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    return store.replay_calculation(body.calc_ref, body.engine_version, actor=actor)


@graph_router.post("/graph/replay/answer")
def replay_answer(body: ReplayAnswerRequest, actor: Actor = Depends(current_actor),
                  store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    return store.replay_answer(body.answer_ref, actor=actor)


@graph_router.post("/requests/{req_id}/audit-pack")
def audit_pack(req_id: str, as_of_tx: str | None = None, actor: Actor = Depends(current_actor),
               store: GraphStore = Depends(get_store)) -> Response:
    from ..auditpack import export_audit_pack

    if store.get(req_id, actor=actor) is None:
        raise GraphError("GRAPH_NOT_FOUND", f"{req_id} not found")
    data, manifest = export_audit_pack(store, req_id, actor=actor, as_of_tx=as_of_tx,
                                       signing_key=getattr(store, "signing_key", None))
    return Response(content=data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{manifest["filename"]}"', "X-Belegt-Pack-Hash": manifest["pack_hash"]})


@graph_router.post("/blobs", status_code=201)
async def post_blob(request: Request, actor: Actor = Depends(current_actor),
                    store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    data = await request.body()
    sha = store.put_blob(actor.org_id, data, request.headers.get("content-type"), uploaded_by=actor.id)
    return {"sha256": sha, "bytes": len(data)}


@graph_router.get("/graph/audit/verify")
def audit_verify(actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    if actor.is_user and not actor.has_role("org_admin", "compliance_owner", "auditor_readonly"):
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "audit verification requires org_admin/compliance_owner/auditor_readonly")
    return store.verify_audit_chain(actor.org_id).to_dict()


# ---- 01 所需的最小 UI 列表端点（只读；审批台等模块可挂载更丰富的路由）
ui_router = APIRouter()


def _list(store: GraphStore, actor: Actor, type_name: str, status: str | None, **f: Any) -> dict[str, Any]:
    if status:
        f["status"] = status
    return {"items": store.query(type_name, actor=actor, **f)}


@ui_router.get("/requests")
def list_requests(status: str | None = None, actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)):
    return _list(store, actor, "Request", status)


@ui_router.get("/requests/{req_id}")
def get_request(req_id: str, actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)):
    r = store.get(req_id, actor=actor)
    if r is None or not req_id.startswith("req_"):
        raise GraphError("GRAPH_NOT_FOUND", f"{req_id} not found")
    return {"request": r, "questions": store.query("Question", actor=actor, request_id=req_id)}


@ui_router.get("/approvals")
def list_approvals(status: str | None = None, actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)):
    return _list(store, actor, "Approval", status)


@ui_router.get("/closed-questions")
def list_cqs(status: str | None = None, actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)):
    return _list(store, actor, "ClosedQuestion", status)


@ui_router.get("/conflicts")
def list_conflicts(status: str | None = None, actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)):
    return _list(store, actor, "Conflict", status)


@ui_router.get("/inbox")
def inbox(actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)):
    cqs = [c for c in store.query("ClosedQuestion", actor=actor, status="open")
           if c.get("assignee") == actor.id or c.get("assignee_role") in actor.roles]
    aprs = [a for a in store.query("Approval", actor=actor, status="pending")
            if a.get("assignee") == actor.id or set(a.get("required_role") or []) & set(actor.roles)]
    return {"closed_questions": cqs, "approvals": aprs}


def create_app(store: GraphStore | None = None, token_resolver: TokenResolver | None = None,
               routers: Iterable[APIRouter] = ()) -> FastAPI:
    app = FastAPI(title="Belegt Graph API", version="0.1.0",
                  description="证据图写入/读取 API（03 §6.8）。错误响应：{error:{code,message,rule_id,path,details}}")
    app.state.store = store or GraphStore(os.environ.get("BELEGT_DB_URL", "sqlite+pysqlite:///:memory:"))
    app.state.tokens = token_resolver or StaticTokenTable.from_env()

    @app.exception_handler(GraphError)
    async def _graph_error(_: Request, e: GraphError) -> JSONResponse:
        return JSONResponse(status_code=e.http_status, content=e.to_response())

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError) -> JSONResponse:
        errs = e.errors()
        path = "/" + "/".join(str(p) for p in errs[0].get("loc", ())) if errs else None
        ge = GraphError("GRAPH_SCHEMA_INVALID", errs[0].get("msg", "invalid request") if errs else "invalid request",
                        path=path, details={"errors": [{"loc": list(x.get("loc", ())), "msg": x.get("msg")} for x in errs[:10]]})
        return JSONResponse(status_code=400, content=ge.to_response())

    app.include_router(graph_router)
    # 扩展路由先于最小 UI 列表端点挂载：同路径时（/inbox、/conflicts …）扩展模块的完整实现优先（06 子任务）
    for r in routers:
        app.include_router(r)
    app.include_router(ui_router)
    return app


def __getattr__(name: str) -> Any:
    if name == "app":  # 延迟创建默认实例
        global app
        app = create_app()
        return app
    raise AttributeError(name)


__all__ = ["create_app", "get_store", "current_actor", "graph_router", "ui_router", "WriteRequest", "BatchRequest"]
