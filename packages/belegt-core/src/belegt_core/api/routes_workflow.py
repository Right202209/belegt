# Spec: 01 §2–§3（收件/上传问卷、UI-04 问卷列表与详情、"下一步"、halted 横幅 [Fortsetzen]）、04 §2.2（T01 intake-triage）、
#       §3（Request 状态机）、§4.3（Temporal：signal/query）、§5.2（halted → resume 携带 budget_patch）、03 §6.8（错误体）、
#       web/README.md "后端接口约定"（RequestView、QuestionRow、POST /blobs → {blob_sha256}、X-Belegt-Actor 演示头）
"""问卷工作流路由 ``workflow_router``（挂载：``create_app(store, tokens, routers=[workflow_router, ...])`` 后
``configure_workflow(app, svc, engine="local"|"temporal")``；``app_factory.create_full_app`` 已组装好）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | ``/requests`` | multipart：``file``（可多个 ``files``）+ ``subject?``、``body?``、``sender?``、``channel?``、``counterparty_id?``、``title?`` → T01 intake-triage 建 ``Request``，按配置启动 LocalRunner 或 Temporal ``RequestWorkflow``（201） |
| GET | ``/requests`` | ``{items: RequestView[]}``（web ``RequestView``：``request``、``counterparty_name``、``progress{…}``） |
| GET | ``/requests/{id}`` | ``RequestView`` + ``questions``（Question 对象，兼容 core 的最小端点）+ ``workflow``（状态/待处理项） |
| GET | ``/requests/{id}/questions`` | ``{items: [{question, answer?, risk_level?, evidence_count}]}``（sales 等 approved_only 角色看不到草稿值） |
| POST | ``/requests/{id}/advance`` | 推进：可带 ``{signal, payload}``（``human_answered``/``supplier_responded``…）；Local 同步跑到下一个挂起点 |
| POST | ``/requests/{id}/resume`` | T21：``{budget_patch?}``，仅 ``halted`` 时（``org_admin``/``compliance_owner``） |
| POST | ``/requests/{id}/cancel`` | T22/T23 取消（``compliance_owner``/``org_admin``） |
| POST | ``/blobs`` | 原始请求体或 multipart ``file`` → ``{sha256, blob_sha256, bytes, mime}``（同时兼容 core 与 web） |

开发认证：``DevActorMiddleware``（仅 ``BELEGT_DEV_AUTH=1`` 时由 app_factory 安装）把 ``X-Belegt-Actor: user:<id>`` 映射为静态令牌表中
该执行者的令牌（注入 ``Authorization: Bearer …``）；表中没有的执行者仍返回 401。
"""
from __future__ import annotations

import mimetypes
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse

from ..authz import Actor
from ..errors import GraphError
from ..store import GraphStore
from . import current_actor, get_store

workflow_router = APIRouter(tags=["workflow"])
# ``POST /blobs`` 单独成一个路由器：core 的 graph_router 先挂载且只返回 ``sha256``，app_factory 把本路由器放到最前面
blobs_router = APIRouter(tags=["workflow"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 01 §3.5 错误态"上传 > 25 MB"
CREATE_ROLES = ("compliance_owner", "org_admin")
DRIVE_ROLES = ("compliance_owner", "org_admin")
RESUME_ROLES = ("compliance_owner", "org_admin")  # 04 §3.2 T21 user[org_admin,compliance_owner]
MIME_BY_SUFFIX = {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".csv": "text/csv",
                  ".xls": "application/vnd.ms-excel", ".pdf": "application/pdf", ".json": "application/json"}
# 估算剩余分钟（UI-04 "预计剩余分钟"；启发式：卡片 1 min、审批按级别、冲突 5 min）
MINUTES_PER = {"card": 1.0, "conflict": 5.0, "L0": 0.1, "L1": 0.5, "L2": 3.0, "L3": 8.0}
SIGNAL_LOCAL_NAMES = {"supplier_responded": "supplier_replied"}


class WorkflowAPIError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}

    def to_response(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "rule_id": None, "path": None, "details": self.details}}


