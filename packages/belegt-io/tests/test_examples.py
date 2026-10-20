# Spec: 01 AC-501-2（100 题预览 ≤ 5 s）；示例文件可复现
from __future__ import annotations

import time
from pathlib import Path

import pytest

from belegt_io import fill_back, parse_questionnaire
from belegt_io.examples import generate

PKG_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.skipif(not PKG_EXAMPLES.exists(), reason="examples not generated")
def test_committed_examples_parse():
    p = parse_questionnaire(PKG_EXAMPLES / "oem_fragebogen_de_100.xlsx")
    assert len(p.questions) == 100
    assert parse_questionnaire(PKG_EXAMPLES / "cbam_communication_template.xlsx").template_ref == "tm:cbam_communication@2024"
    assert len(parse_questionnaire(PKG_EXAMPLES / "oem_fragebogen_kurz.csv").questions) == 15


@pytest.mark.skipif(not PKG_EXAMPLES.exists(), reason="examples not generated")
def test_examples_reproducible(tmp_path):
    generate(tmp_path)
    for name in ("oem_fragebogen_kurz.csv", "stromrechnung_2025.pdf", "pact_productfootprint_sample.json",
                 "catenax_pcf_sample.json", "battery_passport_cf_fragment_sample.json"):
        assert (tmp_path / name).read_bytes() == (PKG_EXAMPLES / name).read_bytes(), name
    a = parse_questionnaire(tmp_path / "oem_fragebogen_de_100.xlsx")
    b = parse_questionnaire(PKG_EXAMPLES / "oem_fragebogen_de_100.xlsx")
    assert [(q.native_id, q.locator.sheet_cell, q.answer_format) for q in a.questions] == \
           [(q.native_id, q.locator.sheet_cell, q.answer_format) for q in b.questions]


def test_parse_and_fill_100_under_5s(oem_path, tmp_path):
    t0 = time.perf_counter()
    p = parse_questionnaire(oem_path)
    rep = fill_back(oem_path, {q.locator: "x" for q in p.questions if q.answer_format in ("text", "file")},
                    tmp_path / "o.xlsx", parsed=p)
    rep.to_markdown()
    assert time.perf_counter() - t0 < 5.0
