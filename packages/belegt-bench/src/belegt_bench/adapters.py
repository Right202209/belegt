# Spec: 11 §4.1（为每次运行创建一次性沙箱租户与内存版证据图，Agent 走与生产相同的代码路径与写入校验；
#       所有 LLM 调用经 guard）、§4.3（temperature=0 / 固定模型；默认 MockLLM，离线确定性）
"""被测对象适配器：把 belegt_core 的真实 Agent / guard 组件接到运行器上。不可用时抛 `AdapterUnavailable(原因)`，
运行器据此跳过并提示（不当作失败）。

question-parser 适配器的路径（与生产一致）：
    合成问卷 → CSV blob → Request(quarantine) → build_hints（trusted 检索）→ `QuestionParser.run`
    （belegt-io 切题 → MapGraph → LLM(MockLLM，经 GuardedLLM) → self_check → Mapping 写入校验）→ 读回 Mapping。

另可用 `--entrypoint module:function` 接入任意实现，协议：
    fn(questions: list[{question_id, text, lang, unit_requested, period_requested, native_id}], llm) ->
        list[{question_id, candidates: [{datapoint_id, confidence, rank?}]}]
"""
from __future__ import annotations

import importlib
from decimal import Decimal
from typing import Any, Callable


class AdapterUnavailable(RuntimeError):
    pass


def _mock_llm() -> Any:
    try:
        from belegt_core.llm import MockLLM
    except Exception as e:  # noqa: BLE001
        raise AdapterUnavailable(f"belegt_core.llm.MockLLM 不可导入：{e}") from e
    return MockLLM()


def entrypoint_mapper(spec: str) -> Callable[[list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    mod, _, fn = spec.partition(":")
    try:
        f = getattr(importlib.import_module(mod), fn)
    except Exception as e:  # noqa: BLE001
        raise AdapterUnavailable(f"入口 {spec} 不可导入：{e}") from e
    try:
        llm = _mock_llm()
    except AdapterUnavailable:
        llm = None

    def run(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        qs = [{"question_id": s["question_id"], "text": s["text"], "lang": s["lang"], "native_id": s.get("native_id"),
               "unit_requested": s.get("unit_hint"), "period_requested": s.get("period_hint")} for s in samples]
        out = f(qs, llm)
        return {o["question_id"]: {"candidates": o.get("candidates") or []} for o in out}

    return run


class CoreQuestionParser:
    """belegt_core.agents.QuestionParser 端到端（每份问卷一个沙箱租户）。"""

    name = "core:question-parser"

    def __init__(self, llm_factory: Callable[[], Any] | None = None) -> None:
        try:
            from belegt_core.agents.base import OrgConfig, Services, build_registry  # noqa: F401
            from belegt_core.agents.hints import build_hints, put_hints  # noqa: F401
            from belegt_core.agents.question_parser import QuestionParser  # noqa: F401
            from belegt_core.guard import Guard  # noqa: F401
            from belegt_core.store import GraphStore  # noqa: F401
        except Exception as e:  # noqa: BLE001
            raise AdapterUnavailable(f"belegt_core.agents 不可导入：{type(e).__name__}: {e}") from e
        self._llm_factory = llm_factory or _mock_llm
        self._registry = None
        self.cost_eur = Decimal(0)
        self.model_id = None
        self.prompt_version = None

    def _reg(self) -> Any:
        if self._registry is None:
            from belegt_core.agents.base import build_registry
            self._registry = build_registry()
        return self._registry

    def run_cluster(self, samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        from belegt_core.agents.base import OrgConfig, Services
        from belegt_core.agents.hints import build_hints, put_hints
        from belegt_core.agents.question_parser import QuestionParser
        from belegt_core.authz import Actor
        from belegt_core.guard import Guard
        from belegt_core.guard.promotion import readback_questionnaire
        from belegt_core.ids import new_id
        from belegt_core.store import GraphStore

        store = GraphStore("sqlite+pysqlite:///:memory:", datapoints=self._reg())
        guard = Guard(store)
        org, cp = new_id("org_"), new_id("cp_")
        admin = Actor.of("user:u_bench_admin", org, ["org_admin"])
        store.write("Organization", {"id": org, "legal_name": "Bench Sandbox GmbH", "country": "DE",
                                     "default_consolidation": "operational_control", "data_residency": "eu",
                                     "locale_default": "de"}, actor=admin)
        store.write("Counterparty", {"id": cp, "role": "customer", "legal_name": "Bench OEM", "country": "DE",
                                     "domains": ["oem.bench.example"]}, actor=admin)
        llm = self._llm_factory()
        svc = Services(store=store, llm=llm, guard=guard, org=OrgConfig(org_id=org))
        lines = ["Nr.;Frage;Antwort;Einheit"]
        by_native: dict[str, dict[str, Any]] = {}
        for i, s in enumerate(samples, start=1):
            text = s["text"].replace(";", ",").replace("\n", " ")
            lines.append(f"{i};{text};;{s.get('unit_hint') or ''}")
            by_native[str(i)] = s
        sha = store.put_blob(org, ("\r\n".join(lines) + "\r\n").encode("utf-8"), "text/csv")
        rid = new_id("req_")
        ref = store.write("Request", {
            "id": rid, "trust_zone": "quarantine", "status": "received", "counterparty_id": cp, "channel": "upload",
            "source_ref": {"blob_sha256": [sha]}, "title": "belegt-bench sandbox", "language": "de",
            "priority": "normal", "triage_confidence": 0.95,
            "reporting_period": {"start": "2025-01-01", "end": "2025-12-31"}}, actor=Actor.of("agent:intake-triage", org))
        pq = readback_questionnaire(store, org, store.get(rid, org_id=org))
        if pq is None:
            raise AdapterUnavailable("belegt-io 无法回读合成问卷 CSV")
        hints = put_hints(svc, build_hints(svc, [{"qid": f"q{d.ordinal}", "text": d.text} for d in pq.questions]))
        QuestionParser(svc).run(ref, hints, idem_prefix=f"wf:{rid}:act:parse_questions:1")
        qnative = {q["id"]: str(q.get("native_id") or "") for q in store.query("Question", org_id=org)}
        out: dict[str, dict[str, Any]] = {s["question_id"]: {"candidates": []} for s in samples}
        for m in store.query("Mapping", org_id=org):
            s = by_native.get(qnative.get(m["question_id"], ""))
            if s is None:
                continue
            out[s["question_id"]]["candidates"].append({"datapoint_id": m["datapoint_id"], "rank": m["rank"],
                                                        "confidence": float(m["confidence"]),
                                                        "method": m.get("method")})
        try:
            self.cost_eur += Decimal(str(guard.ledger.totals().get("cost_eur", 0)))
        except Exception:  # noqa: BLE001
            pass
        self.prompt_version = QuestionParser(svc).prompt_version()
        self.model_id = getattr(llm, "model", None) or type(llm).__name__
        return out


def core_injection_detector() -> Any:
    try:
        from .baselines import CoreGuardInjectionDetector
        return CoreGuardInjectionDetector()
    except Exception as e:  # noqa: BLE001
        raise AdapterUnavailable(f"belegt_core.guard.injection 不可导入：{e}") from e
