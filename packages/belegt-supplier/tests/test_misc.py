# Spec: 03 §2.2（DEC-STR）、08 §6.2（多语言 UI 完整性）、CLI / 自托管入口
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from belegt_supplier import cli
from belegt_supplier.decimal_util import dec_str, is_dec_str, normalize_user_number
from belegt_supplier.i18n import UI_LOCALES, Translator, negotiate

PKG = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("raw,loc,out", [
    ("1,85", "de", "1.85"), ("1.85", "en", "1.85"), ("1.234,5", "de", "1234.5"), ("1,234.5", "en", "1234.5"),
    (" 2 ", "tr", "2"), ("0.50", "en", "0.5"), (".5", "en", "0.5"), ("+3", "en", "3"), ("abc", "en", None), ("1e3", "en", None),
    ("", "en", None),
])
def test_normalize_user_number(raw, loc, out):
    assert normalize_user_number(raw, loc) == out


def test_dec_str():
    assert is_dec_str("1.85") and is_dec_str("-0.5") and not is_dec_str("01") and not is_dec_str("1e3") and not is_dec_str(1.5)
    assert not is_dec_str("1" * 19)
    assert dec_str(Decimal("1.2300")) == "1.23" and dec_str(Decimal("1E+3")) == "1000"


def _flatten(d, prefix=""):
    out = set()
    for k, v in d.items():
        if k == "_meta":
            continue
        if isinstance(v, dict):
            out |= _flatten(v, f"{prefix}{k}.")
        else:
            out.add(prefix + k)
    return out


def test_ui_locales_have_identical_keys():
    root = PKG / "src/belegt_supplier/data/locales"
    keys = {loc: _flatten(yaml.safe_load((root / f"{loc}.yaml").read_text("utf-8"))) for loc in UI_LOCALES}
    for loc in UI_LOCALES:
        assert keys[loc] == keys["en"], (loc, keys["en"] ^ keys[loc])


def test_hint_keys_cover_client_side_rules():
    from belegt_supplier.plausibility import CLIENT_SIDE_RULES
    t = Translator("de")
    for rid in CLIENT_SIDE_RULES:
        assert t(f"hint.{rid}") != f"hint.{rid}", rid


def test_translator_fallback_and_negotiate():
    assert Translator("xx").locale == "en"
    assert Translator("zh")("days_left", n=3) == "还剩 3 天"
    assert negotiate("fr-FR,tr;q=0.8,en;q=0.5") == "tr" and negotiate(None) == "en" and negotiate("fr") == "en"


def test_cli_templates_lint_and_render(capsys):
    assert cli.main(["templates", "lint"]) == 0
    assert cli.main(["templates", "render", "stpl:cbam_see_request.zh@1.0.0"]) == 0
    out = capsys.readouterr().out
    assert "主题" not in out and "CBAM 隐含排放数据" in out
    assert cli.main(["templates", "list", "--no-db"]) == 0
    assert "stpl:pcf_request.en@1.0.0" in capsys.readouterr().out


def test_self_hosting_artifacts_present():
    df = (PKG / "Dockerfile").read_text()
    assert 'ENTRYPOINT ["belegt-supplier"]' in df and '"serve"' in df
    assert "Apache License" in (PKG / "LICENSE").read_text()
    assert "belegt-supplier = \"belegt_supplier.cli:main\"" in (PKG / "pyproject.toml").read_text()
