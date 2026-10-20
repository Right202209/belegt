# Spec: 02 §6.2 — test helpers: a writable copy of the shipped dictionary to build DC positive/negative cases
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from belegt_dictionary import default_root, load_dictionary
from belegt_dictionary.validate import expected_dp_path, validate_tree
from belegt_dictionary.yamlio import dump_yaml, load_yaml


class Tree:
    """Writable dictionary root with small editing helpers (all writes in canonical YAML)."""

    def __init__(self, root: Path):
        self.root = root

    def path(self, rel: str) -> Path:
        return self.root / rel

    def read(self, rel: str) -> Any:
        return load_yaml(self.path(rel))

    def write(self, rel: str, data: Any) -> None:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dump_yaml(data), encoding="utf-8")

    def dp_rel(self, dp_id: str) -> str:
        return expected_dp_path(dp_id)

    def dp(self, dp_id: str) -> dict:
        return self.read(self.dp_rel(dp_id))

    def edit_dp(self, dp_id: str, fn: Callable[[dict], Any]) -> None:
        d = self.dp(dp_id)
        fn(d)
        self.write(self.dp_rel(dp_id), d)

    def add_dp(self, data: dict, rel: str | None = None) -> None:
        self.write(rel or expected_dp_path(data["id"]), data)

    def edit_mapping(self, tid: str, ver: str, fn: Callable[[dict], Any]) -> None:
        rel = f"mappings/{tid}/{ver}.yaml"
        d = self.read(rel)
        fn(d)
        self.write(rel, d)

    def validate(self, **kw):
        return validate_tree(self.root, **kw)


@pytest.fixture(scope="session")
def shipped_dict():
    return load_dictionary()


@pytest.fixture(scope="session")
def shipped_report():
    return validate_tree()


@pytest.fixture
def tree(tmp_path) -> Tree:
    dst = tmp_path / "dict"
    shutil.copytree(default_root(), dst)
    return Tree(dst)


@pytest.fixture
def make_tree(tmp_path) -> Callable[[str], Tree]:
    def _mk(name: str) -> Tree:
        dst = tmp_path / name
        shutil.copytree(default_root(), dst)
        return Tree(dst)
    return _mk


def minimal_dp(dp_id: str, **over) -> dict:
    """A valid bool DataPoint skeleton for negative tests."""
    d = {
        "id": dp_id, "version": "1.0.0", "introduced_in": "v2027.1", "status": "draft",
        "label": {"de": "Testpunkt", "en": "Test point", "zh": "测试点"},
        "definition": {"en": "A data point used only in tests of the dictionary validator."},
        "value_type": "bool", "period_type": "point_in_time", "boundary": ["product"],
        "sensitivity": "public",
        "evidence_requirements": {"accepted_source_types": ["document"], "min_sources": 1,
                                  "requires_calculation": False, "human_confirmation_allowed": False},
    }
    d.update(over)
    return d
