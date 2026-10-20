# Spec: 01 UI-04（问卷列表/详情/"下一步"/halted 恢复）、04 §2.2 T01、§3、§4.3、§5.2；web/README.md 端点表
"""``api/routes_workflow``：上传问卷 → LocalRunner 推进 → 列表/详情/题目 → 推进/恢复/取消；/blobs 兼容；开发头。离线。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from belegt_core.app_factory import Settings, create_full_app
from belegt_core.demo import DemoHuman, build_env, find_example

H_OWNER = {"Authorization": "Bearer demo-owner"}
H_SALES = {"Authorization": "Bearer demo-sales"}


def _csv() -> Path:
    try:
        return find_example("oem_fragebogen_kurz.csv")
    except FileNotFoundError:
        pytest.skip("examples/oem_fragebogen_kurz.csv 不在")


def _app(env, **kw):
    s = Settings(services_factory="none", **kw)
    return create_full_app(s, services=env)


def _upload(client: TestClient, path: Path, headers=H_OWNER):
    return client.post("/requests", headers=headers,
                       files={"file": (path.name, path.read_bytes(), "text/csv")},
                       data={"subject": "Lieferanten-Fragebogen 2025", "sender": "einkauf@oem-a.example"})


@pytest.fixture(scope="module")
def flow():
    """一次上传 + 推进到 in_review（模块内共享，约数秒）。"""
    env = build_env()
    app = _app(env)
    client = TestClient(app)
    r = _upload(client, _csv())
    assert r.status_code == 201, r.text
    body = r.json()
    req_id = body["request"]["id"]
    human = DemoHuman(env.store, env.user("owner"), executive=env.user("exec"))
    states = []
    detail = client.get(f"/requests/{req_id}", headers=H_OWNER).json()
    for _ in range(15):
        wf = detail["workflow"]
        states.append(wf["state"])
        if wf["state"] in ("in_review", "completed", "failed", "halted"):
            break
        if wf["state"] == "awaiting_supplier":
            adv = client.post(f"/requests/{req_id}/advance", headers=H_OWNER, json={
                "signal": "supplier_responded", "payload": _supplier_reply(env, wf["pending"])})
        else:
            assert human.resolve(wf["pending"]) > 0, wf
            adv = client.post(f"/requests/{req_id}/advance", headers=H_OWNER)
        assert adv.status_code == 200, adv.text
        detail = client.get(f"/requests/{req_id}", headers=H_OWNER).json()
    return {"env": env, "client": client, "req_id": req_id, "upload": body, "states": states, "detail": detail}


def _supplier_reply(env, pending):
    import json

    sid = pending[0]
    payload = {"format": "belegt-supplier-submission/1", "values": env.tenant["supplier_submission"], "sreq_id": sid}
    blob = env.store.put_blob(env.org_id, json.dumps(payload).encode(), "application/json")
    return {"sreq_id": sid, "blob_sha256": blob, "kind": "submission"}


def test_upload_creates_request_and_starts_local_runner(flow):
    up = flow["upload"]
    assert up["request"]["status"] in ("received", "triaged", "parsed", "awaiting_human", "mapped", "evidence_gathering",
                                       "awaiting_supplier", "drafting", "auditing", "in_review")
    assert up["request_ref"].startswith(up["request"]["id"] + "#v")
    assert up["blob_sha256"][0].startswith("sha256:")
    assert up["workflow"]["engine"] == "local" and up["workflow"]["workflow_id"] == f"req-{up['request']['id']}"
    assert flow["states"][-1] == "in_review", flow["states"]


def test_list_requests_request_view(flow):
    items = flow["client"].get("/requests", headers=H_OWNER).json()["items"]
    view = next(i for i in items if i["request"]["id"] == flow["req_id"])
    assert view["counterparty_name"] == "OEM-A AG"
    p = view["progress"]
    assert set(p) == {"questions", "mapped", "evidenced", "approved", "submitted", "est_minutes_remaining", "open_conflicts",
                      "open_cards"}
    assert p["questions"] >= 10 and p["mapped"] == p["questions"]
    assert p["est_minutes_remaining"] >= 0 and p["submitted"] == 0
    # 历史导入的问卷也在列表里（submitted 答案不挂题目）
    assert any(i["request"]["title"].startswith("Fragebogen OEM-B") for i in items)


def test_request_detail_and_questions(flow):
    c, rid = flow["client"], flow["req_id"]
    d = c.get(f"/requests/{rid}", headers=H_OWNER).json()
    assert d["request"]["status"] == "in_review"
    assert d["workflow"]["state"] == "in_review" and d["workflow"]["pending"]
    assert len(d["questions"]) == d["progress"]["questions"]
    rows = c.get(f"/requests/{rid}/questions", headers=H_OWNER).json()["items"]
    assert [r["question"]["ordinal"] for r in rows] == sorted(r["question"]["ordinal"] for r in rows)
    with_ans = [r for r in rows if "answer" in r]
    assert with_ans and all(r["answer"]["status"] in ("draft", "in_review", "approved", "submitted") for r in with_ans)
    assert any(r.get("risk_level") in ("L0", "L1", "L2", "L3") for r in with_ans)
    assert any(r["evidence_count"] > 0 for r in with_ans)
    # sales（approved_only）看不到草稿值（01 §5.2 "in Vorbereitung"）
    srows = c.get(f"/requests/{rid}/questions", headers=H_SALES).json()["items"]
    assert all(r.get("answer", {}).get("status", "approved") in ("approved", "submitted") for r in srows)
    assert c.get("/requests/req_01NOPE0000000000000000000A/questions", headers=H_OWNER).status_code == 404


def test_permissions(flow):
    c, rid = flow["client"], flow["req_id"]
    r = _upload(c, _csv(), headers=H_SALES)
    assert r.status_code == 403 and r.json()["error"]["code"] == "GRAPH_ACTOR_NOT_ALLOWED"
    assert c.post(f"/requests/{rid}/advance", headers=H_SALES).status_code == 403
    assert c.get("/requests").status_code == 401
    r = c.post("/requests", headers=H_OWNER, data={"subject": "x"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "GRAPH_SCHEMA_INVALID"
    # resume 只在 halted 时（避免预先授权下一次熔断）
    r = c.post(f"/requests/{rid}/resume", headers=H_OWNER, json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "GRAPH_ILLEGAL_TRANSITION"


def test_halt_resume_and_cancel():
    env = build_env()
    c = TestClient(_app(env, local_background=False))
    up = _upload(c, _csv()).json()
    rid = up["request"]["id"]
    assert up["workflow"]["state"] == "awaiting_human"  # 同步推进模式直接给出挂起点
    st = env.store.get(rid, org_id=env.org_id)["status"]
    ret = st if st != "awaiting_human" else "parsed"
    env.svc.guard.halt(env.org_id, rid, "budget_cost", ret, detail="test")
    assert env.store.get(rid, org_id=env.org_id)["status"] == "halted"
    d = c.get("/requests", headers=H_OWNER).json()["items"]
    assert next(i for i in d if i["request"]["id"] == rid)["request"]["status"] == "halted"
    r = c.post(f"/requests/{rid}/resume", headers={"Authorization": "Bearer demo-exec"}, json={})
    assert r.status_code == 403  # T21 仅 org_admin / compliance_owner
    r = c.post(f"/requests/{rid}/resume", headers=H_OWNER, json={"budget_patch": {"cost_eur": "2"}})
    assert r.status_code == 400  # 04 §5.2 形状：{"request": {...}} / {"<agent>": {...}}
    r = c.post(f"/requests/{rid}/resume", headers=H_OWNER, json={"budget_patch": {"request": {"cost_eur": "8"}}})
    assert r.status_code == 200, r.text
    assert r.json()["workflow"]["engine"] == "local"
    assert env.store.get(rid, org_id=env.org_id)["status"] != "halted"
    r = c.post(f"/requests/{rid}/cancel", headers=H_OWNER)
    assert r.status_code == 200 and r.json()["workflow"]["state"] == "cancelled"
    assert env.store.get(rid, org_id=env.org_id)["status"] == "cancelled"


def test_blobs_compat_and_multipart():
    env = build_env()
    c = TestClient(_app(env))
    r = c.post("/blobs", headers={**H_OWNER, "Content-Type": "application/pdf"}, content=b"%PDF-1.4 test")
    assert r.status_code == 201
    j = r.json()
    assert j["sha256"] == j["blob_sha256"] and j["bytes"] == 13 and j["mime"] == "application/pdf"
    assert env.store.get_blob(env.org_id, j["sha256"])[0] == b"%PDF-1.4 test"
    r = c.post("/blobs", headers=H_OWNER, files={"file": ("cert.pdf", b"%PDF-1.7 x", "application/pdf")})
    assert r.status_code == 201 and r.json()["filename"] == "cert.pdf"
    assert c.post("/blobs", headers=H_OWNER, content=b"").status_code == 400


def test_dev_actor_header(monkeypatch):
    env = build_env()
    c = TestClient(_app(env, dev_auth=True))
    assert c.get("/requests", headers={"X-Belegt-Actor": "user:u_owner"}).status_code == 200
    assert c.get("/requests", headers={"X-Belegt-Actor": "user:u_nobody"}).status_code == 401
    assert c.get("/requests").status_code == 401
    # web 的 /blobs 上传不带头：仅配置了默认执行者时才映射
    c2 = TestClient(_app(env, dev_auth=True, dev_default_actor="user:u_owner"))
    assert c2.post("/blobs", content=b"abc", headers={"Content-Type": "text/plain"}).status_code == 201
    # 未开启 dev_auth：开发头无效
    c3 = TestClient(_app(env))
    assert c3.get("/requests", headers={"X-Belegt-Actor": "user:u_owner"}).status_code == 401
    # 开发头不覆盖显式令牌
    assert c.get("/requests", headers={"X-Belegt-Actor": "user:u_owner", "Authorization": "Bearer bad"}).status_code == 401


def test_temporal_engine_mode():
    """``workflow_engine=temporal``：上传即启动 RequestWorkflow；detail 读 query；advance 发信号（需本地 Temporal 测试服务器）。"""
    import uuid

    import httpx

    from test_temporal import _env

    from belegt_core.workflow.temporal import TemporalEngine, WorkflowOptions
    from belegt_core.workflow.temporal_worker import create_worker

    async def main() -> None:
        tenv = await _env()
        try:
            env = build_env()
            tq = f"tq-{uuid.uuid4().hex[:8]}"
            eng = TemporalEngine(tenv.client, task_queue=tq, options=WorkflowOptions(human_timeout_s=None, max_timer_s=0))
            app = create_full_app(Settings(services_factory="none", workflow_engine="temporal"), services=env, temporal_engine=eng)
            async with create_worker(tenv.client, env.svc, task_queue=tq):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                    p = _csv()
                    r = await c.post("/requests", headers=H_OWNER, files={"file": (p.name, p.read_bytes(), "text/csv")},
                                     data={"sender": "einkauf@oem-a.example", "subject": "Lieferanten-Fragebogen 2025"})
                    assert r.status_code == 201, r.text
                    j = r.json()
                    rid = j["request"]["id"]
                    assert j["workflow"] == {"engine": "temporal", "workflow_id": f"req-{rid}", "state": "running"}
                    st = await eng.wait_for(rid, timeout_s=60)
                    d = (await c.get(f"/requests/{rid}", headers=H_OWNER)).json()
                    assert d["workflow"]["engine"] == "temporal" and d["workflow"]["state"] == st["state"]
                    human = DemoHuman(env.store, env.user("owner"), executive=env.user("exec"))
                    assert human.resolve(st["pending"]) > 0
                    a = await c.post(f"/requests/{rid}/advance", headers=H_OWNER)
                    assert a.status_code == 200 and a.json()["signal"] == "human_answered"
                    st2 = await eng.wait_for(rid, after_revision=st["revision"], timeout_s=60)
                    assert st2["revision"] > st["revision"]
                    r = await c.post(f"/requests/{rid}/cancel", headers=H_OWNER)
                    assert r.status_code == 200
                    assert await eng.handle(rid).result() == {"status": "cancelled"}
        finally:
            await tenv.shutdown()

    asyncio.run(main())
