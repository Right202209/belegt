# Spec: 04 §6.1（只读、envelope、cursor 分页、限流自报、合规套件）；03 §3.5 Locator.connector
"""连接器基类：工具注册（只读断言）→ 输入校验 → 处理 → 增量过滤 → 分页 → envelope + locator → 输出校验。

处理函数（handler）签名：`handler(args: dict, ctx: CallContext) -> list[dict] | Page`。

- 返回 `list[dict]`：完整、**按稳定顺序**排好的记录；基类负责增量过滤（按记录的 `_wm` 水位）与分页切片。
- 返回 `Page`：handler 自己做了分页（如 SQL 大表），基类只做包装。

记录（record）字段：`record_key`（必填，同一连接器实例内稳定唯一）、`fields`（任意，数值将转 DEC-STR）、
`unit?`、`period?`、`locator?`（文件类给 blob_sha256 + page/cell/bbox），以及内部字段 `_wm`（增量水位，可比较）。
基类给每条记录补 `locator.connector = {connector_id, query_hash, record_key, fetched_at}`（03 §3.5），
使任何记录都能被 evidence-miner 直接写成 Evidence 的定位。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import jsonschema

from .spec import (
    DEFAULT_PAGE_SIZE, ENVELOPE_SCHEMA, MAX_PAGE_SIZE, SPEC_VERSION, AuthSpec, ConnectorError, ConnectorManifest,
    RateLimit, ToolSpec, decode_cursor, decode_sync_token, encode_cursor, encode_sync_token, query_hash, to_wire,
    utc_now_iso,
)

_VALIDATOR_CACHE: dict[int, jsonschema.Draft202012Validator] = {}


def _validator(schema: dict[str, Any]) -> jsonschema.Draft202012Validator:
    key = id(schema)
    v = _VALIDATOR_CACHE.get(key)
    if v is None:
        v = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
        _VALIDATOR_CACHE[key] = v
    return v


@dataclass
class CallContext:
    connector_id: str
    tool: str
    args: dict[str, Any]
    query_hash: str
    fetched_at: str
    offset: int
    page_size: int
    since: Any = None  # 增量水位（解码后的 sync_token）


@dataclass
class Page:
    records: list[dict[str, Any]]
    next_offset: int | None = None
    total: int | None = None
    watermark: Any = None


@dataclass
class SecretResolver:
    """秘密解析：`env:NAME` 读环境变量；`vault:<key>` 读注入的保险库映射。值不进入日志、manifest 与输出。"""

    vault: Mapping[str, str] = field(default_factory=dict)

    def resolve(self, ref: str | None) -> str:
        if not ref:
            raise ConnectorError("CONNECTOR_AUTH_MISSING", "no secret_ref configured")
        scheme, _, key = ref.partition(":")
        if scheme == "env":
            val = os.environ.get(key)
        elif scheme == "vault":
            val = self.vault.get(key)
        else:
            raise ConnectorError("CONNECTOR_AUTH_MISSING", f"unsupported secret_ref scheme {scheme!r}")
        if not val:
            raise ConnectorError("CONNECTOR_AUTH_MISSING", f"secret {ref!r} not available")
        return val


class BaseConnector:
    """所有连接器的基类。子类在 `__init__` 中调用 `self.register(...)` 注册工具。"""

    kind: str = "base"
    category: str = "file"
    system: str = "base"
    title: str = "Belegt connector"
    version: str = "0.1.0"
    source_system: str = "unknown"
    attested: bool = False
    rate_limit: RateLimit = RateLimit()

    def __init__(self, connector_id: str | None = None, *, auth: AuthSpec | None = None,
                 secrets: SecretResolver | None = None, clock: Callable[[], str] | None = None) -> None:
        self.connector_id = connector_id or f"conn:{self.kind}:default"
        self.auth = auth or AuthSpec()
        self.secrets = secrets or SecretResolver()
        self._clock = clock or utc_now_iso
        self._tools: dict[str, ToolSpec] = {}

    # —— 注册 ——
    def register(self, name: str, description: str, input_schema: dict[str, Any],
                 handler: Callable[[dict[str, Any], CallContext], Any], *, paginated: bool = False,
                 incremental: bool = False, evidence_source_type: str = "none") -> ToolSpec:
        spec = ToolSpec(name=name, description=description, input_schema=input_schema, handler=handler,
                        paginated=paginated, incremental=incremental, evidence_source_type=evidence_source_type)
        jsonschema.Draft202012Validator.check_schema(input_schema)
        if input_schema.get("type") != "object":
            raise ConnectorError("CONNECTOR_INPUT_INVALID", f"{name}: inputSchema must be an object schema")
        if name in self._tools:
            raise ConnectorError("CONNECTOR_TOOL_NAME_INVALID", f"duplicate tool {name}")
        self._tools[name] = spec
        return spec

    def tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def tool(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ConnectorError("CONNECTOR_TOOL_UNKNOWN", f"unknown tool {name!r}") from None

    def scopes(self) -> list[str]:
        return sorted({f"{t.name.rsplit('.', 2)[0]}.read" for t in self._tools.values()})

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            connector_id=self.connector_id, kind=self.kind, title=self.title, version=self.version,
            source_system=self.source_system, category=self.category, scopes=self.scopes(),
            attested=self.attested, auth=self.auth, rate_limit=self.rate_limit,
            tools=[t.mcp_descriptor() for t in self._tools.values()],
        )

    # —— 合规探针（合规套件用的最小合法参数；子类覆盖） ——
    def compliance_probes(self) -> dict[str, dict[str, Any]]:
        return {}

    # —— 调用 ——
    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = self.tool(tool)
        args = dict(args or {})
        errors = sorted(_validator(spec.input_schema).iter_errors(args), key=lambda e: list(e.path))
        if errors:
            e = errors[0]
            raise ConnectorError("CONNECTOR_INPUT_INVALID", f"{tool}: {e.message}",
                                 details={"path": "/".join(str(p) for p in e.path)})
        qh = query_hash(self.connector_id, tool, args)
        page_size = int(args.get("page_size") or DEFAULT_PAGE_SIZE)
        if page_size > MAX_PAGE_SIZE:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", "page_size > 1000")
        offset = decode_cursor(args.get("cursor"), qh) if spec.paginated else 0
        since = decode_sync_token(args.get("sync_token"), qh) if spec.incremental else None
        ctx = CallContext(self.connector_id, tool, args, qh, self._clock(), offset, page_size, since)
        result = spec.handler(args, ctx)
        if isinstance(result, Page):
            page = result
        else:
            rows = list(result)
            watermark = None
            if spec.incremental:
                wms = [r.get("_wm") for r in rows if r.get("_wm") is not None]
                watermark = max(wms) if wms else since
                if since is not None:
                    rows = [r for r in rows if r.get("_wm") is not None and r["_wm"] > since]
            total = len(rows)
            if spec.paginated:
                chunk = rows[offset: offset + page_size]
                nxt = offset + page_size if offset + page_size < total else None
            else:
                chunk, nxt = rows, None
            page = Page(chunk, nxt, total, watermark)
        env = self._envelope(tool, qh, ctx.fetched_at, page, spec)
        self._check_output(env)
        return env

    def iter_all(self, tool: str, args: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """遍历所有分页，逐页产出 envelope。"""
        args = dict(args or {})
        while True:
            env = self.call(tool, args)
            yield env
            if not env.get("next_cursor"):
                return
            args["cursor"] = env["next_cursor"]

    def sync(self, tool: str, args: dict[str, Any] | None = None,
             sync_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        """增量同步：拉取 sync_token 之后的全部记录（跨页），返回 (records, 新 sync_token)。"""
        a = dict(args or {})
        if sync_token:
            a["sync_token"] = sync_token
        records: list[dict[str, Any]] = []
        token = sync_token
        for env in self.iter_all(tool, a):
            records.extend(env["records"])
            token = env.get("sync_token") or token
        return records, token

    # —— 内部 ——
    def _envelope(self, tool: str, qh: str, fetched_at: str, page: Page, spec: ToolSpec) -> dict[str, Any]:
        out_records = []
        for r in page.records:
            rk = str(r["record_key"])
            rec: dict[str, Any] = {"record_key": rk, "fields": to_wire(r.get("fields") or {})}
            if r.get("unit") is not None:
                rec["unit"] = r["unit"]
            if r.get("period") is not None:
                rec["period"] = to_wire(r["period"])
            loc = dict(r.get("locator") or {})
            loc["connector"] = {"connector_id": self.connector_id, "query_hash": qh, "record_key": rk,
                                "fetched_at": fetched_at}
            rec["locator"] = loc
            out_records.append(rec)
        env: dict[str, Any] = {
            "spec": SPEC_VERSION,
            "connector_id": self.connector_id,
            "tool": tool,
            "source_system": self.source_system,
            "records": out_records,
            "query_hash": qh,
            "fetched_at": fetched_at,
            "attested": bool(self.attested),
            "next_cursor": encode_cursor(qh, page.next_offset) if page.next_offset is not None else None,
            "total": page.total,
        }
        if spec.incremental:
            env["sync_token"] = encode_sync_token(qh, page.watermark) if page.watermark is not None else None
        return env

    def _check_output(self, env: dict[str, Any]) -> None:
        errors = list(_validator(ENVELOPE_SCHEMA).iter_errors(env))
        if errors:
            raise ConnectorError("CONNECTOR_OUTPUT_INVALID", errors[0].message)


def safe_join(root: Path, rel: str) -> Path:
    """把相对路径解析到 root 之下；越界（`..`、绝对路径、符号链接跳出）拒绝。"""
    root = root.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ConnectorError("CONNECTOR_PATH_DENIED", f"path {rel!r} escapes connector root")
    return p
