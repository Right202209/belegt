"""06 子任务测试辅助：内存对象工厂（规则单元测试）与图上场景构造（端到端）。"""
from __future__ import annotations

import datetime as dt
from typing import Any

from belegt_core.consistency import AuditContext, ConsistencyEngine, Scope
from belegt_core.ids import new_id

ORG = "org_01JC0A1B2C3D4E5F6G7H8J9K0M"
CP_A = "cp_01JC2B3C4D5E6F7G8H9J0K1M2N"
CP_B = "cp_01JC2B3C4D5E6F7G8H9J0K1M2P"
Y25 = ("2025-01-01", "2025-12-31")
Y24 = ("2024-01-01", "2024-12-31")
TS = "2026-10-01T08:00:00Z"


def _p(period: tuple[str, str]) -> dict[str, str]:
    return {"start": period[0], "end": period[1]}


def ev(value: Any = "100", unit: str | None = "MWh", fact_key: str = "activity.electricity.purchased", *,
       period: tuple[str, str] | None = Y25, valid_from: str = "2025-01-01", valid_until: str | None = None,
       status: str = "verified", vt: str = "number", boundary: str = "organization", dp: str | None = None,
       version: int = 1, id: str | None = None, **kw: Any) -> dict[str, Any]:
    o = {"id": id or new_id("Evidence"), "org_id": ORG, "version": version, "created_at": TS, "created_by": "agent:evidence-miner",
         "trust_zone": "trusted", "source_type": "document", "fact_key": fact_key, "extracted_value": value, "value_type": vt,
         "boundary": boundary, "locator": {"page": 1}, "valid_from": valid_from, "confidence": 0.95, "status": status, **kw}
    if unit:
        o["unit"] = unit
    if period:
        o["period"] = _p(period)
    if valid_until:
        o["valid_until"] = valid_until
    if dp:
        o["datapoint_id"] = dp
    return o


def ans(dp: str, value: Any, unit: str | None = None, *, period: tuple[str, str] = Y25, boundary: str = "organization",
        consolidation: str | None = "operational_control", cs: Any = "all", status: str = "approved", vt: str = "number",
        evidence: list[dict] | None = None, calc: dict | None = None, questions: list[dict] | None = None,
        version: int = 1, id: str | None = None, **kw: Any) -> dict[str, Any]:
    if "@" not in dp:
        dp = dp + "@1.0.0"
    o: dict[str, Any] = {"id": id or new_id("Answer"), "org_id": ORG, "version": version, "created_at": TS,
                         "created_by": "agent:calc-service", "trust_zone": "trusted", "datapoint_id": dp, "value": value,
                         "value_type": vt, "period": _p(period), "boundary": boundary, "customer_scope": cs,
                         "origin": "generated", "status": status, "is_estimate": False, "legal_commitment": False, **kw}
    if unit:
        o["unit"] = unit
    if consolidation:
        o["consolidation"] = consolidation
    if evidence is not None:
        o["evidence_refs"] = [f"{e['id']}#v{e['version']}" for e in evidence]
    if calc is not None:
        o["calculation_ref"] = f"{calc['id']}#v{calc['version']}"
    if questions is not None:
        o["question_refs"] = [q["id"] for q in questions]
    return o


def calc(inputs: list[dict], value: str, unit: str, *, factors: list[dict] | None = None, fs: str = "fs:aib-residual-mix@2025.1",
         status: str = "computed", version: int = 1, id: str | None = None, output_hash: str = "sha256:" + "a" * 64,
         **kw: Any) -> dict[str, Any]:
    return {"id": id or new_id("Calculation"), "org_id": ORG, "version": version, "created_at": TS,
            "created_by": "agent:calc-service", "trust_zone": "trusted", "method_id": "m", "methodology_version": "x@1.0.0",
            "factor_set_version": fs, "engine_version": "belegt-calc@0.4.0",
            "inputs": [{"name": f"i{n}", "ref": f"{i['id']}#v{i['version']}", "value": str(i.get("extracted_value", "0")),
                        "unit": i.get("unit", "MWh")} for n, i in enumerate(inputs)],
            "factors_used": factors or [], "result": {"value": value, "unit": unit, "rounding": {"mode": "half_even", "digits": 1}},
            "input_hash": "sha256:" + "b" * 64, "output_hash": output_hash, "boundary": "organization",
            "period": _p(Y25), "customer_scope": "all", "status": status, **kw}


