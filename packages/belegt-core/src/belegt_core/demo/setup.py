# Spec: CLI `belegt demo`（收件→解析映射→取证→计算→起草→审计→待审批）；演示租户（虚构）
"""演示环境：GraphStore + guard + MockLLM + egress + 连接器 + 主数据 + 历史答卷 + 文档。

``build_env()`` 返回 ``DemoEnv``（含 ``svc``、``runner``、``tokens``、ID 表），供 CLI、API 与测试共用。
"""
from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import yaml

from ..agents.base import OrgConfig, Services, build_registry, latest_ref
from ..agents.calc_service import ensure_factor_set
from ..agents.connectors import DocumentIndex, LocalERPConnector
from ..api.auth import StaticTokenTable
from ..authz import Actor
from ..datapoints import DataPointInfo
from ..egress import CoreEgressGateway, MemoryTransport, StaticTemplateRegistry, supplier_templates
from ..guard import Guard, GuardConfig, TraceRecorder
from ..llm.base import LLMClient
from ..llm.cost import CostLedger
from ..llm.mock import MockLLM
from ..store import GraphStore, WriteItem
from ..workflow.local import LocalRunner

EXAMPLES_DIRS = [Path(__file__).resolve().parents[5] / "examples", Path.cwd() / "examples"]
DEMO_NOW = dt.datetime(2026, 11, 3, 9, 30, tzinfo=dt.timezone.utc)  # 周二工作时间（避开 egress 静默期）


def load_tenant() -> dict[str, Any]:
    return yaml.safe_load(resources.files("belegt_core.demo").joinpath("tenant.yaml").read_text("utf-8"))


def find_example(name: str) -> Path:
    for d in EXAMPLES_DIRS:
        if (d / name).exists():
            return d / name
    raise FileNotFoundError(name)


class SteppingClock:
    """确定性时钟：每次读取前进 1 ms（保证版本时间戳严格递增）。``advance()`` 模拟时间流逝。"""

    def __init__(self, start: dt.datetime = DEMO_NOW) -> None:
        self.t = start

    def __call__(self) -> dt.datetime:
        self.t = self.t + dt.timedelta(milliseconds=1)
        return self.t

    def advance(self, **kw: float) -> None:
        self.t = self.t + dt.timedelta(**kw)


@dataclass
class DemoEnv:
    svc: Services
    runner: LocalRunner
    tokens: StaticTokenTable
    ids: dict[str, str]
    tenant: dict[str, Any]
    transport: MemoryTransport
    clock: Any = None
    ledger: CostLedger = field(default_factory=CostLedger)

    @property
    def store(self) -> GraphStore:
        return self.svc.store

    @property
    def org_id(self) -> str:
        return self.svc.org_id

    def user(self, name: str = "owner") -> Actor:
        u = next(x for x in self.tenant["users"] if x["id"] == f"user:u_{name}")
        return Actor.of(u["id"], self.org_id, tuple(u["roles"]))


def register_custom_datapoints(reg: Any, items: list[dict[str, Any]]) -> None:
    for c in items:
        base = c["id"]
        reg.register(DataPointInfo(
            id=base, semver="1.0.0", value_type=c["vt"], unit=c.get("unit"), dimension=None,
            allowed_boundaries=("organization",), consolidation=None, requires_consolidation=False,
            sensitivity="customer_shared", allowed_units=(c["unit"],) if c.get("unit") else (), status="active",
            extra={"label": {"de": c["de"], "en": c["en"]}, "aliases": list(c.get("aliases") or []),
                   "accepted_source_types": ["document", "erp", "historical_answer", "human_confirmation"],
                   "requires_calculation": False, "tenant_custom": True}))


