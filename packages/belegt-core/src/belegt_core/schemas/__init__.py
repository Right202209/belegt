# Spec: 03 §5（JSON Schema，Draft 2020-12，$id 前缀 https://schemas.belegt.org/graph/2026-10/）
"""JSON Schema 文件加载与校验器。

- ``common/answer/evidence/calculation/conflict.json`` 取自 03 §5.1–§5.5 原文（common 增补 ``promotion``、``date``、
  ``dateTime``、``userActor``、``locator`` 定义；evidence 增补 ``properties.promotion``，见 README"与文档出入"）。
- 其余对象的 schema 按 03 §3 字段表补齐。
"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any, Iterator

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

SCHEMA_BASE = "https://schemas.belegt.org/graph/2026-10/"
SCHEMA_VERSION = "belegt.graph/2026-10"


def schema_names() -> list[str]:
    return sorted(p.name for p in resources.files(__name__).iterdir() if p.name.endswith(".json"))


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    if not name.endswith(".json"):
        name += ".json"
    return json.loads(resources.files(__name__).joinpath(name).read_text("utf-8"))


@lru_cache(maxsize=1)
def registry() -> Registry:
    return Registry().with_resources(
        [(SCHEMA_BASE + n, Resource.from_contents(load_schema(n))) for n in schema_names()]
    )


@lru_cache(maxsize=None)
def validator(name: str) -> Draft202012Validator:
    sch = load_schema(name)
    Draft202012Validator.check_schema(sch)
    return Draft202012Validator(sch, registry=registry(), format_checker=FormatChecker())


def iter_errors(name: str, instance: dict[str, Any]) -> Iterator[Any]:
    return validator(name).iter_errors(instance)


def validate(name: str, instance: dict[str, Any]) -> list[dict[str, str]]:
    """返回错误列表 ``[{path, message}]``（空 = 通过）。"""
    out = []
    for e in sorted(iter_errors(name, instance), key=lambda e: list(e.absolute_path)):
        path = "/" + "/".join(str(p) for p in e.absolute_path)
        out.append({"path": path, "message": e.message[:500]})
    return out
