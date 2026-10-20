# Spec: 04 §4.3（Activity 清单：triage/parse_questions/promote/build_hints/mine_evidence/calculate/draft_answers/audit/
#       route_approvals/chase/review_reply/fill_file/fill_portal/archive_learn/set_request_status；输入输出只含 ObjectRef 与计数）、
#       §3.2（各转移执行者）、§3.4（幂等键）、§2.1（晋升）
"""工作流 Activity 实现（LocalRunner 直接调用；Temporal worker 以 ``@activity.defn`` 包装后调用同一实现）。

每个方法第一个参数是幂等键前缀 ``wf:<req_id>:act:<activity>:<seq>``，返回 JSON 可序列化的 dict（只含 ObjectRef、计数、状态）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

from ..agents import (
    AnswerDrafter, ApprovalRouterAgent, CalcService, ConsistencyAuditorAgent, EvidenceMiner, FillerFile, QuestionParser,
    SupplierReplyReviewer, UpstreamChaser, build_hints, put_hints,
)
from ..agents.base import Services, latest_ref
from ..authz import Actor
from ..guard.promotion import (
    new_cq, promote_mappings, promote_questions, promote_request, promote_request_after_confirm, promote_supplier_evidence,
    readback_questionnaire,
)
from ..statemachines import parse_status_reason
from .states import status_reason, writer_for

BATCH_SIZE = 25  # QuestionBatchWorkflow：每 25 题一个子工作流（04 §4.3）
NON_RETRYABLE = ("GRAPH_TRUST_ZONE_VIOLATION", "GRAPH_PROMOTION_DENIED", "GRAPH_NUM_VALUE_MISMATCH", "GRAPH_HALTED",
                 "BudgetExceeded", "LoopDetected", "Halted")


def _ref(o: dict[str, Any]) -> str:
    return f"{o['id']}#v{o['version']}"


class Activities:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.now_override: dt.datetime | None = None  # timer 推进后的外发时间（LocalRunner.sleep_until）

    @property
    def st(self):
        return self.svc.store

    @property
    def org(self) -> str:
        return self.svc.org_id

    def _idem(self, prefix: str):
        return lambda k: f"{prefix}:{k}"

    def _req(self, req_ref: str) -> dict[str, Any]:
        return self.st.get(req_ref.split("#")[0], org_id=self.org)

    # ------------------------------------------------------------------ 状态
    def set_request_status(self, idem: str, req_ref: str, status: str, reason: str | None = None) -> dict[str, Any]:
        req = self._req(req_ref)
        if req["status"] == status:
            return {"ref": _ref(req), "status": status, "noop": True}
        writer = writer_for(req["status"], status) or "service:workflow"
        patch: dict[str, Any] = {"id": req["id"], "status": status}
        if reason:
            patch["status_reason"] = reason
        ref = self.st.write("Request", patch, expected_version=req["version"], idempotency_key=f"{idem}:status",
                            actor=Actor.of(writer, self.org), patch_mode="merge_from_head")
        self.svc.guard.record_status(req["id"], status)  # LD-03
        return {"ref": ref, "status": status}

    def enter_awaiting(self, idem: str, req_ref: str, reason: str, return_to: str) -> dict[str, Any]:
        req = self._req(req_ref)
        if req["status"] == "awaiting_human":
            return {"ref": _ref(req), "status": "awaiting_human", "noop": True}
        return self.set_request_status(idem, req_ref, "awaiting_human", status_reason("awaiting_human", reason, return_to))

    def leave_awaiting(self, idem: str, req_ref: str) -> dict[str, Any]:
        req = self._req(req_ref)
        if req["status"] != "awaiting_human":
            return {"ref": _ref(req), "status": req["status"], "noop": True}
        info = parse_status_reason(req.get("status_reason"))
        return self.set_request_status(idem, req_ref, info["return_to"] if info else "parsed")

    def request_status(self, req_ref: str) -> dict[str, Any]:
        req = self._req(req_ref)
        return {"status": req["status"], "status_reason": req.get("status_reason"), "ref": _ref(req)}

    # ------------------------------------------------------------------ 晋升 / 解析
    def promote_request(self, idem: str, req_ref: str) -> dict[str, Any]:
        r = promote_request(self.st, self.org, req_ref.split("#")[0], self._idem(idem), overrides=self.svc.org.thresholds)
        return {"promoted": bool(r.promoted), "cq_refs": r.cq_refs, "flags": r.flags}

    def promote_request_confirmed(self, idem: str, req_ref: str, cq_refs: list[str]) -> dict[str, Any]:
        req_id = req_ref.split("#")[0]
        for cr in cq_refs:
            cq = self.st.get(cr.split("#")[0], org_id=self.org)
            ans = (cq or {}).get("answer") or {}
            if cq and cq["status"] == "answered" and ans.get("bool") is True:
                promote_request_after_confirm(self.st, self.org, req_id, cq["id"], self._idem(idem))
                return {"promoted": True, "cancelled": False}
            if cq and cq["status"] == "answered" and ans.get("bool") is False:
                req = self._req(req_ref)
                user = Actor.of(ans.get("answered_by", "user:unknown"), self.org, ("compliance_owner",))
                self.st.write("Request", {"id": req_id, "status": "cancelled"}, expected_version=req["version"],
                              actor=user, patch_mode="merge_from_head", idempotency_key=f"{idem}:cancel")
                return {"promoted": False, "cancelled": True}
        return {"promoted": False, "cancelled": True}

    def build_hints(self, idem: str, req_ref: str) -> dict[str, Any]:
        pq = readback_questionnaire(self.st, self.org, self._req(req_ref))
        hints = build_hints(self.svc, [{"qid": f"q{d.ordinal}", "text": d.text} for d in (pq.questions if pq else [])])
        return {"hints_ref": put_hints(self.svc, hints), "questions": len(hints["candidates"])}

    def parse_questions(self, idem: str, req_ref: str, hints_ref: str | None) -> dict[str, Any]:
        out = QuestionParser(self.svc).run(_ref(self._req(req_ref)), hints_ref, idem_prefix=idem)
        return {"questions": out.details["questions"], "mappings": out.details["mappings"], "degraded": out.details["degraded"],
                "methods": out.details["methods"]}

    def promote_questions(self, idem: str, req_ref: str) -> dict[str, Any]:
        r = promote_questions(self.st, self.org, req_ref.split("#")[0], self._idem(idem))
        cqs = []
        wf = Actor.of("service:workflow", self.org)
        for qid in r.failed:
            q = self.st.get(qid, org_id=self.org)
            cqs.append(new_cq(self.st, wf, self._idem(idem), key=f"qstruct:{qid}", target_ref=_ref(q), request_id=q["request_id"],
                              kind="yes_no", prompt={"de": "Frage korrekt aus dem Original erkannt?",
                                                     "en": "Was this question correctly recognised from the original?"},
                              tags=["PROMO-Q-STRUCT"] + [f for f in r.flags.get(qid, [])]))
        return {"promoted": len(r.promoted), "failed": r.failed, "cq_refs": cqs, "flags": r.flags}

    def question_batches(self, idem: str, req_ref: str) -> dict[str, Any]:
        qs = sorted(self.st.query("Question", org_id=self.org, request_id=req_ref.split("#")[0]), key=lambda q: q["ordinal"])
        ids = [q["id"] for q in qs]
        return {"batches": [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]}

    def promote_mappings_batch(self, idem: str, req_ref: str, question_ids: list[str]) -> dict[str, Any]:
        r = promote_mappings(self.st, self.org, req_ref.split("#")[0], self._idem(idem), overrides=self.svc.org.thresholds,
                             only_questions=set(question_ids))
        unmapped = [_ref(self.st.get(q, org_id=self.org)) for q in r.unmapped]
        return {"confirmed": len(r.promoted), "cq_refs": r.cq_refs, "unmapped_refs": unmapped}

    def finalize_mappings(self, idem: str, req_ref: str) -> dict[str, Any]:
        r = promote_mappings(self.st, self.org, req_ref.split("#")[0], self._idem(idem), overrides=self.svc.org.thresholds)
        return {"confirmed": len(r.promoted), "cq_refs": r.cq_refs, "unmapped": r.unmapped}

    # ------------------------------------------------------------------ 取证 / 计算
    def _default_boundary(self, info: Any) -> str:
        ab = list(info.allowed_boundaries) if info else ["organization"]
        o = self.svc.org
        if "organization" in ab:
            return "organization"
        if "product" in ab and o.default_product_id:
            return f"product:{o.default_product_id}"
        if "product_per_unit" in ab:
            return "product_per_unit"
        if "site" in ab and o.default_site_id:
            return f"site:{o.default_site_id}"
        return "organization"

    def targets(self, req_id: str) -> list[dict[str, Any]]:
        req = self.st.get(req_id, org_id=self.org)
        qs = {q["id"]: q for q in self.st.query("Question", org_id=self.org, request_id=req_id)}
        groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for m in self.st.query("Mapping", org_id=self.org, status="confirmed"):
            q = qs.get(m["question_id"])
            if q is None or m.get("trust_zone") != "trusted":
                continue
            info = self.svc.registry.get(m["datapoint_id"])
            period = m.get("period_hint") or q.get("period_requested") or req.get("reporting_period")
            if not period:
                continue
            boundary = m.get("boundary_hint") or self._default_boundary(info)
            if boundary.startswith("product") and boundary != "product_per_unit" and info and "product" in info.allowed_boundaries \
                    and self.svc.org.default_product_id:
                boundary = f"product:{self.svc.org.default_product_id}"
            elif boundary == "organization" and info and "organization" not in info.allowed_boundaries:
                boundary = self._default_boundary(info)
            k = (m["datapoint_id"], period["start"], period["end"], boundary)
            g = groups.setdefault(k, {"datapoint_id": m["datapoint_id"], "period": {"start": period["start"], "end": period["end"]},
                                      "boundary": boundary, "question_ids": [], "question_refs": []})
            g["question_ids"].append(q["id"])
            g["question_refs"].append(_ref(q))
        return sorted(groups.values(), key=lambda t: (t["datapoint_id"], t["period"]["start"]))

    def prefetch_evidence(self, idem: str, req_ref: str) -> dict[str, Any]:
        """等人时预取（04 §3.3）：只挖已确认 DataPoint 的证据，不改状态、不写 Answer。"""
        t = self.targets(req_ref.split("#")[0])
        out = EvidenceMiner(self.svc).run(_ref(self._req(req_ref)), t, idem_prefix=idem)
        return {"evidence": len(out.refs), "targets": len(t)}

    def _open_cqs(self, req_id: str) -> list[str]:
        return [_ref(c) for c in self.st.query("ClosedQuestion", org_id=self.org, status="open") if c.get("request_id") == req_id
                or (c.get("target_ref", "").startswith("ev_"))]

    def verify_supplier_evidence(self, idem: str, req_id: str) -> list[str]:
        """PROMO-EV-SUP 之后的 trusted candidate → 人核验（numeric_confirm，由 evidence-miner 出卡）。"""
        cqs = []
        miner = Actor.of("agent:evidence-miner", self.org)
        open_targets = {c["target_ref"].split("#")[0] for c in self.st.query("ClosedQuestion", org_id=self.org)}
        for e in self.st.query("Evidence", org_id=self.org, status="candidate"):
            if e.get("source_type") != "supplier_response" or e.get("trust_zone") != "trusted" or e["id"] in open_targets:
                continue
            pv = {"value": e["extracted_value"], "unit": e["unit"], "evidence_ref": _ref(e)} if e.get("value_type") == "number" else None
            cqs.append(new_cq(self.st, miner, self._idem(idem), key=f"supconfirm:{e['id']}", target_ref=_ref(e),
                              request_id=req_id, kind="numeric_confirm" if pv else "yes_no", proposed_value=pv,
                              prompt={"de": "Lieferantenwert bestätigen", "en": "Confirm supplier value"}, tags=["supplier_response"]))
        return cqs

    def gather_evidence(self, idem: str, req_ref: str, supplier_done: bool = False) -> dict[str, Any]:
        req_id = req_ref.split("#")[0]
        rref = _ref(self._req(req_ref))
        t = self.targets(req_id)
        sup_cqs = self.verify_supplier_evidence(idem + ":sup", req_id)
        mined = EvidenceMiner(self.svc).run(rref, t, idem_prefix=idem + ":mine")
        number_targets = [x for x in t if (self.svc.registry.get(x["datapoint_id"]) or None) and
                          self.svc.registry.get(x["datapoint_id"]).value_type == "number"]
        calc = CalcService(self.svc).run(rref, number_targets, idem_prefix=idem + ":calc")
        # 缺口合并：calc 输入缺口与 miner 缺口按 DataPoint 去重；已有数字答案/非数字有来源的不算缺口
        answered = {a["datapoint_id"] for a in self.st.query("Answer", org_id=self.org) if a["status"] in ("draft", "in_review", "approved")
                    and set(a.get("question_refs") or []) & {q for x in t for q in x["question_ids"]}}
        gaps: dict[str, dict[str, Any]] = {}
        for g in mined.gaps + calc.gaps:
            info = self.svc.registry.get(g["datapoint_id"])
            if g["datapoint_id"] in answered:
                continue
            if info and info.value_type != "number" and g.get("fact_key") is None and any(
                    d["datapoint_id"] == g["datapoint_id"] and (d["reuse"] or d["evidence"]) for d in mined.details.get("per_datapoint", [])):
                continue
            gaps.setdefault(g["datapoint_id"], g)
        open_cqs = [c for c in (mined.cq_refs + calc.cq_refs + sup_cqs)
                    if (self.st.get(c.split("#")[0], org_id=self.org) or {}).get("status") == "open"]
        supplier = [] if supplier_done else [g for g in gaps.values() if g["kind"] == "supplier"
                                             and g["datapoint_id"].split("@")[0] in self.svc.org.supplier_for_datapoints]
        internal = [g for g in gaps.values() if g not in supplier]
        gapset = self._gapset([g["datapoint_id"] for g in internal])
        confirmed = any(f"gapset:{gapset}" in (c.get("tags") or []) and c["status"] == "answered" and (c.get("answer") or {}).get("bool")
                        for c in self.st.query("ClosedQuestion", org_id=self.org))
        return {"cq_refs": sorted(set(open_cqs)), "supplier_gaps": supplier, "internal_gaps": internal, "gaps_confirmed": confirmed,
                "counts": {"targets": len(t), "evidence": len(mined.refs), "answers_number": calc.details.get("answers", 0),
                           "calculations": calc.details.get("calculations", 0), "gaps": len(gaps), "cq": len(open_cqs),
                           "degraded": bool(mined.details.get("degraded"))}}

    @staticmethod
    def _gapset(dps: list[str]) -> str:
        return hashlib.sha256("|".join(sorted(dps)).encode()).hexdigest()[:16]

    def confirm_gaps(self, idem: str, req_ref: str, gaps: list[dict[str, Any]]) -> dict[str, Any]:
        """T09 前置：无证据的 DataPoint 由人定性为"暂无数据/待人工撰写"（一张卡）。"""
        req = self._req(req_ref)
        dps = sorted({g["datapoint_id"] for g in gaps})
        labels = ", ".join(d.split(":", 1)[1].split("@")[0] for d in dps[:12]) + (" …" if len(dps) > 12 else "")
        cq = new_cq(self.st, Actor.of("service:workflow", self.org), self._idem(idem), key=f"gaps:{self._gapset(dps)}",
                    target_ref=_ref(req), request_id=req["id"], kind="yes_no",
                    prompt={"de": f"Für {len(dps)} Datenpunkte liegt kein Beleg vor ({labels}). Als 'keine Daten / manuell' markieren?",
                            "en": f"No evidence for {len(dps)} datapoints ({labels}). Mark as 'no data / manual'?"},
                    tags=["gaps-confirm", f"gapset:{self._gapset(dps)}"])
        return {"cq_refs": [cq], "datapoints": dps}

    # ------------------------------------------------------------------ 上游
    def chase_plan(self, idem: str, req_ref: str, gaps: list[dict[str, Any]]) -> dict[str, Any]:
        out = UpstreamChaser(self.svc).plan(_ref(self._req(req_ref)), gaps, idem_prefix=idem)
        dues = [self.st.get(r.split("#")[0], org_id=self.org)["due_date"] for r in out.refs]
        return {"sreq_refs": out.refs, "approval_refs": out.cq_refs, "due_date": min(dues) if dues else None,
                "no_supplier": out.details.get("no_supplier", [])}

    def chase_send(self, idem: str, req_ref: str, sreq_refs: list[str]) -> dict[str, Any]:
        ch = UpstreamChaser(self.svc)
        sent, deferred, denied, retry = [], [], [], []
        for r in sreq_refs:
            res = ch.send(r.split("#")[0], idem_prefix=f"{idem}:{r.split('#')[0]}", request_id=req_ref.split("#")[0],
                          now=getattr(self, "now_override", None))
            if res["status"] == "sent":
                sent.append(res["sreq_ref"])
            elif res["status"] == "deferred":
                deferred.append(r)
                retry.append(res["denied"].get("retry_after"))
            else:
                denied.append({"sreq_ref": r, **res["denied"]})
        return {"sent": sent, "deferred": deferred, "retry_after": min(retry) if retry else None, "denied": denied}

    def supplier_status(self, sreq_refs: list[str]) -> dict[str, Any]:
        out = {}
        for r in sreq_refs:
            s = self.st.get(r.split("#")[0], org_id=self.org)
            out[s["id"]] = s["status"]
        return out

    def review_replies(self, idem: str, req_ref: str, replies: list[dict[str, Any]]) -> dict[str, Any]:
        """隔离区审核每个回复 → workflow 写 SupplierRequest responded/reviewed → guard PROMO-EV-SUP。"""
        import json

        wf = Actor.of("service:workflow", self.org)
        ev_refs, cqs = [], []
        for rep in replies:
            sid = rep["sreq_id"]
            blob = self.st.get_blob(self.org, rep["blob_sha256"]) if rep.get("blob_sha256") else None
            kw: dict[str, Any] = {}
            if blob and rep.get("kind") == "submission":
                kw["submission"] = json.loads(blob[0])
            elif blob:
                kw["reply_text"] = blob[0].decode("utf-8", "replace")
            s = self.st.get(sid, org_id=self.org)
            if s["status"] in ("sent", "reminded", "escalated"):
                self.st.write("SupplierRequest", {"id": sid, "status": "responded"}, expected_version=s["version"], actor=wf,
                              patch_mode="merge_from_head", idempotency_key=f"{idem}:{sid}:responded")
            out = SupplierReplyReviewer(self.svc).run(f"{sid}", idem_prefix=f"{idem}:{sid}", **kw)
            ev_refs += out.refs
            for er in out.refs:
                r = promote_supplier_evidence(self.st, self.org, er.split("#")[0], self._idem(f"{idem}:{sid}"))
                cqs += r.cq_refs
            s = self.st.get(sid, org_id=self.org)
            if s["status"] == "responded":
                self.st.write("SupplierRequest", {"id": sid, "status": "reviewed"}, expected_version=s["version"], actor=wf,
                              patch_mode="merge_from_head", idempotency_key=f"{idem}:{sid}:reviewed")
        return {"evidence": ev_refs, "cq_refs": cqs}

    def expire_supplier_requests(self, idem: str, sreq_refs: list[str]) -> dict[str, Any]:
        wf = Actor.of("service:workflow", self.org)
        out = []
        for r in sreq_refs:
            s = self.st.get(r.split("#")[0], org_id=self.org)
            if s["status"] in ("sent", "reminded", "escalated"):
                out.append(self.st.write("SupplierRequest", {"id": s["id"], "status": "expired"}, expected_version=s["version"],
                                         actor=wf, patch_mode="merge_from_head", idempotency_key=f"{idem}:{s['id']}:expired"))
        return {"expired": out}

    # ------------------------------------------------------------------ 起草 / 审计 / 审批
    def draft_answers_batch(self, idem: str, req_ref: str, question_ids: list[str]) -> dict[str, Any]:
        req_id = req_ref.split("#")[0]
        qset = set(question_ids)
        targets = []
        for t in self.targets(req_id):
            if not qset & set(t["question_ids"]):
                continue
            base = t["datapoint_id"].split("@")[0]
            t["reuse"] = [_ref(a) for a in self.st.query("Answer", org_id=self.org, status="submitted", datapoint_base=base)
                          if a.get("trust_zone") == "trusted" and a.get("value_type") != "number"]
            t["evidence_refs"] = [_ref(e) for e in self.st.query("Evidence", org_id=self.org, status="verified")
                                  if (e.get("datapoint_id") or "").split("@")[0] == base and e.get("trust_zone") == "trusted"
                                  and e.get("value_type") != "number"]
            targets.append(t)
        out = AnswerDrafter(self.svc).run(_ref(self._req(req_ref)), targets, idem_prefix=idem)
        return {"answers": len(out.refs), "pending_manual": len(out.details.get("pending_manual", [])),
                "pending_manual_datapoints": [p["datapoint_id"] for p in out.details.get("pending_manual", [])]}

    def request_answers(self, req_id: str, statuses: tuple[str, ...] = ("draft",)) -> list[dict[str, Any]]:
        qids = {q["id"] for q in self.st.query("Question", org_id=self.org, request_id=req_id)}
        return [a for a in self.st.query("Answer", org_id=self.org) if a["status"] in statuses and set(a.get("question_refs") or []) & qids]

    def audit(self, idem: str, req_ref: str) -> dict[str, Any]:
        out = ConsistencyAuditorAgent(self.svc).run(_ref(self._req(req_ref)), idem_prefix=idem)
        d = out.details
        return {"gate": d.get("gate", "pass"), "blocking": d.get("blocking", []) if d.get("gate") == "block" else [],
                "conflicts": len(d.get("conflicts_written", [])), "implementation": d.get("implementation")}

    def route_approvals(self, idem: str, req_ref: str) -> dict[str, Any]:
        req_id = req_ref.split("#")[0]
        answers = [_ref(a) for a in self.request_answers(req_id)]
        out = ApprovalRouterAgent(self.svc).run(_ref(self._req(req_ref)), answers, idem_prefix=idem)
        pending = [_ref(a) for a in self.st.query("Approval", org_id=self.org, status="pending") if a.get("request_id") == req_id
                   and a["subject_ref"].startswith("ans_")]
        return {"approval_refs": sorted(set(out.details["approval_refs"]) | set(pending)), "levels": out.details["levels"],
                "blocked": len(out.details["blocked"]), "implementation": out.details["implementation"]}

    def approvals_outcome(self, apr_refs: list[str]) -> dict[str, Any]:
        st = {"pending": [], "approved": [], "rejected": [], "modified": []}
        for r in apr_refs:
            a = self.st.get(r.split("#")[0], org_id=self.org)
            st.setdefault(a["status"], []).append(r)
        if st["pending"]:
            return {"outcome": None, "pending": st["pending"]}
        if st["modified"]:
            return {"outcome": "modified", "pending": []}
        if st["rejected"]:
            return {"outcome": "rejected", "pending": []}
        return {"outcome": "approved", "pending": []}

    def check_pending(self, refs: list[str]) -> dict[str, Any]:
        """未解决的人工项：CQ open、Approval pending、Conflict open、Question unmapped/parsed、SupplierRequest 未回复。"""
        pending = []
        for r in refs:
            o = self.st.get(r.split("#")[0], org_id=self.org)
            if o is None:
                continue
            pfx = o["id"].split("_")[0]
            s = o.get("status")
            if (pfx == "cq" and s == "open") or (pfx == "apr" and s == "pending") or (pfx == "cfl" and s == "open") or \
                    (pfx == "q" and s in ("parsed", "unmapped")) or (pfx == "sreq" and s in ("draft", "sent", "reminded")):
                pending.append(r)
        return {"pending": pending}

    def escalate(self, idem: str, refs: list[str]) -> dict[str, Any]:
        """超时升级（CQ 逾期 → executive；永不自动批准，06 §4.3）：写 AuditEvent，状态不变。"""
        for r in refs:
            self.st.record_audit(Actor.of("service:workflow", self.org), "workflow.escalate", object_ref=r,
                                 details={"to": "executive"})
        return {"escalated": len(refs)}

    # ------------------------------------------------------------------ 填报 / 归档
    def fill_file(self, idem: str, req_ref: str) -> dict[str, Any]:
        req = self._req(req_ref)
        out = FillerFile(self.svc).run(_ref(req), idem_prefix=idem)
        cq = new_cq(self.st, Actor.of("service:workflow", self.org), self._idem(idem), key="submit-confirm",
                    target_ref=_ref(req), request_id=req["id"], kind="yes_no",
                    prompt={"de": "Exportdatei geprüft – an den Kunden übermitteln?", "en": "Export reviewed – submit to the customer?"},
                    tags=["submit-confirm", f"export:{out.details.get('export_blob')}"])
        return {"export_blob": out.details.get("export_blob"), "export_input_hash": out.details.get("export_input_hash"),
                "written": out.details.get("written", 0), "rejected": out.details.get("rejected", []), "cq_refs": [cq]}

    def mark_submitted(self, idem: str, req_ref: str, export_input_hash: str) -> dict[str, Any]:
        out = FillerFile(self.svc).mark_submitted(_ref(self._req(req_ref)), export_input_hash, idem_prefix=idem)
        return {"submitted": len(out.refs) - 1}

    def archive_learn(self, idem: str, req_ref: str) -> dict[str, Any]:
        """学习回写（04 §3.2 T17）：已确认映射进入 C2 历史题索引（只存 dp: ID）。"""
        from ..llm.embedding import normalize

        req_id = req_ref.split("#")[0]
        n = 0
        qs = {q["id"]: q for q in self.st.query("Question", org_id=self.org, request_id=req_id)}
        for m in self.st.query("Mapping", org_id=self.org, status="confirmed"):
            q = qs.get(m["question_id"])
            if q:
                self.svc.ref_cache.put("C2", normalize(" ".join(q["text"].values())), m["datapoint_id"].split("@")[0])
                n += 1
        return {"learned": n}

    def halt(self, idem: str, req_ref: str, reason: str, return_to: str, detail: str = "") -> dict[str, Any]:
        ref = self.svc.guard.halt(self.org, req_ref.split("#")[0], reason, return_to, detail=detail)
        return {"ref": ref, "reason": reason, "return_to": return_to}

    # ------------------------------------------------------------------ progress
    def progress(self, req_ref: str) -> dict[str, Any]:
        req_id = req_ref.split("#")[0]
        req = self.st.get(req_id, org_id=self.org)
        qs = self.st.query("Question", org_id=self.org, request_id=req_id)
        by: dict[str, int] = {}
        for q in qs:
            by[q["status"]] = by.get(q["status"], 0) + 1
        ans: dict[str, int] = {}
        for a in self.request_answers(req_id, ("draft", "in_review", "approved", "submitted")):
            ans[a["status"]] = ans.get(a["status"], 0) + 1
        return {"request_id": req_id, "status": req["status"], "status_reason": req.get("status_reason"),
                "questions": len(qs), "question_status": by, "answers": ans,
                "open_cq": len([c for c in self.st.query("ClosedQuestion", org_id=self.org, status="open") if c.get("request_id") == req_id])}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
