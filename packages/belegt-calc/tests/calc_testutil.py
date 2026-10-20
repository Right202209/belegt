# Spec: 05 §9（测试辅助；文件名唯一，避免 monorepo 一起跑测试时与其他包的模块重名）
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "tests" / "vectors"
FIXTURES = ROOT / "tests" / "fixtures"
Y2025 = {"start": "2025-01-01", "end": "2025-12-31"}


def inp(name, value, unit, ref=None):
    return {"name": name, "ref": ref or "ev_01JE0TESTREF0000000000001#v1", "value": value, "unit": unit}
