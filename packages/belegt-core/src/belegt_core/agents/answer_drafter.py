# Spec: 04 §2.7 answer-drafter（text/enum/bool/date 类答案；对数字答案不写值；复用历史 L0；每句带 cites；legal_commitment
#       关键词规则 + 模型双判；GRAPH_NUM_IN_TEXT/GRAPH_SCHEMA_INVALID 修正 1 次后转人工；模型不可用只做 L0 复用）、
#       §4.4 DraftGraph（assemble_context → draft → cite_check(确定性) → revise(≤1) → emit）、§5.1 GP-05、NFR-03、FR-401
"""答案起草。数字永远不由本 Agent 写入（authz 层也禁止 ``answer-drafter`` 写 ``value_type=number``）；文字说明中的数字
必须能在所引来源中找到（GP-05，写入前由 guard 扫描，写入时 R-NUM/GRAPH_NUM_IN_TEXT 再校验）。"""
from __future__ import annotations

import re
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from ..errors import GraphError
from ..guard.loops import LoopDetector
from ..llm.base import LLMOutputInvalid, ModelUnavailable
from ..llm.embedding import normalize
from .base import AgentBase, AgentOutput, RunContext, dp_label

LEGAL_KEYWORDS = re.compile(r"(verpflicht|garantier|zusicher|gewährleist|commit|guarantee|warrant|undertake|承诺|保证)", re.I)


def _legal_patterns(text: str) -> bool:
    try:
        from ..legal import detect_legal_clauses
    except Exception:  # pragma: no cover - legal 模块缺失时只用关键词
        return False
    return bool(text.strip()) and detect_legal_clauses(text).flagged


TIER_FOR = {"text": "tier-L", "enum": "tier-S", "bool": "tier-S", "date": "tier-S", "file": "tier-S"}


class DraftState(TypedDict, total=False):
    target: dict[str, Any]
    context: dict[str, Any]
    draft: dict[str, Any]
    errors: list[str]
    revisions: int
    result: dict[str, Any]


