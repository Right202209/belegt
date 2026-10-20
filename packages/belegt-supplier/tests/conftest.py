# Spec: 08 —— 测试夹具（无网络、无 LLM）
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from belegt_supplier.config import Settings
from belegt_supplier.core_client import InMemoryCoreClient
from belegt_supplier.egress import MemoryTransport
from belegt_supplier.ids import new_id
from belegt_supplier.models import ApprovalInfo, CounterpartyInfo, RequestManifest
from belegt_supplier.plausibility import BBoxGeoOracle
from belegt_supplier.service import SupplierService
from belegt_supplier.signing import jws_sign


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


@pytest.fixture
def clock():
    # 2026-11-02 是周一；08:00 UTC = 11:00 Europe/Istanbul
    return Clock(datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc))


@pytest.fixture
def settings(tmp_path):
    return Settings(db_url="sqlite://", data_dir=tmp_path / "data", public_url="https://supplier.example.org",
                    instance_secret=b"i" * 32, manifest_secret=b"m" * 32, webhook_secret=b"w" * 32,
                    admin_key="admin-key", internal_domains=["mueller.example"], extra_link_domains=["mueller.example"])


@pytest.fixture
def core():
    return InMemoryCoreClient()


@pytest.fixture
def transport():
    return MemoryTransport()


@pytest.fixture
def svc(settings, core, transport, clock):
    return SupplierService(settings, core=core, transport=transport, clock=clock, geo_oracle=BBoxGeoOracle())


def make_manifest(**over) -> RequestManifest:
    base = dict(
        sreq_ref=f"{new_id('sreq')}#v1", org_id=new_id("org"), counterparty_id=new_id("cp"),
        customer_name="Müller Stanztechnik GmbH", supplier_name="Anadolu Çelik A.Ş.",
        contact_email="ayse@anadolu-celik.example", contact_name="Ayşe Yılmaz", locale="tr",
        datapoints=["dp:prod.cbam.installation_id", "dp:prod.identity.cn_code", "dp:prod.cbam.production_route",
                    "dp:prod.cbam.see_direct", "dp:prod.cbam.see_indirect"],
        products=[{"product_id": "prod_01JD8Y3K5M2N4P6Q8R0S2T4V6W", "name": "Warmband", "cn_code": "72083900"}],
        period={"start": "2026-01-01", "end": "2026-12-31"}, due_date=date(2026, 11, 23),
        supplier_timezone="Europe/Istanbul", supplier_country="TR",
        reference={"cbam_defaults": {"72083900": "2.00"}},
        privacy_note_link="https://mueller.example/privacy",
    )
    base.update(over)
    return RequestManifest.model_validate(base)


def approval_for(sreq_id: str, version: int = 1, status: str = "approved") -> ApprovalInfo:
    return ApprovalInfo(ref=f"{new_id('apr')}#v1", subject_ref=f"{sreq_id}#v{version}", status=status,
                        decision=None if status == "pending" else status, decided_by="user:anna",
                        policy_version="approval-policy@1.0.0")


def counterparty_for(m: RequestManifest) -> CounterpartyInfo:
    return CounterpartyInfo(counterparty_id=m.counterparty_id, name=m.supplier_name, domains=["anadolu-celik.example"],
                            confirmed_addresses=[m.contact_email], country="TR", timezone="Europe/Istanbul")


@pytest.fixture
def manifest():
    return make_manifest()


@pytest.fixture
def issued(svc, manifest):
    """已发送（manifest 模式）的请求 + 门户 token。"""
    res = svc.ingest_manifest(manifest, status="sent", approval_ref=f"{new_id('apr')}#v1")
    token = res["portal_link"].rsplit("/", 1)[1]
    return manifest, token


def signed_manifest(m: RequestManifest, secret: bytes = b"m" * 32) -> str:
    return jws_sign(m.model_dump(mode="json"), secret)
