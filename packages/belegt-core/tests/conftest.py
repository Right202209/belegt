from __future__ import annotations

import pytest

from belegt_core import GraphStore

from helpers import StepClock, build_doc_chain


@pytest.fixture
def clock() -> StepClock:
    return StepClock()


@pytest.fixture
def store(clock) -> GraphStore:
    return GraphStore(clock=clock)


@pytest.fixture
def chain(store) -> dict[str, str]:
    return build_doc_chain(store)


@pytest.fixture(scope="session")
def csv_run():
    """演示租户 + 短问卷（15 题）完整跑到 archived（脚本化的人审批/提交）。会话内共享，只读使用。"""
    from agentkit import drive_to, new_env, upload_file

    env = new_env()
    ref = upload_file(env)
    res, trail = drive_to(env, ref)
    assert res.state == "completed", (res.state, res.error, trail)
    return env, ref, res, trail
