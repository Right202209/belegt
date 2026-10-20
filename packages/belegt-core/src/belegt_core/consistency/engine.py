# Spec: 06 §1.1（只发现并登记冲突，不裁决）、§1.3（触发 T1–T4、fail-closed 门禁）、§1.4（执行流程、严重度映射、
#       Conflict.detail 扩展字段、not_evaluable 两种）、§1.5（解释）、§3.1（终态后再次 fail 生成新 open 并 supersedes 旧冲突；
#       关联对象均 superseded 且复检通过 → auto_cleared）、§8.2（fail-closed、规则版本可回放）；04 §2.6
"""consistency-auditor 规则引擎。

    engine = ConsistencyEngine(store)
    run = engine.run("answer_write", org_id=org, answer_refs=["ans_…#v2"])     # T1 增量
    run = engine.gate(org, "req_…")                                             # T2 门禁：run.gate ∈ pass|block|unknown
    run = engine.daily(org)                                                     # T3 全量
    run = engine.run("context_change", org_id=org, evidence_refs=["ev_…#v3"])  # T4 影响分析

``run.results``：每条规则×输入集的 ``pass|fail|not_evaluable|unknown``；``run.conflicts_written``：新写入的
``Conflict``（``agent:consistency-auditor``）；``info`` 只写 ``obs.audit_finding``（非图表，经 ``ObsSink``）。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol

from ..authz import Actor
from ..canonical import hash_obj
from ..errors import GraphError
from ..hashing import conflict_key
from ..ids import new_id
from .cel import CelError, compile_expr
from .context import AuditConfig, AuditContext, ref_of, split_ref
from .explain import Explainer, guarded_explanation
from .meta import MetaProvider
from .plugins import PLUGINS, Binding, Scope
from .ruleset import SEVERITY_TO_GRAPH, RuleSet, RuleSpec, default_ruleset

AUDITOR = "agent:consistency-auditor"
TERMINAL = ("resolved", "accepted_divergence", "dismissed")
# 失败原因是暂态（如"进入批量批次"）的规则：复检通过即可 auto_cleared，不要求关联对象 superseded
TRANSIENT_RULES = frozenset({"CR-042"})


@dataclass
class RuleResult:
    rule_id: str
    rule_version: str
    outcome: str  # pass | fail | not_evaluable | unknown
    severity: str  # info | warn | block（fail 时的有效严重度）
    subjects: list[str]
    datapoint_id: str | None
    detail: dict[str, Any]
    request_id: str | None = None
    instance_id: str | None = None
    reason: str | None = None
    conflict_key: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditRun:
    run_id: str
    org_id: str
    trigger: str
    ruleset: str
    as_of: str
    request_id: str | None
    results: list[RuleResult] = field(default_factory=list)
    conflicts_written: list[str] = field(default_factory=list)
    conflicts_existing: list[str] = field(default_factory=list)
    auto_cleared: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    gate: str | None = None  # pass | block | unknown（T2）
    result_hash: str | None = None

    @property
    def gate_blocks(self) -> bool:
        """fail-closed：``unknown`` 按 ``block`` 处理（06 §1.3）。"""
        return self.gate in ("block", "unknown")

    def failed(self, rule_id: str | None = None) -> list[RuleResult]:
        return [r for r in self.results if r.outcome == "fail" and (rule_id is None or r.rule_id == rule_id)]

    def outcomes(self, rule_id: str) -> list[str]:
        return [r.outcome for r in self.results if r.rule_id == rule_id]

    def summary(self) -> dict[str, Any]:
        cnt: dict[str, int] = {}
        for r in self.results:
            cnt[r.outcome] = cnt.get(r.outcome, 0) + 1
        return {"run_id": self.run_id, "trigger": self.trigger, "ruleset": self.ruleset, "as_of": self.as_of,
                "request_id": self.request_id, "counts": cnt, "gate": self.gate, "result_hash": self.result_hash,
                "conflicts_written": self.conflicts_written, "conflicts_existing": self.conflicts_existing,
                "auto_cleared": self.auto_cleared, "findings": self.findings, "events": self.events, "errors": self.errors}


class ObsSink(Protocol):
    def record_finding(self, finding: dict[str, Any]) -> None: ...
    def record_run(self, report: dict[str, Any]) -> None: ...


class InMemoryObsSink:
    """``obs.audit_finding`` / ``obs.audit_run`` 的内存实现（非图表，可由图重建，SSOT-7）。"""

    def __init__(self) -> None:
        self.findings: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []

    def record_finding(self, finding: dict[str, Any]) -> None:
        self.findings.append(finding)

    def record_run(self, report: dict[str, Any]) -> None:
        self.runs.append(report)


class ConsistencyEngine:
    def __init__(self, store: Any = None, *, ruleset: RuleSet | None = None, explainer: Explainer | None = None,
                 meta: MetaProvider | None = None, config: AuditConfig | None = None, obs: ObsSink | None = None,
                 plugins: dict[str, Any] | None = None) -> None:
        self.store = store
        self.ruleset = ruleset or default_ruleset()
        self.explainer = explainer
        self.meta = meta or MetaProvider.default()
        self.config = config or AuditConfig()
        self.obs = obs or InMemoryObsSink()
        self.plugins = dict(PLUGINS)
        if plugins:
            self.plugins.update(plugins)

    # ------------------------------------------------------------------ 纯求值
    def evaluate(self, ctx: AuditContext, scope: Scope, *, only: Iterable[str] | None = None) -> list[RuleResult]:
        out: list[RuleResult] = []
        for rule in self.ruleset.for_trigger(scope.trigger, only):
            fn = self.plugins.get(rule.plugin)
            if fn is None:
                out.append(RuleResult(rule.rule_id, rule.rule_version, "unknown", "block", [], None, {},
                                      reason=f"plugin {rule.plugin} not registered"))
                continue
            try:
                bindings = list(fn(ctx, scope))
            except Exception as e:  # 插件异常 → fail-closed（06 §1.3、T-13）
                out.append(RuleResult(rule.rule_id, rule.rule_version, "unknown", "block", [], None, {},
                                      reason=f"plugin error: {type(e).__name__}: {e}"))
                continue
            for b in bindings:
                out.append(self._eval_binding(rule, b))
        return out

    def _eval_binding(self, rule: RuleSpec, b: Binding) -> RuleResult:
        base = dict(rule_id=rule.rule_id, rule_version=rule.rule_version, subjects=list(b.subjects),
                    datapoint_id=b.datapoint_id, detail=b.detail, request_id=b.request_id, instance_id=b.instance_id,
                    events=list(b.events))
        ck = conflict_key(rule.rule_id, b.subjects) if b.subjects else None
        if b.error:
            return RuleResult(outcome="unknown", severity="block", reason=b.error, conflict_key=ck, **base)
        if b.not_evaluable:
            return RuleResult(outcome="not_evaluable", severity=rule.severity, reason=b.not_evaluable, conflict_key=ck, **base)
        try:
            if rule.when and not compile_expr(rule.when).evaluate_bool(b.vars):
                return RuleResult(outcome="not_evaluable", severity=rule.severity, reason="when=false", conflict_key=ck, **base)
            ok = compile_expr(b.assert_override or rule.assert_).evaluate_bool(b.vars)
            if ok:
                return RuleResult(outcome="pass", severity=rule.severity, conflict_key=ck, **base)
            sev = b.severity or (str(compile_expr(rule.severity_expr).evaluate(b.vars)) if rule.severity_expr else rule.severity)
            if sev not in ("info", "warn", "block"):
                sev = "block"
            return RuleResult(outcome="fail", severity=sev, conflict_key=ck, **base)
        except CelError as e:
            return RuleResult(outcome="unknown", severity="block", reason=str(e), conflict_key=ck, **base)

    # ------------------------------------------------------------------ 触发入口
    def run(self, trigger: str, *, org_id: str, request_id: str | None = None, answer_refs: Iterable[str] = (),
            evidence_refs: Iterable[str] = (), submit_date: str | dt.date | None = None, write: bool = True,
            staged_batch_answers: Iterable[str] = (), only: Iterable[str] | None = None,
            context: AuditContext | None = None) -> AuditRun:
        if self.store is None and context is None:
            raise ValueError("ConsistencyEngine.run needs a store or a context")
        ctx = context or AuditContext.from_store(self.store, org_id, meta=self.meta, config=self.config,
                                                 submit_date=submit_date, staged_batch_answers=staged_batch_answers)
        scope = self._scope(ctx, trigger, request_id, answer_refs, evidence_refs)
        run = AuditRun(run_id=_run_id(), org_id=org_id,
                       trigger=trigger, ruleset=self.ruleset.ref, as_of=ctx.today.isoformat(), request_id=request_id)
        run.results = self.evaluate(ctx, scope, only=only)
        for r in run.results:
            if r.outcome == "unknown":
                alert = {"kind": "alert", "rule_id": r.rule_id, "reason": r.reason, "trigger": trigger,
                         "request_id": request_id, "run_id": run.run_id}
                run.findings.append(alert)
                self.obs.record_finding(alert)
            if r.outcome == "fail":
                run.events.extend(r.events)
        if write and self.store is not None:
            self._write_conflicts(ctx, run)
            self._auto_clear(ctx, run, only)
        else:
            for r in run.failed():
                if r.severity == "info":
                    self._finding(run, r)
        run.result_hash = hash_obj([{"rule_id": r.rule_id, "instance_id": r.instance_id, "outcome": r.outcome,
                                     "severity": r.severity, "subjects": r.subjects} for r in run.results])
        if trigger == "pre_submit_gate" or request_id:
            run.gate = self._gate(run, org_id, request_id, ctx)
        self.obs.record_run(run.summary())
        return run

    def gate(self, org_id: str, request_id: str, **kw: Any) -> AuditRun:
        return self.run("pre_submit_gate", org_id=org_id, request_id=request_id, **kw)

    def daily(self, org_id: str, **kw: Any) -> AuditRun:
        return self.run("daily_batch", org_id=org_id, **kw)

    # ------------------------------------------------------------------ 内部
    def _scope(self, ctx: AuditContext, trigger: str, request_id: str | None, answer_refs: Iterable[str],
               evidence_refs: Iterable[str]) -> Scope:
        answer_ids = {split_ref(r)[0] for r in answer_refs}
        ev_ids = {split_ref(r)[0] for r in evidence_refs}
        if trigger == "context_change" and ev_ids:
            for e in list(ev_ids):  # 影响分析：新版本/更正证据所替换的对象也在范围内（03 §4.7）
                for v in ctx.versions(e):
                    for k in ("supersedes", "corrects"):
                        if v.get(k):
                            ev_ids.add(split_ref(v[k])[0])
            for a in ctx.heads("Answer"):
                cited = {split_ref(r)[0] for r in ctx.cited_evidence(a)}
                if cited & ev_ids:
                    answer_ids.add(a["id"])
            return Scope(trigger, request_ids={request_id} if request_id else None, answer_ids=answer_ids, evidence_ids=ev_ids)
        if trigger == "daily_batch" and not request_id and not answer_ids:
            return Scope(trigger)
        return Scope(trigger, request_ids={request_id} if request_id else None,
                     answer_ids=answer_ids or None, evidence_ids=ev_ids or None)

    def _finding(self, run: AuditRun, r: RuleResult) -> None:
        f = {"kind": "audit_finding", "rule_id": r.rule_id, "instance_id": r.instance_id, "severity": "info",
             "subjects": r.subjects, "datapoint_id": r.datapoint_id, "detail": r.detail, "request_id": r.request_id,
             "run_id": run.run_id, "ruleset_version": self.ruleset.ref}
        run.findings.append(f)
        self.obs.record_finding(f)

    def _write_conflicts(self, ctx: AuditContext, run: AuditRun) -> None:
        actor = Actor.of(AUDITOR, run.org_id)
        by_key: dict[str, list[dict[str, Any]]] = {}
        for c in ctx.heads("Conflict"):
            by_key.setdefault(c.get("conflict_key") or conflict_key(c["rule_id"], c["subject_refs"]), []).append(c)
        written: set[str] = set()
        for r in run.failed():
            if r.severity == "info":
                self._finding(run, r)
                continue
            if not r.datapoint_id or not r.subjects:
                self._finding(run, r)
                run.errors.append({"rule_id": r.rule_id, "error": "no datapoint/subjects; recorded as finding"})
                continue
            ck = r.conflict_key or conflict_key(r.rule_id, r.subjects)
            if ck in written:
                continue
            existing = by_key.get(ck, [])
            open_c = [c for c in existing if c.get("status") == "open"]
            supersedes = None
            if open_c:
                cur = open_c[0]
                if cur.get("severity") == SEVERITY_TO_GRAPH[r.severity]:
                    run.conflicts_existing.append(ref_of(cur))
                    continue
                # 严重度变化（如 CR-004 warn → block）：旧冲突 auto_cleared，新冲突 supersedes 它
                try:
                    old = self.store.write("Conflict", {"id": cur["id"], "status": "resolved", "resolution": {
                        "kind": "auto_cleared", "resolved_by": AUDITOR,
                        "note": f"severity changed to {SEVERITY_TO_GRAPH[r.severity]}; superseded by a new conflict"}},
                        expected_version=cur["version"], actor=actor, patch_mode="merge_from_head")
                    run.auto_cleared.append(old)
                    supersedes = old
                except GraphError as e:
                    run.errors.append({"conflict": ref_of(cur), "code": e.code, "message": str(e)})
                    continue
            terminal = sorted([c for c in existing if c.get("status") in TERMINAL], key=lambda c: c.get("created_at", ""))
            if terminal and supersedes is None:
                last = terminal[-1]
                if sorted(last.get("subject_refs") or []) == sorted(r.subjects):
                    run.conflicts_existing.append(ref_of(last))  # 处置结论仍覆盖当前版本
                    continue
                supersedes = ref_of(last)
            body = self._conflict_body(r, run)
            if supersedes:
                body["supersedes"] = supersedes
            exp, src = guarded_explanation(body, self.ruleset.rules[r.rule_id], self.explainer,
                                           {"title": self.ruleset.rules[r.rule_id].title})
            if exp:
                body["explanation"] = exp
                body["explanation_by"] = AUDITOR
            body["detail"]["explanation_source"] = src
            try:
                ref = self.store.write("Conflict", body, actor=actor,
                                       idempotency_key=f"audit:{run.run_id}:{ck[7:31]}")
                run.conflicts_written.append(ref)
                written.add(ck)
            except GraphError as e:
                run.errors.append({"rule_id": r.rule_id, "code": e.code, "message": str(e), "subjects": r.subjects})

    def _conflict_body(self, r: RuleResult, run: AuditRun) -> dict[str, Any]:
        rule = self.ruleset.rules[r.rule_id]
        detail = dict(r.detail)
        detail.update({"ruleset_version": self.ruleset.ref, "rule_version": rule.rule_version,
                       "evaluated_inputs": list(r.subjects), "trigger": run.trigger})
        if r.instance_id:
            detail["instance_id"] = r.instance_id
        body: dict[str, Any] = {"id": new_id("Conflict"), "rule_id": r.rule_id,
                                "severity": SEVERITY_TO_GRAPH[r.severity], "datapoint_id": r.datapoint_id,
                                "subject_refs": list(dict.fromkeys(r.subjects)), "detail": _clean(detail), "status": "open",
                                "trust_zone": "trusted"}
        if r.request_id and r.request_id.startswith("req_"):
            body["request_id"] = r.request_id
        return body

    def _auto_clear(self, ctx: AuditContext, run: AuditRun, only: Iterable[str] | None) -> None:
        evaluated = {r.rule_id for r in self.ruleset.for_trigger(run.trigger, only)}
        unknown_rules = {r.rule_id for r in run.results if r.outcome == "unknown"}
        failing = {r.conflict_key for r in run.results if r.outcome == "fail"}
        passing = {r.conflict_key for r in run.results if r.outcome == "pass"}
        actor = Actor.of(AUDITOR, run.org_id)
        for c in ctx.heads("Conflict", status="open"):
            if c["rule_id"] not in evaluated or c["rule_id"] in unknown_rules:
                continue
            if (c.get("conflict_key") or conflict_key(c["rule_id"], c["subject_refs"])) in failing:
                continue
            ck = c.get("conflict_key") or conflict_key(c["rule_id"], c["subject_refs"])
            transient_ok = c["rule_id"] in TRANSIENT_RULES and ck in passing
            if not transient_ok and not all(not ctx.is_head(s) or (ctx.head(s) or {}).get("status") == "superseded"
                                            for s in c["subject_refs"]):
                continue
            try:
                ref = self.store.write("Conflict", {
                    "id": c["id"], "status": "resolved",
                    "resolution": {"kind": "auto_cleared", "resolved_by": AUDITOR,
                                   "note": ("auto_cleared: re-check passed" if transient_ok else
                                            "auto_cleared: all subjects superseded and re-check passed (06 §3.1)")}},
                    expected_version=c["version"], actor=actor, patch_mode="merge_from_head")
                run.auto_cleared.append(ref)
            except GraphError as e:
                run.errors.append({"conflict": ref_of(c), "code": e.code, "message": str(e)})

    def _gate(self, run: AuditRun, org_id: str, request_id: str | None, ctx: AuditContext) -> str:
        if any(r.outcome == "unknown" for r in run.results) or any("code" in e for e in run.errors):
            return "unknown"
        if self.store is None or not request_id:
            return "block" if any(r.outcome == "fail" and r.severity == "block" for r in run.results) else "pass"
        fresh = AuditContext.from_store(self.store, org_id, meta=self.meta, config=self.config)
        involved: set[str] = set()
        for a in fresh.request_answers(request_id):
            involved.add(a["id"])
            involved |= {split_ref(x)[0] for x in fresh.cited_evidence(a)}
            if a.get("calculation_ref"):
                involved.add(split_ref(a["calculation_ref"])[0])
        for c in fresh.heads("Conflict", status="open"):
            if c.get("severity") != "blocking":
                continue
            if c.get("request_id") == request_id or {split_ref(s)[0] for s in c["subject_refs"]} & involved:
                return "block"
        return "pass"


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def _run_id() -> str:
    from ulid import ULID

    return f"arun_{ULID()}"


__all__ = ["ConsistencyEngine", "AuditRun", "RuleResult", "InMemoryObsSink", "ObsSink", "AUDITOR"]
