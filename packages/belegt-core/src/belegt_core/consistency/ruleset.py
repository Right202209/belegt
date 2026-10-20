# Spec: 06 §1.2（规则 YAML 元数据：rule_id、rule_version、category、severity、scope、triggers、inputs、when、assert、
#       tolerance、default_action、explain_template、tests；规则集版本 consistency-rules@<semver>）、§1.6（规则治理）
"""规则集加载。规则文件位于本包 ``consistency/rules/cr-xxx.yaml``（06 §1.2 中的 ``belegt-core/rules/consistency/``）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .cel import CelError, compile_expr

RULESET_ID = "consistency-rules"
RULESET_VERSION = "1.0.0"
RULES_DIR = Path(__file__).parent / "rules"
TRIGGERS = ("answer_write", "pre_submit_gate", "daily_batch", "context_change")
SEVERITIES = ("info", "warn", "block")
SEVERITY_TO_GRAPH = {"block": "blocking", "warn": "warning"}  # info 不生成 Conflict（06 §1.4）


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    rule_version: str
    category: str
    severity: str
    title: dict[str, str]
    scope: str
    triggers: tuple[str, ...]
    plugin: str
    assert_: str
    when: str | None = None
    severity_expr: str | None = None
    tolerance: dict[str, Any] = field(default_factory=dict)
    default_action: str = "open_conflict"
    explain_template: str | None = None
    tests: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuleSpec":
        sev = d["severity"]
        if sev not in SEVERITIES:
            raise ValueError(f"{d['rule_id']}: severity must be one of {SEVERITIES}")
        trig = tuple(d.get("triggers") or TRIGGERS)
        bad = set(trig) - set(TRIGGERS)
        if bad:
            raise ValueError(f"{d['rule_id']}: unknown triggers {sorted(bad)}")
        spec = cls(rule_id=d["rule_id"], rule_version=str(d.get("rule_version", "1.0.0")), category=d["category"],
                   severity=sev, title=dict(d.get("title") or {}), scope=d.get("scope", "answer"), triggers=trig,
                   plugin=d.get("plugin") or d["rule_id"].lower().replace("-", "_"), assert_=d["assert"],
                   when=d.get("when"), severity_expr=d.get("severity_expr"), tolerance=dict(d.get("tolerance") or {}),
                   default_action=d.get("default_action", "open_conflict"), explain_template=d.get("explain_template"),
                   tests=d.get("tests"))
        spec.compile()
        return spec

    def compile(self) -> None:
        """编译全部 CEL（禁止 double 字面量），规则加载即失败而不是运行时失败。"""
        for src in (self.assert_, self.when, self.severity_expr):
            if src:
                try:
                    compile_expr(src)
                except CelError as e:
                    raise ValueError(f"{self.rule_id}: {e}") from e


@dataclass
class RuleSet:
    rules: dict[str, RuleSpec]
    version: str = RULESET_VERSION

    @property
    def ref(self) -> str:
        return f"{RULESET_ID}@{self.version}"

    def for_trigger(self, trigger: str, only: Iterable[str] | None = None) -> list[RuleSpec]:
        sel = set(only) if only else None
        return [r for k, r in sorted(self.rules.items()) if trigger in r.triggers and (sel is None or k in sel)]

    def __len__(self) -> int:
        return len(self.rules)


def load_ruleset(path: str | Path | None = None, version: str = RULESET_VERSION) -> RuleSet:
    d = Path(path) if path else RULES_DIR
    rules: dict[str, RuleSpec] = {}
    for f in sorted(d.glob("cr-*.yaml")):
        spec = RuleSpec.from_dict(yaml.safe_load(f.read_text("utf-8")))
        if spec.rule_id in rules:
            raise ValueError(f"duplicate rule {spec.rule_id}")
        rules[spec.rule_id] = spec
    return RuleSet(rules, version)


_DEFAULT: RuleSet | None = None


def default_ruleset() -> RuleSet:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = load_ruleset()
    return _DEFAULT


__all__ = ["RuleSpec", "RuleSet", "load_ruleset", "default_ruleset", "RULESET_ID", "RULESET_VERSION", "TRIGGERS",
           "SEVERITY_TO_GRAPH"]
