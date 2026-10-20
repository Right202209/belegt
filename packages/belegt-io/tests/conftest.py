# Spec: 11（测试不访问网络、不需要 LLM key）
from __future__ import annotations

from pathlib import Path

import pytest

from belegt_io.workbooks import (
    build_cbam_template, build_cmrt_template, build_csv_questionnaire, build_oem_questionnaire_de, build_sample_pdf,
)


@pytest.fixture(scope="session")
def ex_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("examples")
    build_oem_questionnaire_de(d / "oem.xlsx")
    build_cbam_template(d / "cbam.xlsx")
    build_cmrt_template(d / "cmrt.xlsx")
    build_csv_questionnaire(d / "kurz.csv")
    build_sample_pdf(d / "strom.pdf")
    return d


@pytest.fixture(scope="session")
def oem_path(ex_dir) -> Path:
    return ex_dir / "oem.xlsx"


@pytest.fixture(scope="session")
def oem_parsed(oem_path):
    from belegt_io import parse_questionnaire

    return parse_questionnaire(oem_path)
