# Spec: 11 §2.4（标注类型 LBL-*，与证据图同构的 JSON）、§2.5（双人标注）、§2.7（分层：模板 × 语言 × 格式）、
#       §2.8（版本化）、§2.9（分区 fewshot/dev/test/holdout）；03 §3.5 Locator、§2.2 DEC-STR
"""黄金集标注 schema（pydantic v2）。`belegt-gold`（私有）与 `belegt-bench`（公开）共用。

每条样本 = 信封（`sample_id`、`label_type`、`partition`、`strata`、`cluster_id`、`org`、`synthetic`、`flags`…）
+ 该标注类型的金标准字段（字段名沿用 03 的 `Mapping`/`Evidence`/`Conflict` 等对象）
+ 可选 `annotations`（双人标注原始记录，用于 κ；裁决后的金标准写在顶层字段）。
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ANNOTATION_VERSION = "lbl-2026.10"
LABEL_TYPES = ("LBL-TRIAGE", "LBL-MAP", "LBL-EVID", "LBL-LOC", "LBL-VALID", "LBL-CONFLICT", "LBL-DRAFT", "LBL-ANOM",
               "LBL-INJ", "LBL-PORTAL")
FLAGS = ("OOD", "ambiguous", "date_conflict", "internal_inconsistency", "derived", "synthetic", "hard")
PARTITIONS = ("fewshot", "dev", "test", "holdout")
FORMATS = ("xlsx", "pdf_text", "pdf_scan", "docx", "email_body", "json", "csv")
TEMPLATES = ("vsme", "cdp_climate", "cbam_communication", "cmrt", "pact_pcf", "eudr_dds", "reach_scip",
             "eu_battery_passport", "catenax_pcf")

DEC_STR_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
DP_RE = re.compile(r"^dp:[a-z]+(\.[a-z0-9_]+){2,3}$")
SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

Partition = Literal["fewshot", "dev", "test", "holdout"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Strata(Model):
    template: str  # 9 个公开 template_id 或 private_*
    lang: str
    format: str

    @field_validator("template")
    @classmethod
    def _tpl(cls, v: str) -> str:
        if v not in TEMPLATES and not v.startswith("private_") and v != "none":
            raise ValueError(f"unknown template stratum {v!r}")
        return v

    @field_validator("format")
    @classmethod
    def _fmt(cls, v: str) -> str:
        if v not in FORMATS:
            raise ValueError(f"unknown format {v!r}")
        return v

    @property
    def cell(self) -> str:
        return f"{self.template}×{self.lang}×{self.format}"


class Period(Model):
    start: str
    end: str


class Locator(Model):
    """03 §3.5 Locator 子集（定位标注 LBL-LOC）。"""

    blob_sha256: str | None = None
    mime: str | None = None
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    cell: str | None = None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)


class Annotation(Model):
    """双人标注的一份原始标注（裁决前）。`label` 为该类型的判定值（分类类直接比较；值类比较规范化键）。"""

    annotator: str
    label: Any
    annotated_at: str | None = None
    note: str | None = None


class SampleBase(Model):
    sample_id: str
    label_type: str
    partition: Partition
    strata: Strata
    cluster_id: str  # 问卷/文档/答案集；bootstrap 的簇
    org: str  # 虚拟企业（企业级切分 LEAK-03）
    synthetic: bool = True
    flags: list[str] = Field(default_factory=list)
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    received_at: str | None = None  # 收件日（LEAK-02 时间切分）
    history_refs: list[dict[str, str]] = Field(default_factory=list)  # [{ref, submitted_at}]
    annotation_version: str = ANNOTATION_VERSION
    annotations: list[Annotation] = Field(default_factory=list)
    adjudicated_by: str | None = None
    notes: str | None = None
    canary: str | None = Field(default=None, alias="_canary")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("flags")
    @classmethod
    def _flags(cls, v: list[str]) -> list[str]:
        for f in v:
            if f not in FLAGS:
                raise ValueError(f"unknown flag {f!r}")
        return v


class MapSample(SampleBase):
    """LBL-MAP：每道 Question 的可接受 datapoint 集合（无版本号的 base id），OOD 时为空。"""

    label_type: Literal["LBL-MAP"] = "LBL-MAP"
    question_id: str
    native_id: str | None = None
    text: str
    lang: str
    answer_format: str | None = None
    acceptable: list[str]
    ood: bool = False
    unit_hint: str | None = None
    period_hint: Period | None = None
    boundary_hint: str | None = None
    hard_kind: str | None = None
    template_mapping_ref: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "MapSample":
        for dp in self.acceptable:
            if not DP_RE.match(dp):
                raise ValueError(f"acceptable must be base dp ids, got {dp!r}")
        if self.ood and self.acceptable:
            raise ValueError("OOD sample must have empty acceptable set")
        if not self.ood and not self.acceptable:
            raise ValueError("non-OOD sample needs at least one acceptable datapoint")
        if self.ood and "OOD" not in self.flags:
            self.flags.append("OOD")
        if len(self.acceptable) > 1 and "ambiguous" not in self.flags:
            raise ValueError("multiple acceptable datapoints require the 'ambiguous' flag")
        return self


class Fact(Model):
    """LBL-EVID + LBL-LOC：一个可作为证据的事实（值 DEC-STR），可有多个可接受定位。"""

    fact_key: str
    extracted_value: str
    unit: str | None = None
    period: Period | None = None
    boundary: str = "organization"
    locators: list[Locator]
    flags: list[str] = Field(default_factory=list)

    @field_validator("extracted_value")
    @classmethod
    def _dec(cls, v: str) -> str:
        if not DEC_STR_RE.match(v):
            raise ValueError(f"extracted_value must be DEC-STR, got {v!r}")
        return v


class Validity(Model):
    valid_from: str | None = None
    valid_until: str | None = None  # null 合法：无到期日不得猜测
    derived: bool = False


class EvidSample(SampleBase):
    label_type: Literal["LBL-EVID"] = "LBL-EVID"
    doc_path: str  # 相对数据集根目录
    doc_sha256: str
    mime: str
    doc_kind: str  # electricity_bill / gas_bill / heat_bill / water_bill / certificate / meter_log
    facts: list[Fact] = Field(default_factory=list)
    validity: Validity | None = None  # LBL-VALID（证书类）
    distractor_values: list[str] = Field(default_factory=list)  # 文档中出现但不是事实的数字（用于误报分析）


class AnswerLite(Model):
    """一致性审计用的答案（03 Answer 的子集）。"""

    answer_id: str
    datapoint_id: str
    value: Any
    unit: str | None = None
    period: Period | None = None
    boundary: str = "organization"
    consolidation: str | None = None
    customer_scope: str | None = None  # cp_…；None = 通用
    request_id: str = "req_A"
    status: Literal["draft", "in_review", "approved", "submitted"] = "approved"
    scope2_method: str | None = None
    is_estimate: bool = False
    divergence_note: str | None = None  # accepted_divergence 说明（存在则 CR-001 不触发）
    evidence_refs: list[str] = Field(default_factory=list)
    submitted_at: str | None = None
    question_refs: list[str] = Field(default_factory=list)


class EvidenceLite(Model):
    evidence_id: str
    fact_key: str
    value: str
    unit: str
    period: Period | None = None
    boundary: str = "organization"
    status: str = "verified"
    valid_until: str | None = None
    source_type: str = "document"


class ExpectedConflict(Model):
    rule_id: str
    objects: list[str]
    severity: Literal["block", "warn", "info"] = "block"


class ConflictSample(SampleBase):
    """LBL-CONFLICT：答案集 + 应被检出的冲突（空 = 不应报任何冲突，用于误报率）。"""

    label_type: Literal["LBL-CONFLICT"] = "LBL-CONFLICT"
    answers: list[AnswerLite]
    evidence: list[EvidenceLite] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)  # submit_date, due_date, …
    expected: list[ExpectedConflict] = Field(default_factory=list)
    negative_reason: str | None = None  # 无冲突样本：为什么不是冲突（容差内、已注明口径差异…）


class InjSample(SampleBase):
    """LBL-INJ：外部内容是否含提示词注入及意图类别（红队样本 + 难负例）。"""

    label_type: Literal["LBL-INJ"] = "LBL-INJ"
    channel: Literal["email_body", "supplier_reply", "attachment_text", "portal_page", "questionnaire_cell"]
    text: str
    lang: str
    is_injection: bool
    intent: Literal["none", "action", "approval_manipulation", "exfiltration", "instruction_override",
                    "data_tampering"] = "none"
    technique: str | None = None  # direct / hidden_text / encoding / roleplay / multilingual / delimiter …
    forbidden_actions: list[str] = Field(default_factory=list)  # 被注入诱导的越权动作（guard 必须阻止）

    @model_validator(mode="after")
    def _check(self) -> "InjSample":
        if self.is_injection == (self.intent == "none"):
            raise ValueError("is_injection must agree with intent")
        return self


class CitedValue(Model):
    ref: str
    value: str
    unit: str | None = None
    period: Period | None = None  # 所引对象的期间：其年/月/日也属于允许集合（与 GRAPH_NUM_IN_TEXT 一致）


class DraftSample(SampleBase):
    """LBL-DRAFT（R-NUM 子集）：草稿文本 + 所引对象的值；`expected_new_numbers` 为文本中不在所引对象中的数字。"""

    label_type: Literal["LBL-DRAFT"] = "LBL-DRAFT"
    text: str
    lang: str
    cited: list[CitedValue]
    expected_new_numbers: list[str] = Field(default_factory=list)


SAMPLE_MODELS: dict[str, type[SampleBase]] = {
    "LBL-MAP": MapSample, "LBL-EVID": EvidSample, "LBL-CONFLICT": ConflictSample, "LBL-INJ": InjSample,
    "LBL-DRAFT": DraftSample,
}


def parse_sample(obj: dict[str, Any]) -> SampleBase:
    lt = obj.get("label_type")
    model = SAMPLE_MODELS.get(lt or "")
    if model is None:
        raise ValueError(f"unsupported label_type {lt!r}")
    return model.model_validate(obj)


def json_schemas() -> dict[str, dict[str, Any]]:
    """导出各标注类型的 JSON Schema（随数据集发布于 schema/）。"""
    return {lt: m.model_json_schema(by_alias=True) for lt, m in SAMPLE_MODELS.items()}
