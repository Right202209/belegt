# Spec: 03 §1.6（按 trust_zone 的读写权限）、07 §2.1（信任分区）、§4.1 RBAC、§4.2 ABAC、§4.3 按客户授权、§4.4
"""执行者（user:/agent:/service:）身份与权限。

- ``Actor``：写入/读取时的执行者上下文（API 由令牌推出，Agent 不能自报身份）。
- ``can_write_type``：类型级写权限（03 §1.6 表 + 07 §4.1 RBAC 矩阵的 C 列）；状态迁移级权限在 statemachines。
- ``can_read``：读过滤（信任区、``approved_only``、``cp_scope``、隔离区 Agent 的可读类型），模拟 03 §6.5 RLS。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import GraphError

ROLES = (
    "compliance_owner", "executive", "sales", "contributor", "supplier_user", "customer_buyer", "org_admin",
    "auditor_readonly",
)
AGENTS = (
    "intake-triage", "question-parser", "evidence-miner", "calc-service", "consistency-auditor", "answer-drafter",
    "approval-router", "upstream-chaser", "supplier-reply-reviewer", "filler", "reg-radar", "sales-assistant",
    "guard", "egress-gateway",
)
QUARANTINE_AGENTS = frozenset({"intake-triage", "question-parser", "supplier-reply-reviewer"})
CROSSCUT_AGENTS = frozenset({"guard", "egress-gateway"})
# 本包使用的服务执行者（03 新增标识 + 本包补充 service:graph-api）
KNOWN_SERVICES = frozenset({"workflow", "factor-import", "audit-anchor", "graph-api", "dictionary-release", "erp-connector"})
SYSTEM_ACTOR = "service:graph-api"  # 写入 API 自动追加的版本（如 superseded）

# --- 03 §1.6 + 07 §4.1：类型级写权限 -------------------------------------------------
AGENT_WRITE: dict[str, frozenset[str]] = {
    "intake-triage": frozenset({"Request", "Counterparty"}),
    "question-parser": frozenset({"Question", "Mapping"}),
    "supplier-reply-reviewer": frozenset({"Evidence", "SupplierRequest", "Request"}),
    "guard": frozenset({"Request", "Question", "Mapping", "Evidence"}),
    "evidence-miner": frozenset({"Evidence", "ClosedQuestion", "SupplierRequest", "Request"}),
    "calc-service": frozenset({"Calculation", "Answer", "ClosedQuestion", "Request"}),
    "answer-drafter": frozenset({"Answer", "ClosedQuestion", "Request"}),
    "consistency-auditor": frozenset({"Conflict", "Evidence", "Request", "ClosedQuestion"}),
    "approval-router": frozenset({"Approval", "Answer", "Question", "Request", "ClosedQuestion"}),
    "upstream-chaser": frozenset({"SupplierRequest", "Request", "ClosedQuestion"}),
    "filler": frozenset({"Answer", "Request"}),
    "egress-gateway": frozenset({"SupplierRequest", "Request"}),
    "reg-radar": frozenset(),
    "sales-assistant": frozenset(),
}
ROLE_WRITE: dict[str, frozenset[str]] = {
    "org_admin": frozenset({"Organization", "Counterparty", "Site", "Product", "FactorSet", "Request"}),
    "compliance_owner": frozenset({"Request", "Question", "Mapping", "Evidence", "Answer", "Approval", "Conflict",
                                   "SupplierRequest", "ClosedQuestion", "Organization", "Counterparty", "Site",
                                   "Product"}),
    "executive": frozenset({"Answer", "Approval", "ClosedQuestion"}),
    "contributor": frozenset({"Evidence", "SupplierRequest", "ClosedQuestion"}),
    "supplier_user": frozenset({"Evidence", "ClosedQuestion"}),
    "sales": frozenset(),
    "customer_buyer": frozenset(),
    "auditor_readonly": frozenset(),
}
SERVICE_WRITE: dict[str, frozenset[str]] = {
    "workflow": frozenset({"Request", "ClosedQuestion", "SupplierRequest"}),
    "graph-api": frozenset({"Evidence", "Calculation", "Answer", "Mapping", "Question", "ClosedQuestion", "Approval"}),
    "factor-import": frozenset({"FactorSet"}),
    "erp-connector": frozenset({"Site", "Product"}),
    "dictionary-release": frozenset(),
    "audit-anchor": frozenset(),
}
# 隔离区执行者可读类型（03 §1.6）
QUARANTINE_READ: dict[str, frozenset[str]] = {
    "intake-triage": frozenset({"Request", "Counterparty", "DataPoint"}),
    "question-parser": frozenset({"Request", "Question", "Mapping", "DataPoint"}),
    "supplier-reply-reviewer": frozenset({"SupplierRequest", "Evidence", "DataPoint"}),
}
# 审批角色：risk_level → 可裁决角色（_baseline §F、07 §4.1）
APPROVER_ROLES: dict[str, frozenset[str]] = {
    "L0": frozenset({"compliance_owner"}),
    "L1": frozenset({"compliance_owner"}),
    "L2": frozenset({"compliance_owner"}),
    "L3": frozenset({"compliance_owner", "executive"}),
}
AUDIT_PACK_ROLES = frozenset({"compliance_owner", "auditor_readonly", "org_admin"})


@dataclass(frozen=True)
class Actor:
    id: str  # user:<id> | agent:<agent_id> | service:<svc_id>
    org_id: str
    roles: frozenset[str] = field(default_factory=frozenset)
    cp_scope: tuple[str, ...] | None = None  # None = '*'
    approved_only: bool = False
    ip: str | None = None
    user_agent: str | None = None

    @classmethod
    def of(cls, id: str, org_id: str, roles: Iterable[str] = (), **kw: Any) -> "Actor":
        return cls(id=id, org_id=org_id, roles=frozenset(roles), **kw)

    @property
    def kind(self) -> str:
        return self.id.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.id.split(":", 1)[1]

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def is_agent(self) -> bool:
        return self.kind == "agent"

    @property
    def is_service(self) -> bool:
        return self.kind == "service"

    @property
    def zone(self) -> str:
        """执行者所在信任区：quarantine | trusted | crosscut。"""
        if self.is_agent and self.name in QUARANTINE_AGENTS:
            return "quarantine"
        if self.is_agent and self.name in CROSSCUT_AGENTS:
            return "crosscut"
        if self.is_user and self.roles and self.roles <= {"supplier_user"}:
            return "quarantine"
        return "trusted"

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles & set(roles))

    def effective_cp_scope(self) -> tuple[str, ...] | None:
        if self.cp_scope is not None:
            return self.cp_scope
        return None

    def must_approved_only(self) -> bool:
        if self.approved_only:
            return True
        if self.is_agent and self.name in ("sales-assistant", "filler"):
            return True
        return self.is_user and bool(self.roles) and self.roles <= {"customer_buyer", "sales"}


def validate_actor(actor: Actor) -> None:
    import re

    if not re.match(r"^(user:[a-z0-9_]+|agent:[a-z-]+|service:[a-z-]+)$", actor.id):
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"malformed actor {actor.id!r}", rule_id="AUTHZ")
    if actor.is_agent and actor.name not in AGENTS:
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"agent {actor.name!r} not in _baseline §C", rule_id="AUTHZ")
    for r in actor.roles:
        if r not in ROLES:
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"unknown role {r!r}", rule_id="AUTHZ")


def can_write_type(actor: Actor, type_name: str) -> bool:
    if actor.is_agent:
        return type_name in AGENT_WRITE.get(actor.name, frozenset())
    if actor.is_service:
        return type_name in SERVICE_WRITE.get(actor.name, frozenset())
    return any(type_name in ROLE_WRITE.get(r, frozenset()) for r in actor.roles)


def check_write_type(actor: Actor, type_name: str, obj: dict[str, Any]) -> None:
    """类型级写权限 + 03 §1.6"禁止"列中与字段相关的约束。"""
    validate_actor(actor)
    if not can_write_type(actor, type_name):
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", f"{actor.id} may not write {type_name}", rule_id="AUTHZ",
                         details={"actor": actor.id, "type": type_name})
    if actor.id == "agent:answer-drafter" and type_name == "Answer" and obj.get("value_type") == "number":
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "answer-drafter may not write number answers (NFR-03)",
                         rule_id="AUTHZ", path="/value_type")
    if actor.id == "agent:calc-service" and type_name == "Answer" and obj.get("value_type") != "number":
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "calc-service writes number answers only", rule_id="AUTHZ",
                         path="/value_type")
    if actor.id == "agent:supplier-reply-reviewer" and type_name == "Evidence" and obj.get("source_type") != "supplier_response":
        raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "supplier-reply-reviewer writes supplier_response evidence only",
                         rule_id="AUTHZ", path="/source_type")
    if actor.is_user and type_name == "Evidence" and actor.roles <= {"contributor"} and actor.roles:
        if obj.get("source_type") not in ("human_confirmation", "document"):
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "contributor may write human_confirmation/upload evidence only",
                             rule_id="AUTHZ", path="/source_type")
    if actor.is_user and type_name == "Evidence" and actor.roles <= {"supplier_user"} and actor.roles:
        if obj.get("source_type") != "supplier_response":
            raise GraphError("GRAPH_ACTOR_NOT_ALLOWED", "supplier_user may only upload supplier_response evidence",
                             rule_id="AUTHZ", path="/source_type")


def can_read(actor: Actor | None, type_name: str, body: dict[str, Any]) -> bool:
    """读过滤（模拟 RLS p_tenant/p_zone/p_customer_scope/p_approved_only）。actor=None 表示系统内部读。"""
    if actor is None:
        return True
    org = body.get("org_id")
    if org not in (actor.org_id, "org_global"):
        return False
    zone = body.get("trust_zone")
    if actor.zone == "quarantine":
        if actor.is_agent:
            allowed = QUARANTINE_READ.get(actor.name, frozenset())
            if type_name not in allowed:
                return False
        if type_name not in ("DataPoint", "FactorSet") and zone != "quarantine":
            # 隔离区只能看 quarantine 行（例外：供应商回复审核者读当前 SupplierRequest 元数据）
            if not (type_name == "SupplierRequest" and actor.id == "agent:supplier-reply-reviewer"):
                return False
    if type_name == "Answer":
        if actor.must_approved_only() and body.get("status") not in ("approved", "submitted"):
            return False
        scope = actor.effective_cp_scope()
        if scope is not None:
            cs = body.get("customer_scope")
            if cs != "all" and not (set(cs or []) & set(scope)):
                return False
    if actor.is_user and actor.roles and actor.roles <= {"supplier_user"}:
        return type_name in ("SupplierRequest", "Evidence") and zone == "quarantine" or type_name == "SupplierRequest"
    return True
