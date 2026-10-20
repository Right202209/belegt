# Spec: 11 §2.2(3)（bench-synth：确定性生成“值已知”的问卷与文档；LLM 只做文字改写，数字由生成器写入——本实现
#       完全不用 LLM，改写来自 synth_lexicon 的人写模板）、§2.4 LBL-MAP、§2.7 分层（模板 × 语言 × 格式）
"""合成问卷生成器：基于 `belegt_dictionary` 的 DataPoint 生成多语言题目变体。

- `question_variants(dp, …)`：一个 DataPoint 的全部候选问法（改写词库 + 字典标签模板 + 噪声变换），确定性。
- `synthesize_questionnaire(...)`：按模板分层选题（公开模板走 `TemplateMapping` 条目，题号用条目 `ref`；
  `private_*` 走 `oem_custom`/通用标签），每题一个 `LBL-MAP` 金标准。
- `render_csv(...)`：把合成问卷渲染成 belegt-io 可解析的 CSV（`Nr.;Frage;Antwort;Einheit`），供端到端运行。
"""
from __future__ import annotations

import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from .synth_lexicon import PARAPHRASES

UNIT_DISPLAY = {"tCO2e": {"de": "t CO2e", "en": "t CO2e"}, "m3": {"de": "m³", "en": "m³"},
                "1": {"de": "Anzahl", "en": "count"}, "%": {"de": "%", "en": "%"}}
#: 题目要求的“非字典单位”（同量纲，考 unit_hint 与换算），按 DataPoint 单位
UNIT_ALTERNATIVES = {"MWh": ["kWh", "GJ"], "tCO2e": ["kgCO2e"], "m3": ["L"], "t": ["kg"], "EUR": ["EUR"]}

LABEL_TEMPLATES: dict[str, list[str]] = {
    "de": ["{label} {y}", "{label} ({u})", "Bitte geben Sie an: {label}", "{label} – Berichtsjahr {y}",
           "Angabe: {label} ({u}), Geschäftsjahr {y}"],
    "en": ["{label} for {y}", "{label} ({u})", "Please report: {label}", "What is your {label_l}?",
           "{label}, reporting year {y} ({u})"],
    "zh": ["{label}（{y}年）", "请填写：{label}", "{label}（单位：{u}）"],
}
LABEL_TEMPLATES_NOPERIOD: dict[str, list[str]] = {
    "de": ["{label}", "Bitte geben Sie an: {label}", "{label}?"],
    "en": ["{label}", "Please state: {label}", "{label}?"],
    "zh": ["{label}", "请填写：{label}"],
}


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def shingles(text: str, k: int = 5) -> set[str]:
    t = re.sub(r"\s+", " ", _strip_accents(text.lower())).strip()
    return {t[i:i + k] for i in range(max(1, len(t) - k + 1))}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


@dataclass
class Variant:
    text: str
    lang: str
    source: str  # paraphrase | label | noise:<kind>
    unit_requested: str | None = None
    difficulty: str = "medium"


def _fmt(tpl: str, label: str, y: int, unit: str | None, lang: str) -> str:
    u = UNIT_DISPLAY.get(unit or "", {}).get(lang, unit or "")
    label_l = label[:1].lower() + label[1:] if lang == "en" else label
    out = tpl.format(label=label, label_l=label_l, y=y, u=u)
    return re.sub(r"\s*\(\s*\)", "", out).strip()


def typo(text: str, rng: random.Random) -> str:
    """交换最长单词中两个相邻内部字母（确定性，由 rng 决定位置）。"""
    words = re.findall(r"[A-Za-zÄÖÜäöüß]{6,}", text)
    if not words:
        return text
    w = max(words, key=lambda x: (len(x), x))
    i = rng.randrange(1, len(w) - 2)
    w2 = w[:i] + w[i + 1] + w[i] + w[i + 2:]
    return text.replace(w, w2, 1)


def question_variants(dp: Any, *, year: int = 2025, langs: Iterable[str] = ("de", "en"),
                      unit: str | None = None) -> list[Variant]:
    """一个 DataPoint 的全部问法（不含噪声）。`dp` 为 belegt_dictionary.DataPoint。"""
    unit = unit or dp.unit
    out: list[Variant] = []
    has_period = dp.period_type != "point_in_time" and dp.value_type in ("number", "file")
    for lang in langs:
        for tpl in PARAPHRASES.get(dp.id, {}).get(lang, []):
            out.append(Variant(_fmt(tpl, "", year, unit, lang), lang, "paraphrase", unit, "medium"))
        label = getattr(dp.label, lang, None)
        if not label:
            continue
        tpls = LABEL_TEMPLATES if has_period else LABEL_TEMPLATES_NOPERIOD
        for tpl in tpls.get(lang, []):
            if "{u}" in tpl and not unit:
                continue
            out.append(Variant(_fmt(tpl, label, year, unit, lang), lang, "label", unit, "easy"))
    return out


