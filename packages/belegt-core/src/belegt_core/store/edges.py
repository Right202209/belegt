# Spec: 03 §1.3（边全部由对象字段派生，同事务物化；数值相关边 cites/derived_from/computed_with 必须钉住版本）、SSOT-3
from __future__ import annotations

from typing import Any, Callable

from ..rules.refspec import edge_type_for, iter_path, ref_fields, target_id

EDGE_TYPES = (
    "answers", "cites", "derived_from", "maps_to", "part_of", "supersedes", "approves", "conflicts_with", "resolves",
    "requested_from", "fulfills", "computed_with", "scoped_to",
)


def derive_edges(type_name: str, body: dict[str, Any], head_version: Callable[[str], int | None]) -> list[dict[str, Any]]:
    """由对象版本的字段派生边。``head_version`` 用于把 FactorSet 等"按 ID 引用但须钉住"的目标钉到当前 head。"""
    out: list[dict[str, Any]] = []
    me_id, me_v = body["id"], body["version"]
    seen = set()
    for f in ref_fields(type_name):
        for ptr, val, _ in iter_path(body, f.path):
            tid, tver = target_id(f, val)
            if tid is None:
                continue
            et = edge_type_for(f, tid)
            if et is None:
                continue
            if f.pin_edge and tver is None and f.kind == "fs":
                tver = head_version(tid)
            if not f.pin_edge:
                tver = None
            if f.reverse:
                e = {"edge_type": et, "from_id": tid, "from_version": tver or 0, "to_id": me_id, "to_version": me_v,
                     "attrs": {"field": f.path, "derived_from_object": f"{me_id}#v{me_v}"}}
            else:
                e = {"edge_type": et, "from_id": me_id, "from_version": me_v, "to_id": tid, "to_version": tver,
                     "attrs": {"field": f.path}}
            key = (e["edge_type"], e["from_id"], e["from_version"], e["to_id"], e["to_version"])
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
    return out
