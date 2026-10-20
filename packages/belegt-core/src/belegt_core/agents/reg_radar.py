# Spec: 04 §2.12 reg-radar（离线批处理；输入 {sources[], since}；输出 {celex, article, effective_date, affected_template_ids[],
#       affected_datapoint_ids[]}，日期必须引用原文片段；禁止法律结论；抓取内容先经 guard 注入扫描；人审核后才生成 RFC）、§4.3
#       RegRadarSchedule（每周一 06:00 CET）
"""法规雷达（离线）：读本地法规源清单（``agents/data/reg_sources.yaml`` 或给定路径），生成"下季度会被问到"清单与字典 RFC 线索。

与文档出入：04 §2.12 写"只写 public_source 证据"，但 03 §1.6 / authz 的 ``AGENT_WRITE['reg-radar']`` 为空集（reg-radar 无写权限），
因此本实现只产出内部报告（不进 Request 工作流、不写图）；若要落 public_source Evidence 需先调整 03 的写权限表。
"""
from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ..llm.base import LLMOutputInvalid, ModelUnavailable
from .base import AgentBase, AgentOutput


def load_sources(path: str | Path | None = None) -> dict[str, Any]:
    if path:
        return yaml.safe_load(Path(path).read_text("utf-8"))
    return yaml.safe_load(resources.files("belegt_core.agents").joinpath("data/reg_sources.yaml").read_text("utf-8"))


class RegRadar(AgentBase):
    agent_id = "reg-radar"

    def run(self, *, idem_prefix: str, sources_path: str | Path | None = None, since: str | None = None) -> AgentOutput:
        doc = load_sources(sources_path)
        items, rejected = [], []
        with self.context(None, idem_prefix, activity="reg_radar", input_obj={"version": doc.get("version")}) as ctx:
            for src in doc.get("sources") or []:
                self.svc.guard.check_tool(self.agent_id, "web.fetch", {"readOnlyHint": True}, span=ctx.span,
                                          activity_scope=ctx.idem_prefix, args={"id": src["id"]})
                flags = self.svc.guard.scan_external(src.get("text") or "", zone="quarantine", span=ctx.span)
                try:
                    out = ctx.ask("reg_radar", {"source": {k: src.get(k) for k in ("celex", "title", "template_hints", "datapoint_hints")}},
                                  external={"source_text": src.get("text") or ""})
                except (ModelUnavailable, LLMOutputInvalid):
                    rejected.append({"id": src["id"], "reason": "model_unavailable"})  # 跳过本周，下周补跑
                    continue
                date, quote = out.get("effective_date"), out.get("quote") or ""
                text = src.get("text") or ""
                # 日期必须引用原文片段（确定性校验）：quote 是原文子串，且日期（ISO 或 DD.MM.YYYY）出现在 quote 中
                ok = bool(quote) and quote.strip() in text and date is not None and (
                    date in quote or _de(date) in quote)
                if not ok:
                    rejected.append({"id": src["id"], "reason": "date_not_quoted"})
                    continue
                if since and date < since:
                    continue
                known = {d for d in out.get("affected_datapoint_ids") or [] if self.svc.registry.versions(d.split("@")[0])}
                items.append({"source_id": src["id"], "publisher": src.get("publisher"), "celex": out.get("celex"),
                              "article": out.get("article"), "effective_date": date, "quote": quote.strip(),
                              "affected_template_ids": out.get("affected_template_ids") or [],
                              "affected_datapoint_ids": sorted(known), "flags": flags,
                              "rfc_candidate": not known, "requires_human_review": True})
        items.sort(key=lambda x: x["effective_date"])
        return AgentOutput(outcome="report", details={"items": items, "rejected": rejected, "sources_version": doc.get("version")})


def _de(iso: str) -> str:
    try:
        y, m, d = iso.split("-")
        return f"{d}.{m}.{y}"
    except ValueError:
        return iso
