# Spec: 04 §2.5 calc-service（确定性：belegt-calc 把 verified 活动数据 + FactorSet 算成 Calculation，并装配数字类 Answer(draft)；
#       方法论多选 → ClosedQuestion(single_choice)，人选择后记录在 params；R-HASH 失败告警不重试；输入缺失返回 gaps[]）、
#       05 §8.1（calculate API、CALC_PARAM_REQUIRES_DECISION）、03 §3.6、§4.5（input_hash）、04 §7.2 C5、NFR-03（数字通道）
"""计算服务（无 LLM、无模型网关路由）。

数字答案的两条通道（都只经 calc-service 写入，answer-drafter 不碰数字）：
1. 有计算配方的 DataPoint：``belegt_calc.calculate`` → ``Calculation`` → ``Answer(calculation_ref)``；
2. 直接以已核验证据回答的 DataPoint（如电力消耗、员工人数）：``Answer(evidence_refs=[唯一数值 Evidence])``，值为证据值经
   确定性单位换算（R-VAL 逐位校验）。
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from ..authz import Actor
from ..canonical import normalize_dec
from ..guard.promotion import new_cq
from .base import AgentBase, AgentOutput, RunContext
from .evidence_miner import DP_FACT
from .facts import CALC_RECIPES, DECISION_LABELS, DECISION_OPTIONS, OPTION_LABELS, Recipe


def ensure_factor_set(store: Any, fs_id: str) -> str:
    """把 belegt-calc 随包因子库登记进图（``service:factor-import``，全局 FactorSet + 因子行，03 §3.13）。"""
    if store.head(fs_id, org_id="org_global") is not None:
        return fs_id
    from belegt_calc import load_factor_set

    fs = load_factor_set(fs_id)
    meta = {k: getattr(fs, k, None) for k in ("source", "license", "published_at", "valid_from", "valid_until", "rows_hash")}
    rows = [r if isinstance(r, dict) else {k: getattr(r, k) for k in ("factor_key", "geography", "year", "value", "unit", "source_row_id")}
            for r in getattr(fs, "rows", [])]
    body = {"id": fs_id, "org_id": "org_global", "source": meta["source"] or fs_id[3:].split("@")[0],
            "license": meta["license"] or "unknown", "published_at": str(meta["published_at"]), "valid_from": str(meta["valid_from"]),
            "row_count": len(rows), "rows_hash": meta["rows_hash"], "status": "active"}
    if meta.get("valid_until"):
        body["valid_until"] = str(meta["valid_until"])
    store.write("FactorSet", body, actor=Actor.of("service:factor-import", "org_global"))
    store.put_factor_rows(fs_id, [{k: str(v) for k, v in r.items()} for r in rows])
    return fs_id


class CalcService(AgentBase):
    agent_id = "calc-service"

    def run(self, request_ref: str, targets: list[dict[str, Any]], *, idem_prefix: str,
            customer_scope: Any = None) -> AgentOutput:
        req_id = request_ref.split("#")[0]
        out = AgentOutput(outcome="calculated")
        scope = customer_scope if customer_scope is not None else self._scope(req_id)
        with self.context(req_id, idem_prefix, activity="calculate", input_obj={"targets": [t["datapoint_id"] for t in targets]}) as ctx:
            for t in targets:
                info = self.svc.registry.get(t["datapoint_id"])
                if info is None or info.value_type != "number":
                    continue
                if self._existing_answer(t, scope):
                    out.refs.append(self._existing_answer(t, scope))
                    continue
                base = t["datapoint_id"].split("@")[0]
                recipe = CALC_RECIPES.get(base)
                res = self._calc(ctx, t, recipe, scope) if recipe else self._direct(ctx, t, scope)
                out.refs += res.get("refs", [])
                out.cq_refs += res.get("cq", [])
                out.gaps += res.get("gaps", [])
        out.details["answers"] = sum(1 for r in out.refs if r.startswith("ans_"))
        out.details["calculations"] = sum(1 for r in out.refs if r.startswith("calc_"))
        return out

    # ------------------------------------------------------------------
    def _scope(self, req_id: str) -> Any:
        r = self.svc.store.get(req_id, org_id=self.svc.org_id)
        return [r["counterparty_id"]] if r and r.get("counterparty_id") else "all"

    def _existing_answer(self, t: dict[str, Any], scope: Any) -> str | None:
        qrefs = set(t.get("question_ids") or [])
        for a in self.svc.store.query("Answer", org_id=self.svc.org_id, datapoint_base=t["datapoint_id"].split("@")[0]):
            if a["status"] in ("draft", "in_review", "approved") and qrefs and qrefs & set(a.get("question_refs") or []):
                return f"{a['id']}#v{a['version']}"
        return None

    def _verified(self, fact_key: str | None, datapoint_id: str | None, period: dict[str, str]) -> list[dict[str, Any]]:
        out = []
        for e in self.svc.store.query("Evidence", org_id=self.svc.org_id, status="verified"):
            if e.get("trust_zone") != "trusted" or e.get("value_type") != "number":
                continue
            if fact_key and e.get("fact_key") != fact_key:
                continue
            if datapoint_id and (e.get("datapoint_id") or "").split("@")[0] != datapoint_id.split("@")[0]:
                continue
            ep = e.get("period") or {"start": e.get("valid_from"), "end": e.get("valid_until") or "9999-12-31"}
            if ep["start"] <= period["start"] and ep["end"] >= period["end"]:
                out.append(e)
        out.sort(key=lambda e: ({"erp": 0, "document": 1}.get(e["source_type"], 2), e["created_at"]))
        return out

    def _answer_base(self, t: dict[str, Any], scope: Any) -> dict[str, Any]:
        info = self.svc.registry.get(t["datapoint_id"])
        body: dict[str, Any] = {"datapoint_id": t["datapoint_id"], "value_type": "number", "unit": info.unit,
                                "period": t["period"], "boundary": t["boundary"], "customer_scope": scope,
                                "question_refs": list(t.get("question_ids") or []), "origin": "generated",
                                "is_estimate": False, "legal_commitment": False, "status": "draft"}
        if t["boundary"] == "organization" and info.requires_consolidation:
            body["consolidation"] = self.svc.org.consolidation
        return body

    def _decision(self, ctx: RunContext, t: dict[str, Any], param: str, cq_id: str, options: tuple[str, ...]) -> tuple[str | None, str | None]:
        """返回 (已选值, 待答 CQ ref)。CQ 以第一道题为 target。"""
        key = f"calcdecision:{t['datapoint_id']}:{param}"
        st = self.svc.store
        for cq in st.query("ClosedQuestion", org_id=self.svc.org_id):
            if f"decision:{cq_id}:{param}:{t['datapoint_id'].split('@')[0]}" in (cq.get("tags") or []):
                if cq["status"] == "answered":
                    return (cq["answer"] or {}).get("choice"), None
                if cq["status"] == "open":
                    return None, f"{cq['id']}#v{cq['version']}"
        qid = (t.get("question_ids") or [None])[0]
        q = st.get(qid, org_id=self.svc.org_id) if qid else None
        if q is None:
            return None, None
        ref = new_cq(st, self.actor(), lambda k: ctx.key(k), key=key, target_ref=f"{q['id']}#v{q['version']}",
                     request_id=ctx.request_id, kind="single_choice",
                     options=[{"key": o, "label": OPTION_LABELS.get(o, {"de": o, "en": o})} for o in options],
                     prompt=DECISION_LABELS.get(cq_id, {"de": cq_id, "en": cq_id}),
                     tags=[f"decision:{cq_id}:{param}:{t['datapoint_id'].split('@')[0]}", cq_id])
        return None, ref

    def _answered_decisions(self, t: dict[str, Any]) -> dict[str, str]:
        """本 DataPoint 已作答的方法论决策卡（标签 ``decision:<CQ-METH-xx>:<param>:<dp>``）→ ``{param: choice}``。"""
        base = t["datapoint_id"].split("@")[0]
        out: dict[str, str] = {}
        for cq in self.svc.store.query("ClosedQuestion", org_id=self.svc.org_id, status="answered"):
            for tag in cq.get("tags") or []:
                parts = tag.split(":", 3)
                if len(parts) == 4 and parts[0] == "decision" and parts[3] == base and (cq.get("answer") or {}).get("choice"):
                    out[parts[2]] = cq["answer"]["choice"]
        return out

    def _calc(self, ctx: RunContext, t: dict[str, Any], r: Recipe, scope: Any) -> dict[str, Any]:
        from belegt_calc import CalcError, InputValue, Period, calculate, load_factor_set

        st = self.svc.store
        inputs, refs, gaps = [], [], []
        for b in r.inputs:
            evs = self._verified(b.fact_key, None, t["period"])
            if not evs:
                if not b.optional:
                    gaps.append({"datapoint_id": t["datapoint_id"], "fact_key": b.fact_key, "kind": "internal",
                                 "question_refs": t.get("question_refs", []), "period": t["period"], "boundary": t["boundary"]})
                continue
            e = evs[0]
            v = st.units.convert(Decimal(str(e["extracted_value"])), e["unit"], b.unit) if e["unit"] != b.unit else Decimal(str(e["extracted_value"]))
            inputs.append(InputValue(b.name, f"{e['id']}#v{e['version']}", normalize_dec(v), b.unit))
        if gaps:
            return {"gaps": gaps}
        params = {k: v.format(consolidation=self.svc.org.consolidation, grid_region=self.svc.org.grid_region) for k, v in r.params.items()}
        # 引擎曾要求、人已作答的方法论决策（CALC_PARAM_REQUIRES_DECISION → ClosedQuestion → 作答）写回 params
        params.update(self._answered_decisions(t))
        cqs = []
        for p, (cq_id, opts) in r.decisions.items():
            choice, cq = self._decision(ctx, t, p, cq_id, opts)
            if choice:
                params[p] = choice
            elif cq:
                cqs.append(cq)
        fs_id = ensure_factor_set(st, r.factor_set)
        try:
            res = calculate(r.method_id, r.methodology_version, load_factor_set(fs_id), inputs, params, t["boundary"],
                            Period(t["period"]["start"], t["period"]["end"]))
        except CalcError as e:
            gap = {"datapoint_id": t["datapoint_id"], "kind": "internal", "error": e.code,
                   "question_refs": t.get("question_refs", []), "period": t["period"], "boundary": t["boundary"]}
            if e.code == "CALC_PARAM_REQUIRES_DECISION":
                # 05 §8.1：缺少须由人决定的方法论参数 → ClosedQuestion(single_choice)，选项取 05 CQ-METH-* 的合法取值
                m = re.search(r"params\.([A-Za-z0-9_]+) is required", e.detail or "")
                opts = DECISION_OPTIONS.get(e.decision or "")
                if m is None or not opts or m.group(1) in params:
                    return {"cq": cqs, "gaps": [gap]}  # 无法构造合法的 2–4 项选择 / 已作答仍失败 → 缺口转人工
                _, cq = self._decision(ctx, t, m.group(1), e.decision, opts)  # type: ignore[arg-type]
                return {"cq": cqs + ([cq] if cq else [])}
            return {"gaps": [gap]}
        cache_key = self.svc.ref_cache.key(res.input_hash, res.engine_version if hasattr(res, "engine_version") else "")
        calc_ref = self.svc.ref_cache.get("C5", cache_key)
        if calc_ref is None:
            for c in st.query("Calculation", org_id=self.svc.org_id, status="computed"):
                if c.get("input_hash") == res.input_hash:
                    calc_ref = f"{c['id']}#v{c['version']}"
                    break
        if calc_ref is None:
            body = res.to_dict()
            body.update(id=ctx.det_id("Calculation", f"calc:{res.input_hash}"), customer_scope=scope, status="computed")
            calc_ref = ctx.write("Calculation", body, key=f"calc:{res.input_hash}")
            self.svc.ref_cache.put("C5", cache_key, calc_ref)
        calc = st.get(calc_ref, org_id=self.svc.org_id)
        info = self.svc.registry.get(t["datapoint_id"])
        val = Decimal(calc["result"]["value"])
        if calc["result"]["unit"] != info.unit:
            val = st.units.convert(val, calc["result"]["unit"], info.unit)
        ans = self._answer_base(t, scope)
        ans.update(id=ctx.det_id("Answer", f"ans:{t['datapoint_id']}"), value=normalize_dec(val), calculation_ref=calc_ref)
        if res.annotations.get("is_estimate"):
            ans.update(is_estimate=True, estimate_method=str(res.annotations.get("estimate_method") or "belegt-calc estimate")[:200])
        aref = ctx.write("Answer", ans, key=f"ans:{t['datapoint_id']}")
        return {"refs": [calc_ref, aref], "cq": cqs}

    def _direct(self, ctx: RunContext, t: dict[str, Any], scope: Any) -> dict[str, Any]:
        st = self.svc.store
        base = t["datapoint_id"].split("@")[0]
        info = self.svc.registry.get(t["datapoint_id"])
        accepted = set((info.extra or {}).get("accepted_source_types") or ["document", "erp", "historical_answer", "supplier_response"])
        evs = self._verified(DP_FACT.get(base), None if base in DP_FACT else t["datapoint_id"], t["period"])
        evs = [e for e in evs if e["source_type"] in accepted]
        if not evs:
            return {"gaps": [{"datapoint_id": t["datapoint_id"], "fact_key": DP_FACT.get(base), "question_refs": t.get("question_refs", []),
                              "period": t["period"], "boundary": t["boundary"],
                              "kind": "supplier" if "supplier_response" in accepted else "internal"}]}
        e = evs[0]
        val = Decimal(str(e["extracted_value"]))
        if e.get("unit") != info.unit:
            val = st.units.convert(val, e["unit"], info.unit)
        ans = self._answer_base(t, scope)
        ans.update(id=ctx.det_id("Answer", f"ans:{t['datapoint_id']}"), value=normalize_dec(val),
                   evidence_refs=[f"{e['id']}#v{e['version']}"])
        if e["boundary"] != t["boundary"]:
            ans["boundary"] = e["boundary"]
        return {"refs": [ctx.write("Answer", ans, key=f"ans:{t['datapoint_id']}")]}
