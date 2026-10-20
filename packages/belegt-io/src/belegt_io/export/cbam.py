# Spec: 05 §6.2、§6.9（运营者 → 进口商回传 SEE_dir、SEE_ind、SEC、生产路线、装置标识）；02 §4.3.4；04 §2.11
"""CBAM communication（运营者 → 进口商）xlsx 导出。

实现方式：生成（或使用调用方给出的）沟通模板 → `parse_questionnaire` 按模板版面定位答案单元格 →
`fill_back` 回填。因此导出物与解析器、回填器同一套 locator，可往返（导出后再解析得到相同值）。
SEE 按 05 §1.2 默认 6 位小数、比电耗 3 位（`transform.round`），不回写证据图。
"""
from __future__ import annotations

import re
import shutil
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field, field_validator

from ..hashing import canonical_hash
from ..models import ExportResult, Period
from .common import ExportValidationError

ROUTE_EXPORT = {"bf_bof": "Blast furnace route", "eaf": "Electric arc furnace", "dri_eaf": "DRI-EAF", "other": "Other"}
METHOD_EXPORT = {"actual": "Actual (verified)", "default": "Default value", "other": "Other"}


class CbamInstallation(BaseModel):
    installation_id: str = Field(min_length=1)  # dp:prod.cbam.installation_id
    name: str
    operator_name: str
    country: str  # ISO 3166-1 alpha-2
    address: str | None = None
    un_locode: str | None = None
    reporting_period: Period
    contact_email: str | None = None

    @field_validator("country")
    @classmethod
    def _iso(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z]{2}", v):
            raise ValueError("country must be ISO 3166-1 alpha-2")
        return v

    @field_validator("contact_email")
    @classmethod
    def _mail(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("invalid e-mail")
        return v


class CbamGood(BaseModel):
    cn_code: str  # dp:prod.identity.cn_code
    description: str | None = None
    production_route: Literal["bf_bof", "eaf", "dri_eaf", "other"]  # dp:prod.cbam.production_route
    see_direct: Decimal = Field(ge=0)  # dp:prod.cbam.see_direct，tCO2e/t
    see_indirect: Decimal | None = Field(default=None, ge=0)  # dp:prod.cbam.see_indirect
    specific_electricity_consumption: Decimal | None = Field(default=None, ge=0)  # MWh/t
    determination_method: Literal["actual", "default", "other"] = "actual"

    @field_validator("cn_code")
    @classmethod
    def _cn(cls, v: str) -> str:
        v = v.replace(" ", "")
        if not re.fullmatch(r"[0-9]{8}", v):
            raise ValueError("CN code must have 8 digits (02 §4.3.4 format ^[0-9]{8}$)")
        return v


class CbamProcess(BaseModel):
    process: str
    production_route: Literal["bf_bof", "eaf", "dri_eaf", "other"]
    cn_code: str
    activity_level_t: Decimal = Field(ge=0)

    @field_validator("cn_code")
    @classmethod
    def _cn(cls, v: str) -> str:
        v = v.replace(" ", "")
        if not re.fullmatch(r"[0-9]{8}", v):
            raise ValueError("CN code must have 8 digits")
        return v


class CbamCommunication(BaseModel):
    installation: CbamInstallation
    goods: list[CbamGood] = Field(min_length=1)
    processes: list[CbamProcess] = Field(default_factory=list)
    see_digits: int = 6
    sec_digits: int = 3
    source_refs: dict[str, str] = Field(default_factory=dict)


def _answers_for(parsed: Any, data: CbamCommunication) -> dict[str, Any]:
    inst = data.installation
    form = {
        "Installation identifier": inst.installation_id,
        "Name of the installation": inst.name,
        "Operator name": inst.operator_name,
        "Country": inst.country,
        "Address": inst.address,
        "UN/LOCODE": inst.un_locode,
        "Reporting period start": inst.reporting_period.start,
        "Reporting period end": inst.reporting_period.end,
        "Contact e-mail": inst.contact_email,
    }
    answers: dict[str, Any] = {}
    max_rows = {"Summary_Products": 0, "D_Processes": 0}
    for q in parsed.questions:
        h = q.hints
        sheet = q.locator.sheet
        if h.get("layout") == "form" and h.get("label") in form:
            if form[h["label"]] is not None:
                answers[q.locator.key] = form[h["label"]]
        elif h.get("layout") == "table" and sheet in max_rows:
            i = int(h["row_index"])
            max_rows[sheet] = max(max_rows[sheet], i)
            col = h["column"]
            if sheet == "Summary_Products" and i <= len(data.goods):
                g = data.goods[i - 1]
                val = {
                    "CN code": g.cn_code,
                    "Goods description": g.description,
                    "Production route": ROUTE_EXPORT[g.production_route],
                    "SEE (direct)": {"value": g.see_direct, "round": data.see_digits},
                    "SEE (indirect)": None if g.see_indirect is None else {"value": g.see_indirect, "round": data.see_digits},
                    "Specific electricity consumption": None if g.specific_electricity_consumption is None
                    else {"value": g.specific_electricity_consumption, "round": data.sec_digits},
                    "Determination method": METHOD_EXPORT[g.determination_method],
                }.get(col)
                if val is not None:
                    answers[q.locator.key] = val
            elif sheet == "D_Processes" and i <= len(data.processes):
                pr = data.processes[i - 1]
                val = {
                    "Production process": pr.process,
                    "Production route": ROUTE_EXPORT[pr.production_route],
                    "CN code": pr.cn_code,
                    "Activity level": {"value": pr.activity_level_t, "round": 3},
                }.get(col)
                if val is not None:
                    answers[q.locator.key] = val
    if len(data.goods) > max_rows["Summary_Products"] or len(data.processes) > max_rows["D_Processes"]:
        raise ExportValidationError("cbam_communication", ["more rows than the template provides"])
    return answers


def export_cbam_communication(data: CbamCommunication | Mapping[str, Any], out_path: str | Path, *,
                              template_path: str | Path | None = None) -> ExportResult:
    """写出 CBAM 沟通模板 xlsx。`template_path` 为空时使用 Belegt 样例版面（`build_cbam_template`）。"""
    from ..fill import fill_back
    from ..parse import parse_questionnaire
    from ..workbooks import build_cbam_template

    if not isinstance(data, CbamCommunication):
        data = CbamCommunication(**data)
    tmpdir = Path(tempfile.mkdtemp(prefix="belegt-cbam-"))
    try:
        if template_path is None:
            rows = max(10, len(data.goods), len(data.processes))
            template_path = build_cbam_template(tmpdir / "cbam_template.xlsx", rows=rows)
        parsed = parse_questionnaire(template_path, template_hint="cbam_communication")
        answers = _answers_for(parsed, data)
        report = fill_back(template_path, answers, out_path, parsed=parsed)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not report.ok:
        raise ExportValidationError("cbam_communication",
                                    [f"{c.sheet}!{c.cell}: {c.code} {c.message}" for c in report.rejected])
    return ExportResult(
        format="cbam_communication@2024", out_path=str(out_path),
        export_input_hash=canonical_hash({"format": "cbam_communication",
                                          "data": data.model_dump(mode="json", exclude={"source_refs"}),
                                          "refs": data.source_refs}),
        warnings=report.warnings,
    )