@dataclass
class WorkflowServices:
    """每个应用一份：租户 ``Services``、执行引擎（``local`` LocalRunner / ``temporal`` TemporalEngine）。"""

    svc: Any
    mode: str = "local"
    runner: Any = None
    engine: Any = None
    engine_factory: Callable[[], Any] | None = None   # async () -> TemporalEngine（首个请求时连接）
    background: bool = True                            # local：上传后在后台推进（False = 请求内同步推进）
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def store(self) -> GraphStore:
        return self.svc.store

    @property
    def org_id(self) -> str:
        return self.svc.org_id

    async def temporal(self) -> Any:
        if self.engine is None:
            if self.engine_factory is None:
                raise WorkflowAPIError(503, "WORKFLOW_UNAVAILABLE", "temporal engine not configured")
            try:
                self.engine = await self.engine_factory()
            except Exception as e:  # noqa: BLE001
                raise WorkflowAPIError(503, "WORKFLOW_UNAVAILABLE", f"temporal unreachable: {type(e).__name__}: {e}") from e
        return self.engine

    def drive_local(self, req_ref: str) -> dict[str, Any]:
        with self.lock:  # LocalRunner / Services 不是线程安全的：同一应用内串行推进
            return self.runner.run(req_ref).to_dict()


def configure_workflow(app: Any, svc: Any, *, engine: str = "local", runner: Any = None, temporal_engine: Any = None,
                       engine_factory: Callable[[], Any] | None = None, background: bool = True) -> WorkflowServices:
    if engine not in ("local", "temporal"):
        raise ValueError(f"unknown workflow engine {engine!r}")
    if getattr(app.state, "store", None) is not None and app.state.store is not svc.store:
        raise ValueError("workflow Services must share the app's GraphStore")
    if engine == "local" and runner is None:
        from ..workflow.local import LocalRunner

        runner = LocalRunner(svc)
    ws = WorkflowServices(svc=svc, mode=engine, runner=runner, engine=temporal_engine, engine_factory=engine_factory,
                          background=background)
    app.state.workflow = ws
    if not getattr(app.state, "_workflow_handler", False):
        @app.exception_handler(WorkflowAPIError)
        async def _wf_error(_: Request, e: WorkflowAPIError) -> JSONResponse:
            return JSONResponse(status_code=e.status, content=e.to_response())

        app.state._workflow_handler = True
    return ws


def get_workflow(request: Request) -> WorkflowServices:
    ws = getattr(request.app.state, "workflow", None)
    if ws is None:
        raise GraphError("GRAPH_NOT_FOUND", "workflow engine not configured (configure_workflow)")
    return ws


def _require(actor: Actor, roles: tuple[str, ...]) -> None:
    if actor.is_service or actor.has_role(*roles):
        return
    raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"requires one of {list(roles)}", details={"required_role": list(roles)})


def _same_tenant(ws: WorkflowServices, actor: Actor) -> None:
    if actor.org_id != ws.org_id:
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "workflow services are configured for another organization")


def _req_or_404(store: GraphStore, actor: Actor, req_id: str) -> dict[str, Any]:
    r = store.get(req_id, actor=actor) if req_id.startswith("req_") else None
    if r is None:
        raise GraphError("GRAPH_NOT_FOUND", f"{req_id} not found")
    return r


# ---------------------------------------------------------------------------- 视图
class _Index:
    """一次读取当前租户的 Question/Answer/Approval/CQ/Conflict，按 Request 分组（列表页不做 N+1 查询）。"""

    def __init__(self, store: GraphStore, actor: Actor) -> None:
        self.questions: dict[str, list[dict[str, Any]]] = {}
        for q in store.query("Question", actor=actor):
            self.questions.setdefault(q.get("request_id") or "", []).append(q)
        self.answers_by_q: dict[str, list[dict[str, Any]]] = {}
        for a in store.query("Answer", actor=actor):
            for qr in a.get("question_refs") or []:
                self.answers_by_q.setdefault(qr.split("#")[0], []).append(a)
        self.approvals = store.query("Approval", actor=actor, status="pending")
        self.cqs = store.query("ClosedQuestion", actor=actor, status="open")
        self.conflicts = store.query("Conflict", actor=actor, status="open")
        self.cps = {c["id"]: c for c in store.query("Counterparty", actor=actor)}


ANSWER_RANK = {"submitted": 5, "approved": 4, "in_review": 3, "draft": 2, "rejected": 1, "superseded": 0}


def _effective(answers: list[dict[str, Any]]) -> dict[str, Any] | None:
    live = [a for a in answers if a.get("status") != "superseded"] or answers
    return max(live, key=lambda a: (ANSWER_RANK.get(a.get("status"), 0), a.get("created_at", ""))) if live else None


