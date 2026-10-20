# Spec: 03 §3.3 Question；04 §2.2（intake-triage 附件指纹）、§2.3（question-parser 的确定性版面切分）；02 §4.4
"""问卷解析（确定性部分）：xlsx / csv → `ParsedQuestionnaire(questions=[QuestionDraft...])`。

流程：
1. 文件 sha256 → `Locator.blob_sha256`；
2. 第一轮指纹（工作表名 + 首屏文本）识别已知模板；命中且有 `layouts` 的工作表按模板版面切题；
3. 其余工作表做通用切题：表头识别（题号/题干/答案/单位/备注…列）、合并单元格（分节行识别、答案单元格取左上角）、
   数据验证下拉 → enum 候选、答案列定位；
4. 第二轮指纹（加入题号）→ 精确映射 `exact_template`（02 §4.4，置信度 < 0.85 不用）。
LLM 不参与；题干按外部数据处理，不做任何指令解释（07 §3）。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any

from .hashing import file_sha256, mime_for
from .hints import detect_language, dominant_language, guess_answer_format, guess_period, guess_unit
from .models import (
    BelegtIOError, IOErrorCode, Locator, ParsedQuestionnaire, QuestionDraft, SheetLayout,
)
from .sheets import GridSheetView, SheetView, XlsxSheetView, coord, read_csv_rows
from .templates import detect_template, layouts_for, match_entries

HEADER_SCAN_ROWS = 40

_ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("id", re.compile(r"^(?:nr|no|nummer|lfd ?nr|laufende ?nr|id|#|frage ?nr|frage ?id|fragen ?nr|question ?(?:no|number|id)|"
                      r"kennung|ref|referenz|item|pos|position|kapitel ?nr|q ?id)$")),
    ("unit", re.compile(r"^(?:einheit|unit|maßeinheit|masseinheit|units?)$")),
    ("comment", re.compile(r"^(?:kommentar|kommentare|bemerkung|bemerkungen|anmerkung|anmerkungen|comments?|remarks?|notes?|erläuterung|erläuterungen)")),
    ("evidence", re.compile(r"^(?:nachweis|nachweise|evidence|beleg|belege|dokument|dokumente|attachment|anhang|link|quelle|source)")),
    ("section", re.compile(r"^(?:kategorie|bereich|section|thema|topic|kapitel|category|abschnitt|themenfeld)$")),
    ("period", re.compile(r"^(?:zeitraum|berichtsjahr|berichtszeitraum|period|reporting ?(?:year|period)|jahr|year)$")),
    ("options", re.compile(r"^(?:antwortoptionen|antwortmöglichkeiten|optionen|options|auswahl|answer ?options)$")),
    ("answer", re.compile(r"(?:antwort|answer|wert|value|response|eingabe|rückmeldung|lieferantenangabe|supplier ?response|ihre ?angabe)")),
    ("question", re.compile(r"(?:frage|fragestellung|question|anforderung|requirement|beschreibung|description|kriterium|criterion|"
                            r"abfrage|indikator|indicator|datenpunkt|item ?text)")),
]

_SKIP_SHEETS = re.compile(r"^(?:listen?|lists?|dropdowns?|auswahl(?:listen)?|hilfstabelle|helper|lookup|config|stammdaten-listen)$", re.I)
_LEADING_ID = re.compile(r"^\s*((?:[A-Z]{1,3}[\-.]?)?\d{1,3}(?:[.\-]\d{1,3}){0,3}[a-z]?)(?:[).:]|\s)\s*\S")


def _norm_header(s: str) -> str:
    s = s.lower().replace("\n", " ")
    s = re.sub(r"[.:*()\[\]/_\-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _role_of(header: str) -> str | None:
    h = _norm_header(header)
    if not h:
        return None
    for role, rx in _ROLE_PATTERNS:
        if role in ("answer", "question"):
            if rx.search(h):
                # "Frage-Nr." 已被 id 吃掉；"Antwort" 优先于 "Frage"（如 "Antwort auf Frage"）
                return role
        elif rx.search(h):
            return role
    return None


def detect_header(view: SheetView) -> tuple[int | None, dict[str, int]]:
    """返回 (表头行, {role: col})。题干列必需；分数 = 识别出的角色数。"""
    best_row, best_cols, best_score = None, {}, 0
    for r in range(1, min(view.max_row, HEADER_SCAN_ROWS) + 1):
        cols: dict[str, int] = {}
        for c in range(1, view.max_col + 1):
            t = view.text(r, c)
            if not t or len(t) > 60:
                continue
            role = _role_of(t)
            if role and role not in cols:
                cols[role] = c
        if "question" not in cols:
            continue
        score = len(cols) + (1 if "answer" in cols else 0)
        if score > best_score:
            best_row, best_cols, best_score = r, cols, score
    if best_row is None or best_score < 2:
        return None, {}
    return best_row, best_cols


def _infer_answer_col(view: SheetView, header_row: int, cols: dict[str, int]) -> int:
    used = set(cols.values())
    qc = cols["question"]
    # 1) 题干右侧第一个空表头列
    for c in range(qc + 1, max(view.max_col, qc + 1) + 1):
        if c in used:
            continue
        if not view.text(header_row, c):
            return c
    # 2) 有数据验证的列
    for c in range(1, view.max_col + 1):
        if c not in used and view.dv(header_row + 1, c):
            return c
    return qc + 1


def _native_id(raw: str) -> str | None:
    s = raw.strip()
    if not s:
        return None
    return s[:64]


def _split_options(s: str) -> list[str]:
    parts = re.split(r"\s*(?:;|\||\n|/)\s*", s)
    return [p for p in (x.strip() for x in parts) if p]


class _Ctx:
    def __init__(self, sha: str, mime: str, is_csv: bool):
        self.sha = sha
        self.mime = mime
        self.is_csv = is_csv
        self.ordinal = 0
        self.doc_lang = "und"
        self.doc_period = None
        self.doc_period_type = None

    def loc(self, view: SheetView, r: int, c: int) -> Locator:
        return Locator(blob_sha256=self.sha, mime=self.mime, sheet=None if self.is_csv else view.name, cell=coord(r, c))


def _make_question(ctx: _Ctx, view: SheetView, *, text: str, text_rc: tuple[int, int], answer_rc: tuple[int, int],
                   native_id: str | None, section: str | None, unit_raw: str | None = None,
                   unit_rc: tuple[int, int] | None = None, comment_rc: tuple[int, int] | None = None,
                   options: list[str] | None = None, fmt_hint: str | None = None, unit_hint: str | None = None,
                   period_text: str | None = None, hints: dict[str, Any] | None = None) -> QuestionDraft:
    ar, ac = view.top_left(*answer_rc)
    dv = view.dv(ar, ac)
    enum_source = None
    if dv and dv.type == "list" and dv.options:
        options = dv.options
        enum_source = f"data_validation:{dv.formula}"
    elif options:
        enum_source = "options_column"
    unit, unit_reasons = guess_unit(text, unit_raw)
    if unit_hint and not unit:
        unit = unit_hint
    fmt, fmt_conf = guess_answer_format(text, unit, options)
    if fmt_hint and not options:
        fmt, fmt_conf = fmt_hint, 0.95
    if dv and dv.type in ("decimal", "whole") and fmt not in ("number",):
        fmt, fmt_conf = "number", 0.9
    elif dv and dv.type == "date":
        fmt, fmt_conf = "date", 0.9
    period, ptype = guess_period(" ".join(x for x in (text, period_text or "") if x))
    if period is None and ptype is None and fmt == "number" and ctx.doc_period is not None:
        period, ptype = ctx.doc_period, ctx.doc_period_type
    lang, lconf = detect_language(text)
    if lang == "und" or lconf < 0.4:
        lang = ctx.doc_lang if ctx.doc_lang != "und" else lang
    ctx.ordinal += 1
    h = dict(hints or {})
    if unit_reasons:
        h["unit_reasons"] = unit_reasons
    if dv:
        h["data_validation"] = {"type": dv.type, "formula": dv.formula, "allow_blank": dv.allow_blank}
    if view.merged_bounds(*answer_rc):
        h["answer_cell_merged"] = True
    if lang == "und":
        h["text_lang_assumed"] = "en"
    existing = view.value(ar, ac)
    return QuestionDraft(
        ordinal=ctx.ordinal,
        native_id=native_id,
        # 03 I18nText 键须为 ISO 639-1；无法判定时退回 en 并在 hints 标注
        text={lang if lang != "und" else "en": text},
        answer_format=fmt,  # type: ignore[arg-type]
        options=options or None,
        unit_requested=unit if fmt == "number" else (unit if unit and fmt in ("table",) else None),
        period_requested=period,
        locator=ctx.loc(view, ar, ac),
        language=lang,
        language_confidence=lconf,
        text_locator=ctx.loc(view, *text_rc),
        unit_locator=ctx.loc(view, *unit_rc) if unit_rc else None,
        comment_locator=ctx.loc(view, *comment_rc) if comment_rc else None,
        section=section,
        period_type_hint=ptype,
        answer_format_confidence=fmt_conf,
        enum_source=enum_source,
        existing_value=existing if not isinstance(existing, str) or not existing.startswith("=") else None,
        hints=h,
    )


# —— 通用版面 ——

def _parse_generic(ctx: _Ctx, view: SheetView, layout: SheetLayout, row_range: tuple[int, int] | None = None,
                   cols_override: dict[str, int] | None = None, header_row: int | None = None) -> list[QuestionDraft]:
    if cols_override is None:
        header_row, cols = detect_header(view)
        if header_row is None:
            layout.skipped_reason = "no_header_detected"
            return []
    else:
        cols = dict(cols_override)
    assert header_row is not None
    if "answer" not in cols:
        cols["answer"] = _infer_answer_col(view, header_row, cols)
        layout.columns["answer(inferred)"] = coord(1, cols["answer"])[:-1]
    layout.header_row = header_row
    for role, c in cols.items():
        layout.columns.setdefault(role, coord(1, c)[:-1])
    qc, ac = cols["question"], cols["answer"]
    out: list[QuestionDraft] = []
    section: str | None = None
    start, end = row_range or (header_row + 1, view.max_row)
    empty_streak = 0
    for r in range(start, end + 1):
        tl = view.top_left(r, qc)
        if tl != (r, qc):
            mb = view.merged_bounds(r, qc)
            # 从左侧列开始横跨题干与答案列的合并行 = 分节标题（如 A:E 合并）
            if mb and mb[0] == r and mb[3] >= ac and view.text(*tl):
                section = view.text(*tl)
            continue  # 合并区域内的非左上角
        text = view.text(r, qc)
        nid = _native_id(view.text(r, cols["id"])) if "id" in cols else None
        if not text:
            empty_streak += 1
            if row_range and empty_streak >= 2:
                break
            continue
        empty_streak = 0
        if view.is_formula(r, qc):
            continue
        # 分节行：题干合并到答案列 / 答案单元格与题干同一合并区域 / 无题号的粗体短行
        mb = view.merged_bounds(r, qc)
        if mb and mb[3] >= ac:
            section = text
            continue
        if view.top_left(r, ac) == (r, qc):
            section = text
            continue
        other_filled = any(view.text(r, c) for c in range(1, view.max_col + 1) if c not in (qc,))
        if "id" in cols and not nid and not text.endswith("?") and (view.bold(r, qc) or not other_filled) and len(text) < 100:
            section = text
            continue
        if "section" in cols and view.text(r, cols["section"]):
            section = view.text(r, cols["section"])
        if not nid:
            m = _LEADING_ID.match(text)
            nid = m.group(1) if m else None
        unit_raw = view.text(r, cols["unit"]) if "unit" in cols else None
        opts = _split_options(view.text(r, cols["options"])) if "options" in cols and view.text(r, cols["options"]) else None
        period_text = view.text(r, cols["period"]) if "period" in cols else None
        q = _make_question(
            ctx, view, text=text, text_rc=(r, qc), answer_rc=(r, ac), native_id=nid, section=section,
            unit_raw=unit_raw or None, unit_rc=(r, cols["unit"]) if "unit" in cols else None,
            comment_rc=(r, cols["comment"]) if "comment" in cols else None, options=opts, period_text=period_text,
        )
        out.append(q)
    layout.question_count += len(out)
    return out


# —— 模板版面 ——

def _find_label(view: SheetView, label: str) -> tuple[int, int] | None:
    target = _norm_header(label)
    for r in range(1, view.max_row + 1):
        for c in range(1, view.max_col + 1):
            t = view.text(r, c)
            if t and _norm_header(t).startswith(target) and view.top_left(r, c) == (r, c):
                return r, c
    return None


def _right_of(view: SheetView, r: int, c: int) -> tuple[int, int]:
    mb = view.merged_bounds(r, c)
    nc = (mb[3] if mb else c) + 1
    return r, nc


def _find_header_row(view: SheetView, names: list[str], start: int = 1) -> tuple[int, dict[str, int]] | None:
    targets = {n: _norm_header(n) for n in names}
    for r in range(start, view.max_row + 1):
        found: dict[str, int] = {}
        for c in range(1, view.max_col + 1):
            t = view.text(r, c)
            if not t:
                continue
            nt = _norm_header(t)
            for n, tn in targets.items():
                if n not in found and (nt == tn or nt.startswith(tn)):
                    found[n] = c
        if len(found) == len(targets):
            return r, found
    return None


def _parse_layout(ctx: _Ctx, view: SheetView, lay: dict[str, Any], layout: SheetLayout) -> list[QuestionDraft]:
    kind = lay.get("kind")
    out: list[QuestionDraft] = []
    if kind == "form":
        for spec in lay.get("labels", []):
            pos = _find_label(view, spec["label"])
            if not pos:
                continue
            ar = _right_of(view, *pos)
            out.append(_make_question(
                ctx, view, text=view.text(*pos), text_rc=pos, answer_rc=ar, native_id=None, section=view.name,
                fmt_hint=spec.get("answer_format"), unit_hint=spec.get("unit"),
                hints={"label": spec["label"], "layout": "form"},
            ))
    elif kind == "table":
        cols_spec = lay.get("columns", [])
        found = _find_header_row(view, [c["column"] for c in cols_spec])
        if not found:
            return out
        hr, colmap = found
        layout.header_row = hr
        n = int(lay.get("data_rows", 10))
        last = max(hr + n, view.max_row)
        for i, r in enumerate(range(hr + 1, last + 1), start=1):
            if i > n and not any(view.text(r, colmap[c["column"]]) for c in cols_spec):
                break
            for spec in cols_spec:
                c = colmap[spec["column"]]
                header_text = view.text(hr, c)
                out.append(_make_question(
                    ctx, view, text=header_text, text_rc=(hr, c), answer_rc=(r, c),
                    native_id=f"{spec['column']}#{i}"[:64], section=view.name, fmt_hint=spec.get("answer_format"),
                    unit_hint=spec.get("unit"),
                    hints={"column": spec["column"], "row_index": i, "table": view.name, "layout": "table",
                           "base_id": None},
                ))
    elif kind in ("matrix", "questionnaire"):
        id_h, text_h = lay["id_header"], lay["text_header"]
        ans = lay.get("answer_headers") or [lay["answer_header"]]
        found = _find_header_row(view, [id_h, text_h, *ans])
        if not found:
            return out
        hr, colmap = found
        layout.header_row = hr
        empty = 0
        for r in range(hr + 1, view.max_row + 1):
            nid = view.text(r, colmap[id_h])
            text = view.text(r, colmap[text_h])
            if not nid and not text:
                empty += 1
                if empty >= 1:
                    break
                continue
            if _find_header_row(view, [id_h], start=r) and _find_header_row(view, [id_h], start=r)[0] == r:
                break
            for a in ans:
                c = colmap[a]
                multi = len(ans) > 1
                out.append(_make_question(
                    ctx, view, text=text, text_rc=(r, colmap[text_h]), answer_rc=(r, c),
                    native_id=(f"{nid}.{a}" if multi else nid)[:64], section=view.name,
                    options=lay.get("options"),
                    hints={"base_id": nid, "column": a if multi else None, "layout": kind},
                ))
    layout.question_count += len(out)
    return out


# —— 入口 ——

def _first_texts(view: SheetView, rows: int = 60, cols: int = 30) -> list[str]:
    out = []
    for r in range(1, min(view.max_row, rows) + 1):
        for c in range(1, min(view.max_col, cols) + 1):
            t = view.text(r, c)
            if t:
                out.append(t)
    return out


def _doc_period(texts: list[str]):
    """文档级报告期（如表头"Berichtsjahr: 2025"）；截止日期等时点不算。"""
    for t in texts[:60]:
        p, pt = guess_period(t)
        if p is not None and pt in ("reporting_period", "calendar_year"):
            return p, pt
    return None, None


def parse_questionnaire(path: str | Path, *, template_hint: str | None = None) -> ParsedQuestionnaire:
    """解析 xlsx/xlsm/csv 问卷。`template_hint` 可强制模板 id（如邮件分拣已识别）。"""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".csv"):
        raise BelegtIOError(IOErrorCode.UNSUPPORTED_FORMAT, f"unsupported questionnaire format {suffix!r}", str(p))
    sha = file_sha256(p)
    mime = mime_for(p)
    ctx = _Ctx(sha, mime, suffix == ".csv")
    views: list[tuple[SheetView, bool]] = []  # (view, hidden)
    warnings: list[str] = []
    if suffix == ".csv":
        rows, enc, delim = read_csv_rows(p)
        views.append((GridSheetView(rows, None), False))
        warnings.append(f"csv encoding={enc} delimiter={delim!r}")
    else:
        try:
            from openpyxl import load_workbook

            wb = load_workbook(p, data_only=False, keep_vba=suffix == ".xlsm")
        except (zipfile.BadZipFile, KeyError, OSError, ValueError) as e:
            raise BelegtIOError(IOErrorCode.FILE_UNREADABLE,
                                f"Datei konnte nicht gelesen werden (passwortgeschützt?): {e}", str(p)) from e
        except Exception as e:  # openpyxl InvalidFileException 等
            raise BelegtIOError(IOErrorCode.FILE_UNREADABLE, f"cannot open workbook: {e}", str(p)) from e
        for ws in wb.worksheets:
            views.append((XlsxSheetView(ws, wb), ws.sheet_state != "visible"))

    all_texts: list[str] = []
    sheet_names = [v.name for v, _ in views if v.name]
    for v, _ in views:
        all_texts.extend(_first_texts(v))
    ctx.doc_lang = dominant_language(all_texts[:400])
    ctx.doc_period, ctx.doc_period_type = _doc_period(all_texts)

    det = detect_template(sheet_names, all_texts)
    if template_hint and (det is None or det.template_id != template_hint):
        from .templates import get_template
        doc = get_template(template_hint)
        from .models import TemplateDetection
        det = TemplateDetection(template_id=template_hint, template_version=str(doc["template_version"]),
                                template_mapping=doc["template_mapping"], confidence=0.9,
                                matched_markers=["hint"])
    layouts = layouts_for(det.template_id) if det and det.is_exact else {}

    questions: list[QuestionDraft] = []
    sheet_layouts: list[SheetLayout] = []
    for view, hidden in views:
        lay = SheetLayout(sheet=view.name or p.name)
        sheet_layouts.append(lay)
        if hidden:
            lay.skipped_reason = "hidden_sheet"
            continue
        if view.name and _SKIP_SHEETS.match(view.name):
            lay.skipped_reason = "list_sheet"
            continue
        if view.name in layouts:
            for spec in layouts[view.name]:
                questions.extend(_parse_layout(ctx, view, spec, lay))
            continue
        questions.extend(_parse_generic(ctx, view, lay))

    # 第二轮指纹：加入题号（CDP/VSME 等靠题号识别）
    det2 = detect_template(sheet_names, all_texts, [q.native_id for q in questions if q.native_id])
    if det2 and (det is None or det2.confidence > det.confidence) and not template_hint:
        det = det2
    if det and det.is_exact:
        match_entries(questions, det)
    lang = dominant_language([next(iter(q.text.values())) for q in questions]) if questions else ctx.doc_lang
    if not questions:
        warnings.append("no questions detected")
    return ParsedQuestionnaire(
        source_path=str(p), blob_sha256=sha, mime=mime, language=lang, template=det,
        questions=questions, sheets=sheet_layouts, warnings=warnings,
    )
