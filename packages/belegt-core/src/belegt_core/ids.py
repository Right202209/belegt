# Spec: 03 §1.2（ID 前缀）、§2.1（id 约束）、阅读约定（ObjectRef）
"""ID 生成与 ObjectRef 解析。

- 逻辑 ID：``<prefix>_<ULID>``（Crockford Base32，26 位）；DataPoint 用 ``dp:``、FactorSet 用 ``fs:<source>@<version>``。
- ObjectRef：``<id>#v<version>``（钉住版本）；不带 ``#v`` 的叫"浮动引用"，写入时由服务端解析为 head。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ulid import ULID

ULID_RE = r"[0-9A-HJKMNP-TV-Z]{26}"

# 对象类型 -> ID 前缀（带下划线的前缀拼 ULID）
TYPE_PREFIX: dict[str, str] = {
    "Request": "req_",
    "Question": "q_",
    "Mapping": "map_",
    "Evidence": "ev_",
    "Calculation": "calc_",
    "Answer": "ans_",
    "Approval": "apr_",
    "Conflict": "cfl_",
    "SupplierRequest": "sreq_",
    "ClosedQuestion": "cq_",
    "Organization": "org_",
    "Counterparty": "cp_",
    "Site": "site_",
    "Product": "prod_",
    "AuditEvent": "aud_",
    "Trace": "trc_",
    "DataPoint": "dp:",
    "FactorSet": "fs:",
}
PREFIX_TYPE: dict[str, str] = {v: k for k, v in TYPE_PREFIX.items()}
# 其它不对应对象类型的前缀
EXTRA_PREFIXES = {"apb_": "ApprovalBatch", "slk_": "SupplierLinkToken"}

ORG_GLOBAL = "org_global"
DATAPOINT_REF_RE = re.compile(
    r"^dp:[a-z]+(\.[a-z0-9_]+){2,3}@(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
FACTORSET_ID_RE = re.compile(r"^fs:[a-z0-9-]+@[0-9A-Za-z.\-]+$")
_ULID_ID_RE = re.compile(r"^([a-z]+_)(" + ULID_RE + r")$")
_REF_RE = re.compile(r"^(?P<id>[^#\s]+)#v(?P<v>[1-9][0-9]*)$")


def new_ulid() -> str:
    return str(ULID())


def new_id(type_or_prefix: str) -> str:
    """``new_id("Answer")`` 或 ``new_id("ans_")`` → ``ans_01J...``。"""
    prefix = TYPE_PREFIX.get(type_or_prefix, type_or_prefix)
    if not prefix.endswith("_"):
        raise ValueError(f"cannot generate ULID id for prefix {prefix!r}")
    return prefix + new_ulid()


def new_trace_id() -> str:
    return new_id("trc_")


def type_of_id(obj_id: str) -> str | None:
    """由 ID 推断对象类型；未知返回 None。"""
    if obj_id == ORG_GLOBAL:
        return "Organization"
    if obj_id.startswith("dp:"):
        return "DataPoint"
    if obj_id.startswith("fs:"):
        return "FactorSet"
    m = _ULID_ID_RE.match(obj_id)
    if not m:
        return None
    return PREFIX_TYPE.get(m.group(1))


def is_valid_id(obj_id: str, type_name: str | None = None) -> bool:
    t = type_of_id(obj_id)
    if t is None:
        return False
    if t == "DataPoint":
        return obj_id.startswith("dp:")
    if t == "FactorSet":
        return bool(FACTORSET_ID_RE.match(obj_id))
    return type_name is None or t == type_name


@dataclass(frozen=True)
class ObjectRef:
    """钉住版本引用 ``<id>#v<version>``；version=None 表示浮动引用。"""

    id: str
    version: int | None = None

    @classmethod
    def parse(cls, s: str) -> "ObjectRef":
        m = _REF_RE.match(s)
        if m:
            return cls(m.group("id"), int(m.group("v")))
        if "#" in s:
            raise ValueError(f"malformed ObjectRef {s!r}")
        return cls(s, None)

    @property
    def pinned(self) -> bool:
        return self.version is not None

    @property
    def type(self) -> str | None:
        return type_of_id(self.id)

    def __str__(self) -> str:
        return f"{self.id}#v{self.version}" if self.version is not None else self.id


def ref(obj_id: str, version: int) -> str:
    return f"{obj_id}#v{version}"


def parse_ref(s: str) -> tuple[str, int | None]:
    r = ObjectRef.parse(s)
    return r.id, r.version


def datapoint_base(dp_ref: str) -> str:
    """``dp:env.ghg.scope2_market_based@1.1.0`` → ``dp:env.ghg.scope2_market_based``。"""
    return dp_ref.split("@", 1)[0]