@dataclass
class QItem:
    question_id: str
    native_id: str | None
    text: str
    lang: str
    acceptable: list[str]
    answer_format: str | None
    unit_hint: str | None
    period_hint: dict[str, str] | None
    boundary_hint: str | None
    difficulty: str
    source: str
    template_mapping_ref: str | None = None
    flags: list[str] = field(default_factory=list)


def _boundary(dp: Any) -> str | None:
    b = list(dp.boundary or [])
    if "organization" in b:
        return "organization"
    if "product_per_unit" in b:
        return "product_per_unit"
    return None


def _dp_pool(dictionary: Any, template: str) -> list[tuple[Any, str | None, str | None]]:
    """(DataPoint, native_id, template_mapping_ref)。公开模板取 TemplateMapping 条目；private_* 取 oem_custom/vsme 标签。"""
    pool: list[tuple[Any, str | None, str | None]] = []
    if not template.startswith("private_"):
        for tm_id in sorted(dictionary.template_mappings):
            tm = dictionary.template_mappings[tm_id]
            if tm.template_id != template:
                continue
            for e in tm.entries:
                if e.datapoint_id and e.datapoint_id in dictionary.datapoints:
                    pool.append((dictionary.datapoints[e.datapoint_id], e.locator.ref,
                                 f"{tm_id}#{e.entry_id}"))
            break  # 同一 template_id 多版本时只取第一个（排序后最早）
    else:
        for dp_id in sorted(dictionary.datapoints):
            dp = dictionary.datapoints[dp_id]
            if {"oem_custom", "vsme", "cdp_climate"} & set(dp.tags or []):
                pool.append((dp, None, None))
    # 去重（同一 DataPoint 多个条目时保留第一个）
    seen: set[str] = set()
    out = []
    for item in pool:
        if item[0].id not in seen:
            seen.add(item[0].id)
            out.append(item)
    return out


def synthesize_questionnaire(dictionary: Any, *, qn_id: str, template: str, lang: str, n: int, rng: random.Random,
                             year: int = 2025, avoid: list[set[str]] | None = None, noise_rate: float = 0.15,
                             easy_only: bool = False) -> list[QItem]:
    """生成一份合成问卷的题目与金标准。`avoid`：需避开的文本 shingle 集（防 fewshot 泄漏，Jaccard ≥ 0.7 视为近重复）。"""
    pool = _dp_pool(dictionary, template)
    if not pool:
        raise ValueError(f"no datapoints for template {template!r}")
    picked = rng.sample(pool, min(n, len(pool)))
    picked.sort(key=lambda t: (t[1] or "", t[0].id))
    items: list[QItem] = []
    for i, (dp, native, tm_ref) in enumerate(picked, start=1):
        unit = dp.unit
        if dp.value_type == "number" and unit in UNIT_ALTERNATIVES and rng.random() < 0.25 and not easy_only:
            unit = rng.choice(UNIT_ALTERNATIVES[unit])
        variants = question_variants(dp, year=year, langs=[lang], unit=unit)
        if easy_only:
            variants = [v for v in variants if v.source == "label"] or variants
        else:
            para = [v for v in variants if v.source == "paraphrase"]
            if para and rng.random() < 0.7:
                variants = para
        rng.shuffle(variants)
        chosen = None
        for v in variants:
            sh = shingles(v.text)
            if avoid and any(jaccard(sh, a) >= 0.7 for a in avoid):
                continue
            chosen = v
            break
        if chosen is None:
            continue  # 所有问法都与 fewshot 近重复 → 跳过该题（防泄漏优先于数量）
        text, source, diff = chosen.text, chosen.source, chosen.difficulty
        if not easy_only and rng.random() < noise_rate:
            kind = rng.choice(["typo", "prefix", "lower"])
            if kind == "typo":
                text = typo(text, rng)
                diff = "hard"
            elif kind == "prefix":
                text = f"{native or f'{i}.'} {text}" if native else f"{i}. {text}"
            else:
                text = text.lower()
            source = f"{source}+noise:{kind}"
        ph = {"start": f"{year}-01-01", "end": f"{year}-12-31"} if dp.period_type in (
            "reporting_period", "calendar_year", "fiscal_year") else None
        items.append(QItem(
            question_id=f"{qn_id}-q{i:02d}", native_id=native or f"{i}", text=text, lang=lang,
            acceptable=[dp.id], answer_format=dp.value_type, unit_hint=unit if dp.value_type == "number" else None,
            period_hint=ph, boundary_hint=_boundary(dp), difficulty=diff, source=source,
            template_mapping_ref=tm_ref))
    return items


def render_csv(items: list[QItem]) -> bytes:
    """渲染为 belegt-io 可解析的问卷 CSV（`;` 分隔，表头德/英通用）。"""
    lines = ["Nr.;Frage;Antwort;Einheit"]
    for it in items:
        unit = it.unit_hint or ""
        text = it.text.replace(";", ",").replace("\n", " ")
        lines.append(f"{it.native_id or ''};{text};;{unit}")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")