def req(cp: str = CP_A, *, due: str | None = "2026-12-15", status: str = "drafting", tags: list[str] | None = None,
        reporting_period: tuple[str, str] | None = None, id: str | None = None) -> dict[str, Any]:
    o = {"id": id or new_id("Request"), "org_id": ORG, "version": 1, "created_at": TS, "created_by": "service:workflow",
         "trust_zone": "trusted", "counterparty_id": cp, "channel": "email", "source_ref": {}, "title": "OEM Fragebogen",
         "language": "de", "priority": "normal", "triage_confidence": 0.95, "status": status}
    if due:
        o["due_date"] = due
    if tags:
        o["tags"] = tags
    if reporting_period:
        o["reporting_period"] = _p(reporting_period)
    return o


def q(request: dict, ordinal: int = 1, *, text: str = "Frage", tags: list[str] | None = None,
      period_requested: tuple[str, str] | None = None, id: str | None = None) -> dict[str, Any]:
    o = {"id": id or new_id("Question"), "org_id": ORG, "version": 1, "created_at": TS, "created_by": "user:u_co",
         "trust_zone": "trusted", "request_id": request["id"], "ordinal": ordinal, "text": {"de": text},
         "answer_format": "number", "locator": {"page": 1}, "status": "mapped"}
    if tags:
        o["tags"] = tags
    if period_requested:
        o["period_requested"] = _p(period_requested)
    return o


def conflict(rule_id: str, subjects: list[dict], *, status: str = "open", severity: str = "blocking",
             resolution: dict | None = None, dp: str = "dp:env.ghg.scope1_total@1.0.0") -> dict[str, Any]:
    o = {"id": new_id("Conflict"), "org_id": ORG, "version": 1, "created_at": TS, "created_by": "agent:consistency-auditor",
         "trust_zone": "trusted", "rule_id": rule_id, "severity": severity, "datapoint_id": dp,
         "subject_refs": [f"{s['id']}#v{s['version']}" for s in subjects], "detail": {"values": [{"ref": "x", "value": "1"}]},
         "status": status}
    if resolution:
        o["resolution"] = resolution
    return o


def make_ctx(objs: list[dict], today: str = "2026-11-01", **kw: Any) -> AuditContext:
    return AuditContext(ORG, [dict(o) for o in objs], today=today, **kw)


_ENGINE = ConsistencyEngine()


def run_rule(rule_id: str, objs: list[dict], *, today: str = "2026-11-01", trigger: str = "daily_batch",
             scope: Scope | None = None, **kw: Any):
    ctx = make_ctx(objs, today=today, **kw)
    return _ENGINE.evaluate(ctx, scope or Scope(trigger), only=[rule_id])


def outcomes(results) -> list[str]:
    return [r.outcome for r in results]


def fails(results, severity: str | None = None):
    return [r for r in results if r.outcome == "fail" and (severity is None or r.severity == severity)]


