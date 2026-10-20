# Spec: 04 §6.1（Belegt 作为 MCP client：belegt-connector/1 —— 工具命名 <category>.<system>.<resource>.<verb>，verb ∈ query/get/list/
#       describe；不支持点号的客户端映射为 "__"；必须 readOnlyHint=true、destructiveHint=false，guard 拒绝非只读工具；统一 envelope
#       {records, query_hash, fetched_at, source_system, attested}；数值 DEC-STR，由 evidence-miner 写入 Locator.connector）、
#       07 §3.4 RT-PI-11（工具描述哈希锁定）、03 §3.5（Evidence / Locator）
"""MCP client 连接器适配：调用外部只读连接器 MCP server，把返回 envelope 转为 ``EvidenceCandidate``。

``MCPConnector`` 与 ``agents.connectors.LocalERPConnector`` 接口一致（``connector_id``、``tools``、``manifest()``、
``call(tool, args, *, now)``、``available``），因此 evidence-miner 可直接替换使用。

连接目标 ``target``：MCP server 对象（进程内，测试）、URL（Streamable HTTP）、``mcp.StdioServerParameters``（本地进程）或任意
``mcp.client.Transport``。每次调用开一个会话（``async with connector.session()`` 可在多次调用间复用）。
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Literal

from pydantic import BaseModel, Field

ALLOWED_VERBS = ("query", "get", "list", "describe")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){2,}$")
DEC_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")


class ConnectorPolicyError(Exception):
    """连接器违反 belegt-connector/1（非只读工具、工具名不合规、描述哈希变化）。``code`` 形如 ``CONNECTOR_NOT_READONLY``。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class ConnectorCallError(Exception):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.details = code, message, details


