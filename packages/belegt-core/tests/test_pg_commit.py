"""PostgreSQL 实库测试：全部迁移 SQL（含 RLS）真实执行 + GraphStore 经 graph.commit_version() 写入 03 §5.6 串联实例
+ 跨租户 / 信任区 / 按客户授权 RLS 拒绝。Spec: 03 §5.6、§6.1–§6.8；07 §2.1、§4.3；10 §8.4。

数据库来源（按顺序）：
1. ``BELEGT_TEST_PG_ADMIN_URL``（超级用户或 belegt_owner 成员，如 ``postgresql://postgres@127.0.0.1:5432/postgres``）；
2. 本机有 ``initdb``（PATH 或 /usr/lib/postgresql/*/bin）且装了 pgvector：在临时目录起一个一次性实例；
3. 都没有 → 整个模块 skip（SQLite 路径的测试见 test_pg_commit_sqlite_passthrough，始终运行）。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from belegt_core import GraphError, GraphStore, content_hash
from belegt_core.store.pg_commit import (PgCommitGraphStore, attach_pg_commit, physical_heads, pg_commit_version,
                                         rls_session, verify_physical_audit_chain)

from helpers import CO, CP, FIX, ORG, ORG_B, StepClock, A, build_doc_chain

MIG = Path(__file__).resolve().parents[1] / "migrations" / "postgres"
APP_PASSWORD = "belegt-app-test"


# ---------------------------------------------------------------------------- SQLite：适配器不改变行为
def test_pg_commit_sqlite_passthrough():
    s = PgCommitGraphStore(clock=StepClock())
    assert not s.pg_commit_enabled
    chain = build_doc_chain(s)
    assert chain["answer"] == f"{FIX['answer']['id']}#v3"
    plain = GraphStore()
    assert attach_pg_commit(plain) is plain and "_commit" not in vars(plain)


# ---------------------------------------------------------------------------- PostgreSQL 实例
def _find_bin(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    cands = sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"), reverse=True)
    return cands[0] if cands else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def pg_admin_url():
    psycopg = pytest.importorskip("psycopg")
    url = os.environ.get("BELEGT_TEST_PG_ADMIN_URL")
    if url:
        yield url
        return
    initdb, pg_ctl = _find_bin("initdb"), _find_bin("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("no PostgreSQL server: set BELEGT_TEST_PG_ADMIN_URL or install postgresql-16 + pgvector")
    d = tempfile.mkdtemp(prefix="belegt-pg-")
    port = _free_port()
    subprocess.run([initdb, "-D", d, "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-locale"], check=True,
                   capture_output=True)
    subprocess.run([pg_ctl, "-D", d, "-w", "-l", os.path.join(d, "log"), "-o",
                    f"-p {port} -k {d} -c listen_addresses=127.0.0.1 -c fsync=off", "start"], check=True,
                   capture_output=True)
    url = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    for _ in range(50):
        try:
            psycopg.connect(url).close()
            break
        except Exception:
            time.sleep(0.1)
    try:
        yield url
    finally:
        subprocess.run([pg_ctl, "-D", d, "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


def run_migrations(conninfo: str) -> None:
    import psycopg

    with psycopg.connect(conninfo, autocommit=True) as c:
        for f in sorted(MIG.glob("*.sql")):
            c.execute(f.read_text("utf-8"))


@pytest.fixture(scope="module")
def pg_db(pg_admin_url):
    """新建数据库 → 执行全部迁移两次（幂等）→ 建应用登录账号 belegt_app（非超级用户、无 BYPASSRLS）。"""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = "belegt_t_" + uuid.uuid4().hex[:8]
    with psycopg.connect(pg_admin_url, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
        if not c.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").fetchone():
            pytest.skip("pgvector extension not installed on the PostgreSQL server")
    params = conninfo_to_dict(pg_admin_url)
    admin_db = make_conninfo(**{**params, "dbname": name})
    run_migrations(admin_db)
    run_migrations(admin_db)  # 第二遍：全部迁移必须幂等
    with psycopg.connect(admin_db, autocommit=True) as c:
        c.execute("DROP ROLE IF EXISTS belegt_app")
        c.execute(f"CREATE ROLE belegt_app LOGIN PASSWORD '{APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS")
        c.execute("GRANT belegt_writer, belegt_reader_trusted, belegt_reader_quarantine, belegt_auditor TO belegt_app")
        c.execute("GRANT CREATE, USAGE ON SCHEMA public TO belegt_app")  # GraphStore 可移植表
    app = {**params, "dbname": name, "user": "belegt_app", "password": APP_PASSWORD}
    host, port = app.get("host", "127.0.0.1"), app.get("port", "5432")
    sa_url = f"postgresql+psycopg://belegt_app:{APP_PASSWORD}@{host}:{port}/{name}"
    yield {"admin": admin_db, "app": make_conninfo(**app), "sa_url": sa_url, "name": name}
    with psycopg.connect(pg_admin_url, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def pg_store(pg_db):
    s = PgCommitGraphStore(pg_db["sa_url"], clock=StepClock())
    assert s.pg_commit_enabled
    return s


@pytest.fixture(scope="module")
def pg_chain(pg_store):
    return build_doc_chain(pg_store)


def test_migrations_ownership_and_roles(pg_db):
    import psycopg

    with psycopg.connect(pg_db["admin"]) as c:
        owners = {r[0] for r in c.execute(
            "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname IN ('graph','audit','vec','doc','obs','ref','dict') AND c.relkind IN ('r','p','v','S')")}
        assert owners == {"belegt_owner"}
        nparts = c.execute("SELECT count(*) FROM pg_inherits WHERE inhparent = 'graph.answer_v'::regclass").fetchone()[0]
        assert nparts == 32  # 03 §6.4 MODULUS 32
        forced = c.execute("SELECT relforcerowsecurity FROM pg_class WHERE oid = 'graph.answer_v'::regclass").fetchone()[0]
        assert forced is True
        secdef = c.execute("SELECT prosecdef, pg_get_userbyid(proowner) FROM pg_proc WHERE proname = 'commit_version'"
                           ).fetchone()
        assert secdef == (True, "belegt_owner")


def test_doc_chain_through_commit_version(pg_store, pg_chain, pg_db):
    """03 §5.6：9 个对象（及前序版本）经 GraphStore → graph.commit_version() 写入物理分区表。"""
    for name in ("request", "question", "mapping", "evidence_bill", "closed_question", "evidence_hkn", "calculation",
                 "answer", "approval"):
        d = FIX[name]
        assert pg_chain[name] == f"{d['id']}#v{d['version']}", name
    with rls_session(pg_store, org_id=ORG, actor="user:u_compliance01") as conn:
        heads = physical_heads(conn)
        # 物理 head 与可移植 head 完全一致
        for oid, (typ, ver, status, zone) in heads.items():
            h = pg_store.head(oid, org_id=ORG)
            assert (h.type, h.version, h.status, h.trust_zone) == (typ, ver, status, zone), oid
        assert heads[FIX["answer"]["id"]][1:3] == (3, "approved")
        # 物理表 content_hash 由数据库 JCS 复核过，且 = Python 03 §4.5 规范化哈希
        for name, table in (("answer", "answer_v"), ("calculation", "calculation_v"), ("evidence_bill", "evidence_v"),
                            ("evidence_hkn", "evidence_v"), ("request", "request_v"), ("approval", "approval_v")):
            d = FIX[name]
            row = conn.exec_driver_sql(f"SELECT content_hash, body FROM graph.{table} WHERE id = %(i)s AND version = %(v)s",
                                       {"i": d["id"], "v": d["version"]}).one()
            assert row.content_hash == content_hash(d) == pg_store.get(pg_chain[name])["content_hash"], name
        # 冗余列从 body 填充（R-NUM 第三道防线所依赖的列）
        a = conn.exec_driver_sql("SELECT value_num, calculation_ref, customer_scope, answer_key, datapoint_base "
                                 "FROM graph.answer_v WHERE id = %(i)s AND version = 3", {"i": FIX["answer"]["id"]}).one()
        assert str(a.value_num) == FIX["answer"]["value"] and a.calculation_ref == pg_chain["calculation"]
        assert a.customer_scope == ["all"] and a.answer_key == FIX["answer"]["answer_key"]
        assert a.datapoint_base == FIX["answer"]["datapoint_id"].split("@")[0]
        eff = conn.exec_driver_sql("SELECT answer_id, version FROM graph.effective_answer WHERE answer_key = %(k)s",
                                   {"k": FIX["answer"]["answer_key"]}).one()
        assert (eff.answer_id, eff.version) == (FIX["answer"]["id"], 3)  # R-ONE
        edges = {r.edge_type for r in conn.exec_driver_sql(
            "SELECT edge_type FROM graph.edge WHERE from_id = %(i)s AND from_version = 3", {"i": FIX["answer"]["id"]})}
        assert {"derived_from", "cites", "answers"} <= edges  # SSOT-3 派生边
        appr = conn.exec_driver_sql("SELECT to_version FROM graph.edge WHERE edge_type='approves' AND from_id=%(i)s",
                                    {"i": FIX["approval"]["id"]}).scalar()
        assert appr == 2
        n_versions = conn.exec_driver_sql("SELECT count(*) FROM graph.object_head").scalar()
        n_out = conn.exec_driver_sql("SELECT count(*) FROM graph.outbox").scalar()
        assert n_out >= n_versions
        promotes = conn.exec_driver_sql("SELECT count(*) FROM audit.audit_event_public WHERE action = 'object.promote'").scalar()
        assert promotes >= 2  # Request v2、Mapping v2 晋升
        assert verify_physical_audit_chain(conn, ORG) == []  # SLO-07：数据库内哈希链连续
    with rls_session(pg_store, org_id="org_global", actor="service:factor-import") as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM ref.factor_set WHERE org_id = 'org_global'").scalar() == 1


def _count(conn, sql: str) -> int:
    return conn.exec_driver_sql(sql).scalar()


def test_rls_cross_tenant_denied(pg_store, pg_chain):
    """跨租户：以 belegt_reader_trusted 读，app.org_id = 另一个租户 → ORG 的行全部不可见（07 T-18 负向）。"""
    pg_store.write("Organization", {"id": ORG_B, "legal_name": "Andere GmbH", "country": "AT",
                                    "default_consolidation": "operational_control", "data_residency": "eu",
                                    "locale_default": "de"}, actor=A("user:u_admin_b", "org_admin", org=ORG_B))
    tables = ("graph.request_v", "graph.question_v", "graph.answer_v", "graph.evidence_v", "graph.calculation_v",
              "graph.approval_v", "graph.object_head", "graph.edge", "graph.effective_answer")
    with rls_session(pg_store, org_id=ORG, actor="user:u_compliance01", role="belegt_reader_trusted") as conn:
        own = {t: _count(conn, f"SELECT count(*) FROM {t}") for t in tables}
        assert all(v > 0 for v in own.values()), own
        assert _count(conn, f"SELECT count(*) FROM graph.object_head WHERE org_id = '{ORG_B}'") == 0
    with rls_session(pg_store, org_id=ORG_B, actor="user:u_admin_b", role="belegt_reader_trusted") as conn:
        for t in tables:
            assert _count(conn, f"SELECT count(*) FROM {t} WHERE org_id = '{ORG}'") == 0, t
        assert _count(conn, "SELECT count(*) FROM graph.object_head") == 1  # 只见自己的 Organization
        assert _count(conn, "SELECT count(*) FROM audit.audit_event_public WHERE org_id <> current_setting('app.org_id')") == 0
    # 未设置 app.org_id（current_setting(..., true) = NULL）→ 什么都看不到
    with pg_store.engine.begin() as conn:
        conn.exec_driver_sql("SET LOCAL ROLE belegt_reader_trusted")
        assert _count(conn, "SELECT count(*) FROM graph.answer_v") == 0


def test_rls_zone_customer_scope_approved_only(pg_store, pg_chain):
    with rls_session(pg_store, org_id=ORG, actor="agent:question-parser", zone="quarantine",
                     role="belegt_reader_quarantine") as conn:
        zones = {r[0] for r in conn.exec_driver_sql("SELECT DISTINCT trust_zone FROM graph.object_head")}
        assert zones == {"quarantine"} or zones == set()
        # 隔离区只能看 quarantine 版本：Request v1（quarantine）可见，v2/v3（trusted）不可见
        vs = sorted(r[0] for r in conn.exec_driver_sql(
            f"SELECT version FROM graph.request_v WHERE id = '{FIX['request']['id']}'"))
        assert vs == [1]
    # belegt_reader_quarantine 对 answer_v 根本没有 SELECT 权限
    from sqlalchemy.exc import ProgrammingError
    with pytest.raises(ProgrammingError, match="permission denied"):
        with rls_session(pg_store, org_id=ORG, actor="agent:question-parser", zone="quarantine",
                         role="belegt_reader_quarantine") as conn:
            conn.exec_driver_sql("SELECT count(*) FROM graph.answer_v")
    # 按客户授权：串联实例的 Answer customer_scope = all → 任何 cp_scope 都可见；
    # 写一条 customer_scope=[CP] 的行后，cp_scope=其他客户 看不到它
    with rls_session(pg_store, org_id=ORG, actor="user:u_sales_x", cp_scope="cp_01JOTHERCUSTOMERXXXXXXXXXX",
                     role="belegt_reader_trusted") as conn:
        ids = {r[0] for r in conn.exec_driver_sql("SELECT DISTINCT id FROM graph.answer_v")}
        assert ids == {FIX["answer"]["id"]}
    with rls_session(pg_store, org_id=ORG, actor="user:u_sales01", cp_scope="*", approved_only=True,
                     role="belegt_reader_trusted") as conn:
        st = {r[0] for r in conn.exec_driver_sql("SELECT status FROM graph.answer_v")}
        assert st == {"approved"}  # draft/in_review 版本被 p_approved_only 过滤



def test_rls_customer_scope_filters_scoped_rows(pg_db):
    import psycopg

    body = {k: v for k, v in FIX["answer"].items() if k not in ("approval_ref", "risk_level", "supersedes")}
    body.update(id="ans_01JDSCOPEDXXXXXXXXXXXXXXXX", version=1, status="draft", customer_scope=[CP],
                created_by="agent:calc-service", answer_key="sha256:" + "1" * 64)
    body.pop("created_at", None)
    body["content_hash"] = content_hash(body)
    with psycopg.connect(pg_db["app"]) as c:
        c.execute("SELECT set_config('app.org_id', %s, true), set_config('app.actor', 'agent:calc-service', true),"
                  " set_config('app.zone', 'trusted', true)", (ORG,))
        c.execute("SET LOCAL ROLE belegt_writer")
        c.execute("SELECT graph.commit_version('Answer', %s::jsonb, 0, '[]'::jsonb)", (json.dumps(body),))
        c.execute("SET LOCAL ROLE belegt_reader_trusted")
        c.execute("SELECT set_config('app.cp_scope', 'cp_01JOTHERCUSTOMERXXXXXXXXXX', true)")
        assert c.execute("SELECT count(*) FROM graph.answer_v WHERE id = %s", (body["id"],)).fetchone()[0] == 0
        c.execute("SELECT set_config('app.cp_scope', %s, true)", (CP,))
        assert c.execute("SELECT count(*) FROM graph.answer_v WHERE id = %s", (body["id"],)).fetchone()[0] == 1
        c.rollback()


def _call(pg_db, org: str, actor: str, zone: str, typ: str, body: dict, ev: int = 0) -> None:
    import psycopg

    with psycopg.connect(pg_db["app"]) as c:
        c.execute("SELECT set_config('app.org_id', %s, true), set_config('app.actor', %s, true),"
                  " set_config('app.zone', %s, true)", (org, actor, zone))
        c.execute("SET LOCAL ROLE belegt_writer")
        try:
            c.execute("SELECT graph.commit_version(%s, %s::jsonb, %s, '[]'::jsonb)", (typ, json.dumps(body), ev))
        finally:
            c.rollback()


def _org_body(org: str, actor: str) -> dict:
    b = {"id": org, "org_id": org, "version": 1, "created_by": actor, "trust_zone": "trusted",
         "provenance": "trc_01JDTESTXXXXXXXXXXXXXXXXXX", "schema_version": "belegt.graph/2026-10", "legal_name": "X GmbH",
         "country": "DE", "default_consolidation": "operational_control", "data_residency": "eu", "locale_default": "de"}
    b["content_hash"] = content_hash(b)
    return b


@pytest.mark.parametrize("case,code", [
    ("cross_tenant", "GRAPH_REF_CROSS_TENANT"),
    ("actor_mismatch", "GRAPH_ACTOR_NOT_ALLOWED"),
    ("quarantine_writes_trusted", "GRAPH_TRUST_ZONE_VIOLATION"),
    ("hash_tampered", "GRAPH_HASH_MISMATCH"),
    ("stale_version", "GRAPH_VERSION_CONFLICT"),
])
def test_commit_version_db_layer_rejections(pg_db, case, code):
    import psycopg

    org = "org_01JDDBLAYERXXXXXXXXXXXXXXX"
    b = _org_body(org, "user:u_x")
    kw = dict(org=org, actor="user:u_x", zone="trusted", typ="Organization", body=b)
    if case == "cross_tenant":
        kw["org"] = ORG_B
    elif case == "actor_mismatch":
        kw["actor"] = "user:u_other"
    elif case == "quarantine_writes_trusted":
        kw["zone"] = "quarantine"
    elif case == "hash_tampered":
        b["legal_name"] = "Tampered GmbH"   # content_hash 未重算
    elif case == "stale_version":
        kw["ev"] = 3
    with pytest.raises(psycopg.Error) as e:
        _call(pg_db, **kw)
    assert e.value.sqlstate == "BG000" and str(e.value.diag.message_primary).startswith(code)


def test_writer_has_no_direct_table_dml(pg_db):
    """SSOT-2：belegt_writer 只能 EXECUTE 写入函数，直接 INSERT/UPDATE 版本表被拒。"""
    import psycopg

    with psycopg.connect(pg_db["app"]) as c:
        c.execute("SET LOCAL ROLE belegt_writer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO graph.object_head (org_id, id, type, head_version, trust_zone) "
                      "VALUES ('o','x','Answer',1,'trusted')")
        c.rollback()
    with psycopg.connect(pg_db["app"]) as c:
        c.execute("SET LOCAL ROLE belegt_reader_trusted")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE graph.object_head SET head_version = 99")
        c.rollback()


def test_graphstore_maps_db_errors(pg_db):
    """数据库层拒绝经适配器映射为 GraphError，整个事务（含可移植表）回滚。"""
    s = PgCommitGraphStore(pg_db["sa_url"], clock=StepClock())
    org = "org_01JDMAPERRXXXXXXXXXXXXXXXX"
    admin = A("user:u_admin_m", "org_admin", org=org)
    body = {"id": org, "legal_name": "M GmbH", "country": "DE", "default_consolidation": "operational_control",
            "data_residency": "eu", "locale_default": "de"}
    # 物理表里预先放一个 v1（模拟物理/可移植 head 分叉）→ 适配器写 v1 时数据库报版本冲突
    _seed = _org_body(org, "user:u_admin_m")
    import psycopg

    with psycopg.connect(pg_db["app"]) as c:
        c.execute("SELECT set_config('app.org_id', %s, true), set_config('app.actor', 'user:u_admin_m', true),"
                  " set_config('app.zone', 'trusted', true)", (org,))
        c.execute("SET LOCAL ROLE belegt_writer")
        c.execute("SELECT graph.commit_version('Organization', %s::jsonb, 0, '[]'::jsonb)", (json.dumps(_seed),))
        c.commit()
    with pytest.raises(GraphError) as e:
        s.write("Organization", body, actor=admin)
    assert e.value.code == "GRAPH_VERSION_CONFLICT" and e.value.rule_id == "DB"
    assert s.head(org, org_id=org) is None  # 可移植表同事务回滚


def test_pg_commit_version_function_roundtrip(pg_store, pg_chain):
    """pg_commit_version() 可单独调用（如重放/补写工具）：同一事务内写 → 读 → 回滚。"""
    org = "org_01JDROUNDTRIPXXXXXXXXXXXXX"
    actor = A("user:u_rt", "org_admin", org=org)
    body = _org_body(org, "user:u_rt")
    with pg_store.engine.connect() as conn:
        tx = conn.begin()
        res = pg_commit_version(conn, "Organization", body, 0, [], actor)
        assert res["object_ref"] == f"{org}#v1" and res["content_hash"] == body["content_hash"]
        with pytest.raises(GraphError) as e:
            pg_commit_version(conn, "Organization", {**body, "version": 2}, 0, [], actor)
        assert e.value.code == "GRAPH_VERSION_CONFLICT"
        tx.rollback()


def test_audit_verify_chain_detects_tampering(pg_store, pg_chain, pg_db):
    """audit.verify_chain()（SLO-07 / ALR-06）：超级用户篡改一条审计事件后，链校验报告该 seq。"""
    import psycopg

    with rls_session(pg_store, org_id=ORG, actor="user:u_auditor", role="belegt_auditor") as conn:
        assert verify_physical_audit_chain(conn) == []
    with psycopg.connect(pg_db["admin"]) as c:
        seq = c.execute("SELECT min(seq) + 2 FROM audit.audit_event WHERE org_id = %s", (ORG,)).fetchone()[0]
        c.execute("UPDATE audit.audit_event SET object_ref = object_ref || 'x' WHERE org_id = %s AND seq = %s", (ORG, seq))
        try:
            c.execute("SELECT set_config('app.org_id', %s, true)", (ORG,))
            broken = [r[0] for r in c.execute("SELECT * FROM audit.verify_chain()")]
            assert broken == [seq]
        finally:
            c.rollback()
