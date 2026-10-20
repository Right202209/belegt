# Spec: 03 §6.8、01、04 §4.3/§6.3、10 §1.3；CONTRACTS（CLI `belegt demo`、package data）
"""``app_factory.create_full_app``、``cli``、``pyproject``（入口与随包数据）。离线。"""
from __future__ import annotations

import fnmatch
import json
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import belegt_core
from belegt_core.app_factory import Settings, create_full_app
from belegt_core.cli import build_parser, main

PKG = Path(belegt_core.__file__).resolve().parent
PYPROJECT = PKG.parents[1] / "pyproject.toml"


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("BELEGT_DB_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("BELEGT_WORKFLOW_ENGINE", "temporal")
    monkeypatch.setenv("BELEGT_DEV_AUTH", "1")
    monkeypatch.setenv("BELEGT_TEMPORAL_OPTIONS", '{"max_timer_s": 0}')
    monkeypatch.delenv("BELEGT_CORS_ORIGINS", raising=False)
    s = Settings.from_env()
    assert s.workflow_engine == "temporal" and s.dev_auth and s.temporal_options == {"max_timer_s": 0}
    assert "http://localhost:5173" in s.cors_origins  # dev 默认放行 Vite
    monkeypatch.setenv("BELEGT_DEV_AUTH", "0")
    assert Settings.from_env().dev_auth is False and Settings.from_env().cors_origins == []


def test_minimal_app_without_services(tmp_path):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"t-owner": {"actor": "user:u_owner", "org_id": "org_01JTEST000000000000000000A",
                                              "roles": ["compliance_owner"]}}))
    app = create_full_app(Settings(services_factory="none", tokens_path=str(tokens), static_dir=str(tmp_path / "missing")))
    c = TestClient(app)
    h = {"Authorization": "Bearer t-owner"}
    assert c.get("/healthz").json()["workflow_engine"] is None
    assert c.get("/requests", headers=h).json() == {"items": []}
    r = c.post("/requests", headers=h, files={"file": ("q.csv", b"a;b\n", "text/csv")})
    assert r.status_code == 404 and "workflow engine not configured" in r.json()["error"]["message"]
    r = c.post("/blobs", headers={**h, "Content-Type": "text/plain"}, content=b"hello")
    assert r.status_code == 201 and r.json()["blob_sha256"] == r.json()["sha256"]  # workflow 版本优先于 core 版本
    assert c.get("/.well-known/agent.json").status_code == 200
    assert c.get("/inbox", headers=h).status_code == 200      # review_router 已挂载
    assert c.get("/graph/audit/verify", headers=h).status_code == 200
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/ui" not in paths  # 无 web/dist


def test_default_demo_services_and_static_and_cors(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Belegt</title>")
    app = create_full_app(Settings(static_dir=str(dist), cors_origins=["http://localhost:5173"], dev_auth=True))
    c = TestClient(app)
    hz = c.get("/healthz").json()
    assert hz["workflow_engine"] == "local" and hz["org_id"].startswith("org_")
    assert c.get("/requests", headers={"Authorization": "Bearer demo-owner"}).status_code == 200  # 演示令牌已合入
    assert c.get("/requests", headers={"X-Belegt-Actor": "user:u_sales"}).status_code == 200
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/ui/"
    assert "Belegt" in c.get("/ui/").text
    pre = c.options("/requests", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
                                          "Access-Control-Request-Headers": "x-belegt-actor"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "http://localhost:5173"
    card = c.get("/.well-known/agent.json").json()
    assert card["name"].startswith("Musterteile GmbH")


def test_temporal_settings_lazy_connect():
    """``workflow_engine=temporal`` 不在启动时连接；连不上时工作流端点返回 503 WORKFLOW_UNAVAILABLE。"""
    app = create_full_app(Settings(workflow_engine="temporal", temporal_address="127.0.0.1:1"))
    c = TestClient(app)
    assert c.get("/healthz").json()["workflow_engine"] == "temporal"
    h = {"Authorization": "Bearer demo-owner"}
    r = c.post("/requests", headers=h, files={"file": ("q.csv", b"Nr.;Frage\n1;Firmenname\n", "text/csv")},
               data={"subject": "Fragebogen 2025"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "WORKFLOW_UNAVAILABLE"


def test_cli_parser_and_asgi_factory(monkeypatch):
    p = build_parser()
    a = p.parse_args(["serve", "--port", "9001", "--engine", "temporal", "--dev-auth"])
    assert a.port == 9001 and a.engine == "temporal" and a.dev_auth
    assert p.parse_args(["worker", "--task-queue", "q1"]).task_queue == "q1"
    assert p.parse_args(["mcp", "--transport", "streamable-http"]).transport == "streamable-http"
    d = p.parse_args(["demo", "--until", "in_review", "--out", "/tmp/x"])
    assert d.until == "in_review" and d.out == "/tmp/x" and d.path is None
    monkeypatch.setenv("BELEGT_SERVICES_FACTORY", "none")
    from belegt_core.cli import asgi_app

    assert TestClient(asgi_app()).get("/healthz").status_code == 200


def test_cli_demo_json(capsys):
    from belegt_core.demo import find_example

    try:
        csv = find_example("oem_fragebogen_kurz.csv")
    except FileNotFoundError:
        pytest.skip("examples 不在")
    assert main(["demo", str(csv), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    rid = out.get("request_id") or out["request"]["id"]  # DemoSummary.to_dict() 或旧 dict 摘要
    assert rid.startswith("req_") and out["questions"]["total"] > 0


def test_pyproject_entry_points_extras_and_package_data():
    cfg = tomllib.loads(PYPROJECT.read_text("utf-8"))
    assert cfg["project"]["scripts"]["belegt"] == "belegt_core.cli:main"
    extras = cfg["project"]["optional-dependencies"]
    assert {"temporal", "mcp", "llm"} <= set(extras)
    assert any(d.startswith("temporalio") for d in extras["temporal"]) and any(d.startswith("mcp") for d in extras["mcp"])
    patterns = cfg["tool"]["setuptools"]["package-data"]["belegt_core"]
    data_files = [f.relative_to(PKG).as_posix() for f in PKG.rglob("*")
                  if f.is_file() and f.suffix in (".yaml", ".yml", ".json") and "__pycache__" not in f.parts]
    assert data_files
    missing = [f for f in data_files if not any(fnmatch.fnmatch(f, pat) for pat in patterns)]
    assert missing == [], f"package-data does not cover: {missing}"
    assert "py.typed" in patterns


def test_lazy_public_exports():
    for name in ("create_full_app", "Settings", "LocalRunner", "RequestWorkflow", "TemporalEngine", "create_mcp_server",
                 "MCPConnector", "EvidenceCandidate", "ApprovedReadService", "a2a_router", "SubscriptionService",
                 "workflow_router", "configure_workflow"):
        assert getattr(belegt_core, name) is not None, name
        assert name in belegt_core.__all__