def _progress(ix: _Index, req: dict[str, Any]) -> dict[str, Any]:
    qs = ix.questions.get(req["id"], [])
    eff = [_effective(ix.answers_by_q.get(q["id"], [])) for q in qs]
    answer_ids = {a["id"] for a in eff if a}
    pend = [a for a in ix.approvals if a.get("request_id") == req["id"] or a.get("subject_ref", "").split("#")[0] in answer_ids]
    cards = [c for c in ix.cqs if c.get("request_id") == req["id"]]
    conflicts = [c for c in ix.conflicts if c.get("request_id") == req["id"] or
                 any(s.split("#")[0] in answer_ids for s in c.get("subject_refs") or [])]
    est = len(cards) * MINUTES_PER["card"] + len(conflicts) * MINUTES_PER["conflict"] + \
        sum(MINUTES_PER.get(a.get("risk_level") or "L1", 1.0) for a in pend)
    return {
        "questions": len(qs),
        "mapped": sum(1 for q in qs if q.get("status") in ("mapped", "answered")),
        "evidenced": sum(1 for a in eff if a and (a.get("evidence_refs") or a.get("calculation_ref"))),
        "approved": sum(1 for a in eff if a and a.get("status") in ("approved", "submitted")),
        "submitted": sum(1 for a in eff if a and a.get("status") == "submitted"),
        "est_minutes_remaining": int(round(est)),
        "open_conflicts": len(conflicts),
        "open_cards": len(cards),
    }


def request_view(ix: _Index, req: dict[str, Any]) -> dict[str, Any]:
    cp = ix.cps.get(req.get("counterparty_id") or "") or {}
    return {"request": req, "counterparty_name": cp.get("legal_name") or req.get("counterparty_id") or "",
            "progress": _progress(ix, req)}


def question_rows(store: GraphStore, actor: Actor, req_id: str) -> list[dict[str, Any]]:
    ix = _Index(store, actor)
    pend = {a.get("subject_ref", "").split("#")[0]: a for a in ix.approvals}
    rows = []
    for q in sorted(ix.questions.get(req_id, []), key=lambda q: q.get("ordinal", 0)):
        a = _effective(ix.answers_by_q.get(q["id"], []))
        row: dict[str, Any] = {"question": q, "evidence_count": 0}
        if a is not None:
            row["answer"] = a
            row["evidence_count"] = len(a.get("evidence_refs") or []) + (1 if a.get("calculation_ref") else 0)
            rl = a.get("risk_level") or (pend.get(a["id"]) or {}).get("risk_level")
            if rl:
                row["risk_level"] = rl
        rows.append(row)
    return rows


async def workflow_status(ws: WorkflowServices, req_id: str) -> dict[str, Any]:
    if ws.mode == "temporal":
        try:
            eng = await ws.temporal()
            st = await eng.status(req_id)
            return {"engine": "temporal", **st}
        except WorkflowAPIError:
            raise
        except Exception as e:  # noqa: BLE001 - 工作流不存在 / worker 未运行
            return {"engine": "temporal", "state": "unknown", "error": f"{type(e).__name__}"}
    from ..workflow.local import LocalRunner

    p = ws.runner.progress(req_id) if ws.runner is not None else {}
    return {"engine": "local", "workflow_id": LocalRunner.workflow_id(req_id), "state": p.get("workflow_state"),
            "pending": p.get("pending") or [], "progress": p.get("summary") or {}}


# ---------------------------------------------------------------------------- 上传
# 浏览器/操作系统给 .csv 报的类型五花八门（application/vnd.ms-excel、text/plain、application/octet-stream…）；
# 问卷解析按 MIME 选择解析器，因此可解析的扩展名以扩展名为准（否则 intake 会判为"附件不可解析"）。
_PARSABLE_SUFFIX = (".xlsx", ".csv", ".pdf")


def _mime_for(name: str, declared: str | None) -> str:
    declared = (declared or "").split(";")[0].strip().lower()
    ext = os.path.splitext(name or "")[1].lower()
    by_ext = MIME_BY_SUFFIX.get(ext)
    if by_ext and ext in _PARSABLE_SUFFIX and declared != by_ext:
        return by_ext
    if declared and declared != "application/octet-stream":
        return declared
    return by_ext or mimetypes.guess_type(name or "")[0] or "application/octet-stream"


