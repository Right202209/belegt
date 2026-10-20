# Spec: 06 §5.1（批量一键批准安全条件 1–6 与上限）、§5.2（10 分钟撤销窗口：延迟提交、暂存只存 ObjectRef、窗口结束复检
#       条件 2–4 后 graph.commit_batch 或整批作废；撤销/提交写 AuditEvent；due_date 剩余不足窗口可"立即提交"）、
#       §4.3（L0 ≤200、L1 ≤100、L2 ≤20 同一 Request + 每条看过详情 + 估算知悉）；CR-042（法律题撤出批次）；03 §3.8（batch_id）
"""批量批准：资格判定 + 服务端暂存（apb_ 批次）+ 撤销窗口。

    stager = BatchStager(store, router, engine, clock=clock)
    b = stager.stage(actor, approval_ids, viewed=…)        # status=staged，commit_at = now + 窗口
    stager.undo(actor, b.batch_id, approval_ids=[…])        # 窗口内整批或剔除部分条目
    stager.settle()                                          # 到期批次复检后提交（同事务 N×Approval + N×Answer）或作废
"""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from ulid import ULID

from ..authz import Actor
from ..errors import GraphError
from ..store import WriteItem
from ..consistency.context import AuditContext, parse_date, ref_of, split_ref
from ..legal.flags import question_legal_flag
from .policy import ApprovalPolicy

BATCHABLE = ("L0", "L1", "L2")


def new_batch_id() -> str:
    return f"apb_{ULID()}"


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@dataclass
class BatchItemCheck:
    approval_id: str
    approval_ref: str | None
    answer_ref: str | None
    risk_level: str | None
    ok: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class BatchCheck:
    ok: bool
    items: list[BatchItemCheck]
    reasons: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reasons": self.reasons, "summary": self.summary, "items": [asdict(i) for i in self.items]}


