# Spec: 03 §2.1（ID 规则）、CONTRACTS.md（<prefix>_<ULID>、ObjectRef）
"""ID 与 ObjectRef 工具。"""
from __future__ import annotations

import re

from ulid import ULID

ULID_RE = r"[0-9A-HJKMNP-TV-Z]{26}"
OBJECT_REF_RE = re.compile(rf"^(?P<id>[a-z]+_{ULID_RE})#v(?P<version>[1-9][0-9]*)$")
SREQ_ID_RE = re.compile(rf"^sreq_{ULID_RE}$")
APR_REF_RE = re.compile(rf"^apr_{ULID_RE}#v[1-9][0-9]*$")
CP_ID_RE = re.compile(rf"^cp_{ULID_RE}$")


def new_id(prefix: str) -> str:
    """生成 `<prefix>_<ULID>`。prefix 不含下划线结尾。"""
    return f"{prefix.rstrip('_')}_{ULID()}"


def make_ref(obj_id: str, version: int) -> str:
    return f"{obj_id}#v{version}"


def parse_ref(ref: str) -> tuple[str, int]:
    m = OBJECT_REF_RE.match(ref)
    if not m:
        raise ValueError(f"invalid ObjectRef: {ref!r}")
    return m.group("id"), int(m.group("version"))


def short_code(sreq_id: str) -> str:
    """`{{request_ref}}`：sreq_ 短码（ULID 后 8 位，08 §3.2）。"""
    return sreq_id[-8:]
