# Spec: 04 §2（Agent 契约）、§3（问卷级状态机）——测试工具：演示租户 + 短问卷（examples/oem_fragebogen_kurz.csv，15 题）
"""Agent / 工作流测试共用的小工具（离线、MockLLM、SQLite 内存库）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from belegt_core.demo import DemoHuman, build_env, drive, find_example, upload
from belegt_core.store import tables as T


def example(name: str) -> Path:
    try:
        return find_example(name)
    except FileNotFoundError:  # pragma: no cover - 仓库外运行
        pytest.skip(f"examples/{name} 不在")


def csv_path() -> Path:
    return example("oem_fragebogen_kurz.csv")


def xlsx_path() -> Path:
    return example("oem_fragebogen_de_100.xlsx")


def new_env(**kw: Any):
    return build_env(**kw)


def upload_file(env: Any, path: Path | None = None, **kw: Any) -> str:
    p = path or csv_path()
    return upload(env, p.read_bytes(), p.name, **kw)


def human(env: Any, *, approve: bool = True, submit: bool = True) -> DemoHuman:
    return DemoHuman(env.store, env.user("owner"), executive=env.user("exec"), approve_answers=approve, submit=submit,
                     clock=env.clock)


def drive_to(env: Any, req_ref: str, *, stop: tuple[str, ...] = (), approve: bool = True, submit: bool = True,
             max_steps: int = 40):
    """用演示中的"脚本化的人"推进，直到 ``stop`` 中的状态或终态。返回 ``(RunResult, trail)``。"""
    return drive(env, req_ref, human=human(env, approve=approve, submit=submit), stop_states=stop, max_steps=max_steps)


def all_versions(store: Any, org_id: str, type_name: str | None = None) -> list[dict[str, Any]]:
    """全部对象版本（含历史版本），每项带 ``created_by``/``trust_zone``/``status``/``body``。"""
    q = select(T.object_version).where(T.object_version.c.org_id == org_id)
    if type_name:
        q = q.where(T.object_version.c.type == type_name)
    with store.engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(q.order_by(T.object_version.c.created_at))]


def writers(store: Any, org_id: str, type_name: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in all_versions(store, org_id, type_name):
        out[v["created_by"]] = out.get(v["created_by"], 0) + 1
    return out


def request_answers(env: Any, req_id: str) -> list[dict[str, Any]]:
    st = env.store
    qids = {q["id"] for q in st.query("Question", org_id=env.org_id, request_id=req_id)}
    return [a for a in st.query("Answer", org_id=env.org_id) if set(a.get("question_refs") or []) & qids]


def audit_actions(env: Any) -> list[dict[str, Any]]:
    return list(env.store.audit_events(env.org_id))