def check_batch(ctx: AuditContext, approval_ids: Iterable[str], approver: Actor, policy: ApprovalPolicy, *,
                viewed: Iterable[str] = (), confirmations: dict[str, Any] | None = None,
                pinned: dict[str, str] | None = None, due_reference: dt.date | None = None) -> BatchCheck:
    """06 §5.1 条件 1–5（条件 6 为界面展示，返回 ``summary``）。``pinned``：approval_id → 暂存时的 Answer ObjectRef。"""
    conf = dict(confirmations or {})
    viewed = set(viewed)
    items: list[BatchItemCheck] = []
    reasons: list[str] = []
    ids = list(dict.fromkeys(approval_ids))
    if not ids:
        return BatchCheck(False, [], ["empty batch"])
    per_level: dict[str, int] = {}
    l2_requests: set[str | None] = set()
    summary: dict[str, Any] = {"count": len(ids), "levels": {}, "datapoints": set(), "customers": set(), "estimates": 0}
    for aid in ids:
        apr = ctx.head(aid)
        it = BatchItemCheck(aid, ref_of(apr) if apr else None, None, apr.get("risk_level") if apr else None, True)
        items.append(it)
        if apr is None:
            it.ok, it.reasons = False, ["approval not found"]
            continue
        if apr.get("status") != "pending":
            it.reasons.append(f"approval is {apr.get('status')}")
        subj = apr.get("subject_ref", "")
        if not subj.startswith("ans_"):
            it.reasons.append("only Answer approvals can be batch-approved here")
            it.ok = False
            continue
        ans = ctx.get(subj)
        head = ctx.head(subj)
        it.answer_ref = subj
        lvl = apr.get("risk_level")
        # 条件 1：级别与法律题
        legal = bool((head or {}).get("legal_commitment")) or any(question_legal_flag(q) for q in ctx.questions_of(head or {}))
        if lvl not in BATCHABLE or not policy.batch_allowed(lvl):
            it.reasons.append(f"{lvl} cannot be batch-approved (R-APR)")
        if legal:
            it.reasons.append("legal commitment: never batched, approve individually (CR-042)")
        # 条件 4：钉住版本未变
        if head is None or ans is None or head["version"] != ans["version"]:
            it.reasons.append("answer changed since routing (stale)")
        if pinned is not None and pinned.get(aid) and pinned[aid] != (ref_of(head) if head else None):
            it.reasons.append("answer head changed since the batch was staged")
        if head is None:
            it.ok = False
            continue
        # 条件 2：无 open 冲突（L0/L1 连 warn 也不得有；L2 有 warn 须处置说明）
        for c in ctx.conflicts_for(head["id"], "open"):
            if c.get("severity") == "blocking":
                it.reasons.append(f"open blocking conflict {c['rule_id']}")
            elif lvl in ("L0", "L1"):
                it.reasons.append(f"open warning conflict {c['rule_id']} (not allowed for {lvl})")
            elif not (conf.get("warn_note") or "").strip():
                it.reasons.append(f"open warning conflict {c['rule_id']} requires warn_note")
        # 条件 3：引用证据在 due_date 时仍有效（CR-004、CR-013）
        req = ctx.request_of(head)
        ref_day = parse_date((req or {}).get("due_date")) or due_reference or ctx.today
        for r in ctx.cited_evidence(head):
            ev = ctx.head(split_ref(r)[0])
            if ev is None:
                it.reasons.append(f"evidence {r} unreadable")
                continue
            vu = parse_date(ev.get("valid_until"))
            if ev.get("status") in ("expired", "rejected") or (vu is not None and vu < ref_day):
                it.reasons.append(f"evidence {split_ref(r)[0]} not valid at due date {ref_day.isoformat()} (CR-004)")
        if head["datapoint_id"].startswith("dp:cert.") and head.get("value_type") == "date":
            need = [d for d in [(req or {}).get("due_date")] + [(q.get("period_requested") or {}).get("end")
                                                                 for q in ctx.questions_of(head)] if d]
            if need and str(head.get("value"))[:10] < max(need):
                it.reasons.append("certificate expires before due date / coverage (CR-013)")
        # 条件 5：角色
        need_roles = set(apr.get("required_role") or [])
        if not (approver.roles & need_roles) or not (approver.roles & set(policy.roles_for(lvl or "L3"))):
            it.reasons.append(f"approver lacks role for {lvl}")
        # L2 附加条件
        if lvl == "L2":
            l2_requests.add((req or {}).get("id"))
            if aid not in viewed and ref_of(apr) not in viewed:
                it.reasons.append("L2 requires the detail view to be opened at least once")
            if head.get("is_estimate") and not conf.get("estimate_ack"):
                it.reasons.append("L2 estimate requires estimate_ack")
        per_level[lvl] = per_level.get(lvl, 0) + 1
        summary["levels"][lvl] = summary["levels"].get(lvl, 0) + 1
        summary["datapoints"].add(head["datapoint_id"].split("@")[0])
        summary["customers"] |= ctx.customers_of(head)
        summary["estimates"] += int(bool(head.get("is_estimate")))
        it.ok = not it.reasons
    for lvl, n in per_level.items():
        if lvl in BATCHABLE and n > policy.max_items(lvl):
            reasons.append(f"{lvl} batch size {n} exceeds max {policy.max_items(lvl)}")
    if len(l2_requests) > 1 and policy.batch_cfg("L2").get("same_request"):
        reasons.append("L2 items must belong to the same Request")
    summary["datapoints"] = sorted(summary["datapoints"])
    summary["customers"] = sorted(summary["customers"])
    ok = not reasons and all(i.ok for i in items)
    return BatchCheck(ok, items, reasons, summary)


@dataclass
class StagedBatch:
    batch_id: str
    org_id: str
    actor: Actor
    items: dict[str, str]  # approval_id → 钉住的 Answer ObjectRef（SSOT-1：只存引用）
    approval_refs: dict[str, str]
    staged_at: dt.datetime
    commit_at: dt.datetime
    status: str = "staged"  # staged | committed | voided | undone
    viewed: set[str] = field(default_factory=set)
    confirmations: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    committed_refs: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "status": self.status, "approval_ids": list(self.items),
                "answer_refs": list(self.items.values()), "staged_at": _iso(self.staged_at), "commit_at": _iso(self.commit_at),
                "undo_until": _iso(self.commit_at), "decided_by": self.actor.id, "reasons": self.reasons,
                "committed_refs": self.committed_refs, "summary": self.summary}


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BatchError(GraphError):
    def __init__(self, message: str, check: BatchCheck | None = None, code: str = "GRAPH_APPROVAL_REQUIRED") -> None:
        super().__init__(code, message, rule_id="06-5.1", details={"check": check.to_dict()} if check else {})


