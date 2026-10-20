"""测试辅助：03 §5.6 串联实例的构造与通用 Actor。"""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path

from belegt_core import Actor, GraphStore, WriteItem
from belegt_core.canonical import sha256_tag

FIX = json.loads((Path(__file__).parent / "fixtures" / "doc_5_6_chain.json").read_text("utf-8"))

ORG = "org_01JC0A1B2C3D4E5F6G7H8J9K0M"
ORG_B = "org_01JZZZZZZZZZZZZZZZZZZZZZZZ"
CP = "cp_01JC2B3C4D5E6F7G8H9J0K1M2N"
FS_ID = "fs:aib-residual-mix@2025.1"
ROWS_HASH = "sha256:35f78fd009ad6f51b4973c5a50e77fdf25d81dfaa58395010609c6a7e3b1b460"


def A(id: str, *roles: str, org: str = ORG, **kw) -> Actor:
    return Actor.of(id, org, roles, **kw)


ADMIN = A("user:u_admin", "org_admin")
CO = A("user:u_compliance01", "compliance_owner")
EXEC = A("user:u_exec01", "executive")
SALES = A("user:u_sales01", "sales", cp_scope=(CP,))
AUDITOR = A("user:u_auditor", "auditor_readonly")
CONTRIB = A("user:u_contrib01", "contributor")
INTAKE = A("agent:intake-triage")
PARSER = A("agent:question-parser")
GUARD = A("agent:guard")
MINER = A("agent:evidence-miner")
CALC = A("agent:calc-service")
DRAFTER = A("agent:answer-drafter")
ROUTER = A("agent:approval-router")
AUDITOR_AGENT = A("agent:consistency-auditor")
CHASER = A("agent:upstream-chaser")
EGRESS = A("agent:egress-gateway")
REVIEWER = A("agent:supplier-reply-reviewer")
FILLER = A("agent:filler")
WORKFLOW = A("service:workflow")
FACTOR_IMPORT = A("service:factor-import", org="org_global")


class StepClock:
    """确定性时钟：每次调用前进 1 秒。"""

    def __init__(self, start: str = "2026-11-03T08:00:00+00:00") -> None:
        self.t = dt.datetime.fromisoformat(start)

    def __call__(self) -> dt.datetime:
        self.t += dt.timedelta(seconds=1)
        return self.t

    def set(self, iso: str) -> None:
        self.t = dt.datetime.fromisoformat(iso)


def doc(name: str) -> dict:
    return copy.deepcopy(FIX[name])


def strip_env(o: dict) -> dict:
    """去掉服务端字段，作为写入请求体。"""
    o = copy.deepcopy(o)
    for k in ("version", "created_at", "content_hash", "created_by"):
        o.pop(k, None)
    return o


def setup_master(store: GraphStore) -> None:
    store.write("Organization", {"id": ORG, "legal_name": "Musterteile GmbH", "country": "DE",
                                 "default_consolidation": "operational_control", "data_residency": "eu",
                                 "locale_default": "de"}, actor=ADMIN)
    store.write("Counterparty", {"id": CP, "role": "customer", "legal_name": "OEM-A AG", "country": "DE",
                                 "domains": ["oem-a.example"]}, actor=ADMIN)
    if store.head(FS_ID, org_id="org_global") is None:
        store.write("FactorSet", {"id": FS_ID, "org_id": "org_global", "source": "aib-residual-mix",
                                  "license": "CC-BY-4.0", "published_at": "2025-05-30", "valid_from": "2025-01-01",
                                  "valid_until": "2025-12-31", "row_count": 2, "rows_hash": ROWS_HASH,
                                  "status": "active"}, actor=FACTOR_IMPORT)
        store.put_factor_rows(FS_ID, [
            {"factor_key": "residual_mix.electricity.DE", "geography": "DE", "year": "2024", "value": "0.56",
             "unit": "tCO2e/MWh", "source_row_id": "DE-2024-RM"},
            {"factor_key": "contractual.electricity.goo", "geography": "GLO", "year": "2025", "value": "0",
             "unit": "tCO2e/MWh", "source_row_id": "GOO-ZERO"},
        ])


