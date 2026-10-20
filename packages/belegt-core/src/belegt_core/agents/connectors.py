# Spec: 04 §6.1（belegt-connector/1：工具命名、只读约束、统一 envelope {records, query_hash, fetched_at, source_system, attested}、
#       数值一律 DEC-STR）、§2.4（MCP 只读：docs.search、docs.get_page、erp.*.query）、07 §3.4 RT-PI-11（描述哈希锁定）
"""evidence-miner 使用的本地连接器（与 ``belegt_core.mcp.client`` 的规范一致）。

- ``LocalERPConnector``：进程内 ERP 替身（fixture 记录），工具 ``erp.<system>.facts.query``，返回 belegt-connector/1 envelope；
- ``DocumentIndex``：租户文档库（证据图 blob），工具 ``docs.search`` / ``docs.get_page``。
真实连接器以 MCP（stdio / Streamable HTTP）接入，见 ``belegt_core.mcp.client.MCPConnector``。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from ..canonical import hash_obj


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=lambda: {"readOnlyHint": True, "destructiveHint": False})


class LocalERPConnector:
    """``records``: ``[{record_key, fact_key|datapoint_id, value, unit, period{start,end}, fields{}}]``（value 为 DEC-STR）。"""

    def __init__(self, system: str, records: list[dict[str, Any]], *, attested: bool = True,
                 fetched_at: str | None = None, connector_id: str | None = None) -> None:
        self.system = system
        self.connector_id = connector_id or f"conn:erp-{system}"
        self.records = list(records)
        self.attested = attested
        self.fetched_at = fetched_at
        self.available = True
        self.tools = [ToolSpec(f"erp.{system}.facts.query", "Query attested activity data and KPIs (read-only).",
                               {"type": "object", "required": ["date_from", "date_to"],
                                "properties": {"fact_key": {"type": "string"}, "datapoint_id": {"type": "string"},
                                               "date_from": {"type": "string", "format": "date"},
                                               "date_to": {"type": "string", "format": "date"}}})]

    def manifest(self) -> dict[str, Any]:
        return {"format": "belegt-connector/1", "id": self.connector_id, "attested": self.attested, "scopes": ["read"],
                "tools": [{"name": t.name, "description": t.description, "inputSchema": t.input_schema,
                           "annotations": t.annotations} for t in self.tools]}

    def call(self, tool: str, args: dict[str, Any], *, now: str) -> dict[str, Any]:
        if not self.available:
            raise ConnectionError(f"{self.connector_id} unavailable")
        if tool != self.tools[0].name:
            raise KeyError(tool)
        out = []
        for r in self.records:
            if args.get("fact_key") and r.get("fact_key") != args["fact_key"]:
                continue
            if args.get("datapoint_id") and r.get("datapoint_id") != args["datapoint_id"]:
                continue
            p = r.get("period") or {}
            if p and not (p.get("start", "0") <= args["date_to"] and p.get("end", "9") >= args["date_from"]):
                continue
            out.append({"record_key": r["record_key"], "fields": {"value": str(r["value"]), **(r.get("fields") or {})},
                        "unit": r.get("unit"), "period": p or None, "value_type": r.get("value_type", "number"),
                        "fact_key": r.get("fact_key"), "datapoint_id": r.get("datapoint_id")})
        return {"records": out, "query_hash": hash_obj({"tool": tool, "args": args}),
                "fetched_at": self.fetched_at or now, "source_system": self.system, "attested": self.attested}


class DocumentIndex:
    """文档库：``docs.search``（按 fact/标签检索已登记 blob）、``docs.get_page``。"""

    def __init__(self, store: Any, org_id: str) -> None:
        self.store, self.org_id = store, org_id
        self.docs: list[dict[str, Any]] = []  # {blob_sha256, mime, title, kind: bill|register, tags}
        self.tools = [ToolSpec("docs.search", "Search tenant documents (read-only).", {"type": "object"}),
                      ToolSpec("docs.get_page", "Get one page/sheet of a document (read-only).", {"type": "object"})]

    def add(self, data: bytes, mime: str, title: str, kind: str = "document", tags: list[str] | None = None) -> str:
        sha = self.store.put_blob(self.org_id, data, mime)
        if not any(d["blob_sha256"] == sha for d in self.docs):
            self.docs.append({"blob_sha256": sha, "mime": mime, "title": title, "kind": kind, "tags": list(tags or [])})
        return sha

    def search(self, query: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        return [d for d in self.docs if kind is None or d["kind"] == kind]

    def get(self, sha: str) -> bytes | None:
        b = self.store.get_blob(self.org_id, sha)
        return b[0] if b else None


def utc_now_str(store: Any) -> str:
    return store.peek_now()[:19] + "Z" if store is not None else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
