# Spec: 06 §1–§3、§8（consistency-auditor：规则 DSL、CR-001…CR-043、触发、执行流程、冲突生命周期）；04 §2.6
"""一致性审计（consistency-auditor）。

    from belegt_core.consistency import ConsistencyEngine, resolve_conflict
    run = ConsistencyEngine(store).gate(org_id, "req_…")   # run.gate: pass | block | unknown（fail-closed）
"""
from .cel import CelError, compile_expr, dec_eq
from .context import AuditConfig, AuditContext
from .engine import AUDITOR, AuditRun, ConsistencyEngine, InMemoryObsSink, ObsSink, RuleResult
from .explain import Explainer, TemplateExplainer, guarded_explanation
from .lifecycle import REASON_CODES, resolve_conflict, validate_divergence
from .meta import DPMeta, MetaProvider
from .plugins import PLUGINS, Binding, Scope
from .ruleset import RULESET_ID, RULESET_VERSION, RuleSet, RuleSpec, default_ruleset, load_ruleset

__all__ = [
    "ConsistencyEngine", "AuditRun", "RuleResult", "InMemoryObsSink", "ObsSink", "AUDITOR", "AuditContext", "AuditConfig",
    "Explainer", "TemplateExplainer", "guarded_explanation", "resolve_conflict", "validate_divergence", "REASON_CODES",
    "MetaProvider", "DPMeta", "PLUGINS", "Binding", "Scope", "RuleSet", "RuleSpec", "load_ruleset", "default_ruleset",
    "RULESET_ID", "RULESET_VERSION", "CelError", "compile_expr", "dec_eq",
]
