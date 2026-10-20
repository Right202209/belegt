# Spec: 04 §2.3 question-parser（隔离区：belegt-io 确定性切题 → Question v1；每题 Top-3 Mapping 候选；每批 10 题；
#       exact_template 命中跳过 LLM；LLM 失败只用嵌入相似度并把置信度封顶 0.84）、§4.4 MapGraph
#       （segment → candidates → rank → self_check → emit）、§7.2 C1/C2、§7.3 级联（tier-M < 0.85 再用 tier-L 复判）
"""题目解析与映射候选。

LangGraph ``MapGraph`` 只在本 Activity 内存在（checkpoint 短期保存，不承担事实源职责，04 §4.2-2）；
候选只写 quarantine ``Mapping(status=candidate)``，是否 confirmed 由 guard 的 PROMO-MAP-0.85 决定。
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from ..guard.loops import LoopDetector
from ..guard.promotion import readback_questionnaire
from ..llm.base import LLMOutputInvalid, ModelUnavailable
from ..llm.embedding import normalize
from ..store import WriteItem
from .base import AgentBase, AgentOutput, RunContext, chunks, latest_ref
from .hints import load_hints

BATCH = 10
EMBED_ONLY_CAP = 0.84
CASCADE_BELOW = 0.85


class MapState(TypedDict, total=False):
    request: dict[str, Any]
    drafts: list[dict[str, Any]]
    question_refs: dict[str, str]
    candidates: dict[str, list[dict[str, Any]]]
    ranked: dict[str, list[dict[str, Any]]]
    methods: dict[str, str]
    degraded: bool
    mapping_refs: list[str]
    dropped: int


class QuestionParser(AgentBase):
    agent_id = "question-parser"
    zone = "quarantine"
    recursion_limit = 12

    def run(self, request_ref: str, hints_ref: str | None, *, idem_prefix: str) -> AgentOutput:
        req_id = request_ref.split("#")[0]
        with self.context(req_id, idem_prefix, activity="parse_questions", input_obj={"request": request_ref}) as ctx:
            graph = self._graph(ctx, hints_ref)
            saver_cfg = {"configurable": {"thread_id": idem_prefix}, "recursion_limit": self.recursion_limit}
            try:
                st: MapState = graph.invoke({"request": self.svc.store.get(req_id, org_id=self.svc.org_id)}, saver_cfg)
            except GraphRecursionError as e:  # LD-04
                raise LoopDetector.from_recursion(e, "MapGraph") from e
            return AgentOutput(refs=list(st["question_refs"].values()) + st["mapping_refs"], outcome="parsed",
                               details={"questions": len(st["question_refs"]), "mappings": len(st["mapping_refs"]),
                                        "degraded": st.get("degraded", False), "methods": _count(st.get("methods", {}))})

    # ------------------------------------------------------------------ MapGraph
    def _graph(self, ctx: RunContext, hints_ref: str | None):
        svc = self.svc
        g = StateGraph(MapState)

        def segment(s: MapState) -> MapState:  # belegt-io 确定性切题 → Question v1（quarantine/parsed）
            req = s["request"]
            pq = readback_questionnaire(svc.store, svc.org_id, req)
            if pq is None:
                raise ValueError("questionnaire attachment not parsable")
            drafts, items = [], []
            for d in pq.questions:
                qid = f"q{d.ordinal}"
                body = d.to_question_fields(req["id"])
                body.update(id=ctx.det_id("Question", qid), trust_zone="quarantine", status="parsed")
                items.append(WriteItem("Question", body, idempotency_key=f"question:{qid}"))
                drafts.append({"qid": qid, "id": body["id"], "text": body["text"], "answer_format": body["answer_format"],
                               "unit_requested": body.get("unit_requested"), "options": body.get("options"),
                               "period_requested": body.get("period_requested"),
                               "exact": [h.model_dump() for h in d.exact_mappings]})
            refs = ctx.write_batch(items)
            return {"drafts": drafts, "question_refs": {d["qid"]: r for d, r in zip(drafts, refs)}}

        def candidates(s: MapState) -> MapState:  # hints（trusted 预计算）+ C1 模板直映射 + C2 历史题匹配
            hints = load_hints(svc, hints_ref)["candidates"] if hints_ref else {}
            hist = self._historical_index()
            ranked, methods, cands = {}, {}, {}
            for d in s["drafts"]:
                if d["exact"]:
                    ranked[d["qid"]] = [{"datapoint_id": h["datapoint_id"].split("@")[0], "confidence": h["confidence"],
                                         "template_mapping_ref": h["template_mapping_ref"], "transform": h.get("transform")}
                                        for h in d["exact"][:3]]
                    methods[d["qid"]] = "exact_template"
                    continue
                key = normalize(" ".join(d["text"].values()))
                if key in hist:
                    ranked[d["qid"]] = [{"datapoint_id": hist[key], "confidence": 0.93}]
                    methods[d["qid"]] = "historical"
                    svc.ref_cache.put("C2", key, hist[key])
                    continue
                cands[d["qid"]] = hints.get(d["qid"], [])
            return {"candidates": cands, "ranked": ranked, "methods": methods}

        def rank(s: MapState) -> MapState:  # LLM（每批 10 题；enum 约束；级联）
            ranked, methods = dict(s["ranked"]), dict(s["methods"])
            degraded = False
            todo = [d for d in s["drafts"] if d["qid"] in s["candidates"]]
            for batch in chunks(todo, BATCH):
                data = {"questions": [{"qid": d["qid"], "text": d["text"], "answer_format": d["answer_format"],
                                       "unit_requested": d["unit_requested"], "options": d["options"]} for d in batch],
                        "candidates": {d["qid"]: s["candidates"][d["qid"]] for d in batch}}
                schema = _enum_schema(svc.prompts.get(self.agent_id, "map_questions").schema,
                                      sorted({c["datapoint_id"] for d in batch for c in s["candidates"][d["qid"]]}))
                try:
                    out = ctx.ask("map_questions", data, schema=schema)
                    low = [m for m in out["mappings"] if not m["candidates"] or m["candidates"][0]["confidence"] < CASCADE_BELOW]
                    if low:  # 级联：tier-M 置信度 < 0.85 的题用 tier-L 复判（04 §7.3-2）
                        ids = {m["qid"] for m in low}
                        data2 = {"questions": [q for q in data["questions"] if q["qid"] in ids],
                                 "candidates": {k: v for k, v in data["candidates"].items() if k in ids}}
                        out2 = ctx.ask("map_questions", data2, schema=schema, tier="tier-L")
                        better = {m["qid"]: m for m in out2["mappings"]}
                        out["mappings"] = [better.get(m["qid"], m) for m in out["mappings"]]
                    for m in out["mappings"]:
                        ranked[m["qid"]] = m["candidates"]
                        methods[m["qid"]] = "semantic"
                except (ModelUnavailable, LLMOutputInvalid):
                    degraded = True  # 只用嵌入相似度，置信度封顶 0.84（全部走封闭式确认）
                    for d in batch:
                        cs = s["candidates"][d["qid"]][:3]
                        ranked[d["qid"]] = [{"datapoint_id": c["datapoint_id"],
                                             "confidence": round(min(EMBED_ONLY_CAP, max(0.0, c["similarity"])), 3)} for c in cs]
                        methods[d["qid"]] = "semantic"
            return {"ranked": ranked, "methods": methods, "degraded": degraded}

        def self_check(s: MapState) -> MapState:  # 只保留候选集/字典中存在的 ID，去重，限 3 个
            ranked, dropped = {}, 0
            for qid, cs in s["ranked"].items():
                allowed = {c["datapoint_id"] for c in s["candidates"].get(qid, [])} if qid in s["candidates"] else None
                seen, keep = set(), []
                for c in sorted(cs, key=lambda c: -float(c["confidence"])):
                    base = c["datapoint_id"].split("@")[0]
                    if base in seen or (allowed is not None and base not in allowed) or latest_ref(svc.registry, base) is None:
                        dropped += 1
                        continue
                    seen.add(base)
                    keep.append({**c, "datapoint_id": base})
                ranked[qid] = keep[:3]
            return {"ranked": ranked, "dropped": dropped}

        def emit(s: MapState) -> MapState:  # Mapping v1（quarantine/candidate）
            items = []
            req = s["request"]
            for d in s["drafts"]:
                for rank_i, c in enumerate(s["ranked"].get(d["qid"], []), start=1):
                    dp_ref = latest_ref(svc.registry, c["datapoint_id"])
                    info = svc.registry.get(dp_ref)
                    unit_hint = d["unit_requested"] if (d["unit_requested"] and info and info.unit and
                                                        svc.store.units.compatible(d["unit_requested"], info.unit)) else None
                    body: dict[str, Any] = {
                        "id": ctx.det_id("Mapping", f"{d['qid']}:{rank_i}"), "trust_zone": "quarantine",
                        "question_id": d["id"], "datapoint_id": dp_ref, "confidence": round(float(c["confidence"]), 3),
                        "rank": rank_i, "method": s["methods"].get(d["qid"], "semantic"), "status": "candidate"}
                    if c.get("template_mapping_ref"):
                        body["template_mapping_ref"] = c["template_mapping_ref"]
                    if unit_hint:
                        body["unit_hint"] = unit_hint
                        if info and info.unit != unit_hint:
                            body["transform"] = {"kind": "unit_convert", "params": {"from": info.unit, "to": unit_hint}}
                    p = d.get("period_requested") or req.get("reporting_period")
                    if p:
                        body["period_hint"] = p
                    if info and "organization" in info.allowed_boundaries:
                        body["boundary_hint"] = "organization"
                    elif info and "product_per_unit" in info.allowed_boundaries:
                        body["boundary_hint"] = "product_per_unit"
                    items.append(WriteItem("Mapping", body, idempotency_key=f"mapping:{d['qid']}:{rank_i}"))
            return {"mapping_refs": ctx.write_batch(items)}

        for name, fn in (("segment", segment), ("candidates", candidates), ("rank", rank), ("self_check", self_check), ("emit", emit)):
            g.add_node(name, fn)
        g.add_edge(START, "segment")
        g.add_edge("segment", "candidates")
        g.add_edge("candidates", "rank")
        g.add_edge("rank", "self_check")
        g.add_edge("self_check", "emit")
        g.add_edge("emit", END)
        return g.compile(checkpointer=InMemorySaver())

    def _historical_index(self) -> dict[str, str]:
        """C2：本租户已确认（trusted confirmed）映射的题干 → DataPoint（不含答案值）。"""
        st, org = self.svc.store, self.svc.org_id
        qtext = {q["id"]: normalize(" ".join(q["text"].values())) for q in st.query("Question", org_id=org)
                 if q.get("trust_zone") == "trusted"}
        out = {}
        for m in st.query("Mapping", org_id=org, status="confirmed"):
            if m["question_id"] in qtext and m.get("trust_zone") == "trusted":
                out[qtext[m["question_id"]]] = m["datapoint_id"].split("@")[0]
        return out


def _enum_schema(schema: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    import copy

    s = copy.deepcopy(schema)
    item = s["properties"]["mappings"]["items"]["properties"]["candidates"]["items"]["properties"]["datapoint_id"]
    if ids:
        item["enum"] = ids
    return s


def _count(methods: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in methods.values():
        out[v] = out.get(v, 0) + 1
    return out
