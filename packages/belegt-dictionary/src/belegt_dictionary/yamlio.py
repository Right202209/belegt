# Spec: 02 §6.1, §6.2 DC-01 (YAML 格式化一致)
"""YAML 读写：读取时不把 ISO 日期转成 date 对象（字典里日期一律字符串）；dump_yaml 是规范格式。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class _Loader(yaml.SafeLoader):
    """SafeLoader without the implicit timestamp resolver: dates stay strings."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False):  # noqa: D401
        return super().increase_indent(flow, False)


def load_yaml_text(text: str) -> Any:
    return yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass


def load_yaml(path: str | Path) -> Any:
    return load_yaml_text(Path(path).read_text(encoding="utf-8"))


def dump_yaml(obj: Any) -> str:
    """Canonical formatting used by all dictionary files (DC-01 format check)."""
    return yaml.dump(obj, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=4096,
                     default_flow_style=False, indent=2)
