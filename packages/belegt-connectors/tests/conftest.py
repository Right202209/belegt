from __future__ import annotations

from pathlib import Path

import pytest

from belegt_connectors.samples import build_all


@pytest.fixture(scope="session")
def sample_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("samples")
    build_all(root)
    return root


@pytest.fixture()
def fixed_clock():
    return lambda: "2026-10-19T08:00:00Z"