def build_doc_chain(store: GraphStore) -> dict[str, str]:
    """按 03 §5.6 时间线写入 9 个对象及其前序版本；返回各文档对象写入后的 ObjectRef 与 content_hash。"""
    setup_master(store)
    out: dict[str, str] = {}
    R = {}

    # Request v1（intake-triage, quarantine, received）→ v2（guard 晋升 PROMO-REQ-0.90, triaged）
    req = strip_env(doc("request"))
    v1 = {**req, "trust_zone": "quarantine", "status": "received", "supersedes": None,
          "provenance": "trc_01JD8Y3K5M2N4P6Q8R0S2T4V6Z"}
    store.write("Request", v1, actor=INTAKE)
    v2 = {**v1, "trust_zone": "trusted", "status": "triaged",
          "promotion": {"rule": "PROMO-REQ-0.90", "schema_version": "belegt.graph/2026-10",
                        "checked_at": "2026-11-03T08:14:01Z"}}
    v2.pop("supersedes")
    store.write("Request", v2, expected_version=1, actor=GUARD)

    # Question v1（人工录入 trusted parsed，见 README"与文档出入"）
    q = strip_env(doc("question"))
    qv1 = {**q, "status": "parsed"}
    qv1.pop("supersedes")
    store.write("Question", qv1, actor=CO)
    # Request v3（T04 triaged→parsed，service:workflow，= 文档 §5.6.2）
    R["request"] = store.write("Request", {k: v for k, v in req.items() if k != "supersedes"} | {"supersedes": req["supersedes"]},
                               expected_version=2, actor=WORKFLOW)
    # Mapping v1（question-parser, quarantine candidate）
    m = strip_env(doc("mapping"))
    mv1 = {k: v for k, v in m.items() if k not in ("promotion", "supersedes")}
    mv1.update(trust_zone="quarantine", status="candidate")
    store.write("Mapping", mv1, actor=PARSER)
    # 同事务：Mapping v2（guard 晋升 confirmed）+ Question v2（guard parsed→mapped）
    res = store.write_batch([
        WriteItem("Mapping", m, expected_version=1),
        WriteItem("Question", q, expected_version=1),
    ], actor=GUARD)
    R["mapping"], R["question"] = res
    # Evidence（电费单）v1 candidate → v2 verified（EV-AUTO-0.90-LOC）
    e1 = strip_env(doc("evidence_bill"))
    e1v1 = {k: v for k, v in e1.items() if k not in ("verification", "supersedes")} | {"status": "candidate"}
    store.write("Evidence", e1v1, actor=MINER)
    R["evidence_bill"] = store.write("Evidence", e1, expected_version=1, actor=MINER)
    # Evidence（绿证）v1 candidate；ClosedQuestion v1 open
    e2 = strip_env(doc("evidence_hkn"))
    e2v1 = {k: v for k, v in e2.items() if k not in ("verification", "supersedes")} | {"status": "candidate"}
    store.write("Evidence", e2v1, actor=MINER)
    cq = strip_env(doc("closed_question"))
    cqv1 = {k: v for k, v in cq.items() if k not in ("answer", "supersedes")} | {"status": "open"}
    store.write("ClosedQuestion", cqv1, actor=MINER)
    # 同事务：CQ v2 answered + Evidence v2 verified（closed_question）
    R["closed_question"], R["evidence_hkn"] = store.write_batch([
        WriteItem("ClosedQuestion", cq, expected_version=1),
        WriteItem("Evidence", e2, expected_version=1),
    ], actor=CO)
    # Calculation v1
    R["calculation"] = store.write("Calculation", strip_env(doc("calculation")), actor=CALC)
    # Answer v1 draft（calc-service）→ v2 in_review（approval-router）；Approval v1 pending
    ans = strip_env(doc("answer"))
    av1 = {k: v for k, v in ans.items() if k not in ("approval_ref", "risk_level", "supersedes", "answer_key")}
    av1["status"] = "draft"
    store.write("Answer", av1, actor=CALC)
    av2 = dict(av1, status="in_review", risk_level="L3")
    store.write("Answer", av2, expected_version=1, actor=ROUTER)
    apr = strip_env(doc("approval"))
    aprv1 = {k: v for k, v in apr.items() if k not in ("decision", "decided_by", "decided_at", "comment", "supersedes")}
    aprv1["status"] = "pending"
    store.write("Approval", aprv1, actor=ROUTER)
    # 同事务：Approval v2（user 批准）+ Answer v3（approved）
    R["approval"], R["answer"] = store.write_batch([
        WriteItem("Approval", apr, expected_version=1, actor=CO),
        WriteItem("Answer", ans, expected_version=2, actor=ROUTER),
    ], actor=ROUTER)
    return R


def blob_sha(b: bytes) -> str:
    return sha256_tag(b)
