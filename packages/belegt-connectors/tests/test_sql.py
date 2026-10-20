from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from belegt_connectors import ConnectorError, SqlReadOnlyConnector, is_readonly_sql, record_to_evidence, run_compliance


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    p = tmp_path / "erp.sqlite"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE energy_meter (id INTEGER PRIMARY KEY, site TEXT, carrier TEXT, qty NUMERIC, unit TEXT,
                                   period_start TEXT, period_end TEXT, updated_at TEXT);
        CREATE VIEW v_electricity AS SELECT * FROM energy_meter WHERE carrier='electricity';
        CREATE TABLE secret_hr (id INTEGER PRIMARY KEY, name TEXT);
    """)
    rows = []
    for m in range(1, 13):
        rows.append((m, "Werk A", "electricity", 400000 + m * 1000, "kWh", f"2025-{m:02d}-01", f"2025-{m:02d}-28",
                     f"2026-01-{m:02d}T00:00:00Z"))
    rows.append((13, "Werk A", "natural_gas", "2150000.5", "kWh", "2025-01-01", "2025-12-31", "2026-01-20T00:00:00Z"))
    con.executemany("INSERT INTO energy_meter VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return p


def test_is_readonly_sql() -> None:
    assert is_readonly_sql("SELECT * FROM t WHERE note = 'drop table x'")
    assert is_readonly_sql("WITH a AS (SELECT 1) SELECT * FROM a")
    assert is_readonly_sql("PRAGMA main.table_info(\"t\")")
    for bad in ("DELETE FROM t", "SELECT 1; DROP TABLE t", "UPDATE t SET a=1", "INSERT INTO t VALUES (1)",
                "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", "ATTACH DATABASE 'x' AS y",
                "PRAGMA writable_schema=1"):
        assert not is_readonly_sql(bad), bad


def test_rows_query_filters_and_paging(db: Path) -> None:
    c = SqlReadOnlyConnector(f"sqlite:///{db}", tables=["energy_meter", "v_electricity"])
    tables = {r["fields"]["name"]: r["fields"]["kind"] for r in c.call("db.sql.tables.list")["records"]}
    assert tables == {"energy_meter": "table", "v_electricity": "view"}
    desc = c.call("db.sql.tables.describe", {"table": "energy_meter"})
    assert any(r["fields"]["column"] == "id" and r["fields"]["primary_key"] for r in desc["records"])
    env = c.call("db.sql.rows.query", {
        "table": "energy_meter", "filters": [{"column": "carrier", "op": "eq", "value": "electricity"}],
        "unit_column": "unit", "period_start_column": "period_start", "period_end_column": "period_end",
        "page_size": 5})
    assert env["total"] == 12 and len(env["records"]) == 5 and env["next_cursor"]
    r0 = env["records"][0]
    assert r0["record_key"] == "energy_meter:1" and r0["fields"]["qty"] == "401000" and r0["unit"] == "kWh"
    assert r0["period"] == {"start": "2025-01-01", "end": "2025-01-28"}
    allrecs = [r for p in c.iter_all("db.sql.rows.query", {"table": "energy_meter", "page_size": 4}) for r in p["records"]]
    assert len(allrecs) == 13 and len({r["record_key"] for r in allrecs}) == 13
    gas = c.call("db.sql.rows.query", {"table": "energy_meter", "columns": ["qty", "unit"],
                                       "filters": [{"column": "carrier", "op": "in", "value": ["natural_gas"]}],
                                       "unit_column": "unit"})
    rec = gas["records"][0]
    assert rec["record_key"] == "energy_meter:13" and rec["fields"] == {"qty": "2150000.5", "unit": "kWh"}
    ev = record_to_evidence(gas, rec, fact_key="activity.natural_gas.consumed", value_field="qty")
    assert ev["source_type"] == "erp" and ev["locator"]["connector"]["query_hash"] == gas["query_hash"]
    with pytest.raises(ConnectorError):
        c.call("db.sql.rows.query", {"table": "secret_hr"})
    with pytest.raises(ConnectorError):
        c.call("db.sql.rows.query", {"table": "energy_meter", "columns": ["nope"]})


def test_incremental_watermark(db: Path) -> None:
    c = SqlReadOnlyConnector(f"sqlite:///{db}")
    args = {"table": "energy_meter", "watermark_column": "updated_at"}
    recs, token = c.sync("db.sql.rows.query", args)
    assert len(recs) == 13 and token
    recs2, token2 = c.sync("db.sql.rows.query", args, token)
    assert recs2 == [] and token2 == token
    con = sqlite3.connect(db)
    con.execute("INSERT INTO energy_meter VALUES (14,'Werk B','electricity',1,'MWh','2026-01-01','2026-01-31',"
                "'2026-02-05T00:00:00Z')")
    con.commit()
    con.close()
    recs3, _ = c.sync("db.sql.rows.query", args, token)
    assert [r["record_key"] for r in recs3] == ["energy_meter:14"]


def test_write_denied_at_engine_level(db: Path) -> None:
    c = SqlReadOnlyConnector(f"sqlite:///{db}")
    with pytest.raises(Exception) as ei:
        with c.engine.begin() as conn:
            conn.execute(text("DELETE FROM energy_meter"))
    assert "CONNECTOR_WRITE_DENIED" in str(ei.value) or "readonly" in str(ei.value).lower()
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM energy_meter").fetchone()[0] == 13
    con.close()


def test_named_queries(db: Path) -> None:
    c = SqlReadOnlyConnector(f"sqlite:///{db}", named_queries={
        "energy_by_carrier": "SELECT carrier, SUM(qty) AS total, unit FROM energy_meter "
                             "WHERE period_start >= :start GROUP BY carrier, unit ORDER BY carrier"})
    env = c.call("db.sql.named_queries.query", {"name": "energy_by_carrier", "params": {"start": "2025-01-01"}})
    totals = {r["fields"]["carrier"]: r["fields"]["total"] for r in env["records"]}
    assert totals["electricity"] == "4878000"
    with pytest.raises(ConnectorError):
        SqlReadOnlyConnector(f"sqlite:///{db}", named_queries={"evil": "DELETE FROM energy_meter"})


def test_dsn_from_secret_not_leaked(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BELEGT_SECRET_SQL", f"sqlite:///{db}")
    c = SqlReadOnlyConnector(secret_ref="env:BELEGT_SECRET_SQL", connector_id="conn:sql:werk")
    assert c.manifest().auth.secret_ref == "env:BELEGT_SECRET_SQL"
    rep = run_compliance(c, secrets=[str(db)])
    assert rep.ok, rep.to_text()
    monkeypatch.delenv("BELEGT_SECRET_SQL")
    with pytest.raises(ConnectorError) as ei:
        SqlReadOnlyConnector(secret_ref="env:BELEGT_SECRET_SQL")
    assert ei.value.code == "CONNECTOR_AUTH_MISSING"