class AnswerDrafter(AgentBase):
    agent_id = "answer-drafter"
    recursion_limit = 10

    def run(self, request_ref: str, targets: list[dict[str, Any]], *, idem_prefix: str, customer_scope: Any = None) -> AgentOutput:
        req_id = request_ref.split("#")[0]
        req = self.svc.store.get(req_id, org_id=self.svc.org_id) or {}
        scope = customer_scope if customer_scope is not None else ([req["counterparty_id"]] if req.get("counterparty_id") else "all")
        out = AgentOutput(outcome="drafted")
        manual: list[dict[str, Any]] = []
        with self.context(req_id, idem_prefix, activity="draft_answers", input_obj={"targets": [t["datapoint_id"] for t in targets]}) as ctx:
            graph = self._graph(ctx, scope, req.get("language") or "de")
            for t in targets:
                info = self.svc.registry.get(t["datapoint_id"])
                if info is None:
                    continue
                if info.value_type == "number":
                    continue  # 数字只经 calc-service（NFR-03）
                existing = self._existing(t)
                if existing:
                    out.refs.append(existing)
                    continue
                cfg = {"configurable": {"thread_id": f"{idem_prefix}:{t['datapoint_id']}"}, "recursion_limit": self.recursion_limit}
                try:
                    st: DraftState = graph.invoke({"target": t, "revisions": 0}, cfg)
                except GraphRecursionError as e:
                    raise LoopDetector.from_recursion(e, "DraftGraph") from e
                r = st.get("result") or {}
                if r.get("ref"):
                    out.refs.append(r["ref"])
                else:
                    manual.append({"datapoint_id": t["datapoint_id"], "question_ids": t.get("question_ids"),
                                   "reason": r.get("reason") or "no_source", "errors": st.get("errors", [])})
        out.details["pending_manual"] = manual  # "待人工撰写"，不阻塞其他题（T10 补偿）
        return out

    def _existing(self, t: dict[str, Any]) -> str | None:
        qrefs = set(t.get("question_ids") or [])
        for a in self.svc.store.query("Answer", org_id=self.svc.org_id, datapoint_base=t["datapoint_id"].split("@")[0]):
            if a["status"] in ("draft", "in_review", "approved") and qrefs & set(a.get("question_refs") or []):
                return f"{a['id']}#v{a['version']}"
        return None

    # ------------------------------------------------------------------ DraftGraph
    def _graph(self, ctx: RunContext, scope: Any, lang: str):
        svc = self.svc
        st = svc.store
        g = StateGraph(DraftState)

        def assemble_context(s: DraftState) -> DraftState:
            t = s["target"]
            info = svc.registry.get(t["datapoint_id"])
            qs = [st.get(q, org_id=svc.org_id) for q in t.get("question_ids") or []]
            qs = [q for q in qs if q]
            reused = []
            for r in t.get("reuse") or []:
                a = st.get(r, org_id=svc.org_id)
                if a and a.get("trust_zone") == "trusted" and a.get("status") in ("submitted", "approved"):
                    reused.append({"ref": r, "value": a.get("value")})
            evidence = []
            for r in t.get("evidence_refs") or []:
                e = st.get(r, org_id=svc.org_id)
                if e and e.get("status") == "verified" and e.get("trust_zone") == "trusted":
                    evidence.append({"ref": r, "value": e.get("extracted_value"), "text": str(e.get("extracted_value"))})
            options = (qs[0].get("options") if qs else None) or None
            ctx_data = {"datapoint_id": t["datapoint_id"].split("@")[0], "label": dp_label(info, lang),
                        "value_type": info.value_type if info else "text", "options": options, "language": lang,
                        "reused": reused[:3], "evidence": evidence[:5]}
            return {"context": {**ctx_data, "question_text": " / ".join(" ".join(q["text"].values()) for q in qs)[:2000]}}

        def draft(s: DraftState) -> DraftState:
            c = s["context"]
            if not c["reused"] and not c["evidence"]:
                return {"draft": {"abstain": True}, "errors": ["no_source"]}
            data = {k: v for k, v in c.items() if k != "question_text"}
            if s.get("errors"):
                data["previous_errors"] = s["errors"]
            try:
                d = ctx.ask("draft_answer", data, external={"question_text": c["question_text"]},
                            tier=TIER_FOR.get(c["value_type"], "tier-L"))
            except (ModelUnavailable, LLMOutputInvalid):  # 模型不可用 → 只做 L0 复用
                if c["reused"]:
                    d = {"value": c["reused"][0]["value"], "sentences": [], "legal_commitment": False, "is_estimate": False}
                else:
                    return {"draft": {"abstain": True}, "errors": ["model_unavailable"]}
            return {"draft": d}

        def cite_check(s: DraftState) -> DraftState:  # 确定性
            c, d = s["context"], s["draft"]
            if d.get("abstain") or d.get("value") is None:
                return {"errors": list(s.get("errors") or ["abstain"]) or ["abstain"]}
            errs: list[str] = []
            allowed_refs = {x["ref"] for x in c["reused"] + c["evidence"]}
            for sen in d.get("sentences") or []:
                if not sen.get("cites") or not set(sen["cites"]) <= allowed_refs:
                    errs.append("SENTENCE_UNCITED")
            vt, v = c["value_type"], d["value"]
            if vt == "bool" and not isinstance(v, bool):
                errs.append("BOOL_REQUIRED")
            if vt == "date" and not (isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)):
                errs.append("DATE_ISO_REQUIRED")
            if vt == "enum" and c.get("options") and isinstance(v, str) and normalize(v) not in {normalize(o) for o in c["options"]}:
                errs.append("ENUM_NOT_IN_OPTIONS")
            texts = [s_["text"] for s_ in d.get("sentences") or []] + ([v] if isinstance(v, str) and vt == "text" else [])
            sources = [x["value"] for x in c["reused"]] + [x["text"] for x in c["evidence"]] + [c.get("question_text") or ""]
            try:
                svc.guard.check_output_numbers(texts, sources, span=ctx.span)  # GP-05
            except GraphError:
                errs.append("GRAPH_NUM_IN_TEXT")
            return {"errors": errs}

        def route(s: DraftState) -> str:
            if not s.get("errors"):
                return "emit"
            if s["errors"] in (["no_source"], ["abstain"], ["model_unavailable"]):
                return "give_up"
            return "revise" if s.get("revisions", 0) < 1 else "give_up"

        def revise(s: DraftState) -> DraftState:
            return {"revisions": s.get("revisions", 0) + 1}

        def give_up(s: DraftState) -> DraftState:
            return {"result": {"ref": None, "reason": ",".join(s.get("errors") or ["unknown"])}}

        def emit(s: DraftState) -> DraftState:
            t, c, d = s["target"], s["context"], s["draft"]
            info = svc.registry.get(t["datapoint_id"])
            texts = " ".join(x["text"] for x in d.get("sentences") or []) + (" " + d["value"] if isinstance(d["value"], str) else "")
            # 规则 + 模型双判；规则通道 = 关键词 + 06 §6 法律承诺模式库（覆盖德语可分动词"sichern … zu"等）
            legal = bool(d.get("legal_commitment")) or bool(LEGAL_KEYWORDS.search(texts)) or _legal_patterns(texts)
            body: dict[str, Any] = {
                "id": ctx.det_id("Answer", f"ans:{t['datapoint_id']}"), "datapoint_id": t["datapoint_id"], "value": d["value"],
                "value_type": c["value_type"], "period": t["period"], "boundary": t["boundary"], "customer_scope": scope,
                "question_refs": list(t.get("question_ids") or []), "origin": "generated",
                "is_estimate": bool(d.get("is_estimate")), "legal_commitment": legal, "status": "draft"}
            if d.get("is_estimate"):
                body["estimate_method"] = "drafted from partial evidence"
            if c["value_type"] == "text":
                body["text_lang"] = c["language"]
            if t["boundary"] == "organization" and info and info.requires_consolidation:
                body["consolidation"] = svc.org.consolidation
            ev = [x["ref"] for x in c["evidence"]]
            if ev:
                body["evidence_refs"] = ev
            if c["reused"] and d["value"] == c["reused"][0]["value"]:
                body["reused_from"] = c["reused"][0]["ref"]
            note = " ".join(x["text"] for x in d.get("sentences") or [] if x.get("text"))
            if note:
                body["drafting_note"] = {c["language"]: note[:2000]}
            err = ctx.validate("Answer", body)  # 04 §2.0：先 /graph/validate
            if err is not None:
                svc.guard.record_rejection(body["id"], err.code)
                return {"result": {"ref": None, "reason": err.code}, "errors": [err.code]}
            return {"result": {"ref": ctx.write("Answer", body, key=f"ans:{t['datapoint_id']}")}}

        for name, fn in (("assemble_context", assemble_context), ("draft", draft), ("cite_check", cite_check),
                         ("revise", revise), ("give_up", give_up), ("emit", emit)):
            g.add_node(name, fn)
        g.add_edge(START, "assemble_context")
        g.add_edge("assemble_context", "draft")
        g.add_edge("draft", "cite_check")
        g.add_conditional_edges("cite_check", route, {"emit": "emit", "revise": "revise", "give_up": "give_up"})
        g.add_edge("revise", "draft")
        g.add_edge("emit", END)
        g.add_edge("give_up", END)
        return g.compile(checkpointer=InMemorySaver())
