# Spec: 03 §2.2（值表示）、§3.3 Question、§3.5 Evidence.locator；02 §4（TemplateMapping）；04 §2.3、§2.11
"""belegt-io 自带的轻量数据类。

不依赖 belegt_core；字段名与 03 的 `Question` / `Evidence` 保持一致，
belegt_core 侧可直接 `Question(**draft.to_question_fields(request_id=...))` 映射。
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AnswerFormat = Literal["number", "text", "enum", "bool", "date", "file", "geo", "table"]
QuestionStatus = Literal["parsed", "mapped", "unmapped", "answered"]
MappingMethod = Literal["exact_template", "historical", "semantic", "human"]

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CELL_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]*(:[A-Z]{1,3}[1-9][0-9]*)?$")
DEC_STR_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")


class IOErrorCode:
    """belegt-io 错误码（本包新增，前缀 `IO_`）。"""

    UNSUPPORTED_FORMAT = "IO_UNSUPPORTED_FORMAT"
    FILE_UNREADABLE = "IO_FILE_UNREADABLE"  # 含加密/口令保护（01 UI-04 错误态）
    HASH_MISMATCH = "IO_HASH_MISMATCH"
    NOT_ANSWER_CELL = "IO_NOT_ANSWER_CELL"
    ENUM_INVALID = "IO_ENUM_INVALID"
    TYPE_MISMATCH = "IO_TYPE_MISMATCH"
    FORMULA_CELL = "IO_FORMULA_CELL"
    LOCATOR_INVALID = "IO_LOCATOR_INVALID"
    EXPORT_INVALID = "IO_EXPORT_INVALID"


class BelegtIOError(Exception):
    def __init__(self, code: str, message: str, path: str | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.path = path


class Period(BaseModel):
    """03 §2.2 `period`：闭区间 ISO 日期。"""

    model_config = ConfigDict(frozen=True)
    start: date
    end: date

    @field_validator("end")
    @classmethod
    def _order(cls, v: date, info: Any) -> date:
        start = info.data.get("start")
        if start and v < start:
            raise ValueError("period.start must be <= period.end")
        return v

    def to_json(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


class Locator(BaseModel):
    """03 §3.5 `Locator`：`{blob_sha256, mime, page?, sheet?, cell?, bbox?, text_excerpt_hash?}`。

    `cell` 不含工作表名（03 §5.3 pattern），工作表放 `sheet`。
    字符串形式（用作回填答案字典的键）：`sha256:<hex>#<sheet>!<cell>`，CSV 无 sheet 时为 `sha256:<hex>#<cell>`，
    PDF 为 `sha256:<hex>#p<page>`。
    """

    model_config = ConfigDict(frozen=True)
    blob_sha256: str | None = None
    mime: str | None = None
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    cell: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    text_excerpt_hash: str | None = None

    @field_validator("blob_sha256", "text_excerpt_hash")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        if v is not None and not SHA256_RE.match(v):
            raise ValueError("must match ^sha256:[0-9a-f]{64}$")
        return v

    @field_validator("cell")
    @classmethod
    def _cell(cls, v: str | None) -> str | None:
        if v is not None and not CELL_RE.match(v):
            raise ValueError(f"invalid cell reference {v!r}")
        return v

    @property
    def key(self) -> str:
        return str(self)

    @property
    def sheet_cell(self) -> str | None:
        if self.cell is None:
            return None
        return f"{self.sheet}!{self.cell}" if self.sheet else self.cell

    def __str__(self) -> str:  # noqa: D105
        prefix = f"{self.blob_sha256}#" if self.blob_sha256 else ""
        if self.cell:
            return f"{prefix}{self.sheet_cell}"
        if self.page:
            return f"{prefix}p{self.page}"
        return self.blob_sha256 or ""

    @classmethod
    def parse(cls, s: str, *, mime: str | None = None) -> "Locator":
        """解析 `sha256:<hex>#Sheet!A1`、`Sheet!A1`、`'My Sheet'!A1`、`A1`、`sha256:..#p3`。"""
        blob = None
        rest = s.strip()
        if rest.startswith("sha256:"):
            blob, _, rest = rest.partition("#")
        rest = rest.lstrip("#")
        if not rest:
            return cls(blob_sha256=blob, mime=mime)
        m = re.fullmatch(r"p(\d+)", rest)
        if m:
            return cls(blob_sha256=blob, mime=mime, page=int(m.group(1)))
        sheet = None
        if "!" in rest:
            sheet, _, rest = rest.rpartition("!")
            if sheet.startswith("'") and sheet.endswith("'"):
                sheet = sheet[1:-1].replace("''", "'")
        cell = rest.replace("$", "").upper()
        try:
            return cls(blob_sha256=blob, mime=mime, sheet=sheet or None, cell=cell)
        except ValueError as e:  # pragma: no cover - pydantic message
            raise BelegtIOError(IOErrorCode.LOCATOR_INVALID, f"cannot parse locator {s!r}: {e}") from e

    def to_json(self) -> dict[str, Any]:
        """按 03 §5.3 输出（省略 None）。"""
        d = self.model_dump(exclude_none=True)
        if "bbox" in d:
            d["bbox"] = list(d["bbox"])
        return d


class TemplateMappingHit(BaseModel):
    """命中的 TemplateMapping 条目 → 可直接生成 `Mapping(method=exact_template)` 候选（03 §3.4）。"""

    template_mapping_ref: str  # tm:<template_id>@<version>#<entry_id>
    datapoint_id: str
    confidence: float = Field(ge=0, le=1)
    method: MappingMethod = "exact_template"
    short_label: str | None = None
    unit_hint: str | None = None
    period_hint: str | None = None  # 02 §3.3 period_type
    boundary_hint: str | None = None
    transform: dict[str, Any] | None = None
    direction: str = "both"


class QuestionDraft(BaseModel):
    """确定性解析产物；`question-parser` 在此基础上补 Mapping（04 §2.3）。

    与 03 §3.3 `Question` 同名字段：ordinal、native_id、text、answer_format、options、
    unit_requested、period_requested、locator（答案单元格，回填用）、parent_question_id、status。
    """

    ordinal: int = Field(ge=1)
    native_id: str | None = Field(default=None, max_length=64)
    text: dict[str, str]  # I18nText，键为检测到的语言
    answer_format: AnswerFormat = "text"
    options: list[str] | None = None
    unit_requested: str | None = None
    period_requested: Period | None = None
    locator: Locator  # 答案单元格
    parent_question_id: str | None = None
    status: QuestionStatus = "parsed"

    # —— 解析辅助信息（不进 03 Question，供 question-parser / UI 使用）——
    language: str = "und"
    language_confidence: float = 0.0
    text_locator: Locator | None = None  # 题干所在单元格
    unit_locator: Locator | None = None
    comment_locator: Locator | None = None
    section: str | None = None
    period_type_hint: str | None = None  # 02 §3.3
    answer_format_confidence: float = 0.5
    enum_source: str | None = None  # data_validation:<formula> / header
    existing_value: Any = None
    exact_mappings: list[TemplateMappingHit] = Field(default_factory=list)
    hints: dict[str, Any] = Field(default_factory=dict)

    def to_question_fields(self, request_id: str) -> dict[str, Any]:
        """生成 03 Question 的业务字段（不含通用信封字段）。"""
        d: dict[str, Any] = {
            "request_id": request_id,
            "ordinal": self.ordinal,
            "text": dict(self.text),
            "answer_format": self.answer_format,
            "locator": self.locator.to_json(),
            "status": self.status,
        }
        if self.native_id:
            d["native_id"] = self.native_id
        if self.options:
            d["options"] = list(self.options)
        if self.unit_requested:
            d["unit_requested"] = self.unit_requested
        if self.period_requested:
            d["period_requested"] = self.period_requested.to_json()
        if self.parent_question_id:
            d["parent_question_id"] = self.parent_question_id
        return d


class SheetLayout(BaseModel):
    sheet: str
    header_row: int | None = None
    columns: dict[str, str] = Field(default_factory=dict)  # role -> column letter
    question_count: int = 0
    skipped_reason: str | None = None


class TemplateDetection(BaseModel):
    template_id: str
    template_version: str
    template_mapping: str  # tm:<id>@<version>
    confidence: float
    matched_markers: list[str] = Field(default_factory=list)

    @property
    def is_exact(self) -> bool:
        return self.confidence >= 0.85


class ParsedQuestionnaire(BaseModel):
    source_path: str
    blob_sha256: str
    mime: str
    language: str = "und"
    template: TemplateDetection | None = None
    questions: list[QuestionDraft] = Field(default_factory=list)
    sheets: list[SheetLayout] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def template_ref(self) -> str | None:
        return self.template.template_mapping if self.template and self.template.is_exact else None

    def by_locator(self) -> dict[str, QuestionDraft]:
        return {q.locator.key: q for q in self.questions}

    def by_native_id(self) -> dict[str, QuestionDraft]:
        return {q.native_id: q for q in self.questions if q.native_id}


class Quantity(BaseModel):
    """02 §3.6 清洗后的带单位数字。value 为 Decimal；区间/上下限不给 value。"""

    raw: str
    value: Decimal | None = None
    unit: str | None = None
    unit_raw: str | None = None
    number_style: Literal["de", "en", "plain", "ch", "space"] | None = None
    span: tuple[int, int] = (0, 0)
    is_range: bool = False
    comparator: str | None = None  # "<", "<=", ">", ">=", "~"
    range_text: str | None = None
    is_estimate: bool = False
    ambiguous: bool = False
    needs_confirmation: bool = False
    reasons: list[str] = Field(default_factory=list)
    alternatives: list[Decimal] = Field(default_factory=list)

    @property
    def dec_str(self) -> str | None:
        from .numbers import to_dec_str

        return None if self.value is None else to_dec_str(self.value)


class EvidenceCandidate(BaseModel):
    """03 §3.5 Evidence 的候选（`status=candidate`，由 evidence-miner 晋升）。"""

    source_type: Literal["document"] = "document"
    fact_key: str | None = None
    datapoint_id: str | None = None
    extracted_value: str | bool | dict[str, Any] | None = None  # 数字为 DEC-STR
    value_type: Literal["number", "text", "enum", "bool", "date", "file", "geo"] = "number"
    unit: str | None = None
    period: Period | None = None
    locator: Locator
    confidence: float = Field(ge=0, le=1)
    status: Literal["candidate"] = "candidate"
    is_estimate: bool = False
    needs_confirmation: bool = False
    reasons: list[str] = Field(default_factory=list)
    label: str | None = None  # 邻近标签（用于 fact_key 判定，属于外部文本）
    excerpt: str | None = None  # 原文片段（外部文本，按数据处理）

    def to_evidence_fields(self, boundary: str = "organization") -> dict[str, Any]:
        d: dict[str, Any] = {
            "source_type": self.source_type,
            "fact_key": self.fact_key or "unclassified.quantity",
            "extracted_value": self.extracted_value,
            "value_type": self.value_type,
            "boundary": boundary,
            "locator": self.locator.to_json(),
            "confidence": self.confidence,
            "status": self.status,
        }
        if self.unit:
            d["unit"] = self.unit
        if self.datapoint_id:
            d["datapoint_id"] = self.datapoint_id
        if self.period:
            d["period"] = self.period.to_json()
        return d


class CellChange(BaseModel):
    locator: str
    sheet: str | None
    cell: str
    old_value: Any = None
    new_value: Any = None
    number_format: str | None = None
    status: Literal["written", "unchanged", "rejected", "skipped"] = "written"
    code: str | None = None
    message: str | None = None
    question_ordinal: int | None = None
    native_id: str | None = None


class FillReport(BaseModel):
    src_path: str
    out_path: str
    src_sha256: str
    out_sha256: str | None = None
    export_input_hash: str
    changes: list[CellChange] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def written(self) -> list[CellChange]:
        return [c for c in self.changes if c.status == "written"]

    @property
    def rejected(self) -> list[CellChange]:
        return [c for c in self.changes if c.status == "rejected"]

    @property
    def ok(self) -> bool:
        return not self.rejected

    def to_markdown(self) -> str:
        lines = [
            f"# 回填报告",
            "",
            f"- 源文件：`{self.src_path}`（{self.src_sha256}）",
            f"- 输出：`{self.out_path}`（{self.out_sha256}）",
            f"- export_input_hash：`{self.export_input_hash}`",
            f"- 写入 {len(self.written)}，拒绝 {len(self.rejected)}，"
            f"未变 {sum(c.status == 'unchanged' for c in self.changes)}",
            "",
            "| 状态 | 单元格 | 题号 | 原值 | 新值 | 代码 | 说明 |",
            "|---|---|---|---|---|---|---|",
        ]
        for c in self.changes:
            ref = f"{c.sheet}!{c.cell}" if c.sheet else c.cell
            lines.append(
                f"| {c.status} | {ref} | {c.native_id or ''} | {_md(c.old_value)} | {_md(c.new_value)} "
                f"| {c.code or ''} | {_md(c.message)} |"
            )
        for w in self.warnings:
            lines.append(f"\n> 警告：{w}")
        return "\n".join(lines) + "\n"


def _md(v: Any) -> str:
    if v is None:
        return ""
    return str(v).replace("|", "\\|").replace("\n", " ")[:80]


class ExportResult(BaseModel):
    """导出器统一返回：文档 + 导出输入哈希 + 校验结果。"""

    format: str
    document: Any = None  # dict（JSON 导出）或 None（文件导出）
    out_path: str | None = None
    export_input_hash: str
    warnings: list[str] = Field(default_factory=list)
    omitted_fields: dict[str, str] = Field(default_factory=dict)

    def to_json(self, indent: int | None = 2) -> str:
        from .export.common import dumps_json

        return dumps_json(self.document, indent=indent)