async def _read_upload(request: Request) -> tuple[list[tuple[bytes, str, str]], dict[str, str]]:
    ctype = request.headers.get("content-type", "")
    files: list[tuple[bytes, str, str]] = []
    fields: dict[str, str] = {}
    if ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        form = await request.form()
        for key, val in form.multi_items():
            if hasattr(val, "read") and hasattr(val, "filename"):
                data = await val.read()
                name = val.filename or "upload"
                mime = _mime_for(name, val.content_type)
                files.append((data, name, mime))
            else:
                fields[key] = str(val)
    else:
        data = await request.body()
        name = request.headers.get("x-filename") or "upload"
        mime = _mime_for(name, ctype)
        if data:
            files.append((data, name, mime))
    for data, name, _ in files:
        if len(data) > MAX_UPLOAD_BYTES:
            raise GraphError("GRAPH_SCHEMA_INVALID", f"{name}: file larger than 25 MB", path="/file",
                             details={"bytes": len(data), "max_bytes": MAX_UPLOAD_BYTES})
    return files, fields


@blobs_router.post("/blobs", status_code=201)
async def post_blob(request: Request, actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    files, _ = await _read_upload(request)
    if not files:
        raise GraphError("GRAPH_SCHEMA_INVALID", "empty upload", path="/file")
    data, name, mime = files[0]
    sha = store.put_blob(actor.org_id, data, mime, uploaded_by=actor.id)
    return {"sha256": sha, "blob_sha256": sha, "bytes": len(data), "mime": mime, "filename": name}


@workflow_router.post("/requests", status_code=201)
async def create_request(request: Request, background: BackgroundTasks, actor: Actor = Depends(current_actor),
                         ws: WorkflowServices = Depends(get_workflow)) -> dict[str, Any]:
    from ..agents.intake_triage import IntakeInput, IntakeTriage

    _require(actor, CREATE_ROLES)
    _same_tenant(ws, actor)
    files, fields = await _read_upload(request)
    if not files:
        raise GraphError("GRAPH_SCHEMA_INVALID", "questionnaire file required (multipart field 'file')", path="/file")
    st = ws.store
    shas = [st.put_blob(actor.org_id, data, mime, uploaded_by=actor.id) for data, _, mime in files]
    channel = fields.get("channel") or "upload"
    if channel not in ("email", "portal", "upload", "api", "a2a"):
        raise GraphError("GRAPH_SCHEMA_INVALID", f"unknown channel {channel!r}", path="/channel")
    inp = IntakeInput(shas, channel=channel, subject=fields.get("subject") or "", body=fields.get("body") or "",
                      sender=fields.get("sender") or None, message_id=fields.get("message_id") or None,
                      filenames=[n for _, n, _ in files], counterparty_id=fields.get("counterparty_id") or None,
                      title=fields.get("title") or None)

    def triage() -> str:
        with ws.lock:
            return IntakeTriage(ws.svc).run(inp, idem_prefix=f"intake:{shas[0][7:31]}").refs[0]

    req_ref = await anyio.to_thread.run_sync(triage)
    req_id = req_ref.split("#")[0]
    wf: dict[str, Any]
    if ws.mode == "temporal":
        eng = await ws.temporal()
        wid = await eng.start(req_ref)
        wf = {"engine": "temporal", "workflow_id": wid, "state": "running"}
    elif ws.background:
        background.add_task(ws.drive_local, req_ref)
        from ..workflow.local import LocalRunner

        wf = {"engine": "local", "workflow_id": LocalRunner.workflow_id(req_id), "state": "scheduled"}
    else:
        res = await anyio.to_thread.run_sync(ws.drive_local, req_ref)
        wf = {"engine": "local", **res}
    return {"request": st.get(req_id, actor=actor), "request_ref": req_ref, "blob_sha256": shas, "workflow": wf}


# ---------------------------------------------------------------------------- 通用对象列表（审批台：供应商追数、证据库）
LISTABLE_TYPES = ("SupplierRequest", "Evidence", "Counterparty", "Site", "Product", "Request", "Question", "Answer", "Approval",
                  "ClosedQuestion", "Conflict", "Calculation", "Mapping")


@workflow_router.get("/graph/objects")
def list_objects(type: str, status: str | None = None, view: str | None = None, actor: Actor = Depends(current_actor),
                 store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    """``GET /graph/objects?type=SupplierRequest&view=timeline`` / ``?type=Evidence&status=verified,expired``（web/README）。
    读取经 ``actor`` 过滤（RLS 模拟：租户、信任区、cp_scope、approved_only）；``status`` 可逗号分隔。"""
    if type not in LISTABLE_TYPES:
        raise GraphError("GRAPH_SCHEMA_INVALID", f"type must be one of {list(LISTABLE_TYPES)}", path="/type")
    wanted = {x.strip() for x in (status or "").split(",") if x.strip()}
    objs = [o for o in store.query(type, actor=actor) if not wanted or o.get("status") in wanted]
    objs.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    if type == "SupplierRequest" and view == "timeline":
        cps = {c["id"]: c for c in store.query("Counterparty", actor=actor)}
        items = []
        for o in objs:
            hist = [{"version": v["version"], "status": v.get("status"), "created_at": v.get("created_at"),
                     "reminder_count": int(v.get("reminder_count") or 0), "created_by": v.get("created_by")}
                    for v in store.history(o["id"], actor=actor)]
            items.append({"supplier_request": o, "supplier_name": (cps.get(o.get("counterparty_id")) or {}).get("legal_name")
                          or o.get("counterparty_id"), "history": hist})
        return {"items": items}
    return {"items": objs}


# ---------------------------------------------------------------------------- 列表 / 详情
@workflow_router.get("/requests")
def list_requests(status: str | None = None, actor: Actor = Depends(current_actor),
                  store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    ix = _Index(store, actor)
    reqs = store.query("Request", actor=actor, **({"status": status} if status else {}))
    reqs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {"items": [request_view(ix, r) for r in reqs]}


@workflow_router.get("/requests/{req_id}")
async def get_request(req_id: str, request: Request, actor: Actor = Depends(current_actor),
                      store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    req = _req_or_404(store, actor, req_id)
    ix = _Index(store, actor)
    out = request_view(ix, req)
    out["questions"] = sorted(ix.questions.get(req_id, []), key=lambda q: q.get("ordinal", 0))
    ws = getattr(request.app.state, "workflow", None)
    if ws is not None and actor.org_id == ws.org_id:
        out["workflow"] = await workflow_status(ws, req_id)
    return out


@workflow_router.get("/requests/{req_id}/questions")
def get_request_questions(req_id: str, actor: Actor = Depends(current_actor),
                          store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    _req_or_404(store, actor, req_id)
    return {"items": question_rows(store, actor, req_id)}


# ---------------------------------------------------------------------------- 推进 / 恢复 / 取消
@workflow_router.post("/requests/{req_id}/advance")
async def advance(req_id: str, request: Request, actor: Actor = Depends(current_actor),
                  ws: WorkflowServices = Depends(get_workflow)) -> dict[str, Any]:
    _require(actor, DRIVE_ROLES)
    _same_tenant(ws, actor)
    req = _req_or_404(ws.store, actor, req_id)
    body: dict[str, Any] = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        raw = await request.body()
        if raw.strip():
            body = await request.json()
    signal = body.get("signal")
    payload = dict(body.get("payload") or {})
    payload.setdefault("by", actor.id)
    if ws.mode == "temporal":
        eng = await ws.temporal()
        await eng.signal(req_id, signal or "human_answered", payload)
        return {"request_id": req_id, "signal": signal or "human_answered", "workflow": await workflow_status(ws, req_id)}
    if signal:
        ws.runner.journal.add_signal(ws.runner.workflow_id(req_id), SIGNAL_LOCAL_NAMES.get(signal, signal), payload)
    res = await anyio.to_thread.run_sync(ws.drive_local, f"{req['id']}#v{req['version']}")
    return {"request_id": req_id, "signal": signal, "workflow": {"engine": "local", **res}}


@workflow_router.post("/requests/{req_id}/resume")
async def resume(req_id: str, request: Request, actor: Actor = Depends(current_actor),
                 ws: WorkflowServices = Depends(get_workflow)) -> dict[str, Any]:
    if not (actor.is_user and actor.has_role(*RESUME_ROLES)):
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "resume requires org_admin or compliance_owner (T21)",
                         details={"required_role": list(RESUME_ROLES)})
    _same_tenant(ws, actor)
    req = _req_or_404(ws.store, actor, req_id)
    if req["status"] != "halted":
        raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"{req_id} is {req['status']}, not halted", path="/status")
    body: dict[str, Any] = {}
    raw = await request.body()
    if raw.strip():
        body = await request.json()
    patch = body.get("budget_patch") or None
    if patch is not None and not (isinstance(patch, dict) and all(isinstance(v, dict) for v in patch.values())):
        raise GraphError("GRAPH_SCHEMA_INVALID", 'budget_patch must look like {"request": {"cost_eur": "8"}}', path="/budget_patch")
    if ws.mode == "temporal":
        from ..statemachines import parse_status_reason

        info = parse_status_reason(req.get("status_reason")) or {}

        def write_back() -> None:
            with ws.lock:
                ws.store.write("Request", {"id": req_id, "status": info.get("return_to", "parsed")}, expected_version=req["version"],
                               actor=actor, patch_mode="merge_from_head")
                ws.svc.guard.release(req_id, patch)

        await anyio.to_thread.run_sync(write_back)
        eng = await ws.temporal()
        await eng.signal(req_id, "resume", {"by": actor.id, "budget_patch": patch or {}})
        return {"request_id": req_id, "workflow": await workflow_status(ws, req_id)}

    def go() -> dict[str, Any]:
        with ws.lock:
            return ws.runner.resume(req_id, actor, patch).to_dict()

    return {"request_id": req_id, "workflow": {"engine": "local", **await anyio.to_thread.run_sync(go)}}


@workflow_router.post("/requests/{req_id}/cancel")
async def cancel(req_id: str, actor: Actor = Depends(current_actor), ws: WorkflowServices = Depends(get_workflow)) -> dict[str, Any]:
    _require(actor, DRIVE_ROLES)
    _same_tenant(ws, actor)
    req = _req_or_404(ws.store, actor, req_id)
    if ws.mode == "temporal":
        def write() -> None:
            with ws.lock:
                ws.store.write("Request", {"id": req_id, "status": "cancelled"}, expected_version=req["version"], actor=actor,
                               patch_mode="merge_from_head")

        await anyio.to_thread.run_sync(write)
        eng = await ws.temporal()
        await eng.signal(req_id, "cancel", {"by": actor.id})
        return {"request_id": req_id, "workflow": await workflow_status(ws, req_id)}

    def go() -> dict[str, Any]:
        with ws.lock:
            res = ws.runner.cancel(req_id, actor).to_dict()
            # LocalRunner 只在下一个 Activity 前检查取消；挂在人工节点时重放会再次停在等待点 → 以图状态为准
            if ws.store.get(req_id, org_id=ws.org_id)["status"] == "cancelled" and res.get("state") != "cancelled":
                ws.runner.journal.save_run(ws.runner.workflow_id(req_id), request_id=req_id, org_id=ws.org_id, state="cancelled",
                                           pending=[])
                res.update(state="cancelled", pending=[])
            return res

    return {"request_id": req_id, "workflow": {"engine": "local", **await anyio.to_thread.run_sync(go)}}


# ---------------------------------------------------------------------------- 开发认证（X-Belegt-Actor）
class DevActorMiddleware:
    """``BELEGT_DEV_AUTH=1`` 时：无 ``Authorization`` 而有 ``X-Belegt-Actor``（或配置了默认执行者）→ 注入该执行者在静态令牌表中的
    Bearer 令牌。只做映射，不创造身份：表中没有的执行者照常 401（07 §4.4：生产以 OIDC 为准）。"""

    def __init__(self, app: Any, *, default_actor: str | None = None) -> None:
        self.app = app
        self.default_actor = default_actor

    @staticmethod
    def token_for(tokens: Any, actor_id: str) -> str | None:
        table = getattr(tokens, "table", None) or {}
        for tok, e in table.items():
            if e.get("actor") == actor_id:
                return tok
        return None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = list(scope.get("headers") or [])
            names = {k.lower() for k, _ in headers}
            if b"authorization" not in names:
                who = next((v.decode("latin-1").strip() for k, v in headers if k.lower() == b"x-belegt-actor"), None) or \
                    self.default_actor
                app = scope.get("app")
                tokens = getattr(getattr(app, "state", None), "tokens", None) if app is not None else None
                if tokens is None:
                    tokens = getattr(self, "_tokens", None)
                tok = self.token_for(tokens, who) if who and tokens is not None else None
                if tok:
                    scope = dict(scope, headers=headers + [(b"authorization", f"Bearer {tok}".encode("latin-1"))])
        await self.app(scope, receive, send)


def dev_auth_enabled() -> bool:
    return os.environ.get("BELEGT_DEV_AUTH", "") == "1"


__all__ = ["workflow_router", "blobs_router", "configure_workflow", "get_workflow", "WorkflowServices", "WorkflowAPIError", "DevActorMiddleware",
           "dev_auth_enabled", "request_view", "question_rows", "MAX_UPLOAD_BYTES"]
