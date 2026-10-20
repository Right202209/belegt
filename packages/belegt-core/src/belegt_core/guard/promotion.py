# Spec: 04 §2.1（PROMO-REQ-0.90、PROMO-Q-STRUCT、PROMO-MAP-0.85 / CQ-MAP-BAND、PROMO-EV-SUP）、§5.1 GP-06、
#       07 §2.3（PC-01…PC-08：只盖章不改值）、03 §3.11（ClosedQuestion）
"""guard 晋升：只把 quarantine 对象"盖章"为 trusted 新版本（``created_by=agent:guard``、``promotion``），不改值。

失败时不晋升，改为生成 ``ClosedQuestion``（由 ``service:workflow`` 代写：图写入规则禁止 guard 新建对象，见 README）。
阈值按租户可调但只能收紧（``thresholds`` 中小于默认值的设置被忽略）。
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable

from ..authz import Actor
from ..errors import GraphError
from ..models.common import SCHEMA_VERSION
from ..store import GraphStore, WriteItem
from .injection import scan

DEFAULT_THRESHOLDS = {"PROMO-REQ-0.90": 0.90, "PROMO-MAP-0.85": 0.85, "PROMO-MAP-GAP": 0.10, "CQ-MAP-BAND-LOW": 0.60}
MIME_SUFFIX = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel.sheet.macroEnabled.12": ".xlsm", "text/csv": ".csv", "application/pdf": ".pdf",
}
PARSABLE = set(MIME_SUFFIX) | {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def thresholds(overrides: dict[str, float] | None = None) -> dict[str, float]:
    t = dict(DEFAULT_THRESHOLDS)
    for k, v in (overrides or {}).items():
        if k in t and (v >= t[k] if k != "CQ-MAP-BAND-LOW" else v >= t[k]):
            t[k] = v  # 只收紧（04 §2.1）
    return t


def _ts(store: GraphStore) -> str:
    return store.peek_now()[:19] + "Z"


def _plus_hours(store: GraphStore, h: int) -> str:
    d = dt.datetime.strptime(store.peek_now()[:19], "%Y-%m-%dT%H:%M:%S") + dt.timedelta(hours=h)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def promotion(store: GraphStore, rule: str) -> dict[str, str]:
    return {"rule": rule, "schema_version": SCHEMA_VERSION, "checked_at": _ts(store)}


@dataclass
class PromotionResult:
    promoted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    cq_refs: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    flags: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("promoted", "rejected", "cq_refs", "unmapped", "failed", "flags")}


Idem = Callable[[str], str]


def _stamp(obj_id: str, version: int, rule: str | None, store: GraphStore, **status: Any) -> WriteItem:
    body: dict[str, Any] = {"id": obj_id, **status}
    if rule:
        body["trust_zone"] = "trusted"
        body["promotion"] = promotion(store, rule)
    return WriteItem("_", body, expected_version=version, patch_mode="merge_from_head")


def _write_items(store: GraphStore, type_name: str, items: list[WriteItem], actor: Actor, idem: Idem, tag: str) -> list[str]:
    out = []
    for i in range(0, len(items), 400):
        chunk = items[i:i + 400]
        todo = []
        for it in chunk:
            it.type = type_name
            it.idempotency_key = idem(f"{tag}:{it.object['id']}:{it.expected_version}")
            prev = idempotent_ref(store, actor.org_id, it.idempotency_key)
            if prev:
                out.append(prev)
            else:
                todo.append(it)
        if todo:
            out.extend(store.write_batch(todo, actor=actor))
    return out


def idempotent_ref(store: GraphStore, org_id: str, key: str) -> str | None:
    """幂等表中仍有效的同键写入结果（重试/重放时直接复用，避免体内时间戳不同导致 GRAPH_IDEMPOTENCY_MISMATCH）。"""
    from sqlalchemy import select

    from ..store import tables as T

    with store.engine.connect() as conn:
        row = conn.execute(select(T.idempotency.c.response, T.idempotency.c.expires_at).where(
            T.idempotency.c.org_id == org_id, T.idempotency.c.idempotency_key == key)).first()
    if row and row.expires_at > store.peek_now():
        return dict(row.response).get("object_ref")
    return None


def new_cq(store: GraphStore, wf: Actor, idem: Idem, *, key: str, target_ref: str, request_id: str | None, kind: str,
           prompt: dict[str, str], assignee_role: str = "compliance_owner", options: list[dict[str, Any]] | None = None,
           proposed_value: dict[str, Any] | None = None, context_refs: list[str] | None = None, hours: int = 72,
           tags: list[str] | None = None) -> str:
    body: dict[str, Any] = {"target_ref": target_ref, "kind": kind, "prompt": prompt, "assignee_role": assignee_role,
                            "expires_at": _plus_hours(store, hours), "status": "open"}
    if request_id:
        body["request_id"] = request_id
    if options:
        body["options"] = options
    if proposed_value:
        body["proposed_value"] = proposed_value
    if context_refs:
        body["context_refs"] = context_refs
    if tags:
        body["tags"] = tags
    k = idem("cq:" + key)
    prev = idempotent_ref(store, wf.org_id, k)
    if prev:
        return prev
    return store.write("ClosedQuestion", body, idempotency_key=k, actor=wf)


# ---------------------------------------------------------------------------- PROMO-REQ-0.90
def promote_request(store: GraphStore, org_id: str, req_id: str, idem: Idem, *, overrides: dict[str, float] | None = None
                    ) -> PromotionResult:
    th = thresholds(overrides)
    g, wf = Actor.of("agent:guard", org_id), Actor.of("service:workflow", org_id)
    res = PromotionResult()
    req = store.get(req_id, org_id=org_id)
    if req is None:
        raise GraphError("GRAPH_NOT_FOUND", f"{req_id} not found")
    if req["trust_zone"] == "trusted":
        res.promoted.append(f"{req_id}#v{req['version']}")
        return res
    reasons = []
    if float(req.get("triage_confidence", 0)) < th["PROMO-REQ-0.90"]:
        reasons.append("confidence")
    cp = store.get(req["counterparty_id"], org_id=org_id)
    if not cp or cp.get("role") != "customer" or not cp.get("domains") or "candidate" in (cp.get("tags") or []):
        reasons.append("counterparty")
    mid = (req.get("source_ref") or {}).get("message_id") or ""
    if cp and mid and "@" in mid:
        dom = mid.strip("<>").rsplit("@", 1)[-1].lower()
        if not any(dom == d or dom.endswith("." + d) for d in cp.get("domains") or []):
            reasons.append("sender_domain")
    parsable = False
    for sha in (req.get("source_ref") or {}).get("blob_sha256") or []:
        b = store.get_blob(org_id, sha)
        if b and (b[1] in PARSABLE):
            parsable = True
    if not parsable:
        reasons.append("attachment")
    flags = scan(req.get("title") or "").flags  # PC-05
    if flags:
        reasons.append("injection")
        res.flags[req_id] = flags
    if reasons:
        res.failed.append(req_id)
        res.cq_refs.append(new_cq(store, wf, idem, key=f"req:{req_id}", target_ref=f"{req_id}#v{req['version']}",
                                  request_id=req_id, kind="yes_no", hours=48,
                                  prompt={"de": "Ist diese Nachricht ein Fragebogen/Datenabfrage eines Kunden?",
                                          "en": "Is this message a customer questionnaire / data request?"},
                                  tags=[f"promo-fail:{r}" for r in reasons]))
        return res
    it = _stamp(req_id, req["version"], "PROMO-REQ-0.90", store, status="triaged")
    res.promoted += _write_items(store, "Request", [it], g, idem, "promote-req")
    return res


def promote_request_after_confirm(store: GraphStore, org_id: str, req_id: str, cq_id: str, idem: Idem) -> str | None:
    """人经 ClosedQuestion 确认后（T02 人确认），guard 盖章：promotion.rule = ``CQ-CONFIRMED:<cq_id>``。"""
    req = store.get(req_id, org_id=org_id)
    if req is None or req["trust_zone"] == "trusted":
        return None
    it = _stamp(req_id, req["version"], f"CQ-CONFIRMED:{cq_id}", store, status="triaged")
    return _write_items(store, "Request", [it], Actor.of("agent:guard", org_id), idem, "promote-req-cq")[0]


# ---------------------------------------------------------------------------- PROMO-Q-STRUCT
def readback_questionnaire(store: GraphStore, org_id: str, req: dict[str, Any]):
    """从原件回读（belegt-io）。返回 ``ParsedQuestionnaire`` 或 None。"""
    from belegt_io import parse_questionnaire

    for sha in (req.get("source_ref") or {}).get("blob_sha256") or []:
        b = store.get_blob(org_id, sha)
        if not b or b[1] not in MIME_SUFFIX or b[1] == "application/pdf":
            continue
        fd, path = tempfile.mkstemp(suffix=MIME_SUFFIX[b[1]])
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(b[0])
            return parse_questionnaire(path)
        finally:
            os.unlink(path)
    return None


def promote_questions(store: GraphStore, org_id: str, req_id: str, idem: Idem) -> PromotionResult:
    from belegt_io import Locator as IOLocator

    g = Actor.of("agent:guard", org_id)
    res = PromotionResult()
    req = store.get(req_id, org_id=org_id)
    pq = readback_questionnaire(store, org_id, req or {})
    by_loc = pq.by_locator() if pq else {}
    items = []
    for q in store.query("Question", org_id=org_id, request_id=req_id):
        if q["trust_zone"] == "trusted":
            continue
        try:
            key = str(IOLocator(**{k: v for k, v in q["locator"].items() if k in ("blob_sha256", "mime", "page", "sheet", "cell")}))
        except Exception:
            key = None
        draft = by_loc.get(key) if key else None
        ok = draft is not None and dict(draft.text) == dict(q["text"])
        flags = scan(" ".join(q["text"].values())).flags
        if flags:
            res.flags[q["id"]] = flags
        if not ok or flags:
            res.failed.append(q["id"])
            continue
        items.append(_stamp(q["id"], q["version"], "PROMO-Q-STRUCT", store, status="parsed"))
    res.promoted += _write_items(store, "Question", items, g, idem, "promote-q")
    return res


# ---------------------------------------------------------------------------- PROMO-MAP-0.85 / CQ-MAP-BAND
def promote_mappings(store: GraphStore, org_id: str, req_id: str, idem: Idem, *, overrides: dict[str, float] | None = None,
                     only_questions: set[str] | None = None) -> PromotionResult:
    th = thresholds(overrides)
    g, wf = Actor.of("agent:guard", org_id), Actor.of("service:workflow", org_id)
    res = PromotionResult()
    maps_by_q: dict[str, list[dict[str, Any]]] = {}
    for m in store.query("Mapping", org_id=org_id):
        maps_by_q.setdefault(m["question_id"], []).append(m)
    qs = [q for q in store.query("Question", org_id=org_id, request_id=req_id)
          if q["trust_zone"] == "trusted" and q["status"] == "parsed" and (only_questions is None or q["id"] in only_questions)]
    confirm_items, reject_items, q_mapped, q_unmapped = [], [], [], []
    for q in qs:
        ms = maps_by_q.get(q["id"], [])
        if any(m["status"] == "confirmed" for m in ms):
            q_mapped.append(_stamp(q["id"], q["version"], None, store, status="mapped"))
            continue
        cands = sorted((m for m in ms if m["status"] == "candidate"), key=lambda m: m["rank"])
        if not cands:
            q_unmapped.append(_stamp(q["id"], q["version"], None, store, status="unmapped"))
            res.unmapped.append(q["id"])
            continue
        top = cands[0]
        # 置信度是 3 位小数的浮点；差值按 6 位舍入再比较，避免 0.85 − 0.75 = 0.0999… 把恰好 0.10 的间隔判为不足
        gap = round(float(top["confidence"]) - (float(cands[1]["confidence"]) if len(cands) > 1 else 0.0), 6)
        dp = store.datapoints.get(top["datapoint_id"])
        unit_ok = not (dp and dp.unit and top.get("unit_hint")) or store.units.compatible(top["unit_hint"], dp.unit)
        exact_ok = top["method"] != "exact_template" or bool(top.get("template_mapping_ref"))
        if (top["rank"] == 1 and top["confidence"] >= th["PROMO-MAP-0.85"] and gap >= th["PROMO-MAP-GAP"]
                and dp is not None and dp.status == "active" and unit_ok and exact_ok):
            confirm_items.append(_stamp(top["id"], top["version"], "PROMO-MAP-0.85", store, status="confirmed"))
            for m in cands[1:]:  # quarantine 对象 status 只能是 candidate（schema），故落选候选以晋升版本标 rejected
                reject_items.append(_stamp(m["id"], m["version"], "PROMO-MAP-0.85", store, status="rejected"))
            q_mapped.append(_stamp(q["id"], q["version"], None, store, status="mapped"))
        elif top["confidence"] >= th["CQ-MAP-BAND-LOW"]:
            opts = []
            for i, m in enumerate(cands[:3]):
                info = store.datapoints.get(m["datapoint_id"])
                label = (info.extra.get("label") if info and info.extra else None) or {"de": m["datapoint_id"], "en": m["datapoint_id"]}
                opts.append({"key": f"o{i + 1}", "label": label, "ref": f"{m['id']}#v{m['version']}"})
            if len(opts) < 2:  # 03 §5 schema：options 2–4 项；只有一个候选时补"都不是"（选它 → 全部候选 rejected → unmapped → 人映射）
                opts.append({"key": "none", "label": {"de": "Keiner davon – manuell zuordnen", "en": "None of these – map manually"}})
            res.cq_refs.append(new_cq(store, wf, idem, key=f"map:{q['id']}", target_ref=f"{q['id']}#v{q['version']}",
                                      request_id=req_id, kind="single_choice", options=opts,
                                      prompt={"de": "Welcher Datenpunkt beantwortet diese Frage?",
                                              "en": "Which datapoint answers this question?"}, tags=["CQ-MAP-BAND"]))
        else:
            q_unmapped.append(_stamp(q["id"], q["version"], None, store, status="unmapped"))
            res.unmapped.append(q["id"])
    res.promoted += _write_items(store, "Mapping", confirm_items, g, idem, "promote-map")
    res.rejected += _write_items(store, "Mapping", reject_items, g, idem, "reject-map")
    _write_items(store, "Question", q_mapped + q_unmapped, g, idem, "q-status")
    return res


# ---------------------------------------------------------------------------- PROMO-EV-SUP
def promote_supplier_evidence(store: GraphStore, org_id: str, ev_id: str, idem: Idem) -> PromotionResult:
    g, wf = Actor.of("agent:guard", org_id), Actor.of("service:workflow", org_id)
    res = PromotionResult()
    ev = store.get(ev_id, org_id=org_id)
    if ev is None or ev["trust_zone"] == "trusted":
        return res
    reasons = []
    if ev.get("source_type") != "supplier_response":
        reasons.append("source_type")
    if (ev.get("plausibility") or {}).get("flag") != "ok":
        reasons.append("plausibility")
    sreq = store.get(ev.get("supplier_request_ref", "").split("#")[0], org_id=org_id) if ev.get("supplier_request_ref") else None
    if not sreq or sreq.get("status") not in ("responded", "reviewed"):
        reasons.append("supplier_request_state")
    dp = store.datapoints.get(ev["datapoint_id"]) if ev.get("datapoint_id") else None
    if ev.get("value_type") == "number":
        allowed = set(dp.allowed_units or ()) | ({dp.unit} if dp and dp.unit else set()) if dp else set()
        if not dp or (allowed and ev.get("unit") not in allowed and not (dp.unit and store.units.compatible(ev.get("unit"), dp.unit))):
            reasons.append("unit")
    tag_flags = [t for t in ev.get("tags") or [] if t.startswith(("injection:", "canary:"))]
    if tag_flags:  # PC-05
        reasons.append("injection")
        res.flags[ev_id] = tag_flags
    if reasons:
        res.failed.append(ev_id)
        pv = None
        if ev.get("value_type") == "number" and ev.get("unit"):
            pv = {"value": ev["extracted_value"], "unit": ev["unit"], "evidence_ref": f"{ev_id}#v{ev['version']}"}
        res.cq_refs.append(new_cq(store, wf, idem, key=f"evsup:{ev_id}", target_ref=f"{ev_id}#v{ev['version']}",
                                  request_id=None, kind="numeric_confirm" if pv else "yes_no", proposed_value=pv,
                                  prompt={"de": "Lieferantenwert prüfen und bestätigen", "en": "Review and confirm the supplier value"},
                                  tags=[f"promo-fail:{r}" for r in reasons]))
        return res
    it = _stamp(ev_id, ev["version"], "PROMO-EV-SUP", store, status="candidate")
    res.promoted += _write_items(store, "Evidence", [it], g, idem, "promote-ev")
    return res
