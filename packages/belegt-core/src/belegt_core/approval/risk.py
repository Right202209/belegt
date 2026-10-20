# Spec: 06 §4.1（维度 D1…D8）、§4.2（判定表 RISK-00…RISK-10，自上而下命中即停，结果取 max）、§6.3（法律题 L3）；
#       04 §2.8（策略服务不可用 → 全部按 L3 路由）
"""风险分级（确定性）。

``compute_inputs(ctx, answer, policy)`` 由图读出 D1…D8；``classify(inputs, policy)`` 按判定表逐行求值。
判定表每行都是 ``RiskRow(rule_id, predicate, level)``；``RISK-07`` 为"至少 L2"，与 ``floors`` 一起按 max 合并。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable

from ..consistency.context import AuditContext, parse_date, same_key_wo, split_ref, tag_map
from ..ids import datapoint_base
from ..legal.flags import question_legal_flag
from .policy import ApprovalPolicy, max_level

COMMITMENT_PATTERNS = ("dp:env.ghg_target.",)  # 06 §4.1 D4："如 dp:env.ghg_target.*、SBTi 目标"


@dataclass
class RiskInputs:
    source: str = "evidence"  # D1 reused | evidence | calculation | drafted_text
    value_type: str = "number"  # D2
    first_submission: bool = False  # D3
    commitment: bool = False  # D4（legal_commitment 或承诺类 DataPoint）
    legal_commitment: bool = False
    customer_tier: str = "tier_c"  # D5
    amount_eur: Decimal | None = None  # D6
    open_blocking: bool = False  # D7
    open_warn: bool = False
    accepted_divergence: bool = False
    cross_customer: bool = False  # CR-001/CR-007 涉及
    is_estimate: bool = False  # D8
    restricted: bool = False
    emission: bool = False
    reuse_ok: bool = False  # RISK-08 的附加条件（同期同口径同客户范围或 all、证据仍有效、无 warn）

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["amount_eur"] = None if self.amount_eur is None else format(self.amount_eur, "f")
        return d


@dataclass
class RiskDecision:
    level: str | None  # None = RISK-00 阻断（不可进入 in_review）
    rule_id: str
    blocked: bool = False
    floors: list[str] = field(default_factory=list)
    trace: list[tuple[str, bool]] = field(default_factory=list)


def _amt(i: RiskInputs) -> Decimal:
    return i.amount_eur if i.amount_eur is not None else Decimal(0)


RiskRow = tuple[str, Callable[[RiskInputs, ApprovalPolicy], bool], str | None]
RISK_TABLE: list[RiskRow] = [
    ("RISK-00", lambda i, p: i.open_blocking, None),
    ("RISK-01", lambda i, p: i.commitment, "L3"),
    ("RISK-02", lambda i, p: i.accepted_divergence or i.cross_customer, "L3"),
    ("RISK-03", lambda i, p: i.first_submission and i.emission and i.value_type == "number", "L3"),
    ("RISK-04", lambda i, p: i.restricted, "L3"),
    ("RISK-05", lambda i, p: _amt(i) >= p.threshold("amount_l3_eur"), "L3"),
    ("RISK-06", lambda i, p: i.source in ("calculation", "drafted_text") or i.is_estimate or i.open_warn, "L2"),
    ("RISK-07", lambda i, p: _amt(i) >= p.threshold("amount_l2_eur") or (i.customer_tier == "tier_a" and i.value_type == "number"), "L2"),
    ("RISK-08", lambda i, p: i.source == "reused" and i.reuse_ok and not i.open_warn, "L0"),
    ("RISK-09", lambda i, p: i.source == "evidence" and not i.is_estimate, "L1"),
    ("RISK-10", lambda i, p: True, "L2"),
]
FLOORS: list[tuple[str, Callable[[RiskInputs], bool]]] = [
    ("legal_commitment => L3", lambda i: i.legal_commitment or i.commitment),
    ("first_submission && emission => L3", lambda i: i.first_submission and i.emission and i.value_type == "number"),
    ("accepted_divergence => L3", lambda i: i.accepted_divergence),
]


def classify(inputs: RiskInputs, policy: ApprovalPolicy) -> RiskDecision:
    trace: list[tuple[str, bool]] = []
    hit_rule, level = "RISK-10", "L2"
    for rule_id, pred, lvl in RISK_TABLE:
        ok = bool(pred(inputs, policy))
        trace.append((rule_id, ok))
        if ok:
            if lvl is None:
                return RiskDecision(None, rule_id, blocked=True, trace=trace)
            hit_rule, level = rule_id, lvl
            break
    floors = [name for name, f in FLOORS if f(inputs)]
    if floors:
        level = max_level(level, "L3")
    return RiskDecision(level, hit_rule, floors=floors, trace=trace)


def fallback_decision(reason: str) -> RiskDecision:
    """策略服务不可用 → 宁严勿宽 L3（04 §2.8）。"""
    return RiskDecision("L3", "POLICY-UNAVAILABLE", floors=[reason])


# ---------------------------------------------------------------------------------- 由图计算维度
def compute_inputs(ctx: AuditContext, a: dict[str, Any], policy: ApprovalPolicy,
                   amount_provider: Callable[[AuditContext, dict[str, Any]], Decimal | None] | None = None) -> RiskInputs:
    i = RiskInputs(value_type=a.get("value_type", "number"))
    # D1
    orig = ctx.get(a["reused_from"]) if a.get("reused_from") else None
    if orig is not None and orig.get("value") == a.get("value"):
        i.source = "reused"
    elif a.get("calculation_ref"):
        i.source = "calculation"
    elif a.get("value_type") == "text" and (a.get("created_by") == "agent:answer-drafter" or a.get("origin") == "generated"):
        i.source = "drafted_text"
    elif a.get("evidence_refs") and all((ctx.get(r) or {}).get("status") == "verified" for r in a["evidence_refs"]):
        i.source = "evidence"
    else:
        i.source = "drafted_text" if a.get("value_type") == "text" else "evidence_unverified"
    # D3
    base, bnd = datapoint_base(a["datapoint_id"]), a.get("boundary")
    i.first_submission = not any(
        v.get("status") == "submitted" and datapoint_base(v.get("datapoint_id", "")) == base and v.get("boundary") == bnd
        for h in ctx.heads("Answer") for v in ctx.versions(h["id"]))
    # D4
    meta = ctx.dp(a)
    qs = ctx.questions_of(a)
    i.legal_commitment = bool(a.get("legal_commitment")) or any(question_legal_flag(q) for q in qs)
    tags = set(meta.tags) if meta else set()
    i.commitment = i.legal_commitment or bool(tags & policy.commitment_tags) or base.startswith(COMMITMENT_PATTERNS)
    # D5
    i.customer_tier = policy.customer_tier(sorted(ctx.customers_of(a)))
    # D6
    i.amount_eur = (amount_provider or default_amount)(ctx, a)
    # D7
    for c in ctx.conflicts_for(a["id"]):
        st, sev = c.get("status"), c.get("severity")
        if st == "open" and sev == "blocking":
            i.open_blocking = True
        if st == "open" and sev == "warning":
            i.open_warn = True
        if st == "accepted_divergence":
            i.accepted_divergence = True
        if c.get("rule_id") in ("CR-001", "CR-007") and st != "dismissed":
            i.cross_customer = True
    # D8
    i.is_estimate = bool(a.get("is_estimate"))
    i.restricted = bool(meta and meta.sensitivity == "restricted")
    i.emission = policy.is_emission(a["datapoint_id"])
    # RISK-08 条件
    if i.source == "reused" and orig is not None:
        scope_ok = orig.get("customer_scope") == "all" or same_key_wo(a, orig)
        ev_ok = True
        for r in ctx.cited_evidence(a):
            h = ctx.head(split_ref(r)[0])
            vu = parse_date((h or {}).get("valid_until"))
            if h is None or h.get("status") in ("expired", "rejected") or (vu is not None and vu < ctx.today):
                ev_ok = False
        i.reuse_ok = bool(scope_ok and ev_ok)
    return i


def default_amount(ctx: AuditContext, a: dict[str, Any]) -> Decimal | None:
    """D6：Calculation.params.amount_eur / Answer 标签 ``amount_eur:<DEC>``（calc-service 提供）。"""
    t = tag_map(a).get("amount_eur")
    if t:
        try:
            return Decimal(t)
        except Exception:
            return None
    calc = ctx.get(a["calculation_ref"]) if a.get("calculation_ref") else None
    v = ((calc or {}).get("params") or {}).get("amount_eur")
    try:
        return Decimal(str(v)) if v is not None else None
    except Exception:
        return None


def assess(ctx: AuditContext, a: dict[str, Any], policy: ApprovalPolicy | None,
           amount_provider: Callable[[AuditContext, dict[str, Any]], Decimal | None] | None = None) -> tuple[RiskDecision, RiskInputs | None]:
    if policy is None:
        return fallback_decision("policy unavailable"), None
    try:
        inputs = compute_inputs(ctx, a, policy, amount_provider)
        return classify(inputs, policy), inputs
    except Exception as e:  # 策略求值失败 → L3
        return fallback_decision(f"policy error: {type(e).__name__}"), None


__all__ = ["RiskInputs", "RiskDecision", "RISK_TABLE", "FLOORS", "classify", "compute_inputs", "assess",
           "fallback_decision", "default_amount"]
