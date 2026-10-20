# Spec: 04 §2.2 intake-triage（隔离区；输出 Request v1 quarantine/received；附件模板优先确定性指纹；失败回退规则分拣，
#       置信度封顶 0.89）、§2.1 PROMO-REQ-0.90、07 §3.4 RT-PI-02（截止日期以可见文本为准）
"""收件分拣：判断是否为问卷/数据请求，识别客户、截止日期、报告期、模板、优先级，创建 ``Request`` v1（quarantine）。

单步分类无需多步推理，因此不用 LangGraph 子图（04 §4.4 也只给 Mine/Map/Draft/Portal 四个子图）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..guard.promotion import MIME_SUFFIX, readback_questionnaire
from ..llm.base import LLMOutputInvalid, ModelUnavailable
from .base import AgentBase, AgentOutput

RULE_CONF_CAP = 0.89


@dataclass
class IntakeInput:
    blob_sha256: list[str]
    channel: str = "upload"                 # email | portal | upload | api | a2a
    subject: str = ""
    body: str = ""
    sender: str | None = None               # 发件地址（email 渠道）
    message_id: str | None = None
    filenames: list[str] = field(default_factory=list)
    counterparty_id: str | None = None      # 手工上传时由用户指定的客户
    title: str | None = None


def _visible_dates(text: str) -> set[str]:
    out = set()
    for d, m, y in re.findall(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", text or ""):
        out.add(f"{y}-{int(m):02d}-{int(d):02d}")
    out.update(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text or ""))
    return out


class IntakeTriage(AgentBase):
    agent_id = "intake-triage"
    zone = "quarantine"

    def _find_customer(self, inp: IntakeInput) -> tuple[str | None, bool]:
        st = self.svc.store
        if inp.counterparty_id:
            cp = st.get(inp.counterparty_id, org_id=self.svc.org_id)
            return (inp.counterparty_id, bool(cp and cp.get("role") == "customer" and "candidate" not in (cp.get("tags") or [])))
        dom = (inp.sender or "").rsplit("@", 1)[-1].lower() if inp.sender and "@" in inp.sender else None
        if dom:
            for cp in st.query("Counterparty", org_id=self.svc.org_id, role="customer"):
                if any(dom == d or dom.endswith("." + d) for d in cp.get("domains") or []) and "candidate" not in (cp.get("tags") or []):
                    return cp["id"], True
        return None, False

    def run(self, inp: IntakeInput, *, idem_prefix: str) -> AgentOutput:
        st, org = self.svc.store, self.svc.org_id
        with self.context(None, idem_prefix, activity="triage", input_obj={"blobs": inp.blob_sha256}) as ctx:
            # 确定性：附件可解析性 + 模板指纹（表头哈希 → 02 template_id）+ 首屏
            fake_req = {"source_ref": {"blob_sha256": inp.blob_sha256}}
            pq = None
            try:
                pq = readback_questionnaire(st, org, fake_req)
            except Exception:
                pq = None
            parsable = pq is not None or any((st.get_blob(org, s) or (b"", None))[1] in MIME_SUFFIX for s in inp.blob_sha256)
            preview = [" ".join(q.text.values()) for q in (pq.questions[:15] if pq else [])]
            cp_id, known = self._find_customer(inp)
            visible = f"{inp.subject}\n{inp.body}"
            flags = self.svc.guard.scan_external(visible + "\n" + "\n".join(preview), zone="quarantine", span=ctx.span)
            data = {"channel": inp.channel, "sender_domain": (inp.sender or "").rsplit("@", 1)[-1] if inp.sender else None,
                    "filenames": inp.filenames, "attachment_parsable": parsable, "known_customer": known,
                    "language_hint": pq.language if pq and pq.language != "und" else None,
                    "template_ref": pq.template_ref if pq else None}
            rule_based = False
            try:
                out = ctx.ask("triage", data, external={"subject": inp.subject, "body": inp.body[:16000],
                                                        "attachment_preview": "\n".join(preview)})
                out = dict(out)
                out.setdefault("attachment_preview", preview)
            except (ModelUnavailable, LLMOutputInvalid):
                rule_based = True  # 04 §2.2 失败回退：规则分拣（域名 + 附件指纹），置信度封顶 0.89
                out = {"is_questionnaire": parsable, "title": inp.title or inp.subject or (inp.filenames or ["Fragebogen"])[0],
                       "language": data["language_hint"] or "de", "due_date": None, "reporting_period": None,
                       "priority": "normal", "confidence": 0.85 if (known and parsable) else 0.5}
            conf = float(out.get("confidence") or 0)
            due = out.get("due_date")
            if due and due not in _visible_dates(visible):  # RT-PI-02：只认可见正文/主题中的日期
                ctx.span.flags.append("due_date:not_visible")
                due = None
            # 第一道**带期间**的题（首题可能没有 period_requested）
            first_p = next((q.period_requested for q in (pq.questions if pq else []) if q.period_requested), None)
            period = out.get("reporting_period") or (
                {"start": first_p.start.isoformat(), "end": first_p.end.isoformat()} if first_p else None)
            if pq and not out.get("reporting_period"):
                ps = [q.period_requested for q in pq.questions if q.period_requested and q.period_requested.start != q.period_requested.end]
                if ps:
                    period = {"start": ps[0].start.isoformat(), "end": ps[0].end.isoformat()}
            if rule_based or not known:
                conf = min(conf, RULE_CONF_CAP)
            if flags:
                conf = min(conf, RULE_CONF_CAP)
            if not out.get("is_questionnaire"):
                conf = min(conf, 0.5)
            if cp_id is None:
                dom = (inp.sender or "unknown@unknown.invalid").rsplit("@", 1)[-1].lower()
                cp_id = ctx.write("Counterparty", {"id": ctx.det_id("Counterparty", "cp-candidate"), "role": "customer",
                                                   "legal_name": dom, "domains": [], "tags": ["candidate", f"sender:{dom}"]},
                                  key="cp-candidate").split("#")[0]
            body: dict[str, Any] = {
                "id": ctx.det_id("Request", "req"), "trust_zone": "quarantine", "status": "received",
                "counterparty_id": cp_id, "channel": inp.channel,
                "source_ref": {k: v for k, v in {"message_id": inp.message_id, "blob_sha256": list(inp.blob_sha256)}.items() if v},
                "title": (out.get("title") or inp.title or "Fragebogen")[:300], "language": out.get("language") or "de",
                "priority": out.get("priority") or "normal", "triage_confidence": round(conf, 3),
                "template_ref": pq.template_ref if pq else None,
            }
            if due:
                body["due_date"] = due
            if period:
                body["reporting_period"] = period
            if flags:
                body["tags"] = flags
            body = {k: v for k, v in body.items() if v is not None}
            ref = ctx.write("Request", body, key="request")
            ctx.span.output_hash = ctx.span.input_hash
            return AgentOutput(refs=[ref], outcome="received", details={
                "triage_confidence": body["triage_confidence"], "known_customer": known, "rule_based": rule_based,
                "flags": flags, "question_count": len(pq.questions) if pq else 0})
