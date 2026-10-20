# Spec: 06 §6.1（条款类型 LC-01…LC-08 与模式库 legal-patterns@<semver>）、§6.2（双通道识别：规则通道 + LLM 通道，
#       OR 融合、LLM 阈值 p ≥ 0.30）、§6.3（提示文案 de/en/zh）
"""法律承诺条款识别（无副作用函数，question-parser 在 quarantine 调用）。

- 规则通道：``patterns.yaml`` 中的正则（德/英），任一命中即标记。
- LLM 通道：可选 ``LegalClassifier`` 协议（``classify(text, lang) -> {LC-xx: p}``，可附 ``spans``），不依赖 llm 包；
  调用方可用 ``belegt_core.llm`` 的客户端包装出该协议。分类器异常 → 只用规则通道（不影响确定性服务）。
- 融合：任一通道命中即 ``flagged=True``（召回优先）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml

LEGAL_LLM_THRESHOLD = 0.30
LC_TYPES = {
    "LC-01": {"de": "Zusicherung/Gewährleistung", "en": "warranty/guarantee", "zh": "保证/担保"},
    "LC-02": {"de": "Freistellung", "en": "indemnity", "zh": "赔偿"},
    "LC-03": {"de": "Auditrecht/Vor-Ort-Prüfung", "en": "audit right/on-site inspection", "zh": "审计权/现场检查"},
    "LC-04": {"de": "Anerkennung des Verhaltenskodex", "en": "code of conduct acceptance", "zh": "行为准则签署/接受"},
    "LC-05": {"de": "Zielverpflichtung", "en": "target commitment", "zh": "目标承诺"},
    "LC-06": {"de": "Bestätigung der Richtigkeit", "en": "accuracy confirmation", "zh": "数据真实完整确认"},
    "LC-07": {"de": "Vertragsstrafe/Haftung", "en": "penalty/liability", "zh": "违约责任/处罚"},
    "LC-08": {"de": "Mitteilungspflicht", "en": "notification duty", "zh": "通知/持续义务"},
}
PATTERNS_FILE = Path(__file__).parent / "patterns.yaml"


@dataclass(frozen=True)
class Pattern:
    id: str
    type: str
    lang: str
    regex: re.Pattern[str]
    examples: tuple[str, ...] = ()
    counterexamples: tuple[str, ...] = ()


@dataclass(frozen=True)
class PatternHit:
    type: str
    pattern_id: str
    lang: str
    start: int
    end: int
    excerpt: str


@dataclass
class LegalDetection:
    flagged: bool
    types: list[str]
    rule_hits: list[PatternHit] = field(default_factory=list)
    llm_probs: dict[str, float] = field(default_factory=dict)
    llm_spans: list[dict[str, Any]] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)  # rule | llm
    llm_error: str | None = None
    patterns_version: str = "legal-patterns@1.0.0"

    @property
    def tags(self) -> list[str]:
        from .flags import legal_tags

        return legal_tags(self.types) if self.flagged else []

    def to_dict(self) -> dict[str, Any]:
        return {"flagged": self.flagged, "types": self.types, "channels": self.channels, "llm_probs": self.llm_probs,
                "rule_hits": [h.__dict__ for h in self.rule_hits], "llm_spans": self.llm_spans,
                "llm_error": self.llm_error, "patterns_version": self.patterns_version}


class LegalClassifier(Protocol):
    def classify(self, text: str, lang: str | None = None) -> Mapping[str, Any]: ...


class ScriptedClassifier:
    """确定性分类器（测试/离线）：按子串或固定概率返回 ``{LC-xx: p}``。"""

    def __init__(self, fixed: Mapping[str, float] | None = None, by_substring: Mapping[str, Mapping[str, float]] | None = None,
                 fail: bool = False) -> None:
        self.fixed = dict(fixed or {})
        self.by_substring = {k: dict(v) for k, v in (by_substring or {}).items()}
        self.fail = fail

    def classify(self, text: str, lang: str | None = None) -> dict[str, float]:
        if self.fail:
            raise RuntimeError("classifier unavailable")
        out = dict(self.fixed)
        for sub, probs in self.by_substring.items():
            if sub.lower() in text.lower():
                for k, p in probs.items():
                    out[k] = max(out.get(k, 0.0), p)
        return out


@lru_cache(maxsize=4)
def load_patterns(path: str | None = None) -> tuple[str, tuple[Pattern, ...]]:
    data = yaml.safe_load(Path(path or PATTERNS_FILE).read_text("utf-8"))
    pats = []
    for p in data["patterns"]:
        if p["type"] not in LC_TYPES:
            raise ValueError(f"unknown legal type {p['type']}")
        pats.append(Pattern(p["id"], p["type"], p["lang"], re.compile(p["regex"], re.IGNORECASE | re.UNICODE),
                            tuple(p.get("examples") or ()), tuple(p.get("counterexamples") or ())))
    return f"legal-patterns@{data['version']}", tuple(pats)


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace("\u00ad", "").replace("\u00a0", " ")


def detect_legal_clauses(text: str | Mapping[str, str], lang: str | None = None, *,
                         classifier: LegalClassifier | None = None, threshold: float = LEGAL_LLM_THRESHOLD,
                         patterns_path: str | None = None) -> LegalDetection:
    """``text`` 可为字符串或 I18nText（逐语言匹配）。"""
    texts = list(text.values()) if isinstance(text, Mapping) else [text]
    version, pats = load_patterns(patterns_path)
    hits: list[PatternHit] = []
    for t in texts:
        t = _norm(t or "")
        for p in pats:
            for m in p.regex.finditer(t):
                hits.append(PatternHit(p.type, p.id, p.lang, m.start(), m.end(), t[max(0, m.start() - 20):m.end() + 20]))
                break
    types = {h.type for h in hits}
    channels = ["rule"] if hits else []
    probs: dict[str, float] = {}
    spans: list[dict[str, Any]] = []
    err = None
    if classifier is not None:
        try:
            raw = dict(classifier.classify(" \n".join(texts), lang))
            spans = list(raw.pop("spans", []) or [])
            probs = {k: float(v) for k, v in raw.items() if k in LC_TYPES}
            llm_types = {k for k, p in probs.items() if p >= threshold}
            if llm_types:
                channels.append("llm")
                types |= llm_types
        except Exception as e:  # LLM 不可用：规则通道照常
            err = f"{type(e).__name__}: {e}"
    return LegalDetection(flagged=bool(types), types=sorted(types), rule_hits=hits, llm_probs=probs, llm_spans=spans,
                          channels=channels, llm_error=err, patterns_version=version)


def notice_text(types: list[str]) -> dict[str, str]:
    """06 §6.3 提示文案（取首个类型名）。"""
    t = LC_TYPES.get(types[0] if types else "", {"de": "rechtlich bindend", "en": "legally binding", "zh": "法律约束"})
    return {
        "de": f"„Diese Frage enthält eine rechtlich bindende Erklärung (Typ: {t['de']}). Bitte lesen Sie den Originaltext "
              f"und bestätigen Sie die Freigabe einzeln.\u201c",
        "en": f"This question contains a legally binding statement (type: {t['en']}). Please read the original text and "
              f"approve it individually.",
        "zh": f"此题包含具有法律约束力的声明（类型：{t['zh']}）。请阅读原文并单条确认。",
    }


__all__ = ["detect_legal_clauses", "LegalDetection", "PatternHit", "Pattern", "LegalClassifier", "ScriptedClassifier",
           "load_patterns", "notice_text", "LC_TYPES", "LEGAL_LLM_THRESHOLD"]
