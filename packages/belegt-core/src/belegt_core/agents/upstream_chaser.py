# Spec: 04 §2.9 upstream-chaser（缺口 → SupplierRequest：选供应商、模板、语言、截止日；首次 sent 必须人确认 Approval；
#       外发只能 egress.send_templated（GP-07）；egress 拒绝 → escalated 并通知采购）、§2.15、08 §2–§4（模板、催办策略）
"""上游追数：把依赖供应商的缺口按供应商分组生成 ``SupplierRequest(draft)``，请求首次外发审批；审批后经 egress-gateway
渲染（``belegt_supplier.TemplateRegistry``）并发送，``agent:egress-gateway`` 写 ``draft→sent``。模型只选模板/语言/白名单槽位。"""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..egress.gateway import EgressDenied, PayloadItem
from ..llm.base import LLMOutputInvalid, ModelUnavailable
from .base import AgentBase, AgentOutput, dp_label

FAMILY_FOR = {"prod.cbam": "cbam_see_request", "prod.pcf": "pcf_request", "supply.eudr": "eudr_geo_request"}


def _family(dp_ids: list[str]) -> str:
    for d in dp_ids:
        base = d.split(":", 1)[1]
        for k, fam in FAMILY_FOR.items():
            if base.startswith(k):
                return fam
    return "supplier_followup_incomplete"


def _add_days(d: str, n: int) -> str:
    return (dt.date.fromisoformat(d) + dt.timedelta(days=n)).isoformat()


