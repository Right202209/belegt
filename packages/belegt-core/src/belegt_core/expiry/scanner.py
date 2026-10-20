# Spec: 06 §7（梯度表）、§2.2 CR-004/CR-013、03 §3.5、§3.7（证书类答案）、02 validity_rule.expires_by
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

from ..authz import Actor
from ..errors import GraphError
from ..consistency.context import AuditContext, parse_date, ref_of, split_ref
from ..consistency.meta import MetaProvider

# (上限天数, tier, level)：d ≤ 0 → expired/block；≤7 d7/warn；≤14 d14/warn；≤30 d30/warn；≤60 d60/info；≤90 d90/info
TIERS: tuple[tuple[int, str, str], ...] = ((0, "expired", "block"), (7, "d7", "warn"), (14, "d14", "warn"),
                                           (30, "d30", "warn"), (60, "d60", "info"), (90, "d90", "info"))
IN_PROGRESS_REQ = ("received", "triaged", "parsed", "mapped", "evidence_gathering", "awaiting_supplier", "drafting",
                   "auditing", "in_review", "approved", "filling", "awaiting_human", "halted")


def expiry_tier(valid_until: str | dt.date | None, today: dt.date) -> tuple[str, str, int] | None:
    vu = parse_date(valid_until) if not isinstance(valid_until, dt.date) else valid_until
    if vu is None:
        return None
    days = (vu - today).days
    for limit, tier, level in TIERS:
        if days <= limit:
            return tier, level, days
    return None


@dataclass
class ExpiryAlert:
    subject_ref: str
    kind: str  # evidence | certificate_answer | answer_validity
    valid_until: str
    days: int
    tier: str
    level: str  # info | warn | block
    notify_roles: list[str]
    notify_users: list[str]
    actions: list[str]
    datapoint_id: str | None = None
    referencing_answers: list[str] = field(default_factory=list)
    submitted_to: list[str] = field(default_factory=list)  # 已提交给哪些客户（到期时由 sales-assistant 起草更新通知）
    daily_repeat: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _notify(tier: str, ctx: AuditContext, uploader: str | None, from_upstream: bool, in_progress: bool,
            tier_a: bool) -> tuple[list[str], list[str], list[str]]:
    roles = ["compliance_owner", "contributor"]
    users = [uploader] if uploader and uploader.startswith("user:") else []
    actions = ["list_on_expiring_board"]
    order = [t for _, t, _ in TIERS][::-1]  # d90 … expired
    rank = order.index(tier)
    if rank >= order.index("d60"):
        actions = ["start_renewal"]
        if from_upstream:
            roles.append("agent:upstream-chaser")
            actions.append("draft_renewal_supplier_request(first send requires human approval)")
    if rank >= order.index("d30"):
        if in_progress:
            roles.append("sales")
        actions.append("mark_in_progress_answers_warn(CR-004)")
    if rank >= order.index("d14") and tier_a:
        roles.append("executive")
        actions.append("escalate")
    if tier == "d7":
        actions.append("daily_digest")
    if tier == "expired":
        roles = sorted(set(roles + ["sales", "executive"]) - {"agent:upstream-chaser"}) + (
            ["agent:upstream-chaser"] if from_upstream else [])
        actions = ["set_evidence_expired", "block_new_references_and_submission(R-REF,R-SUBMIT)",
                   "sales-assistant_drafts_customer_update(external send requires confirmation)"]
    return roles, users, actions


