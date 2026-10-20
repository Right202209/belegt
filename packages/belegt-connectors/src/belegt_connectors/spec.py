# Spec: 04 §6.1（连接器规范 belegt-connector/1，FR-005/FR-203）；03 §2.2 DEC-STR、§3.5 Locator
"""`belegt-connector/1` 规范的机器可读部分：工具命名、只读注解、manifest、输出 envelope、分页游标、query_hash。

所有连接器（本包内置的与社区贡献的）都必须满足这里的约束，并通过 `belegt_connectors.compliance` 合规套件。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SPEC_VERSION = "belegt-connector/1"

#: 04 §6.1：`<category>.<system>.<resource>.<verb>`；verb 仅允许以下四个
ALLOWED_VERBS: frozenset[str] = frozenset({"query", "get", "list", "describe"})
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.(query|get|list|describe)$")
#: 已登记的 category（新 category 需 RFC）
CATEGORIES: frozenset[str] = frozenset({"file", "db", "erp", "mail", "docs", "portal"})

MAX_PAGE_SIZE = 1000  # 04 §6.1：单页 ≤ 1000 行
DEFAULT_PAGE_SIZE = 200

SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DEC_STR_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")
CELL_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]*(:[A-Z]{1,3}[1-9][0-9]*)?$")


class ConnectorError(Exception):
    """连接器错误。`code` 取自本模块 `ERROR_CODES`。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


ERROR_CODES = {
    "CONNECTOR_TOOL_UNKNOWN": "未注册的工具",
    "CONNECTOR_TOOL_NAME_INVALID": "工具名不符合 <category>.<system>.<resource>.<verb>",
    "CONNECTOR_NOT_READONLY": "工具未声明只读（readOnlyHint=true、destructiveHint=false）或 verb 不在白名单",
    "CONNECTOR_INPUT_INVALID": "输入不符合工具 inputSchema",
    "CONNECTOR_OUTPUT_INVALID": "输出不符合 envelope schema（实现缺陷）",
    "CONNECTOR_CURSOR_INVALID": "游标无效或不属于本查询",
    "CONNECTOR_NOT_FOUND": "资源不存在",
    "CONNECTOR_AUTH_MISSING": "缺少凭据（保险库/环境变量中无对应 secret）",
    "CONNECTOR_WRITE_DENIED": "只读连接器拒绝写语句",
    "CONNECTOR_SOURCE_ERROR": "源系统错误",
    "CONNECTOR_PATH_DENIED": "路径越出连接器根目录",
    "CONNECTOR_LIMIT": "结果超出大小上限",
}


# —— 值约定 ——

def dec_str(value: Any) -> str:
    """把数值规范化为 DEC-STR（03 §2.2）：无指数、无前导 +、无尾随 0、`-0`→`0`。

    float 先经 `repr` 转字符串再进 Decimal，避免二进制误差扩散（源系统给 float 时的最佳努力）。
    """
    if isinstance(value, bool):
        raise ValueError("bool is not a number")
    if isinstance(value, float):
        value = repr(value)
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"not a decimal: {value!r}") from e
    if not d.is_finite():
        raise ValueError(f"not finite: {value!r}")
    if d == d.to_integral_value():
        s = f"{d.quantize(Decimal(1)):f}"
    else:
        s = f"{d.normalize():f}"
    if s in ("-0", "-0.0"):
        s = "0"
    if not DEC_STR_RE.match(s):  # pragma: no cover - 防御
        raise ValueError(f"cannot render DEC-STR for {value!r}")
    return s