def utc(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ====================================================================== 图上场景（GraphStore 端到端）
from belegt_core import Actor  # noqa: E402

CO = Actor.of("user:u_compliance01", ORG, ["compliance_owner"])
CO2 = Actor.of("user:u_compliance02", ORG, ["compliance_owner"])
EXEC = Actor.of("user:u_exec01", ORG, ["executive"])
ADMIN = Actor.of("user:u_admin", ORG, ["org_admin"])
CONTRIB = Actor.of("user:u_contrib01", ORG, ["contributor"])
MINER = Actor.of("agent:evidence-miner", ORG)
CALC_A = Actor.of("agent:calc-service", ORG)
DRAFTER = Actor.of("agent:answer-drafter", ORG)
FILLER = Actor.of("agent:filler", ORG)
CHASER = Actor.of("agent:upstream-chaser", ORG)
BLOB = "sha256:" + "c" * 64


class World:
    """用真实写入路径（GraphStore + 规则）构造场景。"""

    def __init__(self, store, *, with_org: bool = True) -> None:
        self.s = store
        if with_org and store.head(ORG, org_id=ORG) is None:
            store.write("Organization", {"id": ORG, "legal_name": "Musterteile GmbH", "country": "DE",
                                         "default_consolidation": "operational_control", "data_residency": "eu",
                                         "locale_default": "de"}, actor=ADMIN)
            self.counterparty(CP_A, "OEM-A AG")
            self.counterparty(CP_B, "OEM-B SE")

    def counterparty(self, cp_id: str | None = None, name: str = "Kunde", role: str = "customer", tags=None) -> str:
        cp_id = cp_id or new_id("Counterparty")
        body = {"id": cp_id, "role": role, "legal_name": name, "country": "DE", "domains": ["x.example"]}
        if tags:
            body["tags"] = tags
        self.s.write("Counterparty", body, actor=ADMIN)
        return cp_id

    def request(self, cp: str = CP_A, *, due: str | None = "2026-12-15", tags=None, title: str = "OEM Fragebogen") -> str:
        body = {"id": new_id("Request"), "counterparty_id": cp, "channel": "email", "source_ref": {"blob_sha256": [BLOB]}, "title": title,
                "language": "de", "priority": "normal", "triage_confidence": 0.95, "status": "received"}
        if due:
            body["due_date"] = due
        if tags:
            body["tags"] = tags
        return self.s.write("Request", body, actor=CO).split("#")[0]

    def question(self, req_id: str, ordinal: int = 1, text: str = "Frage", *, tags=None, period_requested=None) -> str:
        body = {"id": new_id("Question"), "request_id": req_id, "ordinal": ordinal, "text": {"de": text},
                "answer_format": "number", "locator": {"blob_sha256": BLOB, "page": 1}, "status": "parsed"}
        if tags:
            body["tags"] = tags
        if period_requested:
            body["period_requested"] = _p(period_requested)
        return self.s.write("Question", body, actor=CO).split("#")[0]

    def evidence(self, value: str = "4820", unit: str = "MWh", fact_key: str = "activity.electricity.purchased", *,
                 period=Y25, valid_from: str = "2025-01-01", valid_until: str | None = None, dp: str | None = None,
                 corrects: str | None = None, tags=None, source_type: str = "document") -> str:
        body = {"id": new_id("Evidence"), "source_type": source_type, "fact_key": fact_key, "extracted_value": value,
                "value_type": "number", "unit": unit, "boundary": "organization",
                "locator": {"blob_sha256": BLOB, "mime": "application/pdf", "page": 1}, "valid_from": valid_from,
                "confidence": 0.95, "status": "candidate"}
        if period:
            body["period"] = _p(period)
        if valid_until:
            body["valid_until"] = valid_until
        if dp:
            body["datapoint_id"] = dp
        if corrects:
            body["corrects"] = corrects
        if tags:
            body["tags"] = tags
        self.s.write("Evidence", body, actor=MINER)
        return self.s.write("Evidence", {"id": body["id"], "status": "verified",
                                         "verification": {"method": "auto_rule", "rule_id": "EV-AUTO-0.90-LOC", "by": MINER.id}},
                            expected_version=1, actor=MINER, patch_mode="merge_from_head")

    def answer(self, dp: str, value: Any, unit: str | None = None, *, evidence: list[str] | None = None, questions=None,
               cs: Any = "all", period=Y25, consolidation: str | None = "operational_control", boundary="organization",
               status: str = "draft", vt: str = "number", request_for_submission: str | None = None, **kw) -> str:
        if "@" not in dp:
            dp += "@1.0.0"
        body: dict[str, Any] = {"id": new_id("Answer"), "datapoint_id": dp, "value": value, "value_type": vt,
                                "period": _p(period), "boundary": boundary, "customer_scope": cs, "origin": "generated",
                                "status": status, "is_estimate": False, "legal_commitment": False, **kw}
        if unit:
            body["unit"] = unit
        if consolidation:
            body["consolidation"] = consolidation
        if evidence:
            body["evidence_refs"] = list(evidence)
        if questions:
            body["question_refs"] = list(questions)
        if vt == "text":
            body.setdefault("text_lang", "de")
        actor = CALC_A if vt == "number" else DRAFTER
        final = status
        if status in ("approved", "submitted"):
            body["status"] = "draft"
        ref = self.s.write("Answer", body, actor=actor)
        if final in ("approved", "submitted"):
            ref = self.approve(ref)
        if final == "submitted":
            h = self.s.get(ref)
            rid = request_for_submission or (self.s.get(questions[0])["request_id"] if questions else self.request())
            ref = self.s.write("Answer", {"id": h["id"], "status": "submitted", "submission": {
                "request_id": rid, "channel": "file_export", "submitted_at": "2026-03-01T10:00:00Z",
                "export_input_hash": "sha256:" + "d" * 64}}, expected_version=h["version"], actor=FILLER,
                patch_mode="merge_from_head")
        return ref

    def approve(self, ans_ref: str, actor=None) -> str:
        from belegt_core.approval import ApprovalRouter

        router = ApprovalRouter(self.s)
        res = router.route(ORG, answer_refs=[ans_ref])
        assert res.routed, res.to_dict()
        r = res.routed[0]
        router.decide(r.approval_ref.split("#")[0], actor=actor or CO, decision="approved",
                      confirmations={"estimate_ack": True, "warn_note": "ok", "step_up_auth": True, "clause_read_ack": True})
        h = self.s.head(ans_ref.split("#")[0])
        return f"{h.id}#v{h.version}"
