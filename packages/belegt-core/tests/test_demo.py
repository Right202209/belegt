# Spec: CLI `belegt demo`（100 题 OEM 问卷：收件→解析映射→取证→计算→起草→审计→审批→回填→提交→归档）、03 §7（审计包核验）、
#       01 UI-04 / 04 §2.2（上传：按扩展名识别问卷格式）
"""端到端演示：``run_demo`` 跑到 submitted/archived；数值、回填与审计包都核对。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from agentkit import csv_path, new_env, xlsx_path
from belegt_core.auditpack import verify_audit_pack
from belegt_core.demo import run_demo


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    xlsx_path()
    out = tmp_path_factory.mktemp("demo")
    env = new_env()
    s = run_demo(out=out, env=env)
    return s, out, env


def test_demo_reaches_submitted(demo):
    s, out, env = demo
    assert s.workflow_state == "completed" and s.request_status == "archived"
    assert "submitted" in s.reached and s.reached[-2:] == ["submitted", "archived"]
    assert s.questions["total"] == 100 and s.questions["mapped"] == 100
    assert s.answers["by_status"] == {"submitted": s.answers["total"]}
    assert s.answers["numbers"] == s.answers["numbers_with_ref"]  # FR-401：每个数字都有证据/计算
    assert s.approvals["pending"] == 0
    assert s.emails_sent == 1 and s.supplier_requests[0]["status"] == "reviewed"
    assert Decimal(s.llm["cost_eur"]) < Decimal(s.llm["target_eur_p95"])  # 10 §4：P95 ≤ €3
    assert s.llm["budget_warnings"] == []


def test_demo_scope_values(demo):
    s, *_ = demo
    assert s.scope == {"scope1": "481.92 tCO2e", "scope2_location": "1658.08 tCO2e", "scope2_market": "2027.2 tCO2e"}


def test_demo_conflicts_handled(demo):
    s, *_ = demo
    by_rule = {c["rule_id"]: c for c in s.conflicts}
    assert by_rule["CR-032"]["status"] == "resolved" and by_rule["CR-032"]["severity"] == "blocking"
    assert by_rule["CR-007"]["status"] == "resolved"
    assert not [c for c in s.conflicts if c["severity"] == "blocking" and c["status"] == "open"]
    assert by_rule["CR-004"]["severity"] == "warning"  # 证书即将到期：warn，审批时带处置说明


def test_demo_fill_back_and_audit_pack(demo):
    import openpyxl

    s, out, env = demo
    assert s.export["rejected"] == []
    assert s.export["written"] == s.answers["questions_answered"]
    assert s.export["written"] >= 86  # 目标下限（实际 92：8 道题确无数据 → 待人工）
    assert s.export["auditpack_verified"] is True and s.export["auditpack_problems"] == []
    filled = Path(s.outputs["filled_file"])
    pack = Path(s.outputs["auditpack"])
    assert filled.exists() and pack.exists() and (out / "summary.json").exists() and (out / "summary.txt").exists()
    ver = verify_audit_pack(pack.read_bytes())  # 独立再核验一次
    assert ver["ok"], ver.get("problems")
    # 回填的 Scope 1 单元格 = 计算结果（按 Question.locator 定位）
    st, org = env.store, env.org_id
    qs = {q["id"]: q for q in st.query("Question", org_id=org, request_id=s.request_id)}
    s1 = next(a for a in st.query("Answer", org_id=org, status="submitted") if a["datapoint_id"].startswith("dp:env.ghg.scope1_total")
              and set(a["question_refs"]) & set(qs))
    loc = qs[s1["question_refs"][0]]["locator"]
    wb = openpyxl.load_workbook(filled, data_only=True)
    cell = wb[loc["sheet"]][loc["cell"]].value
    assert Decimal(str(cell)) == Decimal("481.92")


def test_csv_upload_by_extension_parses_questions():
    """浏览器给 .csv 报 application/vnd.ms-excel / octet-stream 时，按扩展名识别 → 解析出 15 题。"""
    from fastapi.testclient import TestClient

    from belegt_core.app_factory import Settings, create_full_app

    env = new_env()
    app = create_full_app(Settings(services_factory="none"), services=env)
    c = TestClient(app)
    data = csv_path().read_bytes()
    for i, ctype in enumerate(("application/vnd.ms-excel", "application/octet-stream")):
        if i:
            env = new_env()
            c = TestClient(create_full_app(Settings(services_factory="none"), services=env))
        r = c.post("/requests", headers={"Authorization": "Bearer demo-owner"},
                   files={"file": ("lieferantenfragebogen.csv", data, ctype)},
                   data={"subject": "Fragebogen 2025", "sender": "einkauf@oem-a.example"})
        assert r.status_code == 201, r.text
        req_id = r.json()["request"]["id"]
        qs = c.get(f"/requests/{req_id}/questions", headers={"Authorization": "Bearer demo-owner"}).json()
        assert len(qs["items"] if isinstance(qs, dict) else qs) == 15
        assert env.store.get_blob(env.org_id, r.json()["blob_sha256"][0])[1] == "text/csv"
    b = c.post("/blobs", headers={"Authorization": "Bearer demo-owner"}, files={"file": ("x.csv", data, "application/vnd.ms-excel")})
    assert b.status_code == 201 and b.json()["mime"] == "text/csv" and b.json()["blob_sha256"] == b.json()["sha256"]


def test_demo_csv_until_in_review():
    from belegt_core.demo import build_env

    s = run_demo(env=build_env(), path=csv_path(), until="in_review")
    assert s.workflow_state == "in_review" and s.questions["total"] == 15 and s.approvals["pending"] > 0
