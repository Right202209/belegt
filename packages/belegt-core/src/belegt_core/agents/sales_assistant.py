# Spec: 04 §2.13 sales-assistant（只读视图 app.approved_only=true + app.cp_scope；无任何写入；数字一律由模板渲染 ObjectRef；
#       超出授权范围回答"无授权数据"；禁止引用 draft、跨客户泄露；对外分享 confidential/restricted 需 compliance_owner 批准）、
#       03 §6.5 RLS、07 §4.3
"""销售助手：以 ``agent:sales-assistant``（``must_approved_only``）+ 用户与目标客户的 ``cp_scope`` 交集读取答案；
LLM 只产出带 ``{{ref:ans_…#vN}}`` 占位符的模板，数字由确定性渲染从对象读出。"""
from __future__ import annotations

import re
from typing import Any

from ..authz import Actor
from ..llm.base import LLMOutputInvalid, ModelUnavailable
from .base import AgentBase, AgentOutput, dp_label

NO_DATA = {"de": "Keine freigegebenen Daten für diese Anfrage.", "en": "No authorized data for this request.",
           "zh": "无授权数据"}
_REF = re.compile(r"\{\{ref:([a-z]+_[0-9A-HJKMNP-TV-Z]{26}#v[1-9][0-9]*)\}\}")


class SalesAssistant(AgentBase):
    agent_id = "sales-assistant"

    def run(self, user: Actor, counterparty_id: str, question_text: str, *, idem_prefix: str,
            datapoint_ids: list[str] | None = None, lang: str = "de") -> AgentOutput:
        st = self.svc.store
        scope = (counterparty_id,) if user.cp_scope is None else tuple(c for c in user.cp_scope if c == counterparty_id)
        reader = Actor.of(f"agent:{self.agent_id}", self.svc.org_id, approved_only=True, cp_scope=scope or ("__none__",))
        with self.context(None, idem_prefix, activity="sales_answer", input_obj={"cp": counterparty_id}) as ctx:
            answers = [a for a in st.query("Answer", actor=reader) if a["status"] in ("approved", "submitted")]
            if datapoint_ids:
                bases = {d.split("@")[0] for d in datapoint_ids}
                answers = [a for a in answers if a["datapoint_id"].split("@")[0] in bases]
            answers = [a for a in answers if (self.svc.registry.get(a["datapoint_id"]) is None or
                                              self.svc.registry.get(a["datapoint_id"]).sensitivity != "restricted")]
            if not answers:
                return AgentOutput(outcome="no_data", details={"text": NO_DATA.get(lang, NO_DATA["en"]), "cites": []})
            cands = [{"ref": f"{a['id']}#v{a['version']}", "datapoint_id": a["datapoint_id"],
                      "label": dp_label(self.svc.registry.get(a["datapoint_id"]), lang)} for a in answers]
            try:
                out = ctx.ask("sales_answer", {"answers": cands, "lang": lang}, external={"question_text": question_text})
            except (ModelUnavailable, LLMOutputInvalid):
                out = {"answer_template": " ".join("{{ref:%s}}" % c["ref"] for c in cands), "cites": [c["ref"] for c in cands]}
            allowed = {c["ref"] for c in cands}
            tmpl = out["answer_template"]
            if tmpl.strip() == "NO_AUTHORIZED_DATA":
                return AgentOutput(outcome="no_data", details={"text": NO_DATA.get(lang, NO_DATA["en"]), "cites": []})
            self.svc.guard.check_output_numbers([_REF.sub("", tmpl)], [], span=ctx.span)  # 模型不得自写数字
            by_ref = {f"{a['id']}#v{a['version']}": a for a in answers}

            def render(m: re.Match[str]) -> str:
                r = m.group(1)
                if r not in allowed:
                    return "[—]"
                a = by_ref[r]
                unit = f" {a['unit']}" if a.get("unit") else ""
                return f"{dp_label(self.svc.registry.get(a['datapoint_id']), lang)}: {a['value']}{unit} ({r})"
            text = _REF.sub(render, tmpl)
            return AgentOutput(refs=sorted(allowed & set(_REF.findall(tmpl))), outcome="answered",
                               details={"text": text, "cites": sorted(allowed & set(_REF.findall(tmpl)))})
