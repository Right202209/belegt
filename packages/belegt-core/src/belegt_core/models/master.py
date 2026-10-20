# Spec: 03 §3.12 主数据 Organization / Counterparty / Site / Product（无状态机，append-only；停用用 tags: inactive）
from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field

from .common import Consolidation, GraphObject


class _Master(GraphObject):
    HAS_STATUS: ClassVar[bool] = False


class Organization(_Master):
    TYPE: ClassVar[str] = "Organization"
    PREFIX: ClassVar[str] = "org_"

    legal_name: str
    country: str = Field(pattern=r"^[A-Z]{2}$")
    vat_id: str | None = None
    eori: str | None = None
    default_consolidation: Consolidation | None = None
    data_residency: Literal["eu", "private"] = "eu"
    locale_default: Literal["de", "en"] = "de"


class Counterparty(_Master):
    TYPE: ClassVar[str] = "Counterparty"
    PREFIX: ClassVar[str] = "cp_"

    role: Literal["customer", "supplier"]
    legal_name: str
    country: str | None = None
    domains: list[str] = Field(default_factory=list)
    portal_ids: list[str] | None = None
    contacts: list[dict[str, Any]] | None = None
    data_sharing_grant: dict[str, Any] | None = None


class Site(_Master):
    TYPE: ClassVar[str] = "Site"
    PREFIX: ClassVar[str] = "site_"

    name: str
    country: str
    address: str | None = None
    geo: dict[str, Any] | None = None
    grid_region: str | None = None
    operational_control: bool | None = None


class Product(_Master):
    TYPE: ClassVar[str] = "Product"
    PREFIX: ClassVar[str] = "prod_"

    name: str
    sku: str | None = None
    cn_code: str | None = Field(default=None, pattern=r"^[0-9]{8}$")
    declared_unit: str | None = None
    site_ids: list[str] | None = None
    cbam_relevant: bool | None = None
