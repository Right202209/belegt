# Spec: 11 §3.1 M-DRF-03 ★ / M-AUD-03 ★（文本中新增数字 = 0）、§3.2“数字不由模型生成”；03 §3.7 GRAPH_NUM_IN_TEXT、
#       06 CR-037（文字答案中的数字 token 必须能在所引对象中找到）
"""R-NUM 违规检测：文本中不在所引对象值集合中的数字 token。

数字切分优先复用 `belegt_core.rules.numtext.numeric_tokens`（与写入校验 GRAPH_NUM_IN_TEXT 同一 tokenizer），
在其基础上做三处“以真值为准”的修正（评测要数出真实的新增数字，而不是复刻生产规则的误差）：
1. 标准编号（`ISO 14001`、`IATF 16949:2016`、`EN 15804`）与连字符术语（`Scope-1-Emissionen`）是名称不是数值 → 不计；
2. 完整日期（`31.12.2025`、`2028-03-31`）整体比较：与所引对象的日期值/期间端点相同才允许；
3. 所引对象期间只放行**年份**分量（生产规则还放行月/日分量，会让“下降 12 %”在期间为 …-12-31 时漏检）。
差异 1–3 已作为发现回报 belegt-core。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

_TOKEN_RE = re.compile(r"(?<![A-Za-z_\d.,])-?\d+(?:[.,\u202f\u00a0]\d+)*(?![A-Za-z_\d])")
_TERMS_RE = re.compile(r"\b(scope|kategorie|category|scopes)\s*[123](?:\s*(?:/|und|and|,)\s*[123])*\b", re.I)
_STANDARD_RE = re.compile(r"\b(ISO/IEC|ISO/TS|ISO|IATF|EN|DIN|IEC|OHSAS|ASTM)\s*\d+(?:[-:]\d+)*", re.I)
_SCOPE_HYPHEN_RE = re.compile(r"\b(scope|kategorie|category)-[123]\b", re.I)  # “Scope-1-Emissionen”
_DATE_DE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _norm(s: str) -> str | None:
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    return format(d.normalize(), "f")


def _readings(tok: str) -> set[str]:
    t = tok.replace("\u202f", "").replace("\u00a0", "").replace(" ", "")
    out: set[str] = set()
    for c in (t.replace(",", ""), t.replace(".", "").replace(",", "."), t.replace(",", "."), t):
        if re.fullmatch(r"-?\d+(\.\d+)?", c):
            n = _norm(c)
            if n:
                out.add(n)
                out.add(n.lstrip("-"))
    return out


def _tokens(text: str) -> list[str]:
    try:
        from belegt_core.rules.numtext import numeric_tokens  # 生产 tokenizer
        return numeric_tokens(text)
    except Exception:  # pragma: no cover - core 不可用
        return [m.group(0).strip() for m in _TOKEN_RE.finditer(_TERMS_RE.sub(" ", text))]


def _iso(m: re.Match[str], kind: str) -> str:
    if kind == "de":
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def allowed_from_cited(cited: Iterable[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """返回 (允许的数值读法, 允许的 ISO 日期)。"""
    nums: set[str] = set()
    dates: set[str] = set()
    for c in cited:
        v = str(c.get("value", ""))
        if _DATE_ISO.fullmatch(v):
            dates.add(v)
            nums.add(str(int(v[:4])))
        else:
            n = _norm(v)
            if n:
                nums.add(n)
                nums.add(n.lstrip("-"))
        per = c.get("period") or {}
        for k in ("start", "end"):
            d = str(per.get(k) or "")
            if _DATE_ISO.fullmatch(d):
                dates.add(d)
                nums.add(str(int(d[:4])))  # 只放行年份分量
    return nums, dates


def find_new_numbers(text: str, cited: Iterable[dict[str, Any]]) -> list[str]:
    """返回 text 中无法在 cited 值中找到的数字 token（原文形式；日期在前，其余按出现顺序）。"""
    nums, dates = allowed_from_cited(list(cited))
    bad: list[str] = []
    t = _SCOPE_HYPHEN_RE.sub(" ", _STANDARD_RE.sub(" ", text))

    def date_sub(kind: str) -> Any:
        def f(m: re.Match[str]) -> str:
            try:
                iso = _iso(m, kind)
            except ValueError:  # pragma: no cover
                return m.group(0)
            if iso not in dates:
                bad.append(m.group(0))
            return " "
        return f

    t = _DATE_DE.sub(date_sub("de"), t)
    t = _DATE_ISO.sub(date_sub("iso"), t)
    bad += [tok for tok in _tokens(t) if not (_readings(tok) & nums)]
    return bad
