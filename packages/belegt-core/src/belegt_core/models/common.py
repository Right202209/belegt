# Spec: 03 §2.1（通用字段）、§2.2（值表示）、§3.5（Locator）
"""通用类型与信封基类。

约定：
- 数字字段用 ``DecStr``（十进制字符串，接受 ``Decimal``/``int`` 并规范化）。
- 日期/时间字段保留客户端原字符串（``DateStr``/``DateTimeStr``），以保证 ``content_hash`` 可复现。
- ``to_body()`` 输出 JSON 兼容 dict（去掉值为 None 的字段；null 不参与哈希，见 canonical）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from ..canonical import normalize_dec

SCHEMA_VERSION = "belegt.graph/2026-10"
ULID = r"[0-9A-HJKMNP-TV-Z]{26}"


def _to_dec(v: Any) -> Any:
    if isinstance(v, (Decimal, int)) and not isinstance(v, bool):
        return normalize_dec(v)
    if isinstance(v, str):
        return normalize_dec(v)
    return v


DecStr = Annotated[str, BeforeValidator(_to_dec), StringConstraints(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", max_length=40)]
DateStr = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
DateTimeStr = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")]
Sha256 = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
ObjectRefStr = Annotated[str, StringConstraints(pattern=r"^[a-z]+_" + ULID + r"#v[1-9][0-9]*$")]
DataPointRef = Annotated[str, StringConstraints(pattern=r"^dp:[a-z]+(\.[a-z0-9_]+){2,3}@(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")]
Actor = Annotated[str, StringConstraints(pattern=r"^(user:[a-z0-9_]+|agent:[a-z-]+|service:[a-z-]+)$")]
TraceId = Annotated[str, StringConstraints(pattern=r"^trc_" + ULID + "$")]
Boundary = Annotated[str, StringConstraints(pattern=r"^(organization|product_per_unit|site:site_" + ULID + r"|product:prod_" + ULID + r")$")]
I18nText = dict[str, str]
CustomerScope = Literal["all"] | list[str]
TrustZone = Literal["quarantine", "trusted"]
ValueType = Literal["number", "text", "enum", "bool", "date", "file", "geo"]
Consolidation = Literal["operational_control", "financial_control", "equity_share"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=False)

    def to_body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)


class Period(Model):
    start: DateStr
    end: DateStr

    @model_validator(mode="after")
    def _order(self) -> "Period":
        if self.start > self.end:
            raise ValueError("period.start must be <= period.end")
        return self

    def covers(self, other: "Period") -> bool:
        return self.start <= other.start and self.end >= other.end


class ConnectorLocator(Model):
    connector_id: str
    query_hash: Sha256
    record_key: str
    fetched_at: DateTimeStr


class Locator(Model):
    """03 §3.5 Locator：``{blob_sha256, mime, page?, sheet?, cell?, bbox?, text_excerpt_hash?, connector?}``；
    ``human_confirmation`` 用 ``cq_ref``/``approval_ref``。"""

    blob_sha256: Sha256 | None = None
    mime: str | None = None
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    cell: str | None = None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    text_excerpt_hash: Sha256 | None = None
    connector: ConnectorLocator | None = None
    cq_ref: ObjectRefStr | None = None
    approval_ref: ObjectRefStr | None = None


class Promotion(Model):
    rule: str
    schema_version: str
    checked_at: DateTimeStr


class GraphObject(Model):
    """所有证据图对象共有的信封（03 §2.1）。写入时 ``version``/``created_at``/``content_hash`` 可省略。"""

    TYPE: ClassVar[str] = ""
    PREFIX: ClassVar[str] = ""
    HAS_STATUS: ClassVar[bool] = True
    DEFAULT_ZONE: ClassVar[str] = "trusted"

    id: str
    org_id: str | None = None
    version: int | None = Field(default=None, ge=1)
    created_at: DateTimeStr | None = None
    created_by: Actor | None = None
    trust_zone: TrustZone | None = None
    provenance: TraceId | None = None
    supersedes: ObjectRefStr | None = None
    schema_version: str = SCHEMA_VERSION
    content_hash: Sha256 | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    promotion: Promotion | None = None

    @property
    def ref(self) -> str:
        if self.version is None:
            raise ValueError("object has no version yet")
        return f"{self.id}#v{self.version}"
