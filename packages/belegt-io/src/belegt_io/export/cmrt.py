# Spec: 02 §4.3.7（CMRT：Declaration、Smelter List、Product List）；04 §2.11
"""CMRT 简化导出（xlsx）。结构参照 RMI CMRT 6.5 的工作表与列名 [需核对]，不是 RMI 官方文件。

校验：金属 ∈ 3TG；逐金属答复 ∈ {Yes, No, Unknown}；公司题 ∈ {Yes, No}（B 题为 URL 文本）；
冶炼厂标识须为 RMI `CID` + 6 位数字；国家非空。
"""
from __future__ import annotations

import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field, field_validator

from ..hashing import canonical_hash
from ..models import ExportResult
from .common import ExportValidationError

Metal = Literal["Tantalum", "Tin", "Gold", "Tungsten"]
METALS = ("Tantalum", "Tin", "Gold", "Tungsten")


class CmrtSmelter(BaseModel):
    metal: Metal
    smelter_name: str = Field(min_length=1)
    smelter_country: str = Field(min_length=2)
    smelter_id: str
    comments: str | None = None

    @field_validator("smelter_id")
    @classmethod
    def _cid(cls, v: str) -> str:
        if not re.fullmatch(r"CID\d{6}", v):
            raise ValueError("smelter identification must match CID + 6 digits")
        return v


class CmrtProduct(BaseModel):
    product_number: str
    product_name: str | None = None
    comments: str | None = None


class CmrtDeclaration(BaseModel):
    company_name: str
    declaration_scope: Literal["Company", "Product (or List of Products)", "User defined"] = "Company"
    authorized_representative: str
    contact_email: str
    date_of_completion: date
    # {"Q1": {"Tantalum": "Yes", ...}, ...}
    metal_answers: dict[str, dict[Metal, Literal["Yes", "No", "Unknown"]]] = Field(default_factory=dict)
    # {"A": "Yes", "B": "https://…", ...}
    company_answers: dict[str, str] = Field(default_factory=dict)
    smelters: list[CmrtSmelter] = Field(default_factory=list)
    products: list[CmrtProduct] = Field(default_factory=list)
    source_refs: dict[str, str] = Field(default_factory=dict)

    @field_validator("metal_answers")
    @classmethod
    def _q(cls, v: dict[str, Any]) -> dict[str, Any]:
        for k in v:
            if not re.fullmatch(r"Q[1-7]", k):
                raise ValueError(f"unknown declaration question {k}")
        return v

    @field_validator("company_answers")
    @classmethod
    def _c(cls, v: dict[str, str]) -> dict[str, str]:
        for k, a in v.items():
            if not re.fullmatch(r"[A-H]", k):
                raise ValueError(f"unknown company question {k}")
            if k != "B" and a not in ("Yes", "No"):
                raise ValueError(f"company question {k}: answer must be Yes/No")
        return v


def export_cmrt(data: CmrtDeclaration | Mapping[str, Any], out_path: str | Path) -> ExportResult:
    from ..fill import fill_back
    from ..parse import parse_questionnaire
    from ..workbooks import build_cmrt_template

    if not isinstance(data, CmrtDeclaration):
        data = CmrtDeclaration(**data)
    info = {
        "Company Name": data.company_name, "Declaration Scope": data.declaration_scope,
        "Authorized Company Representative": data.authorized_representative,
        "Contact e-mail": data.contact_email, "Date of Completion": data.date_of_completion,
    }
    tmpdir = Path(tempfile.mkdtemp(prefix="belegt-cmrt-"))
    try:
        tpl = build_cmrt_template(tmpdir / "cmrt.xlsx", smelter_rows=max(20, len(data.smelters)),
                                  product_rows=max(10, len(data.products)))
        parsed = parse_questionnaire(tpl, template_hint="cmrt")
        answers: dict[str, Any] = {}
        for q in parsed.questions:
            h = q.hints
            if h.get("layout") == "form" and h.get("label") in info:
                answers[q.locator.key] = info[h["label"]]
            elif h.get("layout") == "matrix":
                v = data.metal_answers.get(h.get("base_id") or "", {}).get(h.get("column"))  # type: ignore[arg-type]
                if v:
                    answers[q.locator.key] = v
            elif h.get("layout") == "questionnaire":
                v = data.company_answers.get(h.get("base_id") or "")
                if v:
                    answers[q.locator.key] = v
            elif h.get("layout") == "table":
                i = int(h["row_index"])
                if q.locator.sheet == "Smelter List" and i <= len(data.smelters):
                    s = data.smelters[i - 1]
                    v = {"Metal": s.metal, "Smelter Name": s.smelter_name, "Smelter Country": s.smelter_country,
                         "Smelter Identification": s.smelter_id, "Comments": s.comments}.get(h["column"])
                    if v:
                        answers[q.locator.key] = v
                elif q.locator.sheet == "Product List" and i <= len(data.products):
                    p = data.products[i - 1]
                    v = {"Manufacturer's Product Number": p.product_number,
                         "Manufacturer's Product Name": p.product_name, "Comments": p.comments}.get(h["column"])
                    if v:
                        answers[q.locator.key] = v
        report = fill_back(tpl, answers, out_path, parsed=parsed)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not report.ok:
        raise ExportValidationError("cmrt", [f"{c.sheet}!{c.cell}: {c.code} {c.message}" for c in report.rejected])
    expected = sum(len(v) for v in data.metal_answers.values())
    written_matrix = sum(1 for c in report.written if c.sheet == "Declaration" and c.native_id and "." in c.native_id)
    if written_matrix != expected:
        raise ExportValidationError("cmrt", [f"declaration answers written {written_matrix} != given {expected}"])
    return ExportResult(format="cmrt_simplified@6.5", out_path=str(out_path),
                        export_input_hash=canonical_hash({"format": "cmrt",
                                                          "data": data.model_dump(mode="json", exclude={"source_refs"}),
                                                          "refs": data.source_refs}),
                        warnings=report.warnings)
