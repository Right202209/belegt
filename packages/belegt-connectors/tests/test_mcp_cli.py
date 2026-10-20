from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from belegt_connectors import BaseConnector, MockErpConnector, run_compliance
from belegt_connectors.cli import main
from belegt_connectors.spec import object_schema


def test_mcp_in_process_tools_and_call() -> None:
    pytest.importorskip("mcp")
    from belegt_connectors import build_mcp_server

    srv = build_mcp_server(MockErpConnector())

    async def go() -> tuple[Any, Any]:
        tools = await srv.list_tools()
        res = await srv.call_tool("erp.sap_b1.items.get", {"item_code": "ST-COIL-HR"})
        return tools, res

    tools, res = asyncio.run(go())
    t = {x.name: x for x in tools}["erp.sap_b1.purchase_invoices.query"]
    ann = t.annotations.model_dump(by_alias=True, exclude_none=True)
    assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False
    schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema")
    assert schema["required"] == ["date_from", "date_to"] and schema["additionalProperties"] is False
    structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    assert structured["records"][0]["fields"]["customs_group"] == "72085120"


def test_mcp_underscore_names() -> None:
    pytest.importorskip("mcp")
    from belegt_connectors import build_mcp_server

    srv = build_mcp_server(MockErpConnector(), dot_names=False)
    names = [t.name for t in asyncio.run(srv.list_tools())]
    assert "erp__sap_b1__items__get" in names and all("." not in n for n in names)


def test_mcp_stdio_roundtrip() -> None:
    """真实子进程 stdio：MCP client → `belegt-connector serve erp-mock`（不访问网络）。"""
    pytest.importorskip("mcp")
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError:  # pragma: no cover
        pytest.skip("mcp client API unavailable")

    async def go() -> tuple[list[str], Any, Any]:
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "belegt_connectors.cli", "serve", "erp-mock"])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                ok = await s.call_tool("erp.datev.accounts_bookings.query",
                                       {"account_from": "6326", "account_to": "6326", "fiscal_year": 2025})
                bad = await s.call_tool("erp.sap_b1.items.get", {"item_code": "NOPE"})
                return [t.name for t in tools.tools], ok, bad

    names, ok, bad = asyncio.run(asyncio.wait_for(go(), 60))
    assert "erp.sap_b1.purchase_invoices.query" in names
    sc = getattr(ok, "structured_content", None) or getattr(ok, "structuredContent", None)
    assert sc["total"] == 12 and sc["attested"] is True
    assert (getattr(bad, "is_error", None) or getattr(bad, "isError", None)) is True
    assert "CONNECTOR_NOT_FOUND" in bad.content[0].text


def test_cli_tools_call_check_samples(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["samples", str(tmp_path / "s")]) == 0
    capsys.readouterr()
    assert main(["tools", "erp-mock"]) == 0
    m = json.loads(capsys.readouterr().out)
    assert m["spec"] == "belegt-connector/1" and m["read_only"] is True
    assert main(["call", "files", "--root", str(tmp_path / "s" / "files"), "--tool", "file.csv.rows.query",
                 "--args", json.dumps({"path": "zaehlerstaende_2025.csv", "page_size": 2})]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["records"]) == 2
    assert main(["check", "utility-bills", "--root", str(tmp_path / "s" / "bills"), "--no-belegt-io"]) == 0
    assert "PASS" in capsys.readouterr().out
    assert main(["check", "mailbox", "--path", str(tmp_path / "s" / "mail" / "mailbox.mbox"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main(["call", "erp-mock", "--tool", "erp.sap_b1.items.get", "--args", '{"item_code":"X"}']) == 2


class BadConnector(BaseConnector):
    """不合规连接器：输出 JSON number、locator 缺失 connector。合规套件必须报错。"""

    kind = "bad"
    category = "erp"
    system = "bad"

    def __init__(self) -> None:
        super().__init__("conn:bad")
        self.register("erp.bad.rows.list", "x", {"type": "object", "properties": {}}, lambda a, ctx: [])

    def compliance_probes(self) -> dict[str, dict[str, Any]]:
        return {"erp.bad.rows.list": {}}

    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"records": [{"record_key": "k", "fields": {"qty": 12.5}, "locator": {"page": 1}}],
                "query_hash": "sha256:" + "a" * 64, "fetched_at": "2026-10-19T08:00:00Z", "attested": False,
                "connector_id": "conn:bad"}


def test_compliance_detects_violations() -> None:
    rep = run_compliance(BadConnector())
    failed = {f.check for f in rep.failures()}
    assert {"CC-03", "CC-05", "CC-06"} <= failed and not rep.ok