def scan_expiry(store: Any, org_id: str, *, today: dt.date | str | None = None, meta: MetaProvider | None = None,
                tier_a_customers: set[str] | None = None, context: AuditContext | None = None) -> list[ExpiryAlert]:
    ctx = context or AuditContext.from_store(store, org_id, meta=meta, **({"today": today} if today else {}))
    day = parse_date(today) if isinstance(today, str) else (today or ctx.today)
    tier_a = tier_a_customers or set()
    answers = ctx.heads("Answer")
    cites: dict[str, list[dict[str, Any]]] = {}
    for a in answers:
        if a.get("status") in ("superseded", "rejected"):
            continue
        for r in ctx.cited_evidence(a):
            cites.setdefault(split_ref(r)[0], []).append(a)
    out: list[ExpiryAlert] = []
    for ev in ctx.heads("Evidence"):
        if ev.get("status") not in ("verified", "expired") or not ev.get("valid_until"):
            continue
        t = expiry_tier(ev["valid_until"], day)
        if t is None:
            continue
        tier, level, days = t
        refs = cites.get(ev["id"], [])
        in_prog = any((ctx.request_of(a) or {}).get("status") in IN_PROGRESS_REQ and a.get("status") != "submitted" for a in refs)
        is_a = any(ctx.customers_of(a) & tier_a for a in refs)
        roles, users, actions = _notify(tier, ctx, ev.get("created_by"), ev.get("source_type") == "supplier_response",
                                        in_prog, is_a)
        submitted_to = sorted({cp for a in refs if a.get("status") == "submitted" for cp in ctx.customers_of(a)})
        out.append(ExpiryAlert(ref_of(ev), "evidence", ev["valid_until"], days, tier, level, roles, users, actions,
                               ev.get("datapoint_id"), [ref_of(a) for a in refs], submitted_to, daily_repeat=tier == "d7"))
    for a in answers:
        if a.get("status") in ("superseded", "rejected"):
            continue
        meta_a = ctx.dp(a)
        base = a["datapoint_id"].split("@", 1)[0]
        if base.startswith("dp:cert.") and a.get("value_type") == "date" and (meta_a is None or meta_a.expires_by in (None, "value")):
            t = expiry_tier(str(a.get("value"))[:10], day)
            if t is None:
                continue
            tier, level, days = t
            roles, users, actions = _notify(tier, ctx, None, False,
                                            (ctx.request_of(a) or {}).get("status") in IN_PROGRESS_REQ,
                                            bool(ctx.customers_of(a) & tier_a))
            if tier == "expired":
                actions = ["certificate_expired: renew certificate", "block_submission_of_answer(CR-013)"]
            out.append(ExpiryAlert(ref_of(a), "certificate_answer", str(a["value"])[:10], days, tier, level, roles, users,
                                   actions, a["datapoint_id"], [ref_of(a)],
                                   sorted(ctx.customers_of(a)) if a.get("status") == "submitted" else [],
                                   daily_repeat=tier == "d7"))
        elif meta_a and meta_a.expires_by == "period_end_plus_days" and meta_a.expires_after_days and a.get("status") in (
                "approved", "submitted"):
            end = parse_date(a["period"]["end"]) + dt.timedelta(days=int(meta_a.expires_after_days))  # type: ignore[operator]
            t = expiry_tier(end, day)
            if t is None or t[1] == "info":
                continue
            tier, level, days = t
            out.append(ExpiryAlert(ref_of(a), "answer_validity", end.isoformat(), days, tier,
                                   "warn" if level == "block" else level, ["compliance_owner"], [],
                                   ["refresh_value_for_next_request(validity_rule.expires_by)"], a["datapoint_id"],
                                   [ref_of(a)]))
    out.sort(key=lambda x: (x.days, x.subject_ref))
    return out


def apply_expiry(store: Any, org_id: str, *, today: dt.date | str | None = None,
                 alerts: list[ExpiryAlert] | None = None) -> dict[str, Any]:
    """把已过 ``valid_until`` 的 verified Evidence 置 ``expired``（``agent:consistency-auditor``，03 §3.5）。

    R-STATE 只允许在 ``valid_until`` 已过（store 时钟）后迁移；``valid_until`` 当日为 0 天（block 提醒）但仍有效。
    """
    alerts = alerts if alerts is not None else scan_expiry(store, org_id, today=today)
    actor = Actor.of("agent:consistency-auditor", org_id)
    expired, errors = [], []
    now_day = dt.date.fromisoformat(store.peek_now()[:10])
    for al in alerts:
        if al.kind != "evidence" or al.tier != "expired":
            continue
        ev = store.get(al.subject_ref.split("#", 1)[0], org_id=org_id)
        if not ev or ev.get("status") != "verified" or not (parse_date(ev.get("valid_until")) < now_day):  # type: ignore[operator]
            continue
        try:
            expired.append(store.write("Evidence", {"id": ev["id"], "status": "expired"}, expected_version=ev["version"],
                                       actor=actor, patch_mode="merge_from_head"))
        except GraphError as e:
            errors.append({"evidence": al.subject_ref, "code": e.code, "message": e.message})
    return {"expired": expired, "errors": errors, "alerts": [a.to_dict() for a in alerts]}


__all__ = ["scan_expiry", "apply_expiry", "expiry_tier", "ExpiryAlert", "TIERS"]
