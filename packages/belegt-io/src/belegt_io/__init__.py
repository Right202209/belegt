# Spec: CONTRACTS.md「跨包接口 · belegt_io」；01、02 §4、03 §3.3/§3.5、05 §4.6/§6
"""belegt-io：Belegt 文件导入导出（Apache-2.0）。

公开 API：
- 解析：`parse_questionnaire(path) -> ParsedQuestionnaire`、`detect_template`、`detect_json_template`
- 回填：`fill_back(src_path, answers, out_path) -> FillReport`、`export_input_hash`
- 证据：`extract_pdf_evidence`、`extract_xlsx_evidence`、`extract_text_evidence`
- 数字：`extract_quantities`、`parse_number`、`normalize_unit`、`classify_null`、`to_dec_str`
- 导出：`export_pact`、`export_catenax_pcf`、`export_cbam_communication`、`export_cmrt`、`export_battery_passport_cf`
"""
from .evidence import extract_pdf_evidence, extract_text_evidence, extract_xlsx_evidence, guess_fact_key
from .export import (
    DQI, BatteryCfInput, CatenaxExtras, CbamCommunication, CbamGood, CbamInstallation, CbamProcess,
    CmrtDeclaration, CmrtProduct, CmrtSmelter, EmissionFactorSource, ExportValidationError, LifecycleStages,
    PcfInput, dumps_json, export_battery_passport_cf, export_catenax_pcf, export_cbam_communication, export_cmrt,
    export_pact, pcf_input_from_datapoints, validate_battery, validate_catenax, validate_pact,
)
from .fill import adapt_value, export_input_hash, fill_back
from .hashing import canonical_hash, file_sha256
from .hints import detect_language, guess_answer_format, guess_period
from .models import (
    BelegtIOError, CellChange, EvidenceCandidate, ExportResult, FillReport, IOErrorCode, Locator,
    ParsedQuestionnaire, Period, Quantity, QuestionDraft, SheetLayout, TemplateDetection, TemplateMappingHit,
)
from .numbers import classify_null, detect_number_style, extract_quantities, normalize_unit, parse_number, to_dec_str
from .parse import parse_questionnaire
from .templates import TEMPLATE_IDS, detect_json_template, detect_template, load_templates

__version__ = "0.1.0"

__all__ = [
    "parse_questionnaire", "fill_back", "export_input_hash", "adapt_value",
    "extract_pdf_evidence", "extract_xlsx_evidence", "extract_text_evidence", "guess_fact_key",
    "extract_quantities", "parse_number", "normalize_unit", "classify_null", "detect_number_style", "to_dec_str",
    "detect_language", "guess_answer_format", "guess_period",
    "detect_template", "detect_json_template", "load_templates", "TEMPLATE_IDS",
    "export_pact", "export_catenax_pcf", "export_cbam_communication", "export_cmrt", "export_battery_passport_cf",
    "validate_pact", "validate_catenax", "validate_battery", "pcf_input_from_datapoints", "dumps_json",
    "PcfInput", "DQI", "EmissionFactorSource", "CatenaxExtras", "CbamCommunication", "CbamGood", "CbamInstallation",
    "CbamProcess", "CmrtDeclaration", "CmrtSmelter", "CmrtProduct", "BatteryCfInput", "LifecycleStages",
    "ExportValidationError", "BelegtIOError", "IOErrorCode",
    "Locator", "Period", "QuestionDraft", "ParsedQuestionnaire", "SheetLayout", "TemplateDetection",
    "TemplateMappingHit", "EvidenceCandidate", "Quantity", "FillReport", "CellChange", "ExportResult",
    "file_sha256", "canonical_hash",
]