class EvidenceCandidate(BaseModel):
    """03 §3.5 Evidence 候选（``status=candidate``；晋升 verified 由 evidence-miner / guard 的 EV-AUTO-* 规则决定）。"""

    source_type: Literal["erp", "document"] = "erp"
    fact_key: str | None = None
    datapoint_id: str | None = None
    extracted_value: str | bool | dict[str, Any] | None = None   # 数字为 DEC-STR
    value_type: Literal["number", "text", "enum", "bool", "date", "file", "geo"] = "number"
    unit: str | None = None
    period: dict[str, str] | None = None
    locator: dict[str, Any]
    confidence: float = Field(ge=0, le=1)
    status: Literal["candidate"] = "candidate"
    attested: bool = False
    source_system: str | None = None
    record_key: str | None = None
    tool: str | None = None
    tags: list[str] = Field(default_factory=list)

    def to_evidence_fields(self, boundary: str = "organization", *, fact_key: str | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {"source_type": self.source_type, "fact_key": fact_key or self.fact_key or "unclassified.quantity",
                             "extracted_value": self.extracted_value, "value_type": self.value_type, "boundary": boundary,
                             "locator": dict(self.locator), "confidence": round(self.confidence, 4), "status": "candidate",
                             "valid_from": (self.period or {}).get("start") or
                             str((self.locator.get("connector") or {}).get("fetched_at", ""))[:10] or None,
                             "tags": list(self.tags)}
        if self.unit:
            d["unit"] = self.unit
        if self.period:
            d["period"] = dict(self.period)
        if self.datapoint_id:
            d["datapoint_id"] = self.datapoint_id
        return {k: v for k, v in d.items() if v is not None}


def canonical_tool_name(name: str) -> str:
    return name.replace("__", ".")


def check_tool(tool: dict[str, Any], *, strict_names: bool = True) -> None:
    """guard：拒绝非只读工具（04 §6.1）；``strict_names`` 时检查命名与 verb。"""
    ann = tool.get("annotations") or {}
    if ann.get("readOnlyHint") is not True or ann.get("destructiveHint", True) is not False:
        raise ConnectorPolicyError("CONNECTOR_NOT_READONLY", f"tool {tool.get('name')!r} must declare readOnlyHint=true, "
                                                             "destructiveHint=false")
    if strict_names:
        n = canonical_tool_name(tool.get("name") or "")
        if not TOOL_NAME_RE.match(n) or n.rsplit(".", 1)[-1] not in ALLOWED_VERBS:
            raise ConnectorPolicyError("CONNECTOR_TOOL_NAME_INVALID", f"tool {n!r} is not <category>.<system>.<resource>.<verb>")


def description_hash(tool: dict[str, Any]) -> str:
    blob = json.dumps({"name": tool.get("name"), "description": tool.get("description") or "",
                       "inputSchema": tool.get("inputSchema") or {}}, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _get(o: Any, *names: str) -> Any:
    for n in names:
        if isinstance(o, dict) and n in o:
            return o[n]
        if hasattr(o, n):
            return getattr(o, n)
    return None


def _tool_dict(t: Any) -> dict[str, Any]:
    ann = _get(t, "annotations")
    if ann is not None and not isinstance(ann, dict):
        ann = {"readOnlyHint": _get(ann, "read_only_hint", "readOnlyHint"),
               "destructiveHint": _get(ann, "destructive_hint", "destructiveHint"),
               "idempotentHint": _get(ann, "idempotent_hint", "idempotentHint")}
    return {"name": _get(t, "name"), "description": _get(t, "description") or "",
            "inputSchema": _get(t, "input_schema", "inputSchema") or {}, "outputSchema": _get(t, "output_schema", "outputSchema"),
            "annotations": {k: v for k, v in (ann or {}).items() if v is not None}}


def _dec(v: Any) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip()
    if DEC_RE.match(s):
        return s
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    out = format(d, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return "0" if out in ("-0", "") else out


def envelope_to_candidates(envelope: dict[str, Any], *, connector_id: str | None = None, tool: str | None = None,
                           fact_key: str | None = None, datapoint_id: str | None = None, value_field: str | None = None,
                           value_type: str | None = None) -> list[EvidenceCandidate]:
    """belegt-connector/1 envelope → ``EvidenceCandidate[]``。

    - ``locator``：记录自带 locator 优先；否则构造 ``{connector{connector_id, query_hash, record_key, fetched_at}}``。
    - ``source_type``：locator 有 ``blob_sha256`` 且有 page/cell/bbox → ``document``，否则 ``erp``。
    - 数字一律 DEC-STR（非法数字的记录跳过，``reasons`` 不外露原文）；``confidence``：attested 0.95，否则 0.8（晋升阈值见 04 §2.1）。
    """
    cid = connector_id or envelope.get("connector_id") or f"conn:{envelope.get('source_system') or 'mcp'}"
    out = []
    for rec in envelope.get("records") or []:
        fields = rec.get("fields") or {}
        vf = value_field or next((k for k in ("extracted_value", "quantity", "value") if k in fields), None)
        if vf is None:
            continue
        vt = value_type or rec.get("value_type") or "number"
        raw = fields.get(vf)
        if vt == "number":
            val: Any = _dec(raw)
            if val is None:
                continue
        else:
            val = raw
        loc = dict(rec.get("locator") or {})
        if not loc.get("connector") and not loc.get("blob_sha256"):
            loc["connector"] = {"connector_id": cid, "query_hash": envelope.get("query_hash"),
                                "record_key": str(rec.get("record_key")), "fetched_at": envelope.get("fetched_at")}
        st = "document" if loc.get("blob_sha256") and any(k in loc for k in ("page", "cell", "bbox")) else "erp"
        conf = float(fields["confidence"]) if "confidence" in fields else (0.95 if envelope.get("attested") else 0.8)
        out.append(EvidenceCandidate(
            source_type=st, fact_key=fact_key or rec.get("fact_key"), datapoint_id=datapoint_id or rec.get("datapoint_id"),
            extracted_value=val, value_type=vt, unit=rec.get("unit") or fields.get("unit"),
            period=dict(rec["period"]) if rec.get("period") else None, locator=loc, confidence=max(0.0, min(conf, 1.0)),
            attested=bool(envelope.get("attested")), source_system=envelope.get("source_system"),
            record_key=str(rec.get("record_key")) if rec.get("record_key") is not None else None, tool=tool or envelope.get("tool"),
            tags=[f"connector:{cid}", f"attested:{str(bool(envelope.get('attested'))).lower()}"] + ([f"tool:{tool}"] if tool else [])))
    return out


def _run_sync(coro_fn: Any) -> Any:
    """在同步上下文运行协程；当前线程已有事件循环时换一个线程运行（Activity/路由中均可用）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_fn())
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["v"] = asyncio.run(coro_fn())
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


@dataclass
class _ToolSpec:  # 与 agents.connectors.ToolSpec 字段一致
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)


class MCPConnector:
    def __init__(self, target: Any, *, connector_id: str | None = None, system: str | None = None, attested: bool | None = None,
                 strict_names: bool = True, pinned_hashes: dict[str, str] | None = None, timeout_s: float = 60.0) -> None:
        self.target = target
        self.connector_id = connector_id or f"conn:mcp-{system or 'external'}"
        self.system = system
        self.attested_override = attested
        self.strict_names = strict_names
        self.pinned_hashes = dict(pinned_hashes or {})
        self.timeout_s = timeout_s
        self.available = True
        self.tools: list[_ToolSpec] = []
        self._raw_tools: dict[str, dict[str, Any]] = {}
        self._exposed: dict[str, str] = {}  # canonical → 服务器上的名字（可能是 a__b__c）
        self._client: Any = None

    # ------------------------------------------------------------------ session
    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator["MCPConnector"]:
        from mcp import Client

        if self._client is not None:
            yield self
            return
        async with Client(self.target, read_timeout_seconds=self.timeout_s) as c:
            self._client = c
            try:
                yield self
            finally:
                self._client = None

    async def _with_client(self, fn: Any) -> Any:
        if self._client is not None:
            return await fn(self._client)
        async with self.session():
            return await fn(self._client)

    # ------------------------------------------------------------------ tools
    async def refresh(self) -> list[_ToolSpec]:
        """列工具 + 只读/命名检查 + 描述哈希锁定（首次连接记录，之后变化即拒绝，RT-PI-11）。"""

        async def go(c: Any) -> Any:
            return await c.list_tools()

        res = await self._with_client(go)
        tools = [_tool_dict(t) for t in (_get(res, "tools") or [])]
        specs, raw, exposed = [], {}, {}
        for t in tools:
            check_tool(t, strict_names=self.strict_names)
            name = canonical_tool_name(t["name"])
            h = description_hash(t)
            if name in self.pinned_hashes and self.pinned_hashes[name] != h:
                raise ConnectorPolicyError("CONNECTOR_DESCRIPTION_CHANGED", f"tool {name!r} description hash changed")
            self.pinned_hashes.setdefault(name, h)
            raw[name], exposed[name] = t, t["name"]
            specs.append(_ToolSpec(name, t["description"], t["inputSchema"], t["annotations"]))
        self.tools, self._raw_tools, self._exposed = specs, raw, exposed
        return specs

    def refresh_sync(self) -> list[_ToolSpec]:
        return _run_sync(self.refresh)

    def manifest(self) -> dict[str, Any]:
        if not self.tools:
            self.refresh_sync()
        return {"format": "belegt-connector/1", "id": self.connector_id, "transport": "mcp", "scopes": ["read"],
                "attested": self.attested_override, "pinned_hashes": dict(self.pinned_hashes),
                "tools": [{"name": t.name, "description": t.description, "inputSchema": t.input_schema,
                           "annotations": t.annotations} for t in self.tools]}

    # ------------------------------------------------------------------ calls
    async def call_async(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.tools:
            await self.refresh()
        name = canonical_tool_name(tool)
        if name not in self._exposed:
            raise ConnectorCallError("CONNECTOR_TOOL_UNKNOWN", f"{tool!r} not offered by {self.connector_id}")

        async def go(c: Any) -> Any:
            return await c.call_tool(self._exposed[name], {k: v for k, v in args.items() if v is not None})

        try:
            res = await self._with_client(go)
        except ConnectorCallError:
            raise
        except Exception as e:  # noqa: BLE001 - 传输/协议错误 → 连接器不可用（evidence-miner 降级）
            self.available = False
            raise ConnectionError(f"{self.connector_id}: {type(e).__name__}: {e}") from e
        self.available = True
        texts = [getattr(b, "text", None) for b in (_get(res, "content") or [])]
        texts = [t for t in texts if t]
        if _get(res, "is_error", "isError"):
            code, msg, det = "CONNECTOR_SOURCE_ERROR", " ".join(texts)[:500], None
            for t in texts:
                with contextlib.suppress(Exception):
                    j = json.loads(t[t.index("{"):])
                    err = j.get("error") or {}
                    code, msg, det = err.get("code", code), err.get("message", msg), err.get("details")
                    break
            raise ConnectorCallError(code, msg, det)
        env = _get(res, "structured_content", "structuredContent")
        if env is None:
            for t in texts:
                with contextlib.suppress(Exception):
                    env = json.loads(t)
                    break
        if isinstance(env, dict) and "result" in env and "records" not in env and isinstance(env["result"], dict):
            env = env["result"]  # 部分 SDK 把非对象输出包成 {"result": ...}
        if not isinstance(env, dict) or "records" not in env:
            raise ConnectorCallError("CONNECTOR_OUTPUT_INVALID", f"{name}: envelope without records")
        env = dict(env)
        env.setdefault("connector_id", self.connector_id)
        env.setdefault("tool", name)
        if self.attested_override is not None:
            env["attested"] = bool(self.attested_override) and bool(env.get("attested", True))
        return env

    def call(self, tool: str, args: dict[str, Any], *, now: str | None = None) -> dict[str, Any]:
        """同步调用（与 ``LocalERPConnector.call`` 同签名）；``fetched_at`` 缺失时补 ``now``。"""
        env = _run_sync(lambda: self.call_async(tool, args))
        if not env.get("fetched_at"):
            env["fetched_at"] = now or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return env

    async def fetch_candidates_async(self, tool: str, args: dict[str, Any], **kw: Any) -> list[EvidenceCandidate]:
        env = await self.call_async(tool, args)
        return envelope_to_candidates(env, connector_id=env.get("connector_id") or self.connector_id, tool=canonical_tool_name(tool), **kw)

    def fetch_candidates(self, tool: str, args: dict[str, Any], **kw: Any) -> list[EvidenceCandidate]:
        return _run_sync(lambda: self.fetch_candidates_async(tool, args, **kw))


__all__ = ["MCPConnector", "EvidenceCandidate", "envelope_to_candidates", "check_tool", "description_hash",
           "canonical_tool_name", "ConnectorPolicyError", "ConnectorCallError", "ALLOWED_VERBS"]
