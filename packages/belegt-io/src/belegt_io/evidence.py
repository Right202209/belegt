# Spec: 03 §3.5 Evidence（locator：文件哈希 + 页/单元格/bbox）、R-LOC；02 §3.6；04 §2.4 evidence-miner（确定性辅助）
"""文档证据提取的确定性辅助：PDF 文本 + 页码/bbox 定位、xlsx 单元格取值 + 定位、带单位数字抽取。

输出 `EvidenceCandidate(status=candidate)`；是否 `verified` 由 evidence-miner / guard 按 `EV-AUTO-0.90-LOC` 决定。
置信度为确定性启发式：数值与单位均无歧义 0.92；任一歧义/需确认 ≤ 0.6；区间/上下限 0.5。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .hashing import file_sha256, mime_for, text_sha256
from .hints import detect_language, guess_period
from .models import BelegtIOError, EvidenceCandidate, IOErrorCode, Locator, Quantity
from .numbers import detect_number_style, extract_quantities, normalize_unit, to_dec_str

# 关键词 → fact_key（02 活动数据键风格 `activity.*`；不确定时留空，由 evidence-miner 判定）
FACT_KEY_RULES: list[tuple[re.Pattern[str], set[str], str]] = [
    (re.compile(r"(?:strom|electricity|elektrische\s+energie|wirkarbeit|netzbezug)", re.I), {"kWh", "MWh", "GWh"},
     "activity.electricity.purchased"),
    (re.compile(r"(?:erdgas|natural\s+gas|gasverbrauch|\bgas\b)", re.I), {"kWh", "MWh", "m3", "GJ"},
     "activity.natural_gas.consumed"),
    (re.compile(r"(?:diesel|heizöl|heating\s+oil|fuel\s+oil)", re.I), {"L", "m3", "kWh", "MWh"}, "activity.diesel.consumed"),
    (re.compile(r"(?:fernwärme|district\s+heat)", re.I), {"kWh", "MWh", "GJ"}, "activity.district_heat.purchased"),
    (re.compile(r"(?:scope[\s\-]*1)", re.I), {"tCO2e", "kgCO2e"}, "emissions.scope1.total"),
    (re.compile(r"(?:scope[\s\-]*2).*(?:market|markt)", re.I), {"tCO2e", "kgCO2e"}, "emissions.scope2.market_based"),
    (re.compile(r"(?:scope[\s\-]*2)", re.I), {"tCO2e", "kgCO2e"}, "emissions.scope2.location_based"),
    (re.compile(r"(?:wasser|water)", re.I), {"m3", "L"}, "activity.water.withdrawn"),
    (re.compile(r"(?:abfall|waste)", re.I), {"t", "kg"}, "activity.waste.generated"),
    (re.compile(r"(?:mitarbeit|beschäftigte|employees|headcount)", re.I), {"1"}, "org.employees.headcount"),
]


def guess_fact_key(label: str | None, unit: str | None) -> str | None:
    if not label:
        return None
    for rx, units, key in FACT_KEY_RULES:
        if rx.search(label) and (unit is None or unit in units):
            return key
    return None


def _confidence(q: Quantity) -> float:
    if q.is_range:
        return 0.5
    if q.needs_confirmation:
        return 0.55
    if q.ambiguous:
        return 0.75
    if q.unit is None:
        return 0.6
    return 0.92


def _candidate(q: Quantity, locator: Locator, label: str | None, excerpt: str | None,
               period_text: str | None = None) -> EvidenceCandidate:
    period, _ = guess_period(period_text or "")
    reasons = list(q.reasons)
    if q.unit and q.unit not in {"tCO2", "kgCO2", "GWh", "TJ", "MJ"} and normalize_unit(q.unit)[0] is None:
        reasons.append("unit_unknown")
    return EvidenceCandidate(
        fact_key=guess_fact_key(label, q.unit),
        extracted_value=(q.dec_str if not q.is_range else q.range_text),
        value_type="number" if not q.is_range else "text",
        unit=q.unit,
        period=period,
        locator=locator,
        confidence=_confidence(q),
        is_estimate=q.is_estimate,
        needs_confirmation=q.needs_confirmation or q.is_range,
        reasons=reasons,
        label=label,
        excerpt=excerpt,
    )


# —— PDF ——

def pdf_pages_text(path: str | Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise BelegtIOError(IOErrorCode.UNSUPPORTED_FORMAT, "pypdf not installed") from e
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as e:  # pragma: no cover
            raise BelegtIOError(IOErrorCode.FILE_UNREADABLE, "encrypted PDF", str(path)) from e
    return [(p.extract_text() or "") for p in reader.pages]


def _pdf_lines(page: Any) -> list[tuple[str, tuple[float, float, float, float] | None]]:
    """用 pypdf visitor 收集带坐标的文本片段，按基线 y 合并成行，估算 bbox（PDF 点坐标，[x0,y0,x1,y1]）。"""
    frags: list[tuple[float, float, float, str]] = []  # (y, x, font_size, text)

    def visitor(text: str, cm: list[float], tm: list[float], font_dict: Any, font_size: float) -> None:
        if not text or not text.strip():
            return
        # 用户空间坐标 = tm × cm
        a, b, c, d, e, f = tm
        A, B, C, D, E, F = cm
        x = a * 0 + c * 0 + e
        y = b * 0 + d * 0 + f
        ux = x * A + y * C + E
        uy = x * B + y * D + F
        size = abs(font_size * (d if d else 1) * (D if D else 1)) or font_size or 10.0
        frags.append((round(uy, 1), round(ux, 1), size, text))

    page.extract_text(visitor_text=visitor)
    lines: dict[float, list[tuple[float, float, str]]] = {}
    for y, x, size, t in frags:
        key = next((k for k in lines if abs(k - y) < 2.0), y)
        lines.setdefault(key, []).append((x, size, t))
    out = []
    for y in sorted(lines, reverse=True):
        parts = sorted(lines[y], key=lambda p: p[0])
        text = "".join(p[2] for p in parts).replace("\n", " ").strip()
        if not text:
            continue
        size = max(p[1] for p in parts)
        x0 = parts[0][0]
        x1 = x0 + 0.5 * size * len(text)  # 估算宽度（平均字宽 ≈ 0.5 em）
        out.append((text, (round(x0, 1), round(y - 0.2 * size, 1), round(x1, 1), round(y + 0.8 * size, 1))))
    return out


def extract_pdf_evidence(path: str | Path, *, lang: str | None = None, require_unit: bool = True,
                         keywords: Iterable[str] | None = None) -> list[EvidenceCandidate]:
    """PDF → 带单位数字候选，locator 含 `page` 与估算 `bbox`、`text_excerpt_hash`。

    `keywords`：只保留所在行含任一关键词的数字（大小写不敏感）。
    """
    from pypdf import PdfReader

    p = Path(path)
    sha = file_sha256(p)
    reader = PdfReader(str(p))
    pages_lines = []
    all_text: list[str] = []
    for page in reader.pages:
        lines = _pdf_lines(page)
        if not lines:  # visitor 不可用时退回整页文本
            lines = [(ln, None) for ln in (page.extract_text() or "").splitlines() if ln.strip()]
        pages_lines.append(lines)
        all_text.extend(t for t, _ in lines)
    doc_style = detect_number_style(all_text)
    if lang is None:
        lang, _ = detect_language(" ".join(all_text[:200]))
    kws = [k.lower() for k in keywords] if keywords else None
    out: list[EvidenceCandidate] = []
    for pno, lines in enumerate(pages_lines, start=1):
        for text, bbox in lines:
            if kws and not any(k in text.lower() for k in kws):
                continue
            for q in extract_quantities(text, lang=lang, doc_style=doc_style, require_unit=require_unit):
                loc = Locator(blob_sha256=sha, mime="application/pdf", page=pno, bbox=bbox,
                              text_excerpt_hash=text_sha256(text))
                label = text[: q.span[0]].strip(" :;-–\t") or text
                out.append(_candidate(q, loc, label, text, period_text=" ".join(all_text[:30]) + " " + text))
    return out


# —— XLSX ——

def _is_id_header(h: str) -> bool:
    from .parse import _role_of

    return _role_of(h) == "id"


def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def extract_xlsx_evidence(path: str | Path, *, cells: Iterable[str] | None = None, lang: str | None = None,
                          require_unit: bool = False) -> list[EvidenceCandidate]:
    """xlsx 单元格取值 + 定位。

    - `cells` 给定（如 `["Energie!C5"]`）时只取这些单元格；否则扫描全部数值/数字文本单元格，
      以同行左侧最近的文本为标签、同行右侧或表头"Einheit/Unit"列为单位。
    - 数值单元格直接取值（不经文本解析，无千分位歧义）；百分比格式单元格的值 ×100（02 §3.1 存 0–100）。
    """
    from openpyxl import load_workbook

    from .sheets import split_coord

    p = Path(path)
    sha = file_sha256(p)
    wb = load_workbook(p, data_only=True)
    targets: dict[str, set[tuple[int, int]]] | None = None
    if cells is not None:
        targets = {}
        for ref in cells:
            loc = Locator.parse(ref)
            sheet = loc.sheet or wb.sheetnames[0]
            targets.setdefault(sheet, set()).add(split_coord(loc.cell))  # type: ignore[arg-type]
    texts = [_cell_text(c.value) for ws in wb.worksheets for row in ws.iter_rows() for c in row if isinstance(c.value, str)]
    doc_style = detect_number_style(texts)
    if lang is None:
        lang, _ = detect_language(" ".join(texts[:300]))
    out: list[EvidenceCandidate] = []
    for ws in wb.worksheets:
        unit_cols: set[int] = set()
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30)):
            for c in row:
                if isinstance(c.value, str) and re.fullmatch(r"\s*(?:einheit|unit|maßeinheit)\s*", c.value, re.I):
                    unit_cols.add(c.column)
        for row in ws.iter_rows():
            for c in row:
                if targets is not None and (ws.title not in targets or (c.row, c.column) not in targets[ws.title]):
                    continue
                v = c.value
                if v is None or isinstance(v, bool):
                    continue
                left = ""
                for cc in range(c.column - 1, 0, -1):
                    lv = ws.cell(row=c.row, column=cc).value
                    if isinstance(lv, str) and lv.strip():
                        left = lv.strip()
                        break
                header = ""
                for rr in range(c.row - 1, max(0, c.row - 40), -1):
                    hv = ws.cell(row=rr, column=c.column).value
                    if isinstance(hv, str) and hv.strip() and not re.search(r"\d", hv):
                        header = hv.strip()
                        break
                if targets is None and header and _is_id_header(header):
                    continue  # 序号列
                label = " / ".join(x for x in (header, left) if x) if header and header != left else (left or header)
                unit_raw = None
                for uc in sorted(unit_cols) + [c.column + 1]:
                    if uc == c.column:
                        continue
                    uv = ws.cell(row=c.row, column=uc).value
                    if isinstance(uv, str) and normalize_unit(uv)[0]:
                        unit_raw = uv
                        break
                loc = Locator(blob_sha256=sha, mime=mime_for(p), sheet=ws.title, cell=c.coordinate)
                if isinstance(v, (int, float, Decimal)):
                    d = Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
                    unit, ureasons = normalize_unit(unit_raw) if unit_raw else (None, [])
                    reasons = list(ureasons)
                    nf = c.number_format or ""
                    if "%" in nf:
                        d = d * 100
                        unit = "%"
                        reasons.append("percent_cell_format")
                    elif unit == "%" and abs(d) <= 1 and d != 0:
                        reasons.append("percent_value_le_1")
                    if unit is None and (require_unit or not label):
                        continue
                    q = Quantity(raw=_cell_text(v) + (f" {unit_raw}" if unit_raw else ""), value=d, unit=unit,
                                 unit_raw=unit_raw, number_style="plain", reasons=reasons,
                                 needs_confirmation=("percent_value_le_1" in reasons or "ton_ambiguous" in reasons
                                                     or "co2_not_co2e" in reasons),
                                 ambiguous="percent_value_le_1" in reasons or "ton_ambiguous" in reasons)
                    out.append(_candidate(q, loc, label or None, f"{label} {q.raw}".strip(), period_text=label))
                elif isinstance(v, str) and re.search(r"\d", v):
                    # 全表扫描时文本单元格只收带单位的数字（避免把编号、地址里的数字当证据）
                    need_unit = require_unit or targets is None
                    for q in extract_quantities(v + (f" {unit_raw}" if unit_raw and not re.search(r"[A-Za-z%€]", v) else ""),
                                                lang=lang, doc_style=doc_style, require_unit=need_unit):
                        out.append(_candidate(q, loc, label or None, f"{label} {v}".strip(), period_text=label + " " + v))
                elif isinstance(v, (datetime, date)) and targets is not None:
                    d = v.date() if isinstance(v, datetime) else v
                    out.append(EvidenceCandidate(value_type="date", extracted_value=d.isoformat(), locator=loc,
                                                 confidence=0.9, label=label or None, excerpt=f"{label} {d.isoformat()}"))
    return out


def extract_text_evidence(text: str, locator: Locator, *, lang: str | None = None,
                          require_unit: bool = True) -> list[EvidenceCandidate]:
    """对任意已定位文本（邮件正文段落、OCR 结果）抽取数字候选。"""
    out = []
    for q in extract_quantities(text, lang=lang, require_unit=require_unit):
        label = text[: q.span[0]].strip(" :;-–\t") or None
        out.append(_candidate(q, locator, label, text, period_text=text))
    return out
