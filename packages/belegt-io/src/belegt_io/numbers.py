# Spec: 02 §3.1（单位代码）、§3.6（采集时的数值与单位清洗）；03 §2.2（DEC-STR）
"""带单位数字的确定性抽取与清洗。

规则（02 §3.6）：
- 千分位/小数点按文档语言判定，再用同文档其它数字交叉验证；两种解读相差 1000 倍且无法交叉验证时
  `needs_confirmation=True`（生成 ClosedQuestion，不猜测）。
- "ton" 视为歧义单位；"tCO2" 与 "tCO2e" 不等价；kWh/KWh/kwh 归一为 kWh。
- 百分数：单位为 % 且数值 ≤ 1 视为歧义（除非模板/单元格格式约定）。
- 区间与上下限（"< 0,1 %"、"0,3–1,0 %"）不取中值：`value=None`、`is_range=True`、`is_estimate=True`。
- 空值语义：区分 0 / 不适用 / 暂无数据 / 保密（`classify_null`）。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal

from .models import Quantity

NumberStyle = Literal["de", "en", "plain", "ch", "space"]

# —— DEC-STR ——


def to_dec_str(v: Decimal | int | str) -> str:
    """规范化十进制字符串：无科学计数法、去尾零、`-0`→`0`（05 §1.2）。"""
    d = v if isinstance(v, Decimal) else Decimal(str(v))
    if not d.is_finite():
        raise ValueError("NaN/Infinity not allowed in DEC-STR")
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", "", "-"):
        s = "0"
    return s


def to_decimal(v: object) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):
        raise TypeError("bool is not a number")
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(repr(v))
    if isinstance(v, str):
        return Decimal(v.strip())
    raise TypeError(f"cannot convert {type(v).__name__} to Decimal")


# —— 数字解析 ——

_GROUP_SEPS = "'’\u00a0\u202f\u2009"
_NUM_TOKEN = re.compile(
    r"(?<![\w.,])"
    r"(?P<sign>[-+−])?"
    r"(?P<num>\d{1,3}(?:[" + _GROUP_SEPS + r" ]\d{3})+(?:[.,]\d+)?"  # 1 234,5 / 1'234.5
    r"|\d+(?:[.,]\d+)+"  # 1.234,5 / 1,234.5 / 12,5
    r"|\d+)"
    r"(?![\d])"
)


class ParsedNumber:
    __slots__ = ("value", "style", "ambiguous", "needs_confirmation", "alternatives", "reasons")

    def __init__(self, value: Decimal, style: NumberStyle, ambiguous: bool = False,
                 needs_confirmation: bool = False, alternatives: list[Decimal] | None = None,
                 reasons: list[str] | None = None):
        self.value = value
        self.style = style
        self.ambiguous = ambiguous
        self.needs_confirmation = needs_confirmation
        self.alternatives = alternatives or []
        self.reasons = reasons or []

    def __repr__(self) -> str:  # pragma: no cover
        return f"ParsedNumber({self.value}, {self.style}, ambiguous={self.ambiguous})"


def _groups_ok(parts: list[str]) -> bool:
    return 1 <= len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:])


def _lang_style(lang: str | None) -> NumberStyle | None:
    if not lang:
        return None
    lang = lang.lower()[:2]
    if lang in ("de", "fr", "it", "es", "pl", "cs", "nl", "pt", "da", "sv", "fi", "nb", "hu", "ro", "sk", "sl", "hr"):
        return "de"  # 逗号小数
    if lang in ("en", "zh", "ja", "ko"):
        return "en"
    return None


def parse_number(token: str, lang: str | None = None, doc_style: NumberStyle | None = None) -> ParsedNumber:
    """把一个数字串解析为 Decimal。`lang` 为文档语言，`doc_style` 为同文档交叉验证得到的格式。"""
    raw = token.strip()
    neg = raw.startswith(("-", "−"))
    s = raw.lstrip("+-−").strip()
    if not s or not re.fullmatch(r"[\d.,'’\u00a0\u202f\u2009 ]+", s):
        raise ValueError(f"not a number: {token!r}")
    style: NumberStyle = "plain"

    def mk(d: str) -> Decimal:
        val = Decimal(d)
        return -val if neg else val

    # 撇号 / 空格分组（瑞士、法式）
    if any(c in s for c in _GROUP_SEPS + " "):
        style = "ch" if any(c in s for c in "'’") else "space"
        parts = re.split(r"[" + _GROUP_SEPS + r" ]", s)
        tail = parts[-1]
        dec = ""
        m = re.fullmatch(r"(\d{3})([.,]\d+)?", tail)
        if not m or not _groups_ok(parts[:-1] + [m.group(1)]):
            raise ValueError(f"bad digit grouping: {token!r}")
        if m.group(2):
            dec = "." + m.group(2)[1:]
        return ParsedNumber(mk("".join(parts[:-1]) + m.group(1) + dec), style)

    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        last = max(s.rfind("."), s.rfind(","))
        dec_sep = s[last]
        grp_sep = "," if dec_sep == "." else "."
        int_part, frac = s[:last], s[last + 1:]
        if dec_sep in int_part or not _groups_ok(int_part.split(grp_sep)):
            raise ValueError(f"inconsistent separators: {token!r}")
        style = "de" if dec_sep == "," else "en"
        return ParsedNumber(mk(int_part.replace(grp_sep, "") + "." + frac), style)

    if not has_dot and not has_comma:
        return ParsedNumber(mk(s), "plain")

    sep = "." if has_dot else ","
    parts = s.split(sep)
    if len(parts) > 2:  # 只能是千分位
        if not _groups_ok(parts):
            raise ValueError(f"bad digit grouping: {token!r}")
        style = "de" if sep == "." else "en"
        return ParsedNumber(mk("".join(parts)), style)

    int_part, frac = parts
    thousand_like = len(frac) == 3 and 1 <= len(int_part) <= 3 and not int_part.startswith("0")
    as_decimal = mk(int_part + "." + frac)
    if not thousand_like:
        # 唯一合理解读：sep 是小数点
        style = "de" if sep == "," else "en"
        return ParsedNumber(as_decimal, style)

    as_thousands = mk(int_part + frac)
    # sep 为 "," 时：de=小数，en=千分位；sep 为 "." 时：de=千分位，en=小数
    de_value = as_decimal if sep == "," else as_thousands
    en_value = as_thousands if sep == "," else as_decimal
    lang_style = _lang_style(lang)
    reasons = ["thousand_separator_ambiguous"]
    if doc_style in ("de", "en"):
        value = de_value if doc_style == "de" else en_value
        alt = en_value if doc_style == "de" else de_value
        if lang_style and lang_style != doc_style:
            return ParsedNumber(value, doc_style, True, True, [alt], reasons + ["language_vs_document_style_conflict"])
        return ParsedNumber(value, doc_style, True, False, [alt], reasons + ["resolved_by_document_style"])
    if lang_style:
        value = de_value if lang_style == "de" else en_value
        alt = en_value if lang_style == "de" else de_value
        return ParsedNumber(value, lang_style, True, True, [alt], reasons + ["resolved_by_language_only"])
    return ParsedNumber(en_value, "en", True, True, [de_value], reasons + ["no_language_no_document_style"])


def detect_number_style(texts: Iterable[str]) -> NumberStyle | None:
    """同文档交叉验证：统计无歧义的德式/英式数字，占优者胜（需 ≥2 且无反例占比 > 20%）。"""
    de = en = 0
    for t in texts:
        if not t:
            continue
        for m in _NUM_TOKEN.finditer(str(t)):
            try:
                p = parse_number(m.group("num"))
            except ValueError:
                continue
            if p.ambiguous:
                continue
            if p.style == "de":
                de += 1
            elif p.style == "en":
                en += 1
    if de >= 2 and en <= de * 0.2:
        return "de"
    if en >= 2 and de <= en * 0.2:
        return "en"
    return None


# —— 单位 ——

# (正则, 规范代码, 附加原因)；按优先级排列，长者在前
_CO2E = r"CO(?:2|₂)[\s\-]?(?:e\b|eq\b|eq\.|äq\.?|Äq\.?|-?Äquivalente?\b|equivalents?\b|-?e\b)"
_UNIT_PATTERNS: list[tuple[str, str, str | None]] = [
    (r"kg\s?" + _CO2E + r"\s?/\s?kWh", "kgCO2e/kWh", None),
    (r"kg\s?" + _CO2E + r"\s?/\s?kg", "kgCO2e/kg", None),
    (r"t\s?" + _CO2E + r"\s?/\s?t\b", "tCO2e/t", None),
    (r"t\s?" + _CO2E + r"\s?/\s?MWh", "tCO2e/MWh", None),
    (r"t\s?" + _CO2E + r"\s?/\s?(?:EUR|€)", "tCO2e/EUR", None),
    (r"MWh\s?/\s?t\b", "MWh/t", None),
    (r"(?:t|Tonnen|tonnes)\s?" + _CO2E, "tCO2e", None),
    (r"kg\s?" + _CO2E, "kgCO2e", None),
    (r"t\s?CO(?:2|₂)\b(?![\s\-]?(?:e|eq|äq|Äq))", "tCO2", "co2_not_co2e"),
    (r"kg\s?CO(?:2|₂)\b(?![\s\-]?(?:e|eq|äq|Äq))", "kgCO2", "co2_not_co2e"),
    (r"[Gg][Ww][Hh]\b", "GWh", "unit_not_in_dictionary"),
    (r"[Mm][Ww][Hh]\b", "MWh", None),
    (r"[Kk][Ww][Hh]\b", "kWh", None),
    (r"TJ\b", "TJ", "unit_not_in_dictionary"),
    (r"GJ\b", "GJ", None),
    (r"MJ\b", "MJ", "unit_not_in_dictionary"),
    (r"%\s?\(?w/w\)?|Gew\.?-?\s?%|wt\.?\s?%|Massen-?%", "%w/w", None),
    (r"%|Prozent\b|percent\b|per\s?cent\b", "%", None),
    (r"ppm\b", "ppm", None),
    (r"m³|m3\b|cbm\b|Kubikmeter\b", "m3", None),
    (r"m²|m2\b|Quadratmeter\b", "m2", None),
    (r"ha\b|Hektar\b|hectares?\b", "ha", None),
    (r"(?:Liter|Litre|liters|litres|liter)\b|L\b", "L", None),
    (r"Tonnen\b|tonnes\b|metric\s+tons?\b|t\b", "t", None),
    (r"tons?\b", "t", "ton_ambiguous"),
    (r"kg\b|Kilogramm\b|kilograms?\b", "kg", None),
    (r"EUR\b|€|Euro\b", "EUR", None),
    (r"(?:Stunden|Std\.|hours?|h)\b", "h", None),
    (r"(?:Stück|Stk\.?|pcs\.?|pieces?|Mitarbeitende|Mitarbeiter(?:innen)?|employees|FTE|VZÄ)\b", "1", None),
]
_UNIT_RES = [(re.compile(p), code, reason) for p, code, reason in _UNIT_PATTERNS]

KNOWN_UNITS = {
    "t", "kg", "tCO2e", "kgCO2e", "kWh", "MWh", "GJ", "m3", "L", "m2", "ha", "%", "%w/w", "ppm", "1", "h", "EUR",
    "tCO2e/t", "kgCO2e/kg", "kgCO2e/kWh", "MWh/t", "tCO2e/EUR", "tCO2e/MWh",
}


def normalize_unit(raw: str | None) -> tuple[str | None, list[str]]:
    """把原始单位串归一为 02 §3.1 代码；返回 (code, reasons)。无法识别返回 (None, ['unit_unknown'])。"""
    if raw is None:
        return None, []
    s = str(raw).strip().strip("[]()").strip()
    if not s:
        return None, []
    for rx, code, reason in _UNIT_RES:
        m = rx.match(s)
        if m and s[m.end():].strip(" .") == "":
            return code, [reason] if reason else []
    return None, ["unit_unknown"]


def find_unit_in_text(text: str) -> tuple[str | None, list[str]]:
    """在题干中寻找单位提示（如 "in kWh"、"[tCO2e]"）。"""
    if not text:
        return None, []
    for rx, code, reason in _UNIT_RES:
        # 题干中裸 "t"/"h"/"L"/"ha" 噪声太大，只接受括号/“in …”语境
        if code in ("t", "h", "L", "ha", "1", "EUR", "kg") and not reason:
            ctx = re.search(r"(?:\bin\b|\[|\(|\bper\b|\bje\b|\/)\s*(" + rx.pattern + r")", text)
            if ctx:
                return code, []
            continue
        m = rx.search(text)
        if m:
            return code, [reason] if reason else []
    return None, []


# —— 空值语义 ——

_NULL_PATTERNS: list[tuple[str, str]] = [
    (r"^(?:vertraulich|confidential|geheim|nicht\s+öffentlich|keine\s+Offenlegung|not\s+disclosed)\b", "confidential"),
    (r"^(?:n\.?\s?/?\s?a\.?|nicht\s+zutreffend|entfällt|not\s+applicable|trifft\s+nicht\s+zu|irrelevant)$", "not_applicable"),
    (r"^(?:k\.?\s?A\.?|keine\s+Angabe|keine\s+Daten|unbekannt|unknown|not\s+available|no\s+data|noch\s+nicht\s+verfügbar|tbd|t\.b\.d\.|-|–|—)$", "no_data"),
]


def classify_null(text: object) -> str | None:
    """返回 'zero' | 'not_applicable' | 'no_data' | 'confidential' | None（02 §3.6 空值语义）。"""
    if text is None:
        return None
    if isinstance(text, (int, float, Decimal)) and not isinstance(text, bool):
        return "zero" if Decimal(str(text)) == 0 else None
    s = str(text).strip()
    if re.fullmatch(r"0+(?:[.,]0+)?", s):
        return "zero"
    for p, kind in _NULL_PATTERNS:
        if re.match(p, s, flags=re.IGNORECASE):
            return kind
    return None


# —— 抽取 ——

_COMPARATOR = re.compile(
    r"(?P<cmp><=|>=|≤|≥|<|>|~|≈|ca\.|circa|approx\.?|etwa|rund|unter|über|weniger\s+als|mehr\s+als|bis\s+zu|max\.|mind\.|"
    r"less\s+than|more\s+than|up\s+to|at\s+least|at\s+most)\s*$",
    re.IGNORECASE,
)
_CMP_NORM = {
    "<=": "<=", "≤": "<=", ">=": ">=", "≥": ">=", "<": "<", ">": ">", "~": "~", "≈": "~", "ca.": "~",
    "circa": "~", "approx.": "~", "approx": "~", "etwa": "~", "rund": "~", "unter": "<", "über": ">",
    "weniger als": "<", "mehr als": ">", "bis zu": "<=", "max.": "<=", "mind.": ">=", "less than": "<",
    "more than": ">", "up to": "<=", "at least": ">=", "at most": "<=",
}
_RANGE_SEP = re.compile(r"^\s*(?:–|-|—|bis|to)\s*")


def _match_unit_after(text: str, pos: int) -> tuple[str | None, str | None, int, list[str]]:
    rest = text[pos:]
    m_ws = re.match(r"[ \u00a0\u202f]?", rest)
    off = m_ws.end() if m_ws else 0
    for rx, code, reason in _UNIT_RES:
        m = rx.match(rest, off)
        if m:
            return code, m.group(0), pos + m.end(), [reason] if reason else []
    return None, None, pos, []


def extract_quantities(text: str, lang: str | None = None, doc_style: NumberStyle | None = None,
                       require_unit: bool = False) -> list[Quantity]:
    """从文本中抽取带单位数字（02 §3.6）。

    - 年份样式的裸 4 位整数（1900–2100）若无单位，忽略。
    - `require_unit=True` 时只返回带单位的数字。
    """
    out: list[Quantity] = []
    if not text:
        return out
    if doc_style is None:
        doc_style = detect_number_style([text])
    tokens = list(_NUM_TOKEN.finditer(text))
    i = 0
    while i < len(tokens):
        m = tokens[i]
        i += 1
        num_raw = (m.group("sign") or "") + m.group("num")
        try:
            pn = parse_number(num_raw, lang=lang, doc_style=doc_style)
        except ValueError:
            continue
        start, end = m.start(), m.end()
        # 区间：a – b unit
        rng_end = None
        rest = text[end:]
        rs = _RANGE_SEP.match(rest)
        if rs and i < len(tokens) and tokens[i].start() == end + rs.end():
            m2 = tokens[i]
            try:
                pn2 = parse_number(m2.group("num"), lang=lang, doc_style=doc_style)
                rng_end = (m2, pn2)
            except ValueError:
                rng_end = None
        if rng_end:
            m2, pn2 = rng_end
            unit, unit_raw, uend, ureasons = _match_unit_after(text, m2.end())
            if unit is None and require_unit:
                i += 1
                continue
            raw = text[start:uend if unit else m2.end()]
            out.append(Quantity(
                raw=raw, value=None, unit=unit, unit_raw=unit_raw, number_style=pn.style,
                span=(start, uend if unit else m2.end()), is_range=True,
                range_text=f"{to_dec_str(pn.value)}–{to_dec_str(pn2.value)}",
                is_estimate=True, needs_confirmation=False,
                reasons=ureasons + ["range_not_averaged"] + pn.reasons, ambiguous=pn.ambiguous or pn2.ambiguous,
            ))
            i += 1
            continue
        # 前置比较符 / 前置 €
        before = text[max(0, start - 20):start]
        cmp_m = _COMPARATOR.search(before)
        comparator = _CMP_NORM.get(cmp_m.group("cmp").lower().replace("  ", " ")) if cmp_m else None
        unit, unit_raw, uend, ureasons = _match_unit_after(text, end)
        if unit is None and re.search(r"(?:€|EUR)\s?$", before):
            unit, unit_raw, ureasons = "EUR", "€", []
        if unit is None:
            if require_unit:
                continue
            if pn.style == "plain" and len(m.group("num")) == 4 and 1900 <= int(m.group("num")) <= 2100:
                continue  # 年份
        reasons = list(pn.reasons) + ureasons
        needs = pn.needs_confirmation
        ambiguous = pn.ambiguous
        if "ton_ambiguous" in ureasons:
            needs = ambiguous = True
        if "co2_not_co2e" in ureasons:
            needs = True
        if unit == "%" and pn.value is not None and abs(pn.value) <= 1 and pn.value != 0:
            reasons.append("percent_value_le_1")
            needs = ambiguous = True
        raw_end = uend if unit and uend > end else end
        q = Quantity(
            raw=text[start:raw_end], value=pn.value, unit=unit, unit_raw=unit_raw, number_style=pn.style,
            span=(start, raw_end), ambiguous=ambiguous, needs_confirmation=needs, reasons=reasons,
            alternatives=pn.alternatives,
        )
        if comparator and comparator != "~":
            q.is_range = True
            q.comparator = comparator
            q.range_text = f"{comparator} {to_dec_str(pn.value)}"
            q.value = None
            q.is_estimate = True
            q.reasons.append("bound_not_point_value")
        elif comparator == "~":
            q.comparator = "~"
            q.is_estimate = True
            q.reasons.append("approximate")
        out.append(q)
    return out


def parse_quantity(text: str, lang: str | None = None, doc_style: NumberStyle | None = None) -> Quantity | None:
    """解析单个单元格/字段的值（如 "1.234,5 MWh"）。返回第一个数量或 None。"""
    qs = extract_quantities(text, lang=lang, doc_style=doc_style)
    return qs[0] if qs else None


def cleanup_decimal_str(s: str) -> str | None:
    """宽松把 DEC-STR / 数字串转 DEC-STR；失败返回 None。"""
    try:
        return to_dec_str(Decimal(s))
    except (InvalidOperation, ValueError):
        return None
