# Spec: 04 §6.1（只读约束：只读技术用户 + 连接器层拒绝写语句；cursor 分页 ≤ 1000；增量）；FR-203
"""通用 SQL 只读连接器（SQLAlchemy 2.x）。

只读三道防线：
1. 部署要求：数据库侧使用只读技术用户（文档约束，manifest `auth.notes` 写明）。
2. 引擎层：SQLite 以 `mode=ro` URI 打开；PostgreSQL 会话 `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`；
   MySQL `SET SESSION TRANSACTION READ ONLY`。
3. 语句层：`before_cursor_execute` 钩子只放行 `SELECT`/`WITH`/`PRAGMA table_info|table_list` 等只读语句，
   其余一律 `CONNECTOR_WRITE_DENIED`。

工具：
- `db.sql.tables.list`       允许访问的表/视图
- `db.sql.tables.describe`   列、类型、主键
- `db.sql.rows.query`        结构化查询（列、过滤、排序、增量水位列），**不接受原始 SQL**
- `db.sql.named_queries.query` 运维预先登记的只读命名查询（如 ERP 报表视图），参数绑定

DSN 通过 `secret_ref`（`env:`/`vault:`）解析，manifest 与输出中只出现脱敏后的 DSN。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import MetaData, Table, and_, create_engine, event, func, inspect, select, text
from sqlalchemy.engine import Engine, make_url

from ..base import BaseConnector, CallContext, Page
from ..spec import CURSOR_PROPS, SYNC_PROPS, AuthSpec, ConnectorError, object_schema, to_wire

_READONLY_RE = re.compile(r"^\s*(\(\s*)*(select|with|pragma\s+(\w+\.)?(table_info|table_xinfo|index_list|index_info|"
                          r"foreign_key_list|table_list|database_list|compile_options)|show|describe|explain|"
                          r"set\s+session\s+characteristics\s+as\s+transaction\s+read\s+only|"
                          r"set\s+session\s+transaction\s+read\s+only|begin|commit|rollback|release|savepoint)\b",
                          re.I)
_FORBIDDEN_RE = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|attach|detach|"
                           r"replace|vacuum|reindex|copy|call|exec|execute|lock)\b", re.I)
_OPS = {"eq", "ne", "lt", "le", "gt", "ge", "in", "like", "is_null", "not_null"}


def is_readonly_sql(statement: str) -> bool:
    """语句层只读判定：必须以只读关键字开头，且（去掉字符串字面量后）不含写关键字。"""
    stripped = re.sub(r"'(?:[^']|'')*'", "''", statement)
    stripped = re.sub(r"--[^\n]*|/\*.*?\*/", " ", stripped, flags=re.S)
    if ";" in stripped.strip().rstrip(";"):
        return False  # 禁止多语句
    if not _READONLY_RE.match(stripped):
        return False
    head = stripped.strip().lower()
    if head.startswith(("select", "with", "(")):
        return not _FORBIDDEN_RE.search(stripped)
    return True


def redact_dsn(dsn: str) -> str:
    try:
        return make_url(dsn).render_as_string(hide_password=True)
    except Exception:  # pragma: no cover
        return "<redacted>"


def readonly_engine(dsn: str) -> Engine:
    url = make_url(dsn)
    backend = url.get_backend_name()
    if backend == "sqlite" and url.database and url.database != ":memory:" and not url.database.startswith("file:"):
        url = url.set(database=f"file:{url.database}?mode=ro", query={**url.query, "uri": "true"})
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _session_ro(dbapi_conn: Any, _rec: Any) -> None:  # pragma: no cover - 依后端
        if backend in ("postgresql",):
            cur = dbapi_conn.cursor()
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            cur.close()
        elif backend in ("mysql", "mariadb"):
            cur = dbapi_conn.cursor()
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.close()
        elif backend == "sqlite":
            dbapi_conn.execute("PRAGMA query_only = ON")

    @event.listens_for(engine, "before_cursor_execute")
    def _guard(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if statement.strip().lower().startswith("pragma query_only"):
            return
        if not is_readonly_sql(statement):
            raise ConnectorError("CONNECTOR_WRITE_DENIED", "read-only connector refused statement",
                                 details={"statement_head": statement.strip()[:40]})

    return engine


class SqlReadOnlyConnector(BaseConnector):
    kind = "sql"
    category = "db"
    system = "sql"
    title = "Generic SQL (read-only)"
    attested = False

    def __init__(self, dsn: str | None = None, *, connector_id: str | None = None, secret_ref: str | None = None,
                 tables: list[str] | None = None, named_queries: dict[str, str] | None = None,
                 attested: bool = False, **kw: Any) -> None:
        auth = kw.pop("auth", None) or AuthSpec(mode="basic" if secret_ref else "none", secret_ref=secret_ref,
                                                notes="使用数据库只读技术用户；DSN 由 secret_ref 解析，不出现在输出中")
        super().__init__(connector_id or "conn:sql:default", auth=auth, **kw)
        if dsn is None:
            dsn = self.secrets.resolve(secret_ref)
        self.engine = readonly_engine(dsn)
        self.source_system = f"sql:{self.engine.url.get_backend_name()}"
        self.dsn_redacted = redact_dsn(dsn)
        self.attested = attested
        self._allow = set(tables) if tables else None
        self._named: dict[str, str] = {}
        for name, sql in (named_queries or {}).items():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ConnectorError("CONNECTOR_INPUT_INVALID", f"invalid named query id {name!r}")
            if not is_readonly_sql(sql):
                raise ConnectorError("CONNECTOR_WRITE_DENIED", f"named query {name!r} is not read-only")
            self._named[name] = sql
        self._meta = MetaData()
        self._tables: dict[str, Table] = {}

        self.register("db.sql.tables.list", "列出允许访问的表与视图", object_schema({}), self._tables_list)
        self.register("db.sql.tables.describe", "描述表结构：列、类型、可空、主键",
                      object_schema({"table": {"type": "string"}}, ["table"]), self._describe)
        filt = {
            "type": "array",
            "items": {"type": "object", "required": ["column", "op"], "additionalProperties": False,
                      "properties": {"column": {"type": "string"}, "op": {"enum": sorted(_OPS)},
                                     "value": {}}},
        }
        self.register(
            "db.sql.rows.query",
            "结构化只读查询：选择列、过滤（eq/ne/lt/le/gt/ge/in/like/is_null/not_null）、排序；"
            "`watermark_column` + `sync_token` 做增量同步（省略时用单列整数主键，适用 append-only 表）。不接受原始 SQL。",
            object_schema({"table": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}},
                           "filters": filt, "order_by": {"type": "array", "items": {"type": "string"}},
                           "watermark_column": {"type": "string"}, "unit_column": {"type": "string"},
                           "period_start_column": {"type": "string"}, "period_end_column": {"type": "string"},
                           **CURSOR_PROPS, **SYNC_PROPS}, ["table"]),
            self._rows_query, paginated=True, incremental=True, evidence_source_type="erp")
        self.register(
            "db.sql.named_queries.query", "执行运维预先登记的只读命名查询（参数绑定）",
            object_schema({"name": {"enum": sorted(self._named) or ["<none>"]},
                           "params": {"type": "object"}, **CURSOR_PROPS}, ["name"]),
            self._named_query, paginated=True, evidence_source_type="erp")

    def compliance_probes(self) -> dict[str, dict[str, Any]]:
        names = self._table_names()
        probes: dict[str, dict[str, Any]] = {"db.sql.tables.list": {}}
        if names:
            probes["db.sql.tables.describe"] = {"table": names[0]}
            probes["db.sql.rows.query"] = {"table": names[0]}
        if self._named:
            probes["db.sql.named_queries.query"] = {"name": sorted(self._named)[0], "params": {}}
        return probes

    # —— helpers ——
    def _table_names(self) -> list[str]:
        insp = inspect(self.engine)
        names = sorted(set(insp.get_table_names()) | set(insp.get_view_names()))
        return [n for n in names if self._allow is None or n in self._allow]

    def _table(self, name: str) -> Table:
        if name not in self._table_names():
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"table {name!r} not found or not allowed")
        if name not in self._tables:
            self._tables[name] = Table(name, self._meta, autoload_with=self.engine)
        return self._tables[name]

    def _record_key(self, t: Table, row: dict[str, Any], fallback: int) -> str:
        pk = [c.name for c in t.primary_key.columns]
        if pk:
            return f"{t.name}:" + "|".join(str(to_wire(row[c])) for c in pk)
        return f"{t.name}:row{fallback}"

    # —— handlers ——
    def _tables_list(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        insp = inspect(self.engine)
        views = set(insp.get_view_names())
        return [{"record_key": n, "fields": {"name": n, "kind": "view" if n in views else "table"}}
                for n in self._table_names()]

    def _describe(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        t = self._table(args["table"])
        pk = {c.name for c in t.primary_key.columns}
        return [{"record_key": f"{t.name}.{c.name}",
                 "fields": {"column": c.name, "type": str(c.type), "nullable": bool(c.nullable),
                            "primary_key": c.name in pk}} for c in t.columns]

    def _rows_query(self, args: dict[str, Any], ctx: CallContext) -> Page:
        t = self._table(args["table"])
        cols = args.get("columns") or [c.name for c in t.columns]
        for c in cols + [f["column"] for f in args.get("filters") or []] + list(args.get("order_by") or []):
            if c.lstrip("-") not in t.c:
                raise ConnectorError("CONNECTOR_INPUT_INVALID", f"unknown column {c!r}")
        wm_col = args.get("watermark_column")
        if not wm_col:
            # 未指定水位列：单列整数主键作水位（仅对 append-only 表语义正确；有更新的表请指定 updated_at 类列）
            pk = list(t.primary_key.columns)
            try:
                if len(pk) == 1 and pk[0].type.python_type is int:
                    wm_col = pk[0].name
            except NotImplementedError:  # pragma: no cover - 方言自定义类型
                pass
        if wm_col and wm_col not in t.c:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", f"unknown watermark column {wm_col!r}")
        conds = []
        for f in args.get("filters") or []:
            col, op, val = t.c[f["column"]], f["op"], f.get("value")
            conds.append({"eq": lambda: col == val, "ne": lambda: col != val, "lt": lambda: col < val,
                          "le": lambda: col <= val, "gt": lambda: col > val, "ge": lambda: col >= val,
                          "in": lambda: col.in_(list(val or [])), "like": lambda: col.like(val),
                          "is_null": lambda: col.is_(None), "not_null": lambda: col.is_not(None)}[op]())
        if wm_col and ctx.since is not None:
            conds.append(t.c[wm_col] > ctx.since)
        where = and_(*conds) if conds else None
        order = []
        for o in args.get("order_by") or []:
            order.append(t.c[o[1:]].desc() if o.startswith("-") else t.c[o])
        if wm_col:
            order.insert(0, t.c[wm_col])
        order += [c for c in t.primary_key.columns] or [t.c[cols[0]]]
        sel_cols = list(dict.fromkeys(cols + ([wm_col] if wm_col else [])))
        stmt = select(*[t.c[c] for c in sel_cols])
        cnt = select(func.count()).select_from(t)
        wm_stmt = select(func.max(t.c[wm_col])) if wm_col else None
        if where is not None:
            stmt, cnt = stmt.where(where), cnt.where(where)
            if wm_stmt is not None:
                wm_stmt = wm_stmt.where(where)
        stmt = stmt.order_by(*order).offset(ctx.offset).limit(ctx.page_size)
        pk_cols = [c.name for c in t.primary_key.columns]
        with self.engine.connect() as conn:
            total = conn.execute(cnt).scalar_one()
            rows = [dict(r._mapping) for r in conn.execute(stmt)]
            if pk_cols and not set(pk_cols) <= set(sel_cols):
                # 主键不在所选列中时补查主键作 record_key
                pk_stmt = select(*[t.c[c] for c in pk_cols])
                if where is not None:
                    pk_stmt = pk_stmt.where(where)
                pk_stmt = pk_stmt.order_by(*order).offset(ctx.offset).limit(ctx.page_size)
                for r, pkr in zip(rows, conn.execute(pk_stmt)):
                    r.update({f"__pk_{k}": v for k, v in dict(pkr._mapping).items()})
            watermark = conn.execute(wm_stmt).scalar() if wm_stmt is not None else None
        out = []
        for i, r in enumerate(rows):
            if pk_cols and not set(pk_cols) <= set(sel_cols):
                key = f"{t.name}:" + "|".join(str(to_wire(r.pop(f"__pk_{c}"))) for c in pk_cols)
            else:
                key = self._record_key(t, r, ctx.offset + i + 1)
            rec: dict[str, Any] = {"record_key": key, "fields": {k: v for k, v in r.items() if k in cols}}
            if args.get("unit_column") and args["unit_column"] in r:
                rec["unit"] = r[args["unit_column"]]
            ps, pe = args.get("period_start_column"), args.get("period_end_column")
            if ps and pe and r.get(ps) is not None and r.get(pe) is not None:
                rec["period"] = {"start": to_wire(r[ps])[:10], "end": to_wire(r[pe])[:10]}
            out.append(rec)
        nxt = ctx.offset + ctx.page_size if ctx.offset + ctx.page_size < total else None
        if watermark is None:
            watermark = ctx.since
        return Page(out, nxt, total, to_wire(watermark) if watermark is not None else None)

    def _named_query(self, args: dict[str, Any], ctx: CallContext) -> Page:
        name = args["name"]
        if name not in self._named:
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"named query {name!r} not registered")
        sql = self._named[name].strip().rstrip(";")
        params = dict(args.get("params") or {})
        wrapped = f"SELECT * FROM ({sql}) AS q"
        with self.engine.connect() as conn:
            total = conn.execute(text(f"SELECT COUNT(*) FROM ({sql}) AS q"), params).scalar_one()
            res = conn.execute(text(f"{wrapped} LIMIT :_lim OFFSET :_off"),
                               {**params, "_lim": ctx.page_size, "_off": ctx.offset})
            rows = [dict(r._mapping) for r in res]
        out = [{"record_key": f"{name}:{ctx.offset + i + 1}", "fields": r} for i, r in enumerate(rows)]
        nxt = ctx.offset + ctx.page_size if ctx.offset + ctx.page_size < total else None
        return Page(out, nxt, total)