class UpstreamChaser(AgentBase):
    agent_id = "upstream-chaser"

    def plan(self, request_ref: str, gaps: list[dict[str, Any]], *, idem_prefix: str) -> AgentOutput:
        """缺口 → SupplierRequest(draft) + 首次外发 Approval(pending)（由 approval-router 写）。"""
        from .approval_router import ApprovalRouterAgent

        st, org = self.svc.store, self.svc.org_id
        req_id = request_ref.split("#")[0]
        req = st.get(req_id, org_id=org) or {}
        out = AgentOutput(outcome="planned")
        groups: dict[str, list[dict[str, Any]]] = {}
        for g in gaps:
            cp = self.svc.org.supplier_for_datapoints.get(g["datapoint_id"].split("@")[0])
            if cp:
                groups.setdefault(cp, []).append(g)
            else:
                out.details.setdefault("no_supplier", []).append(g["datapoint_id"])
        with self.context(req_id, idem_prefix, activity="chase", input_obj={"gaps": len(gaps)}) as ctx:
            for cp_id, gs in sorted(groups.items()):
                cp = st.get(cp_id, org_id=org)
                if cp is None:
                    continue
                dps = sorted({g["datapoint_id"] for g in gs})
                fam = _family(dps)
                contacts = [c for c in cp.get("contacts") or [] if c.get("confirmed", True)]
                locale = (contacts[0].get("locale") if contacts else None) or "en"
                allowed = []
                reg = self.svc.supplier_templates
                for loc in (locale, "en"):
                    tid = f"stpl:{fam}.{loc}@1.0.0"
                    if reg is None or (reg.is_registered(tid) and reg.effective_status(tid) == "reviewed"):
                        allowed.append(tid)
                try:
                    slots = ctx.ask("chase_slots", {"datapoint_ids": dps, "language": locale, "allowed_templates": allowed,
                                                    "counterparty": cp.get("legal_name")})
                    tid = slots["template_id"] if slots["template_id"] in allowed else (allowed[0] if allowed else f"stpl:{fam}.en@1.0.0")
                except (ModelUnavailable, LLMOutputInvalid):
                    tid = allowed[0] if allowed else f"stpl:{fam}.en@1.0.0"
                period = gs[0]["period"]
                today = st.peek_now()[:10]
                due = _add_days(today, 21)
                if req.get("due_date"):
                    due = min(due, _add_days(req["due_date"], -7))  # Request.due_date − 5 工作日（≈ 7 天）
                if due <= today:
                    due = _add_days(today, 5)
                body: dict[str, Any] = {
                    "id": ctx.det_id("SupplierRequest", f"sreq:{cp_id}"), "counterparty_id": cp_id, "datapoint_ids": dps,
                    "period": period, "language": tid.split(".")[1].split("@")[0], "template_id": tid, "channel": "email",
                    "due_date": due, "reminder_count": 0, "status": "draft"}
                prods = sorted({g["boundary"].split(":", 1)[1] for g in gs if g.get("boundary", "").startswith("product:")})
                if not prods and self.svc.org.default_product_id:
                    prods = [self.svc.org.default_product_id]
                if prods:
                    body["product_ids"] = prods
                qrefs = [r for g in gs for r in g.get("question_refs") or [] if "#v" in r]
                if qrefs:
                    body["trigger_ref"] = qrefs[0]
                sref = ctx.write("SupplierRequest", body, key=f"sreq:{cp_id}")
                out.refs.append(sref)
                apr = ApprovalRouterAgent(self.svc).route_supplier_request(sref.split("#")[0])
                out.cq_refs.append(apr)  # 人工节点：首次外发审批（06 §4.4）
        return out

    def send(self, sreq_id: str, *, idem_prefix: str, request_id: str | None = None,
             now: dt.datetime | None = None) -> dict[str, Any]:
        """经 egress-gateway 渲染并外发（首次需已批准 Approval；催办同样走这里）。返回 ``{status, sreq_ref|denied}``。"""
        st, org = self.svc.store, self.svc.org_id
        s = st.get(sreq_id, org_id=org)
        cp = st.get(s["counterparty_id"], org_id=org)
        contacts = [c for c in cp.get("contacts") or [] if c.get("confirmed", True) and c.get("email")]
        if not contacts:
            return {"status": "denied", "denied": {"code": "EGRESS_DENIED_RECIPIENT_NOT_ALLOWED", "rule_id": "UPC-LIM-06"}}
        reg = self.svc.supplier_templates
        products = []
        for pid in s.get("product_ids") or []:
            p = st.get(pid, org_id=org)
            if p:
                products.append({"name": p["name"], "cn_code": p.get("cn_code")})
        o = self.svc.org
        ctx_vars = {"supplier_name": cp["legal_name"], "customer_name": o.company_name or "Belegt", "products": products or [{"name": "—"}],
                    "datapoints": list(s["datapoint_ids"]), "period": s["period"], "due_date": s["due_date"],
                    "portal_link": f"{o.portal_base_url}/r/{sreq_id}", "link_expires_at": _add_days(s["due_date"], 14),
                    "sender_name": o.sender_name, "sender_role": o.sender_role, "sreq_id": sreq_id,
                    "privacy_note_link": o.privacy_note_link, "contact_name": contacts[0].get("name")}
        with self.context(request_id, idem_prefix, activity="chase_send", input_obj={"sreq": sreq_id}) as ctx:
            self.svc.guard.check_tool(self.agent_id, "egress.send_templated", {"readOnlyHint": False}, span=ctx.span)
            rendered = reg.render(s["template_id"], ctx_vars) if reg is not None else _plain_render(s, ctx_vars)
            payload = [PayloadItem(d, (self.svc.registry.get(d).sensitivity if self.svc.registry.get(d) else "customer_shared"), False)
                       for d in s["datapoint_ids"]]
            try:
                res = self.svc.egress.send_supplier_request(sreq_id, org, recipient=contacts[0]["email"], rendered=rendered,
                                                             request_id=request_id, now=now, payload=payload,
                                                             idempotency_key=ctx.key(f"sent:{sreq_id}:{s['version']}"))
            except EgressDenied as e:
                if e.escalate and s["status"] == "reminded":  # UPC-LIM-01：上限 → escalated，通知采购
                    st.write("SupplierRequest", {"id": sreq_id, "status": "escalated"}, expected_version=s["version"],
                             actor=self.actor(), patch_mode="merge_from_head")
                return {"status": "deferred" if e.retry_after else "denied", "denied": e.to_dict()}
            return {"status": "sent", "sreq_ref": res["sreq_ref"], "message_id": res["receipt"].message_id}


class _Rendered:
    def __init__(self, template_id: str, subject: str, body: str) -> None:
        import hashlib

        self.template_id = template_id
        self.template_hash = "sha256:" + hashlib.sha256(template_id.encode()).hexdigest()
        self.subject, self.body_text, self.links = subject, body, []


def _plain_render(s: dict[str, Any], v: dict[str, Any]) -> _Rendered:
    return _Rendered(s["template_id"], f"Data request {v['sreq_id']}", f"Please provide {', '.join(v['datapoints'])} by {v['due_date']}.")