def to_wire(value: Any) -> Any:
    """把源系统值转为线上表示：数字 → DEC-STR，日期 → ISO，datetime → UTC ISO 8601，bytes → 拒绝。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float, Decimal)):
        return dec_str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_wire(v) for v in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes_sha256": "sha256:" + hashlib.sha256(bytes(value)).hexdigest(), "size": len(value)}
    return str(value)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    """确定性 JSON（键排序、紧凑、NFC），用于 query_hash / record_key 哈希。"""

    def norm(o: Any) -> Any:
        if isinstance(o, str):
            return unicodedata.normalize("NFC", o)
        if isinstance(o, dict):
            return {norm(k): norm(v) for k, v in o.items() if v is not None}
        if isinstance(o, (list, tuple)):
            return [norm(v) for v in o]
        if isinstance(o, (Decimal, int, float)) and not isinstance(o, bool):
            return dec_str(o)
        return o

    return json.dumps(norm(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


#: 不进入 query_hash 的参数（分页/增量控制参数不改变“这是哪个查询”）
NON_SEMANTIC_ARGS = frozenset({"cursor", "page_size", "sync_token"})


def query_hash(connector_id: str, tool: str, args: dict[str, Any]) -> str:
    """04 §6.1 / 03 §3.5 `Locator.connector.query_hash`：同一连接器实例 + 工具 + 语义参数 → 同一哈希，可重放。"""
    sem = {k: v for k, v in sorted(args.items()) if k not in NON_SEMANTIC_ARGS}
    payload = canonical_json({"connector_id": connector_id, "tool": tool, "args": sem, "spec": SPEC_VERSION})
    return sha256_bytes(payload.encode("utf-8"))


# —— 游标与增量同步令牌（不透明，base64url(JSON)） ——

def _b64(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _unb64(tok: str) -> dict[str, Any]:
    pad = "=" * (-len(tok) % 4)
    try:
        out = json.loads(base64.urlsafe_b64decode(tok + pad).decode())
    except Exception as e:
        raise ConnectorError("CONNECTOR_CURSOR_INVALID", "malformed token") from e
    if not isinstance(out, dict):
        raise ConnectorError("CONNECTOR_CURSOR_INVALID", "malformed token")
    return out


def encode_cursor(qhash: str, offset: int) -> str:
    return _b64({"o": offset, "q": qhash[7:23]})


def decode_cursor(cursor: str | None, qhash: str) -> int:
    if not cursor:
        return 0
    d = _unb64(cursor)
    if d.get("q") != qhash[7:23] or not isinstance(d.get("o"), int) or d["o"] < 0:
        raise ConnectorError("CONNECTOR_CURSOR_INVALID", "cursor does not belong to this query")
    return d["o"]


def encode_sync_token(qhash: str, watermark: Any) -> str:
    return _b64({"w": watermark, "q": qhash[7:23]})


def decode_sync_token(token: str | None, qhash: str) -> Any:
    if not token:
        return None
    d = _unb64(token)
    if d.get("q") != qhash[7:23]:
        raise ConnectorError("CONNECTOR_CURSOR_INVALID", "sync_token does not belong to this query")
    return d.get("w")


# —— 模型 ——

class ToolSpec(BaseModel):
    """一个只读工具。`input_schema` 为 JSON Schema（Draft 2020-12），由连接器作者手写，MCP 层原样暴露。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any] = Field(exclude=True)
    paginated: bool = False
    incremental: bool = False
    #: 记录是否可直接成为 Evidence，以及默认 source_type
    evidence_source_type: Literal["erp", "document", "none"] = "none"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        validate_tool_name(v)
        return v

    @property
    def annotations(self) -> dict[str, Any]:
        """04 §6.1：全部工具 readOnlyHint=true、destructiveHint=false（不可被覆盖）。"""
        return {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

    def mcp_descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "annotations": self.annotations,
            "inputSchema": self.input_schema,
            "outputSchema": ENVELOPE_SCHEMA,
        }


class RateLimit(BaseModel):
    requests_per_minute: int = 60
    burst: int = 10


class AuthSpec(BaseModel):
    """连接器认证声明（04 §6.1）。秘密值从不出现在 manifest / 输出中，只记引用。"""

    mode: Literal["none", "local_file", "basic", "api_key", "oauth2_client_credentials", "oauth2_pkce", "mtls"] = "none"
    secret_ref: str | None = None  # 如 "env:BELEGT_SECRET_ERP" 或 "vault:erp/sap-b1"
    transport: Literal["stdio", "streamable-http"] = "stdio"
    notes: str | None = None


