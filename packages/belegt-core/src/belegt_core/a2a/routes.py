# Spec: 04 §6.3（A2A：Agent Card 发布于 /.well-known/agent-card.json；任务 belegt.subscribe_datapoints；pushNotifications）、
#       07 §4.3（customer_buyer：只读、approved_only、cp_scope）
"""A2A 路由 ``a2a_router``（最小可用）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | ``/.well-known/agent.json``、``/.well-known/agent-card.json`` | Agent Card（无需令牌） |
| POST | ``/a2a`` | JSON-RPC 2.0：``message/send``（data part ``{skill:"belegt.subscribe_datapoints", datapoint_ids[], webhook_url, …}``）→ Task（completed，artifact 为订阅）；``tasks/get`` |
| POST | ``/a2a/subscriptions`` | ``{datapoint_ids[], webhook_url, counterparty_id?, period?, secret?}`` → 201 订阅（生成的 secret 只返回一次） |
| GET | ``/a2a/subscriptions`` | 本人可见的订阅（customer_buyer 仅自己 cp_scope 内；compliance_owner/org_admin 全部） |
| GET / DELETE | ``/a2a/subscriptions/{id}`` | 详情（含推送记录）/ 取消 |
| POST | ``/a2a/subscriptions/dispatch`` | 推送未送达的已批准答案（compliance_owner/org_admin/service；生产由调度器调用） |

只有 ``belegt.subscribe_datapoints`` 已实现；``request_pcf``/``request_auditpack`` 在 Card 中列出但 ``message/send`` 返回
``failed``（V2，见 04 §6.3）。
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from ulid import ULID

from ..authz import Actor
from ..errors import GraphError
from ..api import current_actor
from .subscriptions import MANAGE_ROLES, SubscriptionService

a2a_router = APIRouter(tags=["a2a"])
A2A_VERSION = "0.1.0"
SKILLS = [
    {"id": "belegt.subscribe_datapoints", "name": "Subscribe to datapoints", "tags": ["vsme", "ghg"],
     "description": "Push updates when an approved answer for granted datapoints changes."},
    {"id": "belegt.request_pcf", "name": "Request product PCF", "tags": ["pact", "catena-x"],
     "description": "Returns PACT v3 ProductFootprint for granted products. (not yet available)"},
    {"id": "belegt.request_auditpack", "name": "Request audit pack", "tags": ["verification"],
     "description": "Signed belegt-auditpack/1 link for an approved submission. (not yet available)"},
]


class A2AServices:
    def __init__(self, store: Any, *, org_id: str | None = None, base_url: str | None = None, provider_name: str | None = None,
                 subscriptions: SubscriptionService | None = None, **sub_kw: Any) -> None:
        self.store = store
        self.org_id = org_id
        self.base_url = (base_url or "http://localhost:8000").rstrip("/")
        self.provider_name = provider_name
        self.subs = subscriptions or SubscriptionService(store, **sub_kw)
        self.tasks: dict[str, dict[str, Any]] = {}

    def organization(self) -> dict[str, Any] | None:
        if self.org_id:
            return self.store.get(self.org_id, org_id=self.org_id)
        orgs = self.store.query("Organization")
        return orgs[0] if len(orgs) == 1 else None

    def agent_card(self) -> dict[str, Any]:
        org = self.organization() or {}
        name = self.provider_name or org.get("legal_name") or "Belegt"
        token_url = f"{self.base_url}/oauth/token"
        return {
            "protocolVersion": "0.3.0",
            "name": f"{name} – Belegt Supplier Data Agent",
            "description": "Provides approved sustainability data (PCF, Scope 1/2, CBAM SEE) under supplier-granted consent.",
            "url": f"{self.base_url}/a2a",
            "preferredTransport": "JSONRPC",
            "version": A2A_VERSION,
            "provider": {"organization": name, "url": self.base_url},
            "capabilities": {"streaming": False, "pushNotifications": True, "stateTransitionHistory": False},
            "securitySchemes": {
                "oauth": {"type": "oauth2", "flows": {"clientCredentials": {"tokenUrl": token_url, "scopes": {
                    "belegt.subscribe": "subscribe to datapoints", "belegt.pcf.request": "request product PCF"}}}},
                "bearer": {"type": "http", "scheme": "bearer"}},
            "security": [{"oauth": ["belegt.subscribe"]}, {"bearer": []}],
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": SKILLS,
            "extensions": {"belegt": {"subscriptions_url": f"{self.base_url}/a2a/subscriptions",
                                      "signature": "HMAC-SHA256 over '<X-Belegt-Timestamp>.<body>' in X-Belegt-Signature",
                                      "org_id": org.get("id")}},
        }


def configure_a2a(app: Any, **kw: Any) -> A2AServices:
    svc = A2AServices(app.state.store, **kw)
    app.state.a2a = svc
    return svc


def get_a2a(request: Request) -> A2AServices:
    svc = getattr(request.app.state, "a2a", None)
    if svc is None or svc.store is not request.app.state.store:
        svc = configure_a2a(request.app, base_url=str(request.base_url))
    return svc


@a2a_router.get("/.well-known/agent.json")
def agent_json(svc: A2AServices = Depends(get_a2a)) -> dict[str, Any]:
    return svc.agent_card()


@a2a_router.get("/.well-known/agent-card.json")
def agent_card_json(svc: A2AServices = Depends(get_a2a)) -> dict[str, Any]:
    return svc.agent_card()


@a2a_router.post("/a2a/subscriptions", status_code=201)
def create_subscription(body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor),
                        svc: A2AServices = Depends(get_a2a)) -> dict[str, Any]:
    return svc.subs.subscribe(actor, datapoint_ids=list(body.get("datapoint_ids") or []), webhook_url=str(body.get("webhook_url") or ""),
                              counterparty_id=body.get("counterparty_id"), period=body.get("period"), secret=body.get("secret"))


@a2a_router.get("/a2a/subscriptions")
def list_subscriptions(actor: Actor = Depends(current_actor), svc: A2AServices = Depends(get_a2a)) -> dict[str, Any]:
    return {"items": svc.subs.list(actor)}


@a2a_router.post("/a2a/subscriptions/dispatch")
def dispatch(body: dict[str, Any] | None = Body(default=None), actor: Actor = Depends(current_actor),
             svc: A2AServices = Depends(get_a2a)) -> dict[str, Any]:
    if not (actor.is_service or actor.has_role(*MANAGE_ROLES)):
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "dispatch requires compliance_owner/org_admin or a service actor")
    return svc.subs.dispatch(actor.org_id, subscription_id=(body or {}).get("subscription_id"))


@a2a_router.get("/a2a/subscriptions/{sid}")
def get_subscription(sid: str, actor: Actor = Depends(current_actor), svc: A2AServices = Depends(get_a2a)) -> dict[str, Any]:
    sub = svc.subs.get(actor, sid)
    sub["deliveries"] = [{k: d[k] for k in ("id", "answer_ref", "status", "attempts", "http_status", "updated_at")}
                         for d in svc.subs.deliveries(sid)]
    return sub


@a2a_router.delete("/a2a/subscriptions/{sid}")
def delete_subscription(sid: str, actor: Actor = Depends(current_actor), svc: A2AServices = Depends(get_a2a)) -> dict[str, Any]:
    return svc.subs.cancel(actor, sid)


# ---------------------------------------------------------------------------- JSON-RPC（A2A message/send）
def _rpc_error(rid: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": err})


@a2a_router.post("/a2a")
def a2a_rpc(body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor), svc: A2AServices = Depends(get_a2a)) -> Any:
    rid = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    if body.get("jsonrpc") != "2.0" or not method:
        return _rpc_error(rid, -32600, "invalid request")
    if method == "tasks/get":
        t = svc.tasks.get(params.get("id", ""))
        if t is None or t["_org"] != actor.org_id or t["_by"] != actor.id:
            return _rpc_error(rid, -32001, "task not found")
        return {"jsonrpc": "2.0", "id": rid, "result": {k: v for k, v in t.items() if not k.startswith("_")}}
    if method not in ("message/send", "tasks/send"):
        return _rpc_error(rid, -32601, f"method {method!r} not found")
    msg = params.get("message") or {}
    data = next((p.get("data") for p in msg.get("parts") or [] if p.get("kind", p.get("type")) == "data"), None) or {}
    skill = data.get("skill") or (msg.get("metadata") or {}).get("skill")
    tid = "task_" + str(ULID())
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    task: dict[str, Any] = {"kind": "task", "id": tid, "contextId": msg.get("contextId") or tid, "_org": actor.org_id, "_by": actor.id}
    if skill == "belegt.subscribe_datapoints":
        try:
            sub = svc.subs.subscribe(actor, datapoint_ids=list(data.get("datapoint_ids") or []),
                                     webhook_url=str(data.get("webhook_url") or ""), counterparty_id=data.get("counterparty_id"),
                                     period=data.get("period"), secret=data.get("secret"))
        except GraphError as e:
            task.update(status={"state": "rejected" if e.http_status == 403 else "failed", "timestamp": now,
                                "message": {"role": "agent", "kind": "message", "messageId": tid + "-m",
                                            "parts": [{"kind": "data", "data": e.to_response()}]}})
        else:
            task.update(status={"state": "completed", "timestamp": now},
                        artifacts=[{"artifactId": sub["id"], "name": "subscription", "parts": [{"kind": "data", "data": sub}]}])
    else:
        task.update(status={"state": "failed", "timestamp": now,
                            "message": {"role": "agent", "kind": "message", "messageId": tid + "-m",
                                        "parts": [{"kind": "text", "text": f"skill {skill!r} not available"}]}})
    import copy

    stored = copy.deepcopy(task)
    for art in stored.get("artifacts") or []:
        for part in art.get("parts") or []:
            (part.get("data") or {}).pop("secret", None)  # 生成的 secret 只随首个响应返回
    svc.tasks[tid] = stored
    return {"jsonrpc": "2.0", "id": rid, "result": {k: v for k, v in task.items() if not k.startswith("_")}}


__all__ = ["a2a_router", "A2AServices", "configure_a2a", "get_a2a", "SKILLS"]
