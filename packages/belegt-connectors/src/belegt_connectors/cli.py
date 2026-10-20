# Spec: 04 §6.1（连接器以本地进程 stdio 或 Streamable HTTP 运行；合规套件）
"""`belegt-connector` 命令行。

    belegt-connector serve  <kind> [连接器参数] [--transport stdio|streamable-http]
    belegt-connector tools  <kind> [连接器参数]                 # 打印 manifest（工具 schema）
    belegt-connector call   <kind> [连接器参数] --tool T --args '{...}'
    belegt-connector check  <kind> [连接器参数]                 # 合规套件，失败退出 1
    belegt-connector samples <DIR>                              # 生成样例数据（ERP / PDF / 邮箱 / CSV+Excel）

kind 与参数：
    files          --root DIR
    sql            --dsn DSN | --dsn-secret env:VAR   [--table T ...] [--named-query name=SELECT...]
    erp-mock       [--data DIR]
    mailbox        --path MBOX_OR_DIR | --imap-host H --imap-user U --imap-secret env:VAR [--imap-port 993]
    utility-bills  --root DIR [--no-belegt-io]
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .base import BaseConnector
from .spec import ConnectorError

KINDS = ("files", "sql", "erp-mock", "mailbox", "utility-bills")


def _add_conn_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("kind", choices=KINDS)
    p.add_argument("--id", dest="connector_id")
    p.add_argument("--root")
    p.add_argument("--dsn")
    p.add_argument("--dsn-secret")
    p.add_argument("--table", action="append")
    p.add_argument("--named-query", action="append", default=[])
    p.add_argument("--data")
    p.add_argument("--path")
    p.add_argument("--imap-host")
    p.add_argument("--imap-user")
    p.add_argument("--imap-secret")
    p.add_argument("--imap-port", type=int, default=993)
    p.add_argument("--no-belegt-io", action="store_true")


def make_connector(ns: argparse.Namespace) -> BaseConnector:
    kw: dict[str, Any] = {}
    if ns.connector_id:
        kw["connector_id"] = ns.connector_id
    if ns.kind == "files":
        from .connectors.files import FileFolderConnector
        return FileFolderConnector(_req(ns.root, "--root"), **kw)
    if ns.kind == "sql":
        from .connectors.sql import SqlReadOnlyConnector
        named = dict(item.split("=", 1) for item in ns.named_query)
        return SqlReadOnlyConnector(ns.dsn, secret_ref=ns.dsn_secret, tables=ns.table, named_queries=named, **kw)
    if ns.kind == "erp-mock":
        from .connectors.erp_mock import MockErpConnector
        return MockErpConnector(ns.data, **kw)
    if ns.kind == "mailbox":
        from .connectors.mailbox import MailboxConnector
        if ns.imap_host:
            return MailboxConnector.from_imap(ns.imap_host, _req(ns.imap_user, "--imap-user"),
                                              _req(ns.imap_secret, "--imap-secret"), port=ns.imap_port, **kw)
        return MailboxConnector.from_path(_req(ns.path, "--path"), **kw)
    from .connectors.utility_bills import UtilityBillPdfConnector
    return UtilityBillPdfConnector(_req(ns.root, "--root"), use_belegt_io=False if ns.no_belegt_io else None, **kw)


def _req(v: Any, flag: str) -> Any:
    if not v:
        raise SystemExit(f"error: {flag} is required for this connector kind")
    return v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="belegt-connector", description="Belegt read-only MCP connectors")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve")
    _add_conn_args(p_serve)
    p_serve.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--underscore-names", action="store_true", help="工具名 . → __")
    p_tools = sub.add_parser("tools")
    _add_conn_args(p_tools)
    p_call = sub.add_parser("call")
    _add_conn_args(p_call)
    p_call.add_argument("--tool", required=True)
    p_call.add_argument("--args", default="{}")
    p_check = sub.add_parser("check")
    _add_conn_args(p_check)
    p_check.add_argument("--json", action="store_true")
    p_samples = sub.add_parser("samples")
    p_samples.add_argument("out_dir")
    ns = ap.parse_args(argv)

    try:
        if ns.cmd == "samples":
            from .samples import build_all
            res = build_all(ns.out_dir)
            print(json.dumps({k: [str(x) for x in (v.values() if isinstance(v, dict) else v)] for k, v in res.items()},
                             indent=1))
            return 0
        conn = make_connector(ns)
        if ns.cmd == "serve":
            from .mcp_server import serve
            serve(conn, transport=ns.transport, dot_names=not ns.underscore_names, host=ns.host, port=ns.port)
            return 0
        if ns.cmd == "tools":
            print(conn.manifest().model_dump_json(indent=1))
            return 0
        if ns.cmd == "call":
            print(json.dumps(conn.call(ns.tool, json.loads(ns.args)), ensure_ascii=False, indent=1))
            return 0
        if ns.cmd == "check":
            from .compliance import run_compliance
            rep = run_compliance(conn)
            print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=1) if ns.json else rep.to_text())
            return 0 if rep.ok else 1
    except ConnectorError as e:
        print(json.dumps(e.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