class ConnectorManifest(BaseModel):
    """连接器 manifest：guard 在注册时读取（只读断言、scopes、attested）。"""

    spec: Literal["belegt-connector/1"] = SPEC_VERSION
    connector_id: str
    kind: str
    title: str
    version: str
    source_system: str
    category: str
    scopes: list[str]
    attested: bool = False
    read_only: Literal[True] = True
    auth: AuthSpec = Field(default_factory=AuthSpec)
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    max_page_size: int = MAX_PAGE_SIZE
    tools: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: list[str]) -> list[str]:
        for s in v:
            if not s.endswith(".read"):
                raise ValueError(f"scope {s!r} is not read-only (must end with '.read')")
        return v


def validate_tool_name(name: str) -> None:
    if not TOOL_NAME_RE.match(name):
        parts = name.split(".")
        if len(parts) == 4 and parts[3] not in ALLOWED_VERBS:
            raise ConnectorError("CONNECTOR_NOT_READONLY", f"verb {parts[3]!r} not allowed in {name!r}")
        raise ConnectorError("CONNECTOR_TOOL_NAME_INVALID", f"invalid tool name {name!r}")
    if name.split(".")[0] not in CATEGORIES:
        raise ConnectorError("CONNECTOR_TOOL_NAME_INVALID", f"unknown category in {name!r}")


def mcp_safe_name(name: str) -> str:
    """不支持点号的 MCP 客户端：`.` → `__`（04 §6.1）。"""
    return name.replace(".", "__")


def from_mcp_safe_name(name: str) -> str:
    return name.replace("__", ".")


# —— JSON Schema ——

LOCATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "blob_sha256": {"type": "string", "pattern": SHA256_RE.pattern},
        "mime": {"type": "string"},
        "page": {"type": "integer", "minimum": 1},
        "sheet": {"type": "string"},
        "cell": {"type": "string", "pattern": CELL_RE.pattern},
        "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "text_excerpt_hash": {"type": "string", "pattern": SHA256_RE.pattern},
        "connector": {
            "type": "object",
            "required": ["connector_id", "query_hash", "record_key", "fetched_at"],
            "properties": {
                "connector_id": {"type": "string"},
                "query_hash": {"type": "string", "pattern": SHA256_RE.pattern},
                "record_key": {"type": "string"},
                "fetched_at": {"type": "string", "format": "date-time"},
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
    "minProperties": 1,
}

PERIOD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["start", "end"],
    "properties": {"start": {"type": "string", "format": "date"}, "end": {"type": "string", "format": "date"}},
    "additionalProperties": False,
}

RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["record_key", "fields"],
    "properties": {
        "record_key": {"type": "string", "minLength": 1},
        "fields": {"type": "object"},
        "unit": {"type": ["string", "null"]},
        "period": {"oneOf": [PERIOD_SCHEMA, {"type": "null"}]},
        "locator": LOCATOR_SCHEMA,
    },
}

#: 04 §6.1 统一 envelope（必填 records/query_hash/fetched_at/attested；其余为本规范扩展）
ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["records", "query_hash", "fetched_at", "attested"],
    "properties": {
        "records": {"type": "array", "items": RECORD_SCHEMA, "maxItems": MAX_PAGE_SIZE},
        "query_hash": {"type": "string", "pattern": SHA256_RE.pattern},
        "fetched_at": {"type": "string", "format": "date-time"},
        "source_system": {"type": "string"},
        "attested": {"type": "boolean"},
        "connector_id": {"type": "string"},
        "tool": {"type": "string"},
        "spec": {"const": SPEC_VERSION},
        "next_cursor": {"type": ["string", "null"]},
        "sync_token": {"type": ["string", "null"]},
        "total": {"type": ["integer", "null"]},
    },
}


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """工具 inputSchema 的统一外壳：object、禁止未知参数。"""
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


CURSOR_PROPS: dict[str, Any] = {
    "cursor": {"type": "string", "description": "上一页返回的 next_cursor"},
    "page_size": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE, "default": DEFAULT_PAGE_SIZE},
}
SYNC_PROPS: dict[str, Any] = {
    "sync_token": {"type": "string", "description": "增量同步：上次 envelope 返回的 sync_token；只返回此后新增/变更的记录"},
}