def _register_xlsx(rows: list[list[Any]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Register"
    for r in rows:
        ws.append([r[0], r[1], r[2], r[3] if len(r) > 3 else None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def import_historical(store: GraphStore, org: str, bodies: list[dict[str, Any]], user: Actor, request_id: str) -> list[str]:
    """历史答卷导入（FR-201）：draft → in_review + Approval → approved（人）→ submitted，保持 R-APR/R-REF 完整链路。"""
    router, filler = Actor.of("agent:approval-router", org), Actor.of("agent:filler", org)
    refs = store.write_batch([WriteItem("Answer", b) for b in bodies], actor=user)
    out = []
    for r in refs:
        aid = r.split("#")[0]
        a_ref, apr_ref = store.write_batch([
            WriteItem("Answer", {"id": aid, "status": "in_review", "risk_level": "L1"}, expected_version=1, patch_mode="merge_from_head"),
            WriteItem("Approval", {"subject_ref": f"{aid}#v2", "risk_level": "L1", "required_role": ["compliance_owner"],
                                   "policy_version": "approval-policy@1.0.0", "status": "pending"})], actor=router)
        apr_id = apr_ref.split("#")[0]
        store.write_batch([
            WriteItem("Approval", {"id": apr_id, "status": "approved", "decision": "approved", "decided_by": user.id,
                                   "decided_at": "2026-06-30T09:00:00Z"}, expected_version=1, patch_mode="merge_from_head", actor=user),
            WriteItem("Answer", {"id": aid, "status": "approved", "approval_ref": f"{apr_id}#v2"}, expected_version=2,
                      patch_mode="merge_from_head", actor=router)], actor=user)
        out.append(store.write("Answer", {"id": aid, "status": "submitted",
                                          "submission": {"request_id": request_id, "channel": "file_export",
                                                         "submitted_at": "2026-06-30T10:00:00Z", "export_input_hash": "sha256:" + "0" * 64}},
                               expected_version=3, actor=filler, patch_mode="merge_from_head"))
    return out


def build_env(*, llm: LLMClient | None = None, url: str = "sqlite+pysqlite:///:memory:", clock: Callable[[], dt.datetime] | None = None,
              guard_config: GuardConfig | None = None, with_supplier: bool = True) -> DemoEnv:
    t = load_tenant()
    reg = build_registry()
    register_custom_datapoints(reg, t["custom_datapoints"])
    clk = clock or SteppingClock()
    store = GraphStore(url, datapoints=reg, clock=clk)
    ledger = CostLedger()
    guard = Guard(store, ledger=ledger, recorder=TraceRecorder(store), config=guard_config)
    store.policy_hook = guard.policy_hook
    org = t["org"]["id"]
    admin = Actor.of("user:u_owner", org, ("compliance_owner", "org_admin"))
    store.write("Organization", {"id": org, "legal_name": t["org"]["legal_name"], "country": t["org"]["country"]}, actor=admin)
    cust = store.write("Counterparty", {"role": "customer", **t["customer"]}, actor=admin).split("#")[0]
    cust_b = store.write("Counterparty", {"role": "customer", **t["other_customer"]}, actor=admin).split("#")[0]
    sup = store.write("Counterparty", {"role": "supplier", **t["supplier"]}, actor=admin).split("#")[0]
    site = store.write("Site", {**t["site"]}, actor=admin).split("#")[0]
    prod = store.write("Product", {**t["product"], "site_ids": [site]}, actor=admin).split("#")[0]
    fs = ensure_factor_set(store, "fs:belegt-example@2026.10")
    tokens = StaticTokenTable()
    for u in t["users"]:
        tokens.add(u["token"], u["id"], org, tuple(u["roles"]))
    tokens.add("demo-workflow", "service:workflow", org, ())
    cfg = OrgConfig(org_id=org, default_product_id=prod, default_site_id=site, company_name=t["org"]["legal_name"],
                    sender_name="Dr. Anna Becker", sender_role="Nachhaltigkeit",
                    supplier_for_datapoints={d: sup for d in t["supplier_datapoints"]} if with_supplier else {})
    templates = supplier_templates() or StaticTemplateRegistry()
    transport = MemoryTransport()
    egress = CoreEgressGateway(store, templates, transport, guard=guard,
                               allowed_link_domains=["supplier.belegt.example"], internal_domains=["musterteile.example"])
    docs = DocumentIndex(store, org)
    erp = LocalERPConnector("demo", [{**r, "period": t["period"]} for r in t["erp_records"]], fetched_at=None)
    svc = Services(store=store, llm=llm or MockLLM(), guard=guard, org=cfg, egress=egress,
                   connectors={"erp": erp, "docs": docs}, supplier_templates=templates)
    # 文档：电费年账单（belegt-io 示例 PDF）+ 证书/指标登记表
    try:
        docs.add(find_example("stromrechnung_2025.pdf").read_bytes(), "application/pdf", "Jahresabrechnung Strom 2025", "bill")
    except FileNotFoundError:  # pragma: no cover
        pass
    docs.add(_register_xlsx(t["register_rows"]), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "Zertifikats- und Kennzahlenregister", "register")
    # 历史答卷（L0 复用来源）：导入为已提交答案
    items = []
    cfg_consolidation = cfg.consolidation
    for dp, val in t["historical_answers"].items():
        ref = latest_ref(reg, dp)
        info = reg.get(ref)
        if info is None:
            continue
        ab = info.allowed_boundaries
        boundary = "organization" if "organization" in ab else f"product:{prod}" if "product" in ab else \
            "product_per_unit" if "product_per_unit" in ab else f"site:{site}"
        body: dict[str, Any] = {"datapoint_id": ref, "value": val, "value_type": info.value_type, "period": t["period"],
                                "boundary": boundary, "customer_scope": [cust_b], "origin": "imported", "is_estimate": False,
                                "legal_commitment": False, "status": "draft"}
        if info.value_type == "text":
            body["text_lang"] = "de"
        if boundary == "organization" and info.requires_consolidation:
            body["consolidation"] = cfg_consolidation
        items.append(body)
    hist_req = store.write("Request", {"status": "received", "counterparty_id": cust_b, "channel": "upload",
                                       "title": "Fragebogen OEM-B 2025 (Import)", "language": "de", "priority": "normal",
                                       "triage_confidence": 1.0, "source_ref": {"blob_sha256": []}}, actor=admin).split("#")[0]
    import_historical(store, org, items, admin, hist_req)
    runner = LocalRunner(svc, sleep=lambda s: None)
    ids = {"org": org, "customer": cust, "other_customer": cust_b, "supplier": sup, "site": site, "product": prod, "factor_set": fs}
    return DemoEnv(svc=svc, runner=runner, tokens=tokens, ids=ids, tenant=t, transport=transport, clock=clk, ledger=ledger)
