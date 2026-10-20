# Spec: 04 §2.8 approval-router（确定性：risk_level、required_role、Approval(pending)、批次 apb_；策略服务不可用 → 全部按 L3）、
#       §2.9（SupplierRequest 首次外发 Approval）、06 §4（approval-policy@1.0.0，由 belegt_core.approval 实现）
"""approval-router 调用适配。

``belegt_core.approval``（另一子任务实现 06 的判定表与批量）存在时直接用 ``ApprovalRouter``；不存在时退化为本模块的最小
确定性路由（宁严勿宽：L3 规则更多），接口相同：``route(org_id, request_id, answer_refs) -> {routed, blocked}``、
``route_supplier_request(org_id, sreq_id) -> apr ref``。
"""
from __future__ import annotations

from typing import Any, Iterable

from ..authz import Actor
from ..ids import new_id
from ..store import GraphStore, WriteItem
from .base import AgentBase, AgentOutput

try:  # pragma: no branch
    from ..approval import ApprovalRouter as _CoreApprovalRouter  # type: ignore
except Exception:  # pragma: no cover - 另一子任务未就绪时
    _CoreApprovalRouter = None

# 03 approval.json 要求 ``policy_version`` 匹配 ``^approval-policy@x.y.z$``，回退路由因此写基础版本号，并以 Approval 标签
# ``policy:fallback`` 标明"由最小确定性回退规则定级"（审计时可区分）。
FALLBACK_POLICY = "approval-policy@1.0.0"
FALLBACK_TAG = "policy:fallback"


class FallbackApprovalRouter:
    """最小确定性路由：legal_commitment → L3；估算 → L2；数字（计算）→ L2；数字（直接证据）→ L1；L0 复用 → L0；其余 L1。"""

    def __init__(self, store: GraphStore, policy_available: bool = True) -> None:
        self.store = store
        self.policy_available = policy_available

    def _level(self, a: dict[str, Any]) -> str:
        if not self.policy_available or a.get("legal_commitment"):
            return "L3"
        if a.get("is_estimate") or a.get("calculation_ref"):
            return "L2"
        if a.get("reused_from") and a.get("value_type") != "number":
            return "L0"
        return "L1"

    def route(self, org_id: str, *, request_id: str | None = None, answer_refs: Iterable[str] = ()) -> dict[str, Any]:
        actor = Actor.of("agent:approval-router", org_id)
        ids = {r.split("#")[0] for r in answer_refs}
        out: dict[str, Any] = {"routed": [], "blocked": []}
        batch = {lvl: "apb_" + new_id("Approval")[4:] for lvl in ("L0", "L1", "L2")}
        for aid in sorted(ids):
            a = self.store.get(aid, org_id=org_id)
            if not a or a["status"] != "draft":
                continue
            lvl = self._level(a)
            roles = ["compliance_owner", "executive"] if lvl == "L3" else ["compliance_owner"]
            apr: dict[str, Any] = {"subject_ref": f"{aid}#v{a['version'] + 1}", "risk_level": lvl, "required_role": roles,
                                   "policy_version": FALLBACK_POLICY, "status": "pending", "tags": [FALLBACK_TAG]}
            if request_id:
                apr["request_id"] = request_id
            if lvl != "L3":
                apr["batch_id"] = batch[lvl]
            refs = self.store.write_batch([
                WriteItem("Answer", {"id": aid, "status": "in_review", "risk_level": lvl}, expected_version=a["version"],
                          patch_mode="merge_from_head"),
                WriteItem("Approval", apr)], actor=actor)
            out["routed"].append({"answer_ref": refs[0], "approval_ref": refs[1], "risk_level": lvl})
        return out

    def route_supplier_request(self, org_id: str, sreq_id: str) -> str:
        s = self.store.get(sreq_id, org_id=org_id)
        for a in self.store.query("Approval", org_id=org_id, status="pending"):
            if a["subject_ref"].split("#")[0] == sreq_id:
                return f"{a['id']}#v{a['version']}"
        return self.store.write("Approval", {"subject_ref": f"{sreq_id}#v{s['version']}", "risk_level": "L1",
                                             "required_role": ["compliance_owner"], "policy_version": FALLBACK_POLICY,
                                             "status": "pending", "tags": [FALLBACK_TAG]}, actor=Actor.of("agent:approval-router", org_id))


class ApprovalRouterAgent(AgentBase):
    agent_id = "approval-router"

    def router(self) -> Any:
        if _CoreApprovalRouter is not None:
            try:
                return _CoreApprovalRouter(self.svc.store)
            except Exception:  # pragma: no cover - 策略服务不可用 → 宁严勿宽
                return FallbackApprovalRouter(self.svc.store, policy_available=False)
        return FallbackApprovalRouter(self.svc.store)

    @property
    def implementation(self) -> str:
        return "belegt_core.approval" if _CoreApprovalRouter is not None else "fallback"

    def run(self, request_ref: str, answer_refs: list[str], *, idem_prefix: str) -> AgentOutput:
        req_id = request_ref.split("#")[0]
        with self.context(req_id, idem_prefix, activity="route_approvals", input_obj={"answers": len(answer_refs)}):
            res = self.router().route(self.svc.org_id, request_id=req_id, answer_refs=answer_refs)
            d = res.to_dict() if hasattr(res, "to_dict") else res
            routed = d.get("routed") or []
            out = AgentOutput(refs=[r["approval_ref"] for r in routed] + [r["answer_ref"] for r in routed], outcome="routed")
            levels: dict[str, int] = {}
            for r in routed:
                levels[r["risk_level"]] = levels.get(r["risk_level"], 0) + 1
            out.details = {"implementation": self.implementation, "levels": levels, "blocked": d.get("blocked") or [],
                           "approval_refs": [r["approval_ref"] for r in routed]}
            return out

    def route_supplier_request(self, sreq_id: str) -> str:
        return self.router().route_supplier_request(self.svc.org_id, sreq_id)
