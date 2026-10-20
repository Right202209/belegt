# Spec: 04 §3.4（一个 Request 一个工作流；幂等键 wf:<req_id>:act:<activity>:<seq>；信号去重）、§3.5（断点续跑：已完成的 Activity
#       不重跑，结果是 ObjectRef；进行中的按重试策略重跑，幂等键兜底；halted 后恢复从最后完成的 Activity 之后继续）
"""LocalRunner 的事件日志（Temporal 事件历史的最小替身）：与证据图共用数据库（独立表，不改 GraphStore 表结构）。

``wf_journal``：``(workflow_id, key)`` → ``{kind, name, result}``；key 为 ``"<seq>"``（Activity 结果）、``"<seq>:halt:<n>"``、
``"signal:<name>:<n>"``；``wf_run``：workflow 级状态（预算快照、最后状态、待处理项）。只存 ObjectRef、计数与状态（SSOT-1）。
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.engine import Engine

metadata = MetaData()
wf_journal = Table(
    "wf_journal", metadata,
    Column("workflow_id", String(120), primary_key=True), Column("key", String(200), primary_key=True),
    Column("kind", String(30), nullable=False), Column("name", String(120), nullable=False),
    Column("result", JSON), Column("created_at", String(40), nullable=False),
)
wf_run = Table(
    "wf_run", metadata,
    Column("workflow_id", String(120), primary_key=True), Column("request_id", String(64), nullable=False),
    Column("org_id", String(64), nullable=False), Column("state", String(40), nullable=False),
    Column("pending", JSON), Column("budget", JSON), Column("summary", JSON), Column("signals", Integer, default=0),
    Column("updated_at", String(40), nullable=False), Column("note", Text),
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Journal:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        metadata.create_all(engine)

    def get(self, wf_id: str, key: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            r = c.execute(select(wf_journal).where(wf_journal.c.workflow_id == wf_id, wf_journal.c.key == key)).first()
        return dict(r._mapping) if r else None

    def put(self, wf_id: str, key: str, kind: str, name: str, result: Any) -> None:
        with self.engine.begin() as c:
            if c.execute(select(wf_journal.c.key).where(wf_journal.c.workflow_id == wf_id, wf_journal.c.key == key)).first():
                return
            c.execute(insert(wf_journal).values(workflow_id=wf_id, key=key, kind=kind, name=name,
                                                result=json.loads(json.dumps(result, default=str)), created_at=_now()))

    def entries(self, wf_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            rows = c.execute(select(wf_journal).where(wf_journal.c.workflow_id == wf_id).order_by(wf_journal.c.created_at))
            return [dict(r._mapping) for r in rows]

    def signals(self, wf_id: str, name: str) -> list[dict[str, Any]]:
        return [e for e in self.entries(wf_id) if e["kind"] == "signal" and e["name"] == name]

    def add_signal(self, wf_id: str, name: str, payload: Any) -> str:
        n = len(self.signals(wf_id, name)) + 1
        key = f"signal:{name}:{n}"
        self.put(wf_id, key, "signal", name, payload)
        return key

    # ---------------------------------------------------------------- run state
    def run_state(self, wf_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            r = c.execute(select(wf_run).where(wf_run.c.workflow_id == wf_id)).first()
        return dict(r._mapping) if r else None

    def save_run(self, wf_id: str, *, request_id: str, org_id: str, state: str, pending: Any = None, budget: Any = None,
                 summary: Any = None, note: str | None = None) -> None:
        vals = {"state": state, "pending": json.loads(json.dumps(pending, default=str)) if pending is not None else None,
                "updated_at": _now(), "note": note}
        if budget is not None:
            vals["budget"] = budget
        if summary is not None:
            vals["summary"] = json.loads(json.dumps(summary, default=str))
        with self.engine.begin() as c:
            if c.execute(select(wf_run.c.workflow_id).where(wf_run.c.workflow_id == wf_id)).first():
                c.execute(update(wf_run).where(wf_run.c.workflow_id == wf_id).values(**vals))
            else:
                c.execute(insert(wf_run).values(workflow_id=wf_id, request_id=request_id, org_id=org_id, signals=0, **vals))

    def runs(self) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(select(wf_run))]