class BatchStager:
    """服务端暂存的批量批准（approval-router 工作流状态的进程内实现，可注入时钟）。"""

    def __init__(self, store: Any, router: Any, engine: Any = None, *, clock: Callable[[], dt.datetime] | None = None,
                 policy: ApprovalPolicy | None = None) -> None:
        self.store = store
        self.router = router
        self.engine = engine
        self.clock = clock or _utcnow
        self.policy = policy or router.policy
        self._batches: dict[str, StagedBatch] = {}
        self._lock = threading.RLock()

    @property
    def window(self) -> dt.timedelta:
        return dt.timedelta(minutes=self.policy.undo_window_minutes)

    def get(self, batch_id: str) -> StagedBatch | None:
        return self._batches.get(batch_id)

    def batches(self, org_id: str | None = None) -> list[StagedBatch]:
        return [b for b in self._batches.values() if org_id is None or b.org_id == org_id]

    def _audit(self, actor: Actor, event: str, batch: StagedBatch, **details: Any) -> None:
        self.store.record_audit(actor, "authz.change", object_ref=batch.batch_id,
                                details={"event": event, "batch_id": batch.batch_id, **details})

    def _incremental_audit(self, org_id: str, answer_refs: list[str], staged: Iterable[str]) -> None:
        if self.engine is not None and answer_refs:
            self.engine.run("answer_write", org_id=org_id, answer_refs=answer_refs, staged_batch_answers=staged)

    def check(self, actor: Actor, approval_ids: Iterable[str], *, viewed: Iterable[str] = (),
              confirmations: dict[str, Any] | None = None, pinned: dict[str, str] | None = None) -> BatchCheck:
        ctx = AuditContext.from_store(self.store, actor.org_id, meta=self.router.meta)
        return check_batch(ctx, approval_ids, actor, self.policy, viewed=viewed, confirmations=confirmations, pinned=pinned)

    def stage(self, actor: Actor, approval_ids: Iterable[str], *, batch_id: str | None = None,
              viewed: Iterable[str] = (), confirmations: dict[str, Any] | None = None,
              immediate: bool = False) -> StagedBatch:
        if not actor.is_user:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "batch approvals are decided by users")
        ids = list(dict.fromkeys(approval_ids))
        if batch_id is not None:
            if not batch_id.startswith("apb_"):
                raise GraphError("GRAPH_SCHEMA_INVALID", "batch_id must be apb_<ULID>", path="/batch_id")
            if batch_id in self._batches:
                raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"batch {batch_id} already exists", path="/batch_id")
        ctx = AuditContext.from_store(self.store, actor.org_id, meta=self.router.meta)
        ans_refs = [ctx.head(a)["subject_ref"] for a in ids if ctx.head(a) and ctx.head(a).get("subject_ref", "").startswith("ans_")]  # type: ignore[index,union-attr]
        # 条件 2：批次生成时跑一次 T1 增量审计（含 CR-042 的"进入批次"判断）
        self._incremental_audit(actor.org_id, ans_refs, {split_ref(r)[0] for r in ans_refs})
        chk = self.check(actor, ids, viewed=viewed, confirmations=confirmations)
        if not chk.ok:
            raise BatchError("batch approval conditions not met (06 §5.1)", chk)
        ctx = AuditContext.from_store(self.store, actor.org_id, meta=self.router.meta)
        now = self.clock()
        pinned = {i.approval_id: ref_of(ctx.head(i.answer_ref)) for i in chk.items}  # type: ignore[arg-type]
        b = StagedBatch(batch_id or new_batch_id(), actor.org_id, actor, pinned,
                        {i.approval_id: i.approval_ref for i in chk.items}, now, now + self.window,  # type: ignore[misc]
                        viewed=set(viewed), confirmations=dict(confirmations or {}), summary=chk.summary)
        if immediate:
            if not self._due_within_window(ctx, b, now):
                raise GraphError("GRAPH_ILLEGAL_TRANSITION", "immediate commit is only offered when a Request due date is "
                                 "closer than the undo window (06 §5.2)", path="/immediate")
            b.commit_at = now
        with self._lock:
            self._batches[b.batch_id] = b
        self._audit(actor, "approval.batch.stage", b, count=len(b.items), immediate=immediate,
                    undo_until=_iso(b.commit_at))
        if immediate:
            self._commit(b)
        return b

    def _due_within_window(self, ctx: AuditContext, b: StagedBatch, now: dt.datetime) -> bool:
        for ref in b.items.values():
            req = ctx.request_of(ctx.head(ref) or {})
            d = parse_date((req or {}).get("due_date"))
            if d is not None:
                end = dt.datetime.combine(d, dt.time(23, 59, 59), tzinfo=dt.timezone.utc)
                if end - now < self.window:
                    return True
        return False

    def undo(self, actor: Actor, batch_id: str, approval_ids: Iterable[str] | None = None) -> StagedBatch:
        with self._lock:
            b = self._batches.get(batch_id)
            if b is None or b.org_id != actor.org_id:
                raise GraphError("GRAPH_NOT_FOUND", f"batch {batch_id} not found")
            if b.actor.id != actor.id and not actor.has_role("compliance_owner"):
                raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "only the approver may undo the batch")
            if b.status != "staged" or self.clock() >= b.commit_at:
                raise GraphError("GRAPH_ILLEGAL_TRANSITION", "undo window has ended; withdraw via a new Answer version "
                                 "(approved→superseded) and re-approval (06 §5.2)", path="/batch_id",
                                 details={"status": b.status, "undo_until": _iso(b.commit_at)})
            removed = list(b.items) if approval_ids is None else [a for a in approval_ids if a in b.items]
            for a in removed:
                b.items.pop(a, None)
                b.approval_refs.pop(a, None)
            if not b.items:
                b.status = "undone"
        self._audit(actor, "approval.batch.undo", b, removed=removed, remaining=len(b.items))
        return b

    def settle(self, now: dt.datetime | None = None) -> list[StagedBatch]:
        """窗口结束的批次：复检条件 2–4 → 提交或整批作废（并通知批准人）。"""
        now = now or self.clock()
        out = []
        for b in list(self._batches.values()):
            if b.status == "staged" and now >= b.commit_at:
                self._commit(b)
                out.append(b)
        return out

    def _commit(self, b: StagedBatch) -> None:
        with self._lock:
            if b.status != "staged":
                return
            ans_refs = list(b.items.values())
            try:
                self._incremental_audit(b.org_id, ans_refs, {split_ref(r)[0] for r in ans_refs})
                chk = self.check(b.actor, list(b.items), viewed=b.viewed, confirmations=b.confirmations, pinned=b.items)
            except Exception as e:  # fail-closed
                b.status, b.reasons = "voided", [f"recheck failed: {type(e).__name__}: {e}"]
                self._audit(b.actor, "approval.batch.void", b, reasons=b.reasons, notify=b.actor.id)
                return
            if not chk.ok:
                b.status = "voided"
                b.reasons = chk.reasons + [f"{i.approval_id}: {r}" for i in chk.items for r in i.reasons]
                self._audit(b.actor, "approval.batch.void", b, reasons=b.reasons[:20], notify=b.actor.id)
                return
            ctx = AuditContext.from_store(self.store, b.org_id, meta=self.router.meta)
            now_s = self.store.peek_now()
            router = Actor.of("agent:approval-router", b.org_id)
            items: list[WriteItem] = []
            for aid, ans_ref in b.items.items():
                apr = ctx.head(aid)
                ans = ctx.head(ans_ref)
                items.append(WriteItem("Approval", {"id": aid, "status": "approved", "decision": "approved",
                                                    "decided_by": b.actor.id, "decided_at": now_s, "batch_id": b.batch_id},
                                       expected_version=apr["version"], patch_mode="merge_from_head", actor=b.actor))  # type: ignore[index]
                items.append(WriteItem("Answer", {"id": ans["id"], "status": "approved",  # type: ignore[index]
                                                  "approval_ref": f"{aid}#v{apr['version'] + 1}"},  # type: ignore[index]
                                       expected_version=ans["version"], patch_mode="merge_from_head", actor=router))  # type: ignore[index]
            try:
                b.committed_refs = self.store.write_batch(items, actor=router)
                b.status = "committed"
                self._audit(b.actor, "approval.batch.commit", b, count=len(b.items))
            except GraphError as e:
                b.status, b.reasons = "voided", [f"{e.code}: {e.message}"]
                self._audit(b.actor, "approval.batch.void", b, reasons=b.reasons, notify=b.actor.id)


__all__ = ["check_batch", "BatchCheck", "BatchItemCheck", "BatchStager", "StagedBatch", "BatchError", "new_batch_id"]
