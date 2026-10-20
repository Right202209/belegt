# Spec: 04 §6.1（MCP client：只读约束、命名、envelope → Evidence 候选）、§6.2（MCP server：只读 approved，按 org 与 customer_scope 授权）
"""Belegt MCP server（进程内 ``mcp.Client``）与 MCP client 连接器适配。离线。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

from belegt_core.authz import Actor  # noqa: E402
from belegt_core.demo import build_env  # noqa: E402
from belegt_core.errors import GraphError  # noqa: E402
from belegt_core.mcp.client import (  # noqa: E402
    ConnectorCallError, ConnectorPolicyError, EvidenceCandidate, MCPConnector, envelope_to_candidates,
)
from belegt_core.mcp.server import TOOL_NAMES, create_mcp_server  # noqa: E402
from belegt_core.mcp.service import ApprovedReadService  # noqa: E402


@pytest.fixture(scope="module")
def world() -> dict[str, Any]:
    env = build_env()
    st, org, ids = env.store, env.org_id, env.ids
    owner = env.user("owner")
    submitted = [a for a in st.query("Answer", org_id=org) if a["status"] == "submitted"]
    assert submitted, "demo tenant imports historical submitted answers"
    # 一条草稿（不得出现在 MCP 结果里）：复制一条已提交文本答案的形状
    src = next(a for a in submitted if a["value_type"] == "text")
    draft = {k: src[k] for k in ("datapoint_id", "value_type", "period", "boundary", "text_lang", "consolidation") if k in src}
    draft.update(value="Entwurf", customer_scope=[ids["other_customer"]], origin="imported", is_estimate=False,
                 legal_commitment=False, status="draft")
    draft_ref = None
    try:
        draft_ref = st.write("Answer", draft, actor=owner)
    except GraphError:  # 同 answer_key 已有有效答案时 R-ONE 可能拒绝；改用另一客户范围
        draft["customer_scope"] = [ids["customer"]]
        draft_ref = st.write("Answer", draft, actor=owner)
    return {"env": env, "submitted": submitted, "draft_ref": draft_ref, "owner": owner}


def _actor(env, roles, **kw) -> Actor:
    return Actor.of("user:u_mcp", env.org_id, roles, **kw)


# ---------------------------------------------------------------------------- service（授权）
def test_list_only_approved_and_scoped(world):
    env, ids = world["env"], world["env"].ids
    svc = ApprovedReadService(env.store)
    items = svc.list_approved_answers(world["owner"])
    refs = {i["answer_ref"] for i in items}
    assert {f"{a['id']}#v{a['version']}" for a in world["submitted"]} <= refs
    assert world["draft_ref"] not in refs and all(i["status"] in ("approved", "submitted") for i in items)
    # cp_scope：历史答案只对 OEM-B 可见
    sales_a = _actor(env, ["sales"], cp_scope=(ids["customer"],))
    assert svc.list_approved_answers(sales_a) == []
    sales_b = _actor(env, ["sales"], cp_scope=(ids["other_customer"],))
    assert len(svc.list_approved_answers(sales_b)) == len(world["submitted"])
    buyer_b = _actor(env, ["customer_buyer"], cp_scope=(ids["other_customer"],))
    assert len(svc.list_approved_answers(buyer_b, counterparty_id=ids["other_customer"])) == len(world["submitted"])
    with pytest.raises(GraphError) as e:
        svc.list_approved_answers(buyer_b, counterparty_id=ids["customer"])
    assert e.value.code == "GRAPH_ACTOR_NOT_ALLOWED"
    # 过滤：数据点、期间
    dp = world["submitted"][0]["datapoint_id"]
    one = svc.list_approved_answers(world["owner"], datapoint_ids=[dp.split("@")[0]])
    assert one and all(i["datapoint_id"] == dp for i in one)
    assert svc.list_approved_answers(world["owner"], period={"start": "2030-01-01", "end": "2030-12-31"}) == []


@pytest.mark.parametrize("roles,kw", [
    (["customer_buyer"], {}),                 # 外部客户必须带 cp_scope
    (["supplier_user"], {}),                  # 隔离区
    (["contributor"], {}),                    # 无读已批准答案的角色
])
def test_denied_actors(world, roles, kw):
    svc = ApprovedReadService(world["env"].store)
    with pytest.raises(GraphError) as e:
        svc.list_approved_answers(_actor(world["env"], roles, **kw))
    assert e.value.code == "GRAPH_ACTOR_NOT_ALLOWED"
    with pytest.raises(GraphError):
        svc.list_approved_answers(Actor.of("agent:question-parser", world["env"].org_id))


def test_other_org_sees_nothing(world):
    svc = ApprovedReadService(world["env"].store)
    other = Actor.of("user:u_x", "org_01JOTHER000000000000000000A", ["compliance_owner"])
    assert svc.list_approved_answers(other) == []
    with pytest.raises(GraphError) as e:
        svc.get_answer(other, f"{world['submitted'][0]['id']}#v{world['submitted'][0]['version']}")
    assert e.value.code == "GRAPH_NOT_FOUND"


def test_provenance_summary(world):
    svc = ApprovedReadService(world["env"].store)
    a = world["submitted"][0]
    out = svc.get_answer_with_provenance(world["owner"], f"{a['id']}#v{a['version']}", depth=9)
    assert out["depth"] == 4 and out["answer"]["answer_id"] == a["id"]
    assert out["approval_ref"] and out["approval_ref"].startswith("apr_")
    types = {n["type"] for n in out["provenance"]["nodes"]}
    assert "Approval" in types and "Question" not in types and "Request" not in types
    for n in out["provenance"]["nodes"]:
        assert "object" not in n and "extracted_value" not in n
    with pytest.raises(GraphError) as e:
        svc.get_answer_with_provenance(world["owner"], world["draft_ref"])
    assert e.value.code == "GRAPH_NOT_FOUND"


def test_dictionary_tools(world):
    svc = ApprovedReadService(world["env"].store)
    hits = svc.search_dictionary("Scope 1", "en")
    assert hits and any("scope1" in h["id"].replace("_", "") or "scope_1" in h["id"] for h in hits[:5])
    dp = svc.get_datapoint(hits[0]["id"])
    assert dp["id"] == hits[0]["id"] and "@" in dp["datapoint_id"]
    assert svc.get_datapoint(dp["datapoint_id"])["semver"] == dp["semver"]
    with pytest.raises(GraphError):
        svc.get_datapoint("dp:does.not.exist")
    assert svc.search_dictionary("   ") == []


# ---------------------------------------------------------------------------- MCP 协议
def test_mcp_server_protocol(world):
    env = world["env"]
    server = create_mcp_server(env.store, world["owner"], spec_names=True)

    async def main() -> None:
        async with Client(server) as c:
            tools = (await c.list_tools()).tools
            names = {t.name for t in tools}
            assert set(TOOL_NAMES) <= names and "belegt.answers.list_approved" in names
            for t in tools:
                assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
            r = await c.call_tool("list_approved_answers", {})
            assert not r.is_error
            items = r.structured_content["items"]
            assert len(items) == len(world["submitted"])
            r = await c.call_tool("get_answer_with_provenance", {"answer_ref": items[0]["answer_ref"]})
            assert not r.is_error and r.structured_content["answer"]["answer_ref"] == items[0]["answer_ref"]
            r = await c.call_tool("get_answer_with_provenance", {"answer_ref": world["draft_ref"]})
            assert r.is_error and "GRAPH_NOT_FOUND" in r.content[0].text
            r = await c.call_tool("search_dictionary", {"query": "Scope 2", "lang": "de", "limit": 3})
            assert not r.is_error and 0 < len(r.structured_content["items"]) <= 3
            r = await c.call_tool("get_datapoint", {"datapoint_id": r.structured_content["items"][0]["id"]})
            assert not r.is_error

    asyncio.run(main())

    # 按调用解析执行者（HTTP 形态）：客户 A 的销售看不到 OEM-B 的答案
    sales_a = _actor(env, ["sales"], cp_scope=(env.ids["customer"],))
    server2 = create_mcp_server(env.store, lambda: sales_a)

    async def main2() -> None:
        async with Client(server2) as c:
            r = await c.call_tool("list_approved_answers", {})
            assert r.structured_content["items"] == []
            r = await c.call_tool("list_approved_answers", {"counterparty_id": env.ids["other_customer"]})
            assert r.is_error and "GRAPH_ACTOR_NOT_ALLOWED" in r.content[0].text

    asyncio.run(main2())


# ---------------------------------------------------------------------------- MCP client 连接器
def _erp_server(readonly: bool = True, name: str = "erp.testsys.facts.query"):
    from mcp.server.mcpserver import MCPServer
    from mcp_types import ToolAnnotations

    srv = MCPServer("test-erp")

    def query(date_from: str, date_to: str, fact_key: str | None = None) -> dict[str, Any]:
        """Query facts (read-only)."""
        if date_from > date_to:
            from mcp.server.mcpserver.exceptions import ToolError

            raise ToolError(json.dumps({"error": {"code": "CONNECTOR_INPUT_INVALID", "message": "date_from > date_to"}}))
        return {"records": [
            {"record_key": "OPCH-1", "fact_key": "activity.electricity_purchased", "unit": "kWh",
             "period": {"start": "2025-01-01", "end": "2025-12-31"}, "fields": {"value": "4820000.000"}},
            {"record_key": "OPCH-2", "fact_key": "activity.diesel", "unit": "L", "fields": {"value": 1234.5}},
            {"record_key": "BAD", "fact_key": "x", "unit": "kg", "fields": {"value": "n/a"}},
        ], "query_hash": "sha256:" + "1" * 64, "fetched_at": "2026-11-03T09:30:00Z", "source_system": "testsys", "attested": True}

    srv.add_tool(query, name=name, description="Query attested facts (read-only).",
                 annotations=ToolAnnotations(read_only_hint=readonly, destructive_hint=not readonly), structured_output=True)
    return srv


def test_connector_envelope_to_candidates():
    conn = MCPConnector(_erp_server(), connector_id="conn:erp-testsys", system="testsys")
    m = conn.manifest()
    assert m["format"] == "belegt-connector/1" and m["tools"][0]["name"] == "erp.testsys.facts.query"
    assert m["tools"][0]["annotations"]["readOnlyHint"] is True and m["pinned_hashes"]
    env = conn.call("erp.testsys.facts.query", {"date_from": "2025-01-01", "date_to": "2025-12-31"}, now="2026-11-03T10:00:00Z")
    assert env["connector_id"] == "conn:erp-testsys" and len(env["records"]) == 3
    cands = conn.fetch_candidates("erp.testsys.facts.query", {"date_from": "2025-01-01", "date_to": "2025-12-31"})
    assert len(cands) == 2 and all(isinstance(c, EvidenceCandidate) for c in cands)  # 非法数字记录被跳过
    c0, c1 = cands
    assert c0.source_type == "erp" and c0.extracted_value == "4820000.000" and c0.unit == "kWh" and c0.confidence == 0.95
    assert c0.locator["connector"] == {"connector_id": "conn:erp-testsys", "query_hash": "sha256:" + "1" * 64,
                                       "record_key": "OPCH-1", "fetched_at": "2026-11-03T09:30:00Z"}
    assert c1.extracted_value == "1234.5" and c1.period is None
    ev = c0.to_evidence_fields(datapoint_id_boundary := "organization")
    assert ev["status"] == "candidate" and ev["valid_from"] == "2025-01-01" and ev["boundary"] == datapoint_id_boundary
    assert "attested:true" in ev["tags"]
    with pytest.raises(ConnectorCallError) as e:
        conn.call("erp.testsys.facts.query", {"date_from": "2026-01-01", "date_to": "2025-01-01"})
    assert e.value.code == "CONNECTOR_INPUT_INVALID"
    with pytest.raises(ConnectorCallError) as e:
        conn.call("erp.testsys.facts.delete", {})
    assert e.value.code == "CONNECTOR_TOOL_UNKNOWN"


def test_connector_rejects_non_readonly_and_bad_names_and_drift():
    with pytest.raises(ConnectorPolicyError) as e:
        MCPConnector(_erp_server(readonly=False)).refresh_sync()
    assert e.value.code == "CONNECTOR_NOT_READONLY"
    with pytest.raises(ConnectorPolicyError) as e:
        MCPConnector(_erp_server(name="erp.testsys.facts.update")).refresh_sync()
    assert e.value.code == "CONNECTOR_TOOL_NAME_INVALID"
    # 不支持点号的客户端：a__b__c__verb 映射回点号名
    conn = MCPConnector(_erp_server(name="erp__testsys__facts__query"))
    assert conn.refresh_sync()[0].name == "erp.testsys.facts.query"
    assert conn.call("erp.testsys.facts.query", {"date_from": "2025-01-01", "date_to": "2025-12-31"})["records"]
    # 描述哈希锁定（RT-PI-11）
    with pytest.raises(ConnectorPolicyError) as e:
        MCPConnector(_erp_server(), pinned_hashes={"erp.testsys.facts.query": "sha256:" + "0" * 64}).refresh_sync()
    assert e.value.code == "CONNECTOR_DESCRIPTION_CHANGED"


def test_connector_session_reuse_async():
    async def main() -> None:
        conn = MCPConnector(_erp_server())
        async with conn.session():
            await conn.refresh()
            a = await conn.fetch_candidates_async("erp.testsys.facts.query", {"date_from": "2025-01-01", "date_to": "2025-12-31"},
                                                  datapoint_id="dp:env.energy.electricity_consumption@1.0.0")
            assert a[0].datapoint_id == "dp:env.energy.electricity_consumption@1.0.0"

    asyncio.run(main())


def test_envelope_to_candidates_document_locator():
    env = {"records": [{"record_key": "p3", "unit": "kWh", "fields": {"quantity": "100"},
                        "locator": {"blob_sha256": "sha256:" + "a" * 64, "mime": "application/pdf", "page": 3}}],
           "query_hash": "sha256:" + "b" * 64, "fetched_at": "2026-01-01T00:00:00Z", "attested": False, "connector_id": "conn:bills"}
    (c,) = envelope_to_candidates(env, fact_key="activity.electricity_purchased")
    assert c.source_type == "document" and c.confidence == 0.8 and c.locator["page"] == 3 and "connector" not in c.locator


def test_connector_against_belegt_connectors_mock_erp():
    bc = pytest.importorskip("belegt_connectors")
    from belegt_connectors.mcp_server import build_mcp_server

    erp_cls = getattr(bc, "MockErpConnector", None)
    if erp_cls is None:
        pytest.skip("belegt_connectors.MockErpConnector 不可用")
    erp = erp_cls()
    conn = MCPConnector(build_mcp_server(erp), connector_id=erp.connector_id)
    names = [t.name for t in conn.refresh_sync()]
    assert "erp.sap_b1.purchase_invoices.query" in names
    cands = conn.fetch_candidates("erp.sap_b1.purchase_invoices.query", {"date_from": "2025-01-01", "date_to": "2025-12-31"},
                                  fact_key="activity.purchase")
    assert cands and all(c.locator.get("connector", {}).get("connector_id") == erp.connector_id or c.locator.get("blob_sha256")
                         for c in cands)
    assert all(c.value_type != "number" or isinstance(c.extracted_value, str) for c in cands)
