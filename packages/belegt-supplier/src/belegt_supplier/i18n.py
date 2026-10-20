# Spec: 08 §6.2（多语言 UI：语言按浏览器设置，可切换）、§3.4 规则 4
"""UI 文案：`data/locales/<locale>.yaml`；缺键回退 en。"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

UI_LOCALES = ("de", "en", "tr", "zh")


@lru_cache(maxsize=8)
def _load(locale: str) -> dict[str, Any]:
    f = resources.files("belegt_supplier").joinpath(f"data/locales/{locale}.yaml")
    return yaml.safe_load(f.read_text("utf-8")) if f.is_file() else {}


def negotiate(accept_language: str | None, fallback: str = "en") -> str:
    """解析 Accept-Language，选第一个受支持的语言。"""
    if not accept_language:
        return fallback
    prefs = []
    for i, part in enumerate(accept_language.split(",")):
        lang, _, q = part.strip().partition(";q=")
        try:
            qv = float(q) if q else 1.0
        except ValueError:
            qv = 0.0
        prefs.append((-qv, i, lang.split("-")[0].lower()))
    for _, _, lang in sorted(prefs):
        if lang in UI_LOCALES:
            return lang
    return fallback


class Translator:
    def __init__(self, locale: str) -> None:
        self.locale = locale if locale in UI_LOCALES else "en"
        self._d = _load(self.locale)
        self._en = _load("en")

    def raw(self, key: str) -> Any:
        node: Any = self._d
        for part in key.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            node = self._en
            for part in key.split("."):
                node = node.get(part) if isinstance(node, dict) else None
        return node

    def __call__(self, key: str, **kw: Any) -> str:
        v = self.raw(key)
        if v is None:
            return key
        if isinstance(v, str) and kw:
            try:
                return v.format(**kw)
            except (KeyError, IndexError):
                return v
        return v if isinstance(v, str) else str(v)

    @property
    def beta(self) -> bool:
        return (self._d.get("_meta") or {}).get("status") == "beta"

    @staticmethod
    def languages() -> list[tuple[str, str]]:
        return [(loc, (_load(loc).get("_meta") or {}).get("name", loc)) for loc in UI_LOCALES]
