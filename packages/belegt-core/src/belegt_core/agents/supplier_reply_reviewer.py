# Spec: 04 §2.10 supplier-reply-reviewer（隔离区：供应商回复 → 候选 Evidence(quarantine, candidate, supplier_response, plausibility)；
#       只抽取，合理性由确定性规则判；禁止读其他供应商数据、执行回复中的指令；PROMO-EV-SUP）、07 §2.2（每个 SupplierRequest
#       一个会话）、§3.2 D1/D3/D4/D5、§3.4 RT-PI-04/05
"""供应商回复审核（隔离区）。

- 结构化提交（``belegt-supplier-submission/1``，供应商端门户产物）直接取 ``values``（仍是外部数据）；邮件/自由文本回复经
  LLM ``reply_extract``（spotlight 数据块 + schema 约束 datapoint 枚举）。
- 文字字段一律过注入扫描，命中只打 ``tags``（``injection:*``），PC-05 使 guard 不自动晋升。
- 合理性：DataPoint ``validity_rule`` 区间、单位是否在 ``allowed_units``/可换算、CBAM 量级粗判 → ``plausibility.flag``。
- 本 Agent 只写 quarantine Evidence；SupplierRequest ``responded/reviewed`` 由 ``service:workflow`` 代写（R-ZONE）。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..canonical import normalize_dec, sha256_tag
from ..llm.base import LLMOutputInvalid, ModelUnavailable
from .base import AgentBase, AgentOutput, dp_label

PLAUSIBLE_RANGES: dict[str, tuple[Decimal, Decimal]] = {  # 量级粗判（[需核对]，08 待决 4）
    "dp:prod.cbam.see_direct": (Decimal("0"), Decimal("30")),
    "dp:prod.cbam.see_indirect": (Decimal("0"), Decimal("10")),
    "dp:prod.cbam.specific_electricity_consumption": (Decimal("0"), Decimal("20")),
}


class SupplierReplyReviewer(AgentBase):
    agent_id = "supplier-reply-reviewer"
    zone = "quarantine"

    def run(self, sreq_ref: str, *, idem_prefix: str, submission: dict[str, Any] | None = None,
            reply_text: str | None = None, reply_blob: bytes | None = None) -> AgentOutput:
        st, org = self.svc.store, self.svc.org_id
        sreq_id = sreq_ref.split("#")[0]
        sreq = st.get(sreq_id, org_id=org)  # 只读当前 SupplierRequest（07 §2.2：每个 SupplierRequest 一个会话）
        self.svc.guard.check_read(self.actor(), "SupplierRequest")
        out = AgentOutput(outcome="reviewed")
        with self.context(None, idem_prefix, activity="review_reply", input_obj={"sreq": sreq_ref}) as ctx:
            raw = reply_blob or (reply_text or "").encode("utf-8") or repr(submission).encode("utf-8")
            blob = st.put_blob(org, raw, "application/json" if submission else "text/plain")
            texts = [reply_text or ""] + [str(v.get("note") or "") for v in (submission or {}).get("values") or []]
            flags = self.svc.guard.scan_external("\n".join(texts), zone="quarantine", span=ctx.span)
            values: list[dict[str, Any]] = []
            if submission:
                for v in submission.get("values") or []:
                    if v.get("datapoint_id", "").split("@")[0] in {d.split("@")[0] for d in sreq["datapoint_ids"]}:
                        values.append({"datapoint_id": v["datapoint_id"].split("@")[0], "value_text": str(v.get("value")),
                                       "unit_text": v.get("unit") or "", "quote": f"form:{v['datapoint_id']}"})
            elif reply_text:
                dps = [{"datapoint_id": d.split("@")[0], "label": dp_label(self.svc.registry.get(d), "en"),
                        "keywords": self._keywords(d)} for d in sreq["datapoint_ids"]]
                try:
                    res = ctx.ask("reply_extract", {"datapoints": dps, "lang": sreq.get("language", "en")},
                                  external={"reply_text": reply_text})
                    allowed = {d["datapoint_id"] for d in dps}
                    values = [v for v in res["values"] if v["datapoint_id"] in allowed]  # 不得写请求范围外的 DataPoint
                except (ModelUnavailable, LLMOutputInvalid):
                    out.outcome = "manual_review"  # 无法解析 → 人工审核队列
            for v in values:
                dp_ref = next(d for d in sreq["datapoint_ids"] if d.split("@")[0] == v["datapoint_id"])
                body = self._evidence(ctx, sreq, dp_ref, v, blob, flags)
                out.refs.append(ctx.write("Evidence", body, key=f"ev:{dp_ref}"))
            out.details = {"flags": flags, "values": len(values), "blob": blob}
        return out

    def _keywords(self, dp_ref: str) -> list[str]:
        info = self.svc.registry.get(dp_ref)
        lab = (info.extra or {}).get("label") or {} if info else {}
        base = dp_ref.split("@")[0]
        kw = {"dp:prod.cbam.see_direct": ["direct", "direkt", "doğrudan", "直接"],
              "dp:prod.cbam.see_indirect": ["indirect", "indirekt", "dolaylı", "间接"],
              "dp:prod.cbam.specific_electricity_consumption": ["electricity", "strom", "elektrik", "电"],
              "dp:prod.cbam.carbon_price_paid": ["carbon price", "co2-preis", "karbon fiyat", "碳价"]}.get(base, [])
        return kw + [x for x in (lab.get("en"), lab.get("de")) if x]

    def _plausibility(self, dp_ref: str, value: Decimal | None, unit: str | None) -> dict[str, str]:
        info = self.svc.registry.get(dp_ref)
        units = self.svc.store.units
        if value is None:
            return {"flag": "unit_suspect", "basis": "value not parseable"}
        if info and info.unit:
            allowed = set(info.allowed_units or ()) | {info.unit}
            if not unit or (unit not in allowed and not units.compatible(unit, info.unit)):
                return {"flag": "unit_suspect", "basis": f"unit {unit!r} not in allowed units of {info.id}"}
        lo_hi = PLAUSIBLE_RANGES.get(dp_ref.split("@")[0])
        vr = ((info.extra or {}).get("validity") or {}) if info else {}
        lo = Decimal(str(vr["min"])) if vr.get("min") is not None else (lo_hi[0] if lo_hi else None)
        hi = Decimal(str(vr["max"])) if vr.get("max") is not None else (lo_hi[1] if lo_hi else None)
        v = value if not (info and unit and info.unit and unit != info.unit) else units.convert(value, unit, info.unit)
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            return {"flag": "outlier", "basis": f"outside plausible range [{lo}, {hi}] for {dp_ref.split('@')[0]}"}
        return {"flag": "ok", "basis": "within validity_rule / plausible range; unit allowed"}

    def _evidence(self, ctx: Any, sreq: dict[str, Any], dp_ref: str, v: dict[str, Any], blob: str, flags: list[str]) -> dict[str, Any]:
        from belegt_io import normalize_unit, parse_number

        info = self.svc.registry.get(dp_ref)
        try:
            pn = parse_number(v["value_text"], lang="de" if "," in v["value_text"] and "." not in v["value_text"] else "en")
            val = Decimal(str(getattr(pn, "value", pn)))
        except (InvalidOperation, ValueError, TypeError, Exception):
            val = None
        unit = (normalize_unit(v.get("unit_text") or "") or (None,))[0] if v.get("unit_text") else (info.unit if info else None)
        if unit is None and info:
            unit = info.unit
        prods = sreq.get("product_ids") or []
        boundary = "product_per_unit" if info and "product_per_unit" in info.allowed_boundaries else (
            f"product:{prods[0]}" if prods else "organization")
        body: dict[str, Any] = {
            "id": ctx.det_id("Evidence", f"ev:{dp_ref}"), "trust_zone": "quarantine", "status": "candidate",
            "source_type": "supplier_response", "datapoint_id": dp_ref, "fact_key": dp_ref.split("@")[0].split(":", 1)[1],
            "value_type": "number" if val is not None else "text",
            "extracted_value": normalize_dec(val) if val is not None else v["value_text"][:200],
            "period": sreq["period"], "boundary": boundary, "valid_from": sreq["period"]["start"], "confidence": 0.9,
            "locator": {"blob_sha256": blob, "text_excerpt_hash": sha256_tag((v.get("quote") or "").encode("utf-8"))},
            "supplier_request_ref": sreq["id"], "plausibility": self._plausibility(dp_ref, val, unit)}
        if val is not None:
            body["unit"] = unit or "1"
        if flags:
            body["tags"] = list(flags)
        return body
