# Spec: 04 §2.4 evidence-miner（trusted；历史答卷/文档/ERP 取证，带定位 Evidence；缺失出 ClosedQuestion 或缺口清单；
#       连接器失效降级为文档提取并标 freshness:degraded）、§2.1 EV-AUTO-0.90-LOC / EV-AUTO-ERP-ATTEST / EV-AUTO-HIST-L0、
#       §4.4 MineGraph（plan_sources → retrieve(MCP) → extract → normalize → readback_verify → decide）、07 §2.1 特例
#       （evidence-miner 读取的文档可能含注入：LLM 输出只能是 candidate，须经 EV-AUTO 规则或封闭式确认才 verified）
"""证据挖掘。

两路提取（EV-AUTO-0.90-LOC）：路径 A = ``belegt_io`` 版面解析（确定性，带页码/bbox/text_excerpt_hash）；路径 B = LLM
``extract_fields``（只抽取、不计算）。两路数值逐位相等、置信度 ≥ 0.90、原文回读一致、DataPoint 接受 document 来源时自动核验，
否则生成 ``ClosedQuestion(numeric_confirm)``。ERP 记录满足 attested + query_hash + 新鲜度时 ``connector_attested``。
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from ..canonical import normalize_dec, sha256_tag
from ..guard.loops import LoopDetector
from ..guard.promotion import new_cq
from ..llm.base import LLMOutputInvalid, ModelUnavailable
from ..llm.embedding import normalize
from .base import AgentBase, AgentOutput, RunContext, dp_label
from .facts import FACTS, fact_keys_for

DP_FACT: dict[str, str] = {  # 直接以活动数据回答的 DataPoint（无需计算）
    "dp:energy.consumption.electricity_total": "activity.electricity.purchased",
    "dp:energy.consumption.natural_gas": "activity.natural_gas.hi",
    "dp:energy.procurement.eac_volume": "activity.electricity.eac",
}
AUTO_CONF = 0.90


class MineState(TypedDict, total=False):
    target: dict[str, Any]
    plan: list[dict[str, Any]]
    found: list[dict[str, Any]]
    decided: list[dict[str, Any]]
    reuse: list[str]
    degraded: bool


def _dec(v: Any) -> str | None:
    try:
        return normalize_dec(Decimal(str(v)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _covers(p: dict[str, str] | None, q: dict[str, str] | None) -> bool:
    return bool(p and q and p["start"] <= q["start"] and p["end"] >= q["end"])


class EvidenceMiner(AgentBase):
    agent_id = "evidence-miner"
    recursion_limit = 12

    def run(self, request_ref: str, targets: list[dict[str, Any]], *, idem_prefix: str) -> AgentOutput:
        """``targets``: ``[{datapoint_id: dp:…@x.y.z, question_refs, period, boundary}]``（去重后的 DataPoint）。"""
        req_id = request_ref.split("#")[0]
        out = AgentOutput(outcome="mined")
        with self.context(req_id, idem_prefix, activity="mine_evidence", input_obj={"targets": [t["datapoint_id"] for t in targets]}) as ctx:
            graph = self._graph(ctx)
            for t in targets:
                cfg = {"configurable": {"thread_id": f"{idem_prefix}:{t['datapoint_id']}"}, "recursion_limit": self.recursion_limit}
                try:
                    st: MineState = graph.invoke({"target": t}, cfg)
                except GraphRecursionError as e:
                    raise LoopDetector.from_recursion(e, "MineGraph") from e
                dp_out = {"datapoint_id": t["datapoint_id"], "evidence": [], "cq": [], "reuse": st.get("reuse", [])}
                for d in st.get("decided", []):
                    if d.get("ref"):
                        dp_out["evidence"].append(d["ref"])
                        if d["ref"] not in out.refs:
                            out.refs.append(d["ref"])
                    if d.get("cq"):
                        dp_out["cq"].append(d["cq"])
                        if d["cq"] not in out.cq_refs:
                            out.cq_refs.append(d["cq"])
                    if d.get("gap"):
                        out.gaps.append(d["gap"])
                out.details.setdefault("per_datapoint", []).append(dp_out)
                if st.get("degraded"):
                    out.details["degraded"] = True
        return out

    # ------------------------------------------------------------------ MineGraph
    def _graph(self, ctx: RunContext):
        svc = self.svc
        st = svc.store
        g = StateGraph(MineState)
        tool_cache: dict[Any, Any] = {}

        def plan_sources(s: MineState) -> MineState:
            t = s["target"]
            base = t["datapoint_id"].split("@")[0]
            info = svc.registry.get(t["datapoint_id"])
            accepted = set((info.extra or {}).get("accepted_source_types") or ["document", "erp", "historical_answer"]) if info else set()
            plan = []
            fks = fact_keys_for(base)
            if fks:  # 需计算：取计算输入的活动数据（calc-service 消费），来源不受 DataPoint 来源限制
                plan += [{"kind": "fact", "fact_key": fk, "sources": ["erp", "document"]} for fk in fks]
            elif base in DP_FACT:
                plan.append({"kind": "fact", "fact_key": DP_FACT[base], "sources": [x for x in ("erp", "document") if x in accepted],
                             "datapoint_id": t["datapoint_id"]})
            else:
                plan.append({"kind": "dp", "datapoint_id": t["datapoint_id"], "value_type": info.value_type if info else "text",
                             "sources": [x for x in ("historical_answer", "erp", "document") if x in accepted]})
            for i, it in enumerate(plan):
                it["idx"] = i
            return {"plan": plan}

        def retrieve(s: MineState) -> MineState:
            t = s["target"]
            found, reuse, degraded = [], [], False
            for item in s["plan"]:
                key = item.get("fact_key") or item.get("datapoint_id")
                # 0) 跨 Request 共享：已有 verified 证据（同 fact/DataPoint、期间覆盖）直接复用
                existing = self._existing(item, t)
                if existing:
                    found.append({"item": item, "existing": existing})
                    continue
                # 1) 历史答卷（非数字 DataPoint：L0 复用候选，交 answer-drafter 用 reused_from）
                if "historical_answer" in item["sources"] and item["kind"] == "dp":
                    hist = [a for a in st.query("Answer", org_id=svc.org_id, status="submitted",
                                                datapoint_base=item["datapoint_id"].split("@")[0])
                            if a.get("value_type") != "number" and a.get("trust_zone") == "trusted"]
                    if hist:
                        reuse += [f"{a['id']}#v{a['version']}" for a in hist]
                        continue
                # 2) ERP（MCP 只读工具；GP-03）
                if "erp" in item["sources"]:
                    for c in [c for c in svc.connectors.values() if hasattr(c, "records")]:
                        tool = c.tools[0]
                        args = {"date_from": t["period"]["start"], "date_to": t["period"]["end"]}
                        args["fact_key" if item["kind"] == "fact" else "datapoint_id"] = (
                            item.get("fact_key") if item["kind"] == "fact" else item["datapoint_id"].split("@")[0])
                        ck = (c.connector_id, tool.name, tuple(sorted(args.items())))
                        env = tool_cache.get(ck)
                        if env is None:  # 同一 Activity 内相同查询只调用一次（避免 LD-01 误报与重复计费）
                            svc.guard.check_tool(self.agent_id, tool.name, tool.annotations, span=ctx.span,
                                                 activity_scope=ctx.idem_prefix, args=args)
                            try:
                                env = c.call(tool.name, args, now=st.peek_now()[:19] + "Z")
                            except ConnectionError:
                                degraded = True  # 连接器失效 → 降级为文档提取（PRD §4.5）
                                continue
                            tool_cache[ck] = env
                        for r in env["records"]:
                            found.append({"item": item, "erp": r, "env": env, "connector": c.connector_id})
                    if any(f["item"]["idx"] == item["idx"] for f in found):
                        continue
                # 3) 文档（docs.search / docs.get_page）
                if "document" in item["sources"] and "docs" in svc.connectors:
                    docs = svc.connectors["docs"]
                    if "docs.search" not in tool_cache:
                        svc.guard.check_tool(self.agent_id, "docs.search", {"readOnlyHint": True}, span=ctx.span,
                                             activity_scope=ctx.idem_prefix, args={"org": svc.org_id})
                        tool_cache["docs.search"] = docs.search()
                    for d in tool_cache["docs.search"]:
                        found.extend({"item": item, "doc": d, **x} for x in self._doc_candidates(d, item))
            return {"found": found, "reuse": reuse, "degraded": degraded}

        def extract(s: MineState) -> MineState:  # 路径 B：LLM 抽取（只对文档候选）
            for f in s["found"]:
                if "doc" not in f or f.get("path_b") is not None:
                    continue
                labels = FACTS.get(f["item"].get("fact_key"), {}).get("labels") or [f.get("label") or ""]
                try:
                    res = ctx.ask("extract_fields", {"fact_key": f["item"].get("fact_key") or f["item"].get("datapoint_id"),
                                                     "labels": labels, "unit_expected": FACTS.get(f["item"].get("fact_key"), {}).get("unit"),
                                                     "page": f.get("page"), "lang": "de"},
                                  external={"chunk_text": f.get("page_text", "")})
                except (ModelUnavailable, LLMOutputInvalid):
                    res = {"value_text": None}
                f["path_b"] = res
            return {"found": s["found"]}

        def normalize_vals(s: MineState) -> MineState:  # 确定性：value_text → DEC-STR，单位归一
            from belegt_io import normalize_unit, parse_number

            for f in s["found"]:
                b = f.get("path_b")
                if not b or not b.get("value_text"):
                    f["b_value"] = None
                    continue
                try:
                    pn = parse_number(b["value_text"], lang="de")
                    v = getattr(pn, "value", pn)
                except Exception:
                    v = None
                f["b_value"] = _dec(v) if v is not None else None
                f["b_unit"] = (normalize_unit(b.get("unit_text") or "") or (None,))[0] if b.get("unit_text") else None
            return {"found": s["found"]}

        def readback_verify(s: MineState) -> MineState:  # 原文回读：摘录哈希一致、值出现在定位处
            for f in s["found"]:
                if "doc" in f:
                    exc = f.get("excerpt") or ""
                    f["readback_ok"] = bool(f.get("text_excerpt_hash")) and sha256_tag(exc.encode("utf-8")) == f["text_excerpt_hash"] \
                        if f.get("text_excerpt_hash_kind") == "excerpt" else bool(f.get("text_excerpt_hash")) and exc in (f.get("page_text") or "")
            return {"found": s["found"]}

        def decide(s: MineState) -> MineState:
            t = s["target"]
            decided = []
            by_item: dict[int, list[dict[str, Any]]] = {}
            for f in s["found"]:
                by_item.setdefault(f["item"]["idx"], []).append(f)
            for item in s["plan"]:
                fs = by_item.get(item["idx"], [])
                if not fs:
                    if s.get("reuse") and item["kind"] == "dp":
                        continue
                    decided.append({"gap": self._gap(t, item)})
                    continue
                f = fs[0]
                if "existing" in f:
                    decided.append({"ref": f["existing"]})
                elif "erp" in f:
                    decided.append(self._write_erp(ctx, t, item, f, degraded=bool(s.get("degraded"))))
                else:
                    decided.append(self._write_doc(ctx, t, item, f, degraded=bool(s.get("degraded"))))
            return {"decided": decided}

        for name, fn in (("plan_sources", plan_sources), ("retrieve", retrieve), ("extract", extract),
                         ("normalize", normalize_vals), ("readback_verify", readback_verify), ("decide", decide)):
            g.add_node(name, fn)
        g.add_edge(START, "plan_sources")
        for a, b in (("plan_sources", "retrieve"), ("retrieve", "extract"), ("extract", "normalize"),
                     ("normalize", "readback_verify"), ("readback_verify", "decide")):
            g.add_edge(a, b)
        g.add_edge("decide", END)
        return g.compile(checkpointer=InMemorySaver())

    # ------------------------------------------------------------------ helpers
    def _existing(self, item: dict[str, Any], t: dict[str, Any]) -> str | None:
        st = self.svc.store
        for e in st.query("Evidence", org_id=self.svc.org_id, status="verified"):
            if e.get("trust_zone") != "trusted":
                continue
            if item["kind"] == "fact" and e.get("fact_key") != item["fact_key"]:
                continue
            if item["kind"] == "dp" and (e.get("datapoint_id") or "").split("@")[0] != item["datapoint_id"].split("@")[0]:
                continue
            if e.get("boundary") != t["boundary"] and item["kind"] == "dp":
                continue
            validity = {"start": e.get("valid_from"), "end": e.get("valid_until") or "9999-12-31"}
            if _covers(e.get("period"), t["period"]) or _covers(validity, t["period"]):
                return f"{e['id']}#v{e['version']}"
        return None

    def _doc_candidates(self, d: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
        from belegt_io import extract_pdf_evidence
        from belegt_io.evidence import pdf_pages_text

        docs = self.svc.connectors["docs"]
        data = docs.get(d["blob_sha256"])
        if data is None:
            return []
        out = []
        if d["mime"] == "application/pdf" and item["kind"] == "fact":
            labels = [normalize(x) for x in FACTS.get(item["fact_key"], {}).get("labels", [])]
            fd, path = tempfile.mkstemp(suffix=".pdf")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                pages = pdf_pages_text(path)
                for c in extract_pdf_evidence(path):
                    lab = normalize(c.label or "")
                    if not labels or not any(l and (l in lab or lab.startswith(l)) for l in labels):
                        continue
                    loc = c.locator.to_json()
                    loc["blob_sha256"] = d["blob_sha256"]
                    out.append({"value": _dec(c.extracted_value), "unit": c.unit, "conf": float(c.confidence), "locator": loc,
                                "period": c.period.to_json() if c.period else None, "label": c.label, "excerpt": c.excerpt,
                                "page": c.locator.page, "page_text": pages[(c.locator.page or 1) - 1] if pages else "",
                                "text_excerpt_hash": loc.get("text_excerpt_hash"), "value_type": "number"})
            finally:
                os.unlink(path)
        elif d["kind"] == "register" and item["kind"] == "dp":
            out.extend(self._register_rows(d, data, item))
        return out

    def _register_rows(self, d: dict[str, Any], data: bytes, item: dict[str, Any]) -> list[dict[str, Any]]:
        """键值登记表（xlsx，A=DataPoint ID，B=标签，C=值）：确定性单元格读取（路径 A），带单元格定位。"""
        import io

        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        out = []
        base = item["datapoint_id"].split("@")[0]
        for ws in wb.worksheets:
            for row in ws.iter_rows(min_row=1):
                if len(row) < 3 or str(row[0].value or "").strip() != base:
                    continue
                v = row[2].value
                if isinstance(v, (dt.date, dt.datetime)):
                    v = (v.date() if isinstance(v, dt.datetime) else v).isoformat()
                text = f"{row[1].value}: {v}"
                unit = str(row[3].value).strip() if len(row) > 3 and row[3].value not in (None, "") else None
                out.append({"value": v if not isinstance(v, (int, float, Decimal)) else _dec(v), "unit": unit, "conf": 0.95,
                            "locator": {"blob_sha256": d["blob_sha256"],
                                        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                        "sheet": ws.title, "cell": row[2].coordinate,
                                        "text_excerpt_hash": sha256_tag(text.encode("utf-8"))},
                            "label": str(row[1].value or ""), "excerpt": text, "page_text": text, "text_excerpt_hash_kind": "excerpt",
                            "text_excerpt_hash": sha256_tag(text.encode("utf-8")),
                            "value_type": "number" if item.get("value_type") == "number" else item.get("value_type", "text"),
                            "valid_until": v if item.get("value_type") == "date" and isinstance(v, str) else None})
        return out

    def _gap(self, t: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        info = self.svc.registry.get(t["datapoint_id"])
        accepted = (info.extra or {}).get("accepted_source_types") or [] if info else []
        return {"datapoint_id": t["datapoint_id"], "fact_key": item.get("fact_key"), "question_refs": t.get("question_refs", []),
                "period": t["period"], "boundary": t["boundary"],
                "kind": "supplier" if "supplier_response" in accepted else "internal"}

    def _base_evidence(self, ctx: RunContext, t: dict[str, Any], item: dict[str, Any], key: str) -> dict[str, Any]:
        body: dict[str, Any] = {"id": ctx.det_id("Evidence", key),
                                "fact_key": item.get("fact_key") or item["datapoint_id"].split("@")[0].split(":", 1)[1],
                                "boundary": t["boundary"], "status": "candidate"}
        if item["kind"] == "dp" or item.get("datapoint_id"):
            body["datapoint_id"] = item.get("datapoint_id") or t["datapoint_id"]
        return body

    def _write_erp(self, ctx: RunContext, t: dict[str, Any], item: dict[str, Any], f: dict[str, Any], *, degraded: bool) -> dict[str, Any]:
        r, env = f["erp"], f["env"]
        c = next(x for x in self.svc.connectors.values() if getattr(x, "connector_id", None) == f["connector"])
        key = f"org:ev:erp:{c.connector_id}:{r['record_key']}:{env['fetched_at'][:10]}"
        body = self._base_evidence(ctx, t, item, key)
        p = r.get("period") or t["period"]
        vt = r.get("value_type") or ("number" if item["kind"] == "fact" else "text")
        body.update(source_type="erp", extracted_value=r["fields"]["value"] if vt == "number" else _typed(r["fields"]["value"], vt),
                    value_type=vt, period=p, valid_from=p["start"], confidence=0.97,
                    locator={"connector": {"connector_id": c.connector_id, "query_hash": env["query_hash"],
                                           "record_key": r["record_key"], "fetched_at": env["fetched_at"]}})
        if vt == "number":
            body["unit"] = r.get("unit") or "1"
        if degraded:
            body["tags"] = ["freshness:degraded"]
        ref = ctx.write("Evidence", body, key=key)
        info = self.svc.registry.get(t["datapoint_id"])
        max_age = (info.extra or {}).get("max_age_days") if info else None
        fresh = True
        if max_age:
            fetched = dt.datetime.fromisoformat(env["fetched_at"].replace("Z", "+00:00"))
            now = dt.datetime.fromisoformat(self.svc.store.peek_now()[:19] + "+00:00")
            fresh = (now - fetched).days <= int(max_age)
        if env.get("attested") and env.get("query_hash") and fresh:  # EV-AUTO-ERP-ATTEST
            ref = ctx.write("Evidence", {"id": body["id"], "status": "verified",
                                         "verification": {"method": "connector_attested", "rule_id": "EV-AUTO-ERP-ATTEST",
                                                          "by": f"agent:{self.agent_id}"}},
                            key=key + ":verify", expected_version=1, patch_mode="merge_from_head")
            return {"ref": ref}
        return {"ref": ref, "cq": self._confirm_cq(ctx, ref, body, t)}

    def _write_doc(self, ctx: RunContext, t: dict[str, Any], item: dict[str, Any], f: dict[str, Any], *, degraded: bool) -> dict[str, Any]:
        loc = f["locator"]
        key = f"org:ev:doc:{loc.get('blob_sha256')}:{loc.get('page') or ''}:{loc.get('sheet') or ''}{loc.get('cell') or ''}:{f.get('label')}"
        body = self._base_evidence(ctx, t, item, key)
        vt = f.get("value_type") or "number"
        p = f.get("period") or t["period"]
        valid_until = f.get("valid_until")  # 只有证书类证据有有效期；年度活动数据无 valid_until（避免 CR-004 误判）
        valid_from = p["start"] if not valid_until or p["start"] <= valid_until else valid_until
        body.update(source_type="document", extracted_value=f["value"], value_type=vt, period=p if vt == "number" else None,
                    valid_from=valid_from, valid_until=valid_until, confidence=round(f["conf"], 3), locator=loc)
        if vt == "number":
            body["unit"] = f.get("unit") or "1"
        body = {k: v for k, v in body.items() if v is not None}
        if degraded:
            body["tags"] = ["freshness:degraded"]
        ref = ctx.write("Evidence", body, key=key)
        info = self.svc.registry.get(t["datapoint_id"])
        accepts_doc = (info is None) or "document" in ((info.extra or {}).get("accepted_source_types") or ["document"]) or item["kind"] == "fact"
        two_path = vt != "number" or (f.get("b_value") is not None and f["b_value"] == f["value"] and
                                      (f.get("b_unit") in (None, f.get("unit"))))
        if f["conf"] >= AUTO_CONF and f.get("readback_ok") and two_path and accepts_doc:  # EV-AUTO-0.90-LOC
            ref = ctx.write("Evidence", {"id": body["id"], "status": "verified",
                                         "verification": {"method": "auto_rule", "rule_id": "EV-AUTO-0.90-LOC",
                                                          "by": f"agent:{self.agent_id}"}},
                            key=key + ":verify", expected_version=1, patch_mode="merge_from_head")
            return {"ref": ref}
        return {"ref": ref, "cq": self._confirm_cq(ctx, ref, body, t)}

    def _confirm_cq(self, ctx: RunContext, ev_ref: str, body: dict[str, Any], t: dict[str, Any]) -> str:
        fk = body.get("fact_key")
        label = FACTS.get(fk, {}).get("label") or {"de": dp_label(self.svc.registry.get(t["datapoint_id"]), "de"),
                                                    "en": dp_label(self.svc.registry.get(t["datapoint_id"]), "en")}
        pv = None
        if body.get("value_type") == "number":
            pv = {"value": body["extracted_value"], "unit": body["unit"], "evidence_ref": ev_ref}
        return new_cq(self.svc.store, self.actor(), lambda k: ctx.key("org:" + k), key=f"evconfirm:{body['id']}", target_ref=ev_ref,
                      request_id=ctx.request_id, kind="numeric_confirm" if pv else "yes_no", proposed_value=pv,
                      prompt={"de": f"Wert bestätigen: {label.get('de')}", "en": f"Confirm value: {label.get('en')}"},
                      context_refs=[r for r in t.get("question_refs", []) if "#v" in r][:5])


def _typed(v: Any, vt: str) -> Any:
    if vt == "bool":
        return v if isinstance(v, bool) else str(v).lower() in ("true", "ja", "yes", "1")
    return v
