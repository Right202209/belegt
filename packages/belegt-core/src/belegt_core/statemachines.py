# Spec: 04 §3.1/§3.2（Request 状态机权威定义 T01…T23）；03 §3.2–§3.13 各对象状态机；_baseline §M
"""所有证据图对象的状态机（R-STATE）。

执行者模式（``actors``）：
  ``any`` 任一执行者；``user`` 任一人类；``user[r1,r2]`` 具备角色之一的人；``agent:<id>``/``service:<id>`` 精确匹配；
  ``agent:*trusted`` 任一 trusted 区 Agent。

Request 侧路：``awaiting_human``（T18/T19）与 ``halted``（T20/T21）可从任一非终态进入，``status_reason`` 记录
``awaiting_human:<cq|conflict|confirm>;return_to=<state>`` / ``halted:<reason>;return_to=<state>``，返回只能回到 ``return_to``。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .authz import Actor
from .errors import GraphError


@dataclass(frozen=True)
class Transition:
    src: str | None  # None = 初始（[*]）
    dst: str
    actors: tuple[str, ...]
    id: str | None = None
    note: str = ""


def actor_matches(actor: Actor, pattern: str) -> bool:
    if pattern == "any":
        return True
    if pattern == "user":
        return actor.is_user
    if pattern.startswith("user[") and pattern.endswith("]"):
        roles = {r.strip() for r in pattern[5:-1].split(",")}
        return actor.is_user and bool(actor.roles & roles)
    if pattern == "agent:*trusted":
        return actor.is_agent and actor.zone == "trusted"
    return actor.id == pattern


@dataclass
class StateMachine:
    type_name: str
    states: tuple[str, ...]
    transitions: list[Transition]
    terminal: frozenset[str] = frozenset()  # 终态：不再有出边（Request archived/cancelled）
    frozen: frozenset[str] = frozenset()  # 进入后不允许再写新版本
    self_updates: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def initial_states(self) -> set[str]:
        return {t.dst for t in self.transitions if t.src is None}

    def find(self, src: str | None, dst: str) -> list[Transition]:
        return [t for t in self.transitions if t.src == src and t.dst == dst]

    def is_legal(self, src: str | None, dst: str) -> bool:
        if src is not None and src == dst and src in self.self_updates:
            return True
        return bool(self.find(src, dst))

    def check(self, src: str | None, dst: str, actor: Actor) -> Transition | None:
        """校验 ``src → dst`` 合法且执行者有权；返回匹配的 Transition（同状态更新返回 None）。"""
        if dst not in self.states:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"{self.type_name}: unknown status {dst!r}", path="/status")
        if src is not None and src in self.frozen:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"{self.type_name}: no new versions after terminal status {src!r}",
                             path="/status", details={"from": src, "to": dst})
        if src is not None and src == dst:
            pats = self.self_updates.get(src)
            if pats is None:
                if self.find(src, dst):
                    pats = tuple(p for t in self.find(src, dst) for p in t.actors)
                else:
                    raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"{self.type_name}: no updates allowed in status {src!r}",
                                     path="/status", details={"from": src, "to": dst})
            if not any(actor_matches(actor, p) for p in pats):
                raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"{actor.id} may not update {self.type_name} in {src!r}",
                                 rule_id="R-STATE", path="/status", details={"from": src, "to": dst, "allowed": list(pats)})
            return None
        cands = self.find(src, dst)
        if not cands:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"{self.type_name}: {src or '[*]'} -> {dst} is not a legal transition",
                             path="/status", details={"from": src, "to": dst})
        for t in cands:
            if any(actor_matches(actor, p) for p in t.actors):
                return t
        allowed = sorted({p for t in cands for p in t.actors})
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"{actor.id} may not perform {self.type_name} {src or '[*]'} -> {dst}",
                         rule_id="R-STATE", path="/status",
                         details={"from": src, "to": dst, "allowed": allowed, "transition": cands[0].id})


def _t(src, dst, actors: Iterable[str] | str, id=None, note=""):
    if isinstance(actors, str):
        actors = (actors,)
    return Transition(src, dst, tuple(actors), id, note)


# ------------------------------------------------------------------ Request（04 §3.1/§3.2）
REQUEST_MAIN = (
    "received", "triaged", "parsed", "mapped", "evidence_gathering", "awaiting_supplier", "drafting", "auditing",
    "in_review", "approved", "filling", "submitted", "archived",
)
REQUEST_SIDE = ("awaiting_human", "halted", "cancelled")
REQUEST_STATES = REQUEST_MAIN + REQUEST_SIDE
REQUEST_TERMINAL = frozenset({"archived", "cancelled"})
# 侧路可进入的源：任一非终态（submitted/archived/cancelled 除外）
REQUEST_SIDE_SOURCES = tuple(s for s in REQUEST_STATES if s not in ("submitted", "archived", "cancelled"))
# 可作为 return_to 的状态：主线中 submitted 之前的状态
REQUEST_RETURNABLE = frozenset(REQUEST_MAIN[: REQUEST_MAIN.index("submitted")])
_CANCELLERS = ("user[compliance_owner,org_admin]",)

_req_t: list[Transition] = [
    _t(None, "received", ("agent:intake-triage", "user[compliance_owner]"), "T01",
       "收件；user 仅用于手动上传渠道（本包补充）"),
    _t("received", "triaged", ("agent:guard", "user[compliance_owner]"), "T02"),
    _t("received", "cancelled", _CANCELLERS, "T03"),
    _t("triaged", "parsed", ("agent:guard", "service:workflow"), "T04",
       "03 摘要写 agent:guard；03 §5.6.2 示例 v3 由 service:workflow 写入，故二者均允许"),
    _t("parsed", "mapped", ("agent:guard", "user[compliance_owner]"), "T05"),
    _t("mapped", "evidence_gathering", "service:workflow", "T06"),
    _t("evidence_gathering", "awaiting_supplier", "agent:upstream-chaser", "T07"),
    _t("awaiting_supplier", "evidence_gathering", ("agent:supplier-reply-reviewer", "service:workflow"), "T08"),
    _t("evidence_gathering", "drafting", "service:workflow", "T09"),
    _t("drafting", "auditing", ("agent:answer-drafter", "agent:calc-service"), "T10"),
    _t("auditing", "in_review", ("agent:consistency-auditor", "agent:approval-router"), "T11"),
    _t("in_review", "auditing", "agent:approval-router", "T12"),
    _t("in_review", "drafting", "agent:approval-router", "T13"),
    _t("in_review", "approved", "agent:approval-router", "T14"),
    _t("approved", "filling", "agent:filler", "T15"),
    _t("filling", "submitted", "agent:filler", "T16"),
    _t("submitted", "archived", "service:workflow", "T17"),
]
for _s in REQUEST_SIDE_SOURCES:
    if _s != "awaiting_human":
        _req_t.append(_t(_s, "awaiting_human", "any", "T18"))
    if _s != "halted":
        _req_t.append(_t(_s, "halted", "agent:guard", "T20"))
for _s in REQUEST_RETURNABLE:
    _req_t.append(_t("awaiting_human", _s, "service:workflow", "T19", "仅可回到 status_reason 中的 return_to"))
    _req_t.append(_t("halted", _s, "user[org_admin,compliance_owner]", "T21", "仅可回到 return_to"))
for _s in REQUEST_SIDE_SOURCES:
    if _s == "halted":
        _req_t.append(_t(_s, "cancelled", _CANCELLERS, "T22"))
    elif _s == "in_review":
        _req_t.append(_t(_s, "cancelled", _CANCELLERS, "T23"))
    elif _s != "received":
        _req_t.append(_t(_s, "cancelled", _CANCELLERS, "T22",
                         "cancelled 可从任一 submitted 之前的状态进入（04 §3.1）"))

REQUEST_SM = StateMachine(
    "Request", REQUEST_STATES, _req_t, terminal=REQUEST_TERMINAL, frozen=REQUEST_TERMINAL,
    self_updates={s: ("any",) for s in REQUEST_STATES if s not in REQUEST_TERMINAL},
)

STATUS_REASON_RE = re.compile(
    r"^(?P<kind>awaiting_human|halted):(?P<reason>[a-z0-9_.-]+);return_to=(?P<return_to>[a-z_]+)$"
)


def parse_status_reason(reason: str | None) -> dict[str, str] | None:
    if not reason:
        return None
    m = STATUS_REASON_RE.match(reason)
    return m.groupdict() if m else None


def check_request_side_path(prev: dict | None, new: dict) -> None:
    """侧路不变量：进入 awaiting_human/halted 必须带合法 ``return_to``；离开只能回到 ``return_to``。"""
    dst = new.get("status")
    src = prev.get("status") if prev else None
    if dst in ("awaiting_human", "halted"):
        info = parse_status_reason(new.get("status_reason"))
        if not info or info["kind"] != dst:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION",
                             f"status_reason must be '{dst}:<reason>;return_to=<state>'", path="/status_reason")
        if dst == "awaiting_human" and info["reason"] not in ("cq", "conflict", "confirm"):
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", "awaiting_human reason must be cq|conflict|confirm",
                             path="/status_reason")
        if info["return_to"] not in REQUEST_RETURNABLE:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION", f"return_to {info['return_to']!r} is not a returnable state",
                             path="/status_reason")
    if src in ("awaiting_human", "halted") and dst != src and dst != "cancelled" and dst not in ("awaiting_human", "halted"):
        info = parse_status_reason(prev.get("status_reason")) if prev else None
        if not info or info["return_to"] != dst:
            raise GraphError("GRAPH_ILLEGAL_TRANSITION",
                             f"{src} may only return to return_to={info['return_to'] if info else None!r}, not {dst!r}",
                             path="/status", details={"from": src, "to": dst})


# ------------------------------------------------------------------ 其它对象（03 §3.3–§3.13）
QUESTION_SM = StateMachine(
    "Question", ("parsed", "mapped", "unmapped", "answered"),
    [
        _t(None, "parsed", ("agent:question-parser", "user[compliance_owner]")),
        _t("parsed", "mapped", ("agent:guard", "user[compliance_owner]")),
        _t("parsed", "unmapped", "agent:guard"),
        _t("unmapped", "mapped", "user[compliance_owner]"),
        _t("mapped", "unmapped", "user[compliance_owner]"),
        _t("mapped", "answered", "agent:approval-router"),
        _t("answered", "mapped", "agent:approval-router"),
    ],
    self_updates={
        "parsed": ("agent:question-parser", "agent:guard", "user[compliance_owner]"),
        "mapped": ("user[compliance_owner]", "agent:guard"),
        "unmapped": ("user[compliance_owner]", "agent:guard"),
        "answered": ("agent:approval-router",),
    },
)

MAPPING_SM = StateMachine(
    "Mapping", ("candidate", "confirmed", "rejected"),
    [
        _t(None, "candidate", ("agent:question-parser", "user[compliance_owner]")),
        _t(None, "confirmed", "user[compliance_owner]", note="人工指定 DataPoint（method=human）"),
        _t("candidate", "confirmed", ("agent:guard", "user[compliance_owner]", "service:graph-api")),
        _t("candidate", "rejected", ("agent:guard", "user[compliance_owner]", "service:graph-api")),
        _t("confirmed", "rejected", ("user[compliance_owner]", "service:graph-api")),
    ],
    frozen=frozenset({"rejected"}),
    self_updates={"candidate": ("agent:question-parser", "agent:guard", "user[compliance_owner]")},
)

EVIDENCE_SM = StateMachine(
    "Evidence", ("candidate", "verified", "expired", "superseded", "rejected"),
    [
        _t(None, "candidate", ("agent:evidence-miner", "agent:supplier-reply-reviewer",
                               "user[compliance_owner,contributor,supplier_user]")),
        _t(None, "verified", "user[compliance_owner,contributor]",
           note="human_confirmation（审批界面人工修改数字，03 §3.7）"),
        _t("candidate", "verified", ("agent:evidence-miner", "user[compliance_owner,contributor]", "service:graph-api")),
        _t("candidate", "rejected", ("user[compliance_owner,contributor]", "service:graph-api")),
        _t("verified", "expired", "agent:consistency-auditor"),
        _t("verified", "superseded", "service:graph-api"),
        _t("candidate", "superseded", "service:graph-api"),
        _t("expired", "superseded", "service:graph-api"),
    ],
    frozen=frozenset({"superseded", "rejected"}),
    self_updates={"candidate": ("agent:evidence-miner", "agent:supplier-reply-reviewer", "agent:guard",
                                "user[compliance_owner,contributor,supplier_user]")},
)

CALCULATION_SM = StateMachine(
    "Calculation", ("computed", "superseded"),
    [
        _t(None, "computed", "agent:calc-service"),
        _t("computed", "superseded", ("agent:calc-service", "service:graph-api")),
    ],
    frozen=frozenset({"superseded"}),
)

ANSWER_SM = StateMachine(
    "Answer", ("draft", "in_review", "approved", "rejected", "submitted", "superseded"),
    [
        _t(None, "draft", ("agent:answer-drafter", "agent:calc-service", "user[compliance_owner]")),
        _t(None, "submitted", "user[compliance_owner]", note="历史导入 origin=imported（FR-201）"),
        _t("draft", "in_review", "agent:approval-router"),
        _t("in_review", "approved", ("user[compliance_owner,executive]", "agent:approval-router"),
           note="approval-router 仅能落地已裁决的 Approval（R-APR 校验审批人角色）；03 §5.6.9 示例如此"),
        _t("in_review", "rejected", ("user[compliance_owner,executive]", "agent:approval-router")),
        _t("in_review", "draft", "user[compliance_owner,executive]", note="Approval modified 生成新版本"),
        _t("rejected", "draft", ("agent:answer-drafter", "agent:calc-service", "user[compliance_owner]"),
           note="数字答案由 calc-service 重起草（answer-drafter 不碰数字）"),
        _t("approved", "submitted", "agent:filler"),
        _t("approved", "superseded", "service:graph-api"),
        _t("submitted", "superseded", "service:graph-api"),
    ],
    frozen=frozenset({"superseded"}),
    self_updates={
        "draft": ("agent:answer-drafter", "agent:calc-service", "user[compliance_owner]"),
        "in_review": ("agent:approval-router",),
    },
)

APPROVAL_SM = StateMachine(
    "Approval", ("pending", "approved", "rejected", "modified"),
    [
        _t(None, "pending", "agent:approval-router"),
        _t("pending", "approved", "user[compliance_owner,executive]"),
        _t("pending", "rejected", ("user[compliance_owner,executive]", "agent:approval-router")),
        _t("pending", "modified", "user[compliance_owner,executive]"),
    ],
    frozen=frozenset({"approved", "rejected", "modified"}),
    self_updates={"pending": ("agent:approval-router",)},
)

CONFLICT_SM = StateMachine(
    "Conflict", ("open", "resolved", "accepted_divergence", "dismissed"),
    [
        _t(None, "open", "agent:consistency-auditor"),
        _t("open", "resolved", ("user[compliance_owner]", "agent:consistency-auditor")),
        _t("open", "accepted_divergence", "user[compliance_owner]"),
        _t("open", "dismissed", "user[compliance_owner]"),
    ],
    frozen=frozenset({"resolved", "accepted_divergence", "dismissed"}),
    self_updates={"open": ("agent:consistency-auditor",)},
)

SUPPLIER_REQUEST_SM = StateMachine(
    "SupplierRequest", ("draft", "sent", "reminded", "responded", "reviewed", "closed", "escalated", "expired"),
    [
        _t(None, "draft", ("agent:upstream-chaser", "user[compliance_owner,contributor]")),
        _t("draft", "sent", ("agent:egress-gateway", "user[compliance_owner,contributor]")),
        _t("sent", "reminded", ("agent:upstream-chaser", "agent:egress-gateway")),
        _t("reminded", "reminded", ("agent:upstream-chaser", "agent:egress-gateway")),
        _t("sent", "responded", ("agent:supplier-reply-reviewer", "service:workflow")),
        _t("reminded", "responded", ("agent:supplier-reply-reviewer", "service:workflow")),
        _t("responded", "reviewed", ("agent:supplier-reply-reviewer", "service:workflow")),
        _t("reviewed", "closed", ("user[compliance_owner,contributor]", "agent:evidence-miner")),
        _t("reviewed", "sent", ("agent:upstream-chaser", "agent:egress-gateway")),
        _t("reminded", "escalated", "agent:upstream-chaser"),
        _t("sent", "expired", "service:workflow"),
        _t("reminded", "expired", "service:workflow", note="08 §2.3 并入（_baseline §M）"),
        _t("escalated", "closed", "user[compliance_owner,contributor]"),
        _t("escalated", "responded", ("agent:supplier-reply-reviewer", "service:workflow"),
           note="08 §2.3 并入（_baseline §M）"),
    ],
    frozen=frozenset({"closed", "expired"}),
    self_updates={
        "draft": ("agent:upstream-chaser", "user[compliance_owner,contributor]"),
        "sent": ("agent:upstream-chaser", "agent:egress-gateway"),
    },
)

CLOSED_QUESTION_SM = StateMachine(
    "ClosedQuestion", ("open", "answered", "expired"),
    [
        _t(None, "open", ("agent:*trusted", "agent:guard", "service:workflow", "user[compliance_owner]")),
        _t("open", "answered", "user[compliance_owner,executive,contributor,supplier_user]"),
        _t("open", "expired", "service:workflow"),
    ],
    frozen=frozenset({"answered", "expired"}),
    self_updates={"open": ("agent:*trusted", "agent:guard", "user[compliance_owner]")},
)

FACTOR_SET_SM = StateMachine(
    "FactorSet", ("active", "deprecated"),
    [
        _t(None, "active", ("service:factor-import", "user[org_admin]")),
        _t("active", "deprecated", ("service:factor-import", "user[org_admin]")),
    ],
    frozen=frozenset({"deprecated"}),
)

STATE_MACHINES: dict[str, StateMachine] = {
    sm.type_name: sm
    for sm in (REQUEST_SM, QUESTION_SM, MAPPING_SM, EVIDENCE_SM, CALCULATION_SM, ANSWER_SM, APPROVAL_SM,
               CONFLICT_SM, SUPPLIER_REQUEST_SM, CLOSED_QUESTION_SM, FACTOR_SET_SM)
}


def get_state_machine(type_name: str) -> StateMachine | None:
    return STATE_MACHINES.get(type_name)


def check_transition(type_name: str, prev: dict | None, new: dict, actor: Actor) -> Transition | None:
    """R-STATE 入口：无状态机的类型（主数据）直接放行。"""
    sm = STATE_MACHINES.get(type_name)
    if sm is None:
        return None
    src = prev.get("status") if prev else None
    dst = new.get("status")
    t = sm.check(src, dst, actor)
    if type_name == "Request":
        check_request_side_path(prev, new)
    return t
