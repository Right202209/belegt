from __future__ import annotations

from decimal import Decimal

import pytest

from belegt_connectors import BaseConnector, ConnectorError, ConnectorManifest, dec_str, query_hash, validate_tool_name
from belegt_connectors.spec import (
    decode_cursor, encode_cursor, from_mcp_safe_name, mcp_safe_name, object_schema, to_wire,
)


@pytest.mark.parametrize("name", ["erp.sap_b1.purchase_invoices.query", "file.excel.range.get",
                                  "mail.imap.messages.list", "db.sql.tables.describe"])
def test_valid_tool_names(name: str) -> None:
    validate_tool_name(name)


@pytest.mark.parametrize("name,code", [
    ("erp.sap_b1.purchase_invoices.update", "CONNECTOR_NOT_READONLY"),
    ("erp.sap_b1.invoices.delete", "CONNECTOR_NOT_READONLY"),
    ("erp.sap_b1.query", "CONNECTOR_TOOL_NAME_INVALID"),
    ("ERP.sap.x.get", "CONNECTOR_TOOL_NAME_INVALID"),
    ("weird.sys.res.get", "CONNECTOR_TOOL_NAME_INVALID"),
])
def test_invalid_tool_names(name: str, code: str) -> None:
    with pytest.raises(ConnectorError) as ei:
        validate_tool_name(name)
    assert ei.value.code == code


def test_mcp_safe_names() -> None:
    assert mcp_safe_name("erp.sap_b1.items.get") == "erp__sap_b1__items__get"
    assert from_mcp_safe_name("erp__sap_b1__items__get") == "erp.sap_b1.items.get"


@pytest.mark.parametrize("v,exp", [(Decimal("4820.000"), "4820"), ("1234.50", "1234.5"), (0.1, "0.1"), (-0.0, "0"),
                                   (12, "12"), ("1E+3", "1000"), (Decimal("0.000123"), "0.000123")])
def test_dec_str(v: object, exp: str) -> None:
    assert dec_str(v) == exp


def test_dec_str_rejects() -> None:
    for bad in (True, "abc", float("nan")):
        with pytest.raises(ValueError):
            dec_str(bad)


def test_to_wire_numbers_are_strings() -> None:
    out = to_wire({"a": 1, "b": [1.5, Decimal("2.0")], "c": None, "d": True})
    assert out == {"a": "1", "b": ["1.5", "2"], "c": None, "d": True}


def test_query_hash_ignores_paging_and_key_order() -> None:
    a = query_hash("conn:x", "erp.sap_b1.items.list", {"item_group": "STAHL", "cursor": "abc", "page_size": 5})
    b = query_hash("conn:x", "erp.sap_b1.items.list", {"item_group": "STAHL"})
    c = query_hash("conn:y", "erp.sap_b1.items.list", {"item_group": "STAHL"})
    assert a == b != c
    assert a.startswith("sha256:") and len(a) == 71


def test_cursor_bound_to_query() -> None:
    qh = query_hash("c", "erp.sap_b1.items.list", {})
    other = query_hash("c", "erp.sap_b1.items.list", {"item_group": "X"})
    cur = encode_cursor(qh, 200)
    assert decode_cursor(cur, qh) == 200
    with pytest.raises(ConnectorError):
        decode_cursor(cur, other)
    with pytest.raises(ConnectorError):
        decode_cursor("!!notbase64", qh)


def test_manifest_rejects_write_scope() -> None:
    with pytest.raises(ValueError):
        ConnectorManifest(connector_id="c", kind="k", title="t", version="1", source_system="s", category="erp",
                          scopes=["erp.sap_b1.write"])


def test_register_rejects_write_tool_and_bad_schema() -> None:
    c = BaseConnector("conn:test")
    with pytest.raises(ConnectorError):
        c.register("erp.test.rows.update", "x", object_schema({}), lambda a, ctx: [])
    with pytest.raises(ConnectorError):
        c.register("erp.test.rows.list", "x", {"type": "array"}, lambda a, ctx: [])
    c.register("erp.test.rows.list", "x", object_schema({}), lambda a, ctx: [])
    with pytest.raises(ConnectorError):
        c.register("erp.test.rows.list", "dup", object_schema({}), lambda a, ctx: [])


def test_input_validation_and_unknown_tool() -> None:
    c = BaseConnector("conn:test")
    c.register("erp.test.rows.get", "x", object_schema({"id": {"type": "string"}}, ["id"]),
               lambda a, ctx: [{"record_key": a["id"], "fields": {"v": 1}}])
    with pytest.raises(ConnectorError) as ei:
        c.call("erp.test.rows.get", {})
    assert ei.value.code == "CONNECTOR_INPUT_INVALID"
    with pytest.raises(ConnectorError) as ei:
        c.call("erp.test.rows.get", {"id": "1", "extra": 1})
    assert ei.value.code == "CONNECTOR_INPUT_INVALID"
    with pytest.raises(ConnectorError) as ei:
        c.call("erp.test.rows.nope", {})
    assert ei.value.code == "CONNECTOR_TOOL_UNKNOWN"
    env = c.call("erp.test.rows.get", {"id": "k1"})
    assert env["records"][0]["fields"] == {"v": "1"}
    assert env["records"][0]["locator"]["connector"]["record_key"] == "k1"
    assert env["attested"] is False and env["spec"] == "belegt-connector/1"


def test_base_pagination_and_incremental(fixed_clock) -> None:
    rows = [{"record_key": f"r{i}", "fields": {"i": i}, "_wm": i} for i in range(1, 26)]
    c = BaseConnector("conn:test", clock=fixed_clock)
    c.register("erp.test.rows.list", "x",
               object_schema({"cursor": {"type": "string"}, "page_size": {"type": "integer", "maximum": 1000},
                              "sync_token": {"type": "string"}}),
               lambda a, ctx: list(rows), paginated=True, incremental=True)
    pages = list(c.iter_all("erp.test.rows.list", {"page_size": 10}))
    assert [len(p["records"]) for p in pages] == [10, 10, 5]
    assert pages[0]["total"] == 25 and pages[-1]["next_cursor"] is None
    recs, token = c.sync("erp.test.rows.list")
    assert len(recs) == 25 and token
    rows.append({"record_key": "r26", "fields": {"i": 26}, "_wm": 26})
    recs2, token2 = c.sync("erp.test.rows.list", sync_token=token)
    assert [r["record_key"] for r in recs2] == ["r26"] and token2 != token
    recs3, token3 = c.sync("erp.test.rows.list", sync_token=token2)
    assert recs3 == [] and token3 == token2
