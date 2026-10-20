# Spec: 01 §3（审批收件箱 UI-01、封闭式卡片 UI-02、冲突处理 UI-03、有效期视图）、06 §3–§5、§7、03 §3.8/§3.9/§3.11、
#       04 §2.16（人工节点）；端点形状兼容 code/belegt/web/README.md "后端接口约定"（列表兼容 {items:[...]}）
"""审批台路由 ``review_router``（挂载：``create_app(store, tokens, routers=[review_router])``）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | ``/inbox`` | ``{items: InboxItem[]}``：pending Answer 审批 + 钉住 Answer + 侧栏对象（web ``InboxItem``） |
| POST | ``/approvals`` | 单条 ``{approval_id, decision, comment?, confirmations?, modification?}``；批量 ``{batch_id?, approval_ids[], decision:"approved", viewed_approval_ids?, confirmations?, immediate?}`` → 服务端暂存（202）；首次外发 ``{subject_id:"sreq_…", kind:"supplier_request_first_send", decision}`` |
| POST | ``/approvals/undo`` | ``{batch_id, approval_ids?}`` 窗口内撤销（整批或剔除部分） |
| GET | ``/approvals/batches/{batch_id}`` | 批次状态（staged/committed/voided/undone） |
| POST | ``/approvals/batch-check`` | 只判定 06 §5.1 条件（界面决定是否显示"一键批准"） |
| POST | ``/approvals/route`` | 触发 approval-router 定级路由 |
| GET | ``/approvals/escalations`` | SLA 提醒/升级清单（永不自动批准） |
| GET | ``/closed-questions`` | ``{items: ClosedQuestion[]}``（按角色/指派过滤；``status`` 可选） |
| POST | ``/closed-questions/{id}/answer`` | ``{choice?, bool?, corrected_value?, blob_sha256?[]}`` |
| GET | ``/conflicts`` | ``{items: ConflictView[]}``（``status`` 默认 open） |
| POST | ``/conflicts/{id}/resolve`` | ``{kind, note, base_side?, dismiss_reason?, divergence?}`` |
| GET | ``/expiry`` | ``{items: ExpiryAlert[]}``（06 §7 梯度） |
| POST | ``/consistency/run`` | 触发 T1–T4 审计；``POST /requests/{id}/gate`` 门禁 |
| POST | ``/questions/{id}/legal-flag/cancel`` | ``{reason}`` 取消法律标记（compliance_owner） |
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from ..authz import Actor
from ..errors import GraphError
from ..store import GraphStore, WriteItem
from . import current_actor, get_store

review_router = APIRouter(tags=["review"])


class ReviewServices:
    """每个应用一份：引擎、路由器、批次暂存（可注入时钟/策略/解释器）。"""

    def __init__(self, store: GraphStore, *, clock: Callable[[], dt.datetime] | None = None, policy: Any = None,
                 explainer: Any = None, config: Any = None, tier_a_customers: set[str] | None = None) -> None:
        from ..approval import ApprovalRouter, BatchStager
        from ..consistency import ConsistencyEngine

        self.store = store
        self.engine = ConsistencyEngine(store, explainer=explainer, config=config)
        self.router = ApprovalRouter(store, policy)
        self.stager = BatchStager(store, self.router, self.engine, clock=clock)
        self.tier_a_customers = tier_a_customers or set(self.router.policy.raw.get("customer_tiers", {}).get("tier_a") or [])


def configure_review(app: Any, **kw: Any) -> ReviewServices:
    svc = ReviewServices(app.state.store, **kw)
    app.state.review = svc
    return svc


def get_review(request: Request) -> ReviewServices:
    svc = getattr(request.app.state, "review", None)
    if svc is None or svc.store is not request.app.state.store:
        svc = configure_review(request.app)
    return svc


def _ctx(svc: ReviewServices, actor: Actor):
    from ..consistency import AuditContext

    return AuditContext.from_store(svc.store, actor.org_id, meta=svc.router.meta)


def _require(actor: Actor, *roles: str, services: tuple[str, ...] = ()) -> None:
    if actor.id in services or actor.has_role(*roles):
        return
    raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"requires one of {list(roles) + list(services)}",
                     details={"required_role": list(roles)})


def _visible(store: GraphStore, actor: Actor, ref: str | None) -> dict[str, Any] | None:
    return store.get(ref, actor=actor) if ref else None


# ====================================================================== 收件箱
@review_router.get("/inbox")
def inbox(actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store),
          svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    from ..approval.router import sla_due
    from ..consistency.context import parse_date

    svc.stager.settle()
    ctx = _ctx(svc, actor)
    staged = {aid: b.batch_id for b in svc.stager.batches(actor.org_id) if b.status == "staged" for aid in b.items}
    items = []
    for apr in ctx.heads("Approval", status="pending"):
        subj = apr.get("subject_ref", "")
        if not subj.startswith("ans_"):
            continue
        if not (apr.get("assignee") == actor.id or set(apr.get("required_role") or []) & set(actor.roles)):
            continue
        ans = _visible(store, actor, subj)
        if ans is None:
            continue
        head = ctx.head(subj) or ans
        qs = [q for q in (_visible(store, actor, q) for q in ans.get("question_refs") or []) if q]
        evs = [e for e in (_visible(store, actor, r) for r in ctx.cited_evidence(ans)) if e]
        calc = _visible(store, actor, ans.get("calculation_ref"))
        req = ctx.request_of(ans)
        item: dict[str, Any] = {
            "approval": apr, "answer": ans, "head_version": head["version"], "questions": qs, "evidence": evs,
            "conflicts": [c for c in ctx.conflicts_for(ans["id"]) if c.get("status") == "open"],
            "customer_tier": svc.router.policy.customer_tier(sorted(ctx.customers_of(ans))),
            "request_title": (req or {}).get("title", ""),
            "ui_sla_due": sla_due(svc.router.policy, apr["risk_level"], parse_date(apr.get("created_at")) or ctx.today,
                                  (req or {}).get("due_date")).isoformat(),
        }
        if calc:
            item["calculation"] = calc
        if (req or {}).get("due_date"):
            item["request_due_date"] = req["due_date"]  # type: ignore[index]
        if apr["id"] in staged:
            item["ui_staged_batch"] = staged[apr["id"]]
        if apr["risk_level"] == "L3":
            from ..legal import legal_types, notice_text

            types = sorted({t for q in qs for t in legal_types(q)})
            if types or ans.get("legal_commitment"):
                item["ui_legal_notice"] = notice_text(types)
                item["ui_legal_types"] = types
        items.append(item)
    order = {"L3": 0, "L2": 1, "L1": 2, "L0": 3}
    items.sort(key=lambda i: (order.get(i["approval"]["risk_level"], 9), i.get("request_due_date") or "9999",
                              i["approval"]["id"]))
    return {"items": items}


# ====================================================================== 审批
@review_router.post("/approvals")
def post_approval(body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor),
                  svc: ReviewServices = Depends(get_review)) -> Any:
    svc.stager.settle()
    if body.get("kind") == "supplier_request_first_send" or str(body.get("subject_id", "")).startswith("sreq_"):
        if not body.get("subject_id"):
            raise GraphError("GRAPH_SCHEMA_INVALID", "subject_id (sreq_…) required", path="/subject_id")
        return svc.router.decide_supplier_first_send(body["subject_id"], actor=actor,
                                                     decision=body.get("decision", "approved"), comment=body.get("comment"))
    if "approval_ids" in body or "batch_id" in body:
        if body.get("decision", "approved") != "approved":
            raise GraphError("GRAPH_SCHEMA_INVALID", "batch decisions can only approve (06 §5)", path="/decision")
        ids = body.get("approval_ids") or []
        b = svc.stager.stage(actor, ids, batch_id=body.get("batch_id"),
                             viewed=body.get("viewed_approval_ids") or [], confirmations=body.get("confirmations") or {},
                             immediate=bool(body.get("immediate")))
        return JSONResponse(status_code=200 if b.status == "committed" else 202, content={"batch": b.to_dict()})
    if not body.get("approval_id"):
        raise GraphError("GRAPH_SCHEMA_INVALID", "approval_id, approval_ids or subject_id required", path="/approval_id")
    return svc.router.decide(body["approval_id"], actor=actor, decision=body.get("decision", ""),
                             comment=body.get("comment"), confirmations=body.get("confirmations"),
                             modification=body.get("modification"))


@review_router.post("/approvals/undo")
def undo_batch(body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor),
               svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    if not body.get("batch_id"):
        raise GraphError("GRAPH_SCHEMA_INVALID", "batch_id required", path="/batch_id")
    svc.stager.settle()
    return {"batch": svc.stager.undo(actor, body["batch_id"], body.get("approval_ids")).to_dict()}


@review_router.get("/approvals/batches/{batch_id}")
def get_batch(batch_id: str, actor: Actor = Depends(current_actor), svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    svc.stager.settle()
    b = svc.stager.get(batch_id)
    if b is None or b.org_id != actor.org_id:
        raise GraphError("GRAPH_NOT_FOUND", f"batch {batch_id} not found")
    return {"batch": b.to_dict()}


@review_router.post("/approvals/batch-check")
def batch_check(body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor),
                svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    return svc.stager.check(actor, body.get("approval_ids") or [], viewed=body.get("viewed_approval_ids") or [],
                            confirmations=body.get("confirmations") or {}).to_dict()


@review_router.post("/approvals/route")
def route(body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor),
          svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    _require(actor, "compliance_owner", services=("service:workflow", "agent:approval-router"))
    return svc.router.route(actor.org_id, request_id=body.get("request_id"), answer_refs=body.get("answer_refs") or [],
                            assignee=body.get("assignee")).to_dict()


@review_router.get("/approvals/escalations")
def escalations(actor: Actor = Depends(current_actor), svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    return {"items": svc.router.escalations(actor.org_id)}


# ====================================================================== 封闭式卡片
@review_router.get("/closed-questions")
def list_closed_questions(status: str | None = None, actor: Actor = Depends(current_actor),
                          store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    cqs = store.query("ClosedQuestion", actor=actor)
    if status:
        wanted = set(status.split(","))
        cqs = [c for c in cqs if c.get("status") in wanted]
    if not actor.has_role("compliance_owner"):
        cqs = [c for c in cqs if c.get("assignee") == actor.id or c.get("assignee_role") in actor.roles]
    return {"items": cqs}


@review_router.post("/closed-questions/{cq_id}/answer")
def answer_closed_question(cq_id: str, body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor),
                           store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    from ..ids import new_id

    cq = store.get(cq_id, actor=actor)
    if cq is None or not cq_id.startswith("cq_"):
        raise GraphError("GRAPH_NOT_FOUND", f"{cq_id} not found")
    if cq.get("status") != "open":
        raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"closed question is {cq.get('status')}", path="/status")
    now = store.peek_now()
    if cq.get("expires_at") and cq["expires_at"] < now:
        raise GraphError("GRAPH_ILLEGAL_TRANSITION", "closed question has expired", path="/expires_at")
    kind = cq["kind"]
    ans: dict[str, Any] = {"answered_by": actor.id, "answered_at": now}
    items: list[WriteItem] = []
    created: dict[str, str] = {}
    cq_next = f"{cq['id']}#v{cq['version'] + 1}"
    if kind == "single_choice":
        if body.get("choice") not in {o["key"] for o in cq.get("options") or []}:
            raise GraphError("GRAPH_SCHEMA_INVALID", "choice must be one of options[].key", path="/choice")
        ans["choice"] = body["choice"]
    elif kind == "yes_no":
        if not isinstance(body.get("bool"), bool):
            raise GraphError("GRAPH_SCHEMA_INVALID", "yes_no requires bool", path="/bool")
        ans["bool"] = body["bool"]
    elif kind == "numeric_confirm":
        cv = body.get("corrected_value")
        if cv not in (None, ""):
            from ..canonical import normalize_dec

            ncv = normalize_dec(str(cv))
            if not isinstance(ncv, str) or not ncv.replace("-", "", 1).replace(".", "", 1).isdigit():
                raise GraphError("GRAPH_SCHEMA_INVALID", "corrected_value must be DEC-STR", path="/corrected_value")
            pv = cq.get("proposed_value") or {}
            src = store.get(pv.get("evidence_ref"), actor=actor) if pv.get("evidence_ref") else None
            if src is None:
                raise GraphError("GRAPH_REF_NOT_FOUND", "proposed_value.evidence_ref not found", path="/corrected_value")
            ans.update({"bool": False, "corrected_value": ncv})
            ev_id = new_id("Evidence")
            ev = {"id": ev_id, "source_type": "human_confirmation", "fact_key": src["fact_key"], "extracted_value": ncv,
                  "value_type": "number", "unit": pv.get("unit") or src.get("unit"), "boundary": src["boundary"],
                  "locator": {"cq_ref": cq_next}, "valid_from": src["valid_from"], "confidence": 1, "status": "verified",
                  "trust_zone": "trusted", "corrects": pv["evidence_ref"],
                  "verification": {"method": "human_review", "by": actor.id}}
            for k in ("datapoint_id", "period", "valid_until"):
                if src.get(k):
                    ev[k] = src[k]
            items.append(WriteItem("Evidence", ev))
            created["evidence"] = f"{ev_id}#v1"
        else:
            if not isinstance(body.get("bool"), bool):
                raise GraphError("GRAPH_SCHEMA_INVALID", "numeric_confirm requires bool or corrected_value", path="/bool")
            ans["bool"] = body["bool"]
    elif kind == "file_request":
        blobs = body.get("blob_sha256") or []
        if blobs:
            for b in blobs:
                if store.get_blob(actor.org_id, b) is None:
                    raise GraphError("GRAPH_REF_NOT_FOUND", f"blob {b} not uploaded (POST /blobs)", path="/blob_sha256")
            ans["blob_sha256"] = list(blobs)
        elif body.get("bool") is False:
            ans["bool"] = False
        else:
            raise GraphError("GRAPH_SCHEMA_INVALID", "file_request requires blob_sha256[] (≥1) or bool=false", path="/blob_sha256")
    items.insert(0, WriteItem("ClosedQuestion", {"id": cq["id"], "status": "answered", "answer": ans},
                              expected_version=cq["version"], patch_mode="merge_from_head"))
    refs = store.write_batch(items, actor=actor)
    return {"closed_question_ref": refs[0], "created": created}


# ====================================================================== 冲突
@review_router.get("/conflicts")
def list_conflicts(status: str | None = "open", actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store),
                   svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    from ..consistency.context import parse_date

    ctx = _ctx(svc, actor)
    wanted = set((status or "open").split(",")) if status != "all" else None
    out = []
    for c in ctx.heads("Conflict"):
        if wanted is not None and c.get("status") not in wanted:
            continue
        sides, questions = [], []
        for s in c.get("subject_refs") or []:
            if not s.startswith("ans_"):
                continue
            a = _visible(store, actor, s)
            if a is None:
                continue
            names = []
            cs = a.get("customer_scope")
            for cp in (cs if isinstance(cs, list) else []):
                h = ctx.head(cp)
                names.append((h or {}).get("legal_name", cp))
            side: dict[str, Any] = {"answer": a, "customer_name": ", ".join(names) if names else "all",
                                    "evidence_count": len(ctx.cited_evidence(a))}
            calc = ctx.get(a["calculation_ref"]) if a.get("calculation_ref") else None
            if calc:
                side["methodology_version"] = calc.get("methodology_version")
            sides.append(side)
            questions += [q for q in (_visible(store, actor, q) for q in a.get("question_refs") or []) if q]
        req = ctx.head(c["request_id"]) if c.get("request_id") else None
        due = parse_date((req or {}).get("due_date"))
        out.append({"conflict": c, "sides": sides, "questions": questions,
                    "ui_days_to_due": (due - ctx.today).days if due else None})
    # 06 §3.4：阻断提交的在前，距截止日近的在前
    out.sort(key=lambda v: (v["conflict"].get("severity") != "blocking",
                            v["ui_days_to_due"] if v["ui_days_to_due"] is not None else 10 ** 6, v["conflict"]["id"]))
    return {"items": out}


@review_router.post("/conflicts/{conflict_id}/resolve")
def resolve(conflict_id: str, body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor),
            store: GraphStore = Depends(get_store), svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    from ..consistency import resolve_conflict

    return resolve_conflict(store, conflict_id, actor=actor, kind=body.get("kind", ""), note=body.get("note", ""),
                            base_side=body.get("base_side"), dismiss_reason=body.get("dismiss_reason"),
                            divergence=body.get("divergence"), policy_version=svc.router.policy.version)


# ====================================================================== 有效期
@review_router.get("/expiry")
def expiry(today: str | None = None, actor: Actor = Depends(current_actor), store: GraphStore = Depends(get_store),
           svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    from ..expiry import scan_expiry

    alerts = scan_expiry(store, actor.org_id, today=today, meta=svc.router.meta, tier_a_customers=svc.tier_a_customers)
    return {"items": [a.to_dict() for a in alerts]}


# ====================================================================== 审计触发与法律标记
@review_router.post("/consistency/run")
def run_audit(body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor),
              svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    _require(actor, "compliance_owner", services=("service:workflow", "agent:consistency-auditor"))
    run = svc.engine.run(body.get("trigger", "answer_write"), org_id=actor.org_id, request_id=body.get("request_id"),
                         answer_refs=body.get("answer_refs") or [], evidence_refs=body.get("evidence_refs") or [],
                         submit_date=body.get("submit_date"))
    return run.summary()


@review_router.post("/requests/{request_id}/gate")
def gate(request_id: str, body: dict[str, Any] | None = Body(default=None), actor: Actor = Depends(current_actor),
         svc: ReviewServices = Depends(get_review)) -> dict[str, Any]:
    _require(actor, "compliance_owner", services=("service:workflow", "agent:consistency-auditor"))
    run = svc.engine.gate(actor.org_id, request_id, submit_date=(body or {}).get("submit_date"))
    return {**run.summary(), "gate_blocks": run.gate_blocks}


@review_router.post("/questions/{question_id}/legal-flag/cancel")
def cancel_legal(question_id: str, body: dict[str, Any] = Body(...), actor: Actor = Depends(current_actor),
                 store: GraphStore = Depends(get_store)) -> dict[str, Any]:
    from ..legal import cancel_legal_flag

    return cancel_legal_flag(store, question_id, actor=actor, reason=body.get("reason", ""))


__all__ = ["review_router", "ReviewServices", "configure_review", "get_review"]
