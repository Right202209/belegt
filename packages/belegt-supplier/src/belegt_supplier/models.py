# Spec: 03 §2.2、§3.5、§3.8、§3.10；08 §6.5（请求清单、提交包 belegt-supplier-submission/1）
"""供应商端的 pydantic 模型。字段名与 03 保持一致；供应商端本地扩展字段单独注明。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .decimal_util import is_dec_str
from .ids import APR_REF_RE, OBJECT_REF_RE, SREQ_ID_RE

SupplierRequestStatus = Literal[
    "draft", "sent", "reminded", "responded", "reviewed", "closed", "escalated", "expired"
]
ValueType = Literal["number", "text", "enum", "bool", "date", "file", "geo"]
SUBMISSION_FORMAT = "belegt-supplier-submission/1"
MANIFEST_FORMAT = "belegt-supplier-manifest/1"


class Period(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date

    @model_validator(mode="after")
    def _order(self) -> "Period":
        if self.start > self.end:
            raise ValueError("period.start must be <= period.end")
        return self

    def covers(self, other: "Period") -> bool:
        return self.start <= other.start and self.end >= other.end


class DataPointSpec(BaseModel):
    """表单卡片所需的 DataPoint 摘要（权威定义在 02 / belegt-dictionary）。"""
    model_config = ConfigDict(extra="ignore")
    datapoint_id: str
    value_type: ValueType = "number"
    unit: str | None = None
    allowed_units: list[str] = Field(default_factory=list)
    enum_values: list[str] = Field(default_factory=list)
    scope: Literal["product", "organization"] = "product"
    label: dict[str, str] = Field(default_factory=dict)
    help: dict[str, str] = Field(default_factory=dict)
    example: str | None = None
    required: bool = True
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"

    @property
    def base_id(self) -> str:
        return self.datapoint_id.split("@", 1)[0]

    def label_for(self, locale: str) -> tuple[str, bool]:
        """返回 (label, is_fallback)。缺译回退 en（08 §3.2 `datapoint_list`）。"""
        if locale in self.label:
            return self.label[locale], False
        if "en" in self.label:
            return self.label["en"], True
        return self.base_id, True


class ProductRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    product_id: str
    name: str
    cn_code: str | None = None
    net_mass: str | None = None  # DEC-STR
    net_mass_unit: str | None = "t"
    country_of_origin: str | None = None


class ReferenceData(BaseModel):
    """供应商端即时提示所需的公开参考数据（不含客户私有数据，08 §6.4）。"""
    model_config = ConfigDict(extra="allow")
    cbam_defaults: dict[str, str] = Field(default_factory=dict)  # cn_code -> DEC-STR tCO2e/t（不含加价）
    grid_factors: dict[str, str] = Field(default_factory=dict)  # ISO country -> DEC-STR tCO2e/MWh
    pcf_benchmarks: dict[str, str] = Field(default_factory=dict)  # material -> DEC-STR kgCO2e/kg


class RequestManifest(BaseModel):
    """客户侧推送的签名请求清单（08 §6.5）。"""
    model_config = ConfigDict(extra="ignore")
    format: Literal["belegt-supplier-manifest/1"] = MANIFEST_FORMAT
    sreq_ref: str
    org_id: str
    counterparty_id: str
    customer_name: str
    supplier_name: str
    contact_email: str
    contact_name: str | None = None
    locale: str = "en"
    template_id: str | None = None
    datapoints: list[DataPointSpec | str]
    products: list[ProductRef] = Field(default_factory=list)
    period: Period
    due_date: date
    supplier_timezone: str = "UTC"
    supplier_country: str | None = None
    reference: ReferenceData = Field(default_factory=ReferenceData)
    link_ttl_days: int | None = None
    privacy_note_link: str | None = None

    @field_validator("sreq_ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        m = OBJECT_REF_RE.match(v)
        if not m or not SREQ_ID_RE.match(m.group("id")):
            raise ValueError("sreq_ref must be sreq_<ULID>#v<N>")
        return v

    @property
    def sreq_id(self) -> str:
        return self.sreq_ref.split("#", 1)[0]


class SupplierRequest(BaseModel):
    """03 §3.10 SupplierRequest（供应商端本地副本 + 本地扩展字段）。"""
    model_config = ConfigDict(extra="forbid")
    id: str
    org_id: str
    version: int = 1
    counterparty_id: str
    datapoint_ids: list[str] = Field(min_length=1)
    product_ids: list[str] = Field(default_factory=list)
    period: Period
    language: str
    template_id: str
    channel: Literal["email", "supplier_portal"] = "supplier_portal"
    portal_token_hash: str | None = None
    due_date: date
    reminder_count: int = 0
    last_sent_at: datetime | None = None
    trigger_ref: str | None = None
    default_cost_estimate_ref: str | None = None
    status: SupplierRequestStatus = "draft"
    # —— 本地扩展（不属于 03 字段集合）——
    approval_ref: str | None = None
    egress_audit_ref: str | None = None
    first_sent_at: datetime | None = None
    contact_email: str | None = None
    link_opened_at: datetime | None = None
    reminders_paused: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not SREQ_ID_RE.match(v):
            raise ValueError("id must be sreq_<ULID>")
        return v

    @field_validator("approval_ref")
    @classmethod
    def _apr(cls, v: str | None) -> str | None:
        if v is not None and not APR_REF_RE.match(v):
            raise ValueError("approval_ref must be apr_<ULID>#v<N>")
        return v

    @property
    def ref(self) -> str:
        return f"{self.id}#v{self.version}"


class ApprovalInfo(BaseModel):
    """03 §3.8 Approval 的最小视图（由 CoreClient 提供）。"""
    model_config = ConfigDict(extra="ignore")
    ref: str
    subject_ref: str
    status: Literal["pending", "approved", "rejected", "modified"]
    decision: Literal["approved", "rejected", "modified"] | None = None
    decided_by: str | None = None
    policy_version: str | None = None


class CounterpartyInfo(BaseModel):
    """03 §3.12 Counterparty 的最小视图。"""
    model_config = ConfigDict(extra="ignore")
    counterparty_id: str
    name: str
    domains: list[str] = Field(default_factory=list)
    confirmed_addresses: list[str] = Field(default_factory=list)
    country: str | None = None
    timezone: str = "UTC"


class SubmissionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["form", "excel", "reuse"] = "form"
    sheet: str | None = None
    cell: str | None = None


class SubmissionValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    datapoint_id: str
    value: Any
    unit: str | None = None
    period: Period | None = None
    boundary: str
    source: SubmissionSource = Field(default_factory=SubmissionSource)
    note: str | None = None

    @model_validator(mode="after")
    def _numbers_are_dec_str(self) -> "SubmissionValue":
        if isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            raise ValueError("numbers must be DEC-STR strings (03 §2.2)")
        return self


class BlobRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str
    mime: str
    filename: str


class Attestation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: str
    statement_version: str


class SubmissionPackage(BaseModel):
    """`belegt-supplier-submission/1`（08 §6.5）。`signature` 为对其余字段规范化 JSON 的分离 JWS。"""
    model_config = ConfigDict(extra="forbid")
    format: Literal["belegt-supplier-submission/1"] = SUBMISSION_FORMAT
    request_manifest_hash: str
    sreq_ref: str
    submitted_at: datetime
    locale: str
    values: list[SubmissionValue]
    blobs: list[BlobRef] = Field(default_factory=list)
    attestation: Attestation
    signature: str | None = None

    def unsigned_payload(self) -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude={"signature"})
        return d


def check_dec_str_values(values: list[SubmissionValue]) -> list[str]:
    bad = []
    for v in values:
        if isinstance(v.value, str) and v.unit and not is_dec_str(v.value):
            bad.append(v.datapoint_id)
    return bad
