# Spec: 03 §3.7（drafting_note 中的数字 token 必须都能在所引对象中找到 → GRAPH_NUM_IN_TEXT）、SSOT-4、NFR-03、06 CR-037
"""文字说明中的数字检查。

token：不与字母/下划线相邻的数字串（``CO2``、``C7.3``、``tCO2e`` 中的数字不算）。以下是名称而非数值，不算 token：
``Scope 1/2/3``、``Scope-1-Emissionen``（连字符术语）、标准编号（``ISO 14001``、``IATF 16949:2016``、``EN 15804``）。
完整日期（``31.12.2025``、``2025-12-31``）整体作为一个日期 token，只与所引对象中的日期（值或期间端点）比较。
允许集合：所引对象中出现的全部数值（Evidence.extracted_value、Calculation 的 inputs/factors_used/result/breakdown、
Answer.value、Conflict.detail 值）、日期值与期间端点（整体），以及期间端点的**年份**分量（月/日分量不放行，否则
"Rückgang um 12 %" 会在期间止于 12-31 时漏检）。千分位（``2,027.2`` / ``2.027,2``）按多种读法尝试。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from ..canonical import normalize_dec

_TOKEN_RE = re.compile(r"(?<![A-Za-z_\d.,])-?\d+(?:[.,\u202f\u00a0]\d+)*(?![A-Za-z_\d])")
_TERMS_RE = re.compile(r"\b(scope|kategorie|category|scopes)\s*[123](?:\s*(?:/|und|and|,)\s*[123])*\b", re.IGNORECASE)
_SCOPE_HYPHEN_RE = re.compile(r"\b(scope|kategorie|category)-[123](?:/[123])*(?=\b|-)", re.IGNORECASE)
_STANDARD_RE = re.compile(r"\b(ISO/IEC|ISO/TS|ISO|IATF|EN|DIN|IEC|OHSAS|ASTM|VDA|SA)\s*\d+(?:[-:]\d+)*", re.IGNORECASE)
_DATE_DE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?![\d])")
_DATE_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d])")
_ISO_FULL = re.compile(r"\d{4}-\d{2}-\d{2}")
DATE_PREFIX = "date:"


def _readings(tok: str) -> set[str]:
    if tok.startswith(DATE_PREFIX):
        return {tok}
    t = tok.replace("\u202f", "").replace("\u00a0", "")
    outs = set()
    cands = [
        t.replace(" ", "").replace(",", ""),  # 1,234.5
        t.replace(" ", "").replace(".", "").replace(",", "."),  # 1.234,5
        t.replace(" ", "").replace(",", "."),  # 0,56
        t.replace(" ", ""),
    ]
    for c in cands:
        if re.fullmatch(r"-?\d+(\.\d+)?", c):
            outs.add(normalize_dec(c))
            outs.add(normalize_dec(c.lstrip("-")))
    return outs


def _iso_date(m: re.Match[str], kind: str) -> str | None:
    try:
        if kind == "de":
            y, mo, d = int(m.group(3)), int(m.group(2)), int(m.group(1))
        else:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    except ValueError:  # pragma: no cover
        return None
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _split(text: str) -> tuple[str, list[str]]:
    """去掉术语/标准编号，把完整日期替换为日期 token；返回 (剩余文本, 日期 token 列表)。"""
    text = _TERMS_RE.sub(" ", text or "")
    text = _SCOPE_HYPHEN_RE.sub(" ", text)
    text = _STANDARD_RE.sub(" ", text)
    dates: list[str] = []

    def sub(kind: str):
        def f(m: re.Match[str]) -> str:
            iso = _iso_date(m, kind)
            if iso is None:
                return m.group(0)
            dates.append(DATE_PREFIX + iso)
            return " "
        return f

    text = _DATE_DE.sub(sub("de"), text)
    text = _DATE_ISO.sub(sub("iso"), text)
    return text, dates


def numeric_tokens(text: str) -> list[str]:
    """数字 token（日期以外）。完整日期见 ``date_tokens``。"""
    rest, _ = _split(text)
    return [m.group(0).strip() for m in _TOKEN_RE.finditer(rest)]


def date_tokens(text: str) -> list[str]:
    return _split(text)[1]


def all_tokens(text: str) -> list[str]:
    rest, dates = _split(text)
    return dates + [m.group(0).strip() for m in _TOKEN_RE.finditer(rest)]


def add_date(out: set[str], v: str) -> None:
    """日期值/期间端点：整体日期 + 年份分量。"""
    if _ISO_FULL.fullmatch(v[:10] or ""):
        out.add(DATE_PREFIX + v[:10])
        out.add(normalize_dec(str(int(v[:4]))))


def add_text_tokens(out: set[str], text: str) -> None:
    """来源对象中的文字（如复用的历史答案、文字型证据）：其中出现的数字/日期 token 视为有来源。"""
    for tok in all_tokens(text):
        if tok.startswith(DATE_PREFIX):
            add_date(out, tok[len(DATE_PREFIX):])
        else:
            out.update(_readings(tok))


def _collect(o: Any, out: set[str]) -> None:
    if isinstance(o, dict):
        for k, v in o.items():
            if k in ("value", "extracted_value", "delta", "delta_ratio", "threshold", "corrected_value"):
                if isinstance(v, str) and _ISO_FULL.fullmatch(v):
                    add_date(out, v)
                elif isinstance(v, (str, int)) and not isinstance(v, bool):
                    n = normalize_dec(v)
                    if isinstance(n, str):
                        out.add(n)
                        out.add(n.lstrip("-"))
            if k in ("start", "end", "valid_from", "valid_until") and isinstance(v, str):
                add_date(out, v)
            _collect(v, out)
    elif isinstance(o, list):
        for v in o:
            _collect(v, out)


def allowed_numbers(objects: Iterable[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for o in objects:
        if o:
            _collect(o, out)
    return out


def unsourced_numbers(texts: Iterable[str], allowed: set[str]) -> list[str]:
    bad = []
    for text in texts:
        for tok in all_tokens(text):
            if not (_readings(tok) & allowed):
                bad.append(tok[len(DATE_PREFIX):] if tok.startswith(DATE_PREFIX) else tok)
    return bad
