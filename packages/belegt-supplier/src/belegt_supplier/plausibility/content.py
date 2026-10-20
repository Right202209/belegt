# Spec: 08 §5.3 注入与异常内容检测 PLAUS-030…035（在任何 LLM 读取前执行）
"""内容类规则：文本注入、Excel 隐藏/宏/外链、文件类型与尺寸、控制字符、来源不符。

文件检查只用标准库（zipfile 读 OOXML 结构），不执行任何内容。
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field

from .model import PlausHit

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ZIP_RATIO = 100

# PLAUS-030：指令型语句（多语种规则；可叠加分类器）
INJECTION_PATTERNS = [
    r"ignore (all )?(the )?(previous|prior|above) (instructions|prompts?)",
    r"disregard (all )?(previous|prior|above)",
    r"\bsystem prompt\b", r"\byou are now\b", r"\bact as\b.*\b(assistant|system)\b",
    r"(change|update|replace).{0,30}(recipient|bank account|iban|payment|e-?mail address)",
    r"send .{0,40}(to|an) [\w.+-]+@[\w-]+\.[\w.]+",
    r"already (been )?verified.{0,30}(skip|accept|approve)",
    r"系统提示", r"忽略(之前|以上|前面)的?(所有)?(指令|提示)", r"(更改|修改).{0,10}(收件人|付款|银行账户|账号)", r"直接采纳", r"跳过审核",
    r"vorherige anweisungen ignorieren", r"ignoriere (alle )?(vorherigen|obigen) anweisungen", r"systemanweisung",
    r"önceki talimatları (yok say|görmezden gel)", r"sistem istemi",
]
_INJ = re.compile("|".join(INJECTION_PATTERNS), re.I)
URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
BIDI = {"\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069", "\u200e", "\u200f"}
ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}

MAGIC = {
    "pdf": [b"%PDF-"],
    "xlsx": [b"PK\x03\x04"], "xlsm": [b"PK\x03\x04"], "docx": [b"PK\x03\x04"], "zip": [b"PK\x03\x04"],
    "xls": [b"\xd0\xcf\x11\xe0"], "xlsb": [b"PK\x03\x04"],
    "png": [b"\x89PNG\r\n\x1a\n"], "jpg": [b"\xff\xd8\xff"], "jpeg": [b"\xff\xd8\xff"],
    "json": [b"{", b"["], "geojson": [b"{"], "csv": [], "txt": [], "kml": [b"<?xml", b"<kml"],
}
MIME_BY_EXT = {
    "pdf": "application/pdf", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel", "csv": "text/csv", "json": "application/json", "geojson": "application/geo+json",
    "kml": "application/vnd.google-earth.kml+xml", "zip": "application/zip", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "txt": "text/plain", "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
ALLOWED_EXT = {"pdf", "xlsx", "xls", "csv", "json", "geojson", "kml", "zip", "png", "jpg", "jpeg", "txt", "docx"}


def strip_controls(text: str) -> str:
    """PLAUS-034：去除双向控制字符与零宽字符。"""
    return "".join(ch for ch in text if ch not in BIDI and ch not in ZERO_WIDTH)


def plaus_030(field_path: str, text: str) -> list[PlausHit]:
    if text and _INJ.search(unicodedata.normalize("NFKC", strip_controls(text))):
        return [PlausHit("PLAUS-030", "block", "instruction-like content in free text", {"field": field_path})]
    return []


def plaus_034(field_path: str, text: str) -> list[PlausHit]:
    if not text:
        return []
    kinds = []
    if URL_RE.search(text):
        kinds.append("url")
    if EMAIL_RE.search(text):
        kinds.append("email")
    if any(ch in BIDI for ch in text):
        kinds.append("bidi")
    if any(ch in ZERO_WIDTH for ch in text):
        kinds.append("zero_width")
    if kinds:
        return [PlausHit("PLAUS-034", "warn", "URL/e-mail/control characters in field", {"field": field_path, "kinds": "+".join(kinds)})]
    return []


def scan_text_fields(fields: dict[str, str]) -> list[PlausHit]:
    hits: list[PlausHit] = []
    for path, text in fields.items():
        if isinstance(text, str):
            hits += plaus_030(path, text) + plaus_034(path, text)
    return hits


@dataclass
class FileScan:
    filename: str
    size: int
    detected_ext: str | None
    hits: list[PlausHit] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return any(h.severity == "block" and h.rule_id in ("PLAUS-032", "PLAUS-033") for h in self.hits)


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _ooxml_checks(data: bytes, filename: str) -> list[PlausHit]:
    """PLAUS-031/032：隐藏工作表/行列、注释长文本、宏、外部链接、DDE。"""
    hits: list[PlausHit] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [PlausHit("PLAUS-033", "block", "corrupt OOXML container", {"file": filename})]
    names = zf.namelist()
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        hits.append(PlausHit("PLAUS-032", "block", "macro content rejected", {"file": filename, "kind": "macro"}))
    if any(n.startswith("xl/externalLinks/") for n in names):
        hits.append(PlausHit("PLAUS-032", "block", "external link formulas", {"file": filename, "kind": "external_link"}))
    if any(n.startswith("xl/embeddings/") or "oleObject" in n for n in names):
        hits.append(PlausHit("PLAUS-032", "block", "embedded OLE object", {"file": filename, "kind": "ole"}))
    hidden: list[str] = []
    try:
        wb = zf.read("xl/workbook.xml").decode("utf-8", "replace")
        if re.search(r'<sheet\b[^>]*state="(hidden|veryHidden)"', wb):
            hidden.append("sheet")
    except KeyError:
        pass
    has_formula = False
    for n in names:
        if n.startswith("xl/worksheets/") and n.endswith(".xml"):
            x = zf.read(n).decode("utf-8", "replace")
            if re.search(r'<row\b[^>]*hidden="(1|true)"', x) or re.search(r'<col\b[^>]*hidden="(1|true)"', x):
                hidden.append("row_col")
            if "<f>" in x or "<f " in x:
                has_formula = True
            if re.search(r"<f[^>]*>[^<]*(DDE|\|')", x, re.I):
                hits.append(PlausHit("PLAUS-032", "block", "DDE formula", {"file": filename, "kind": "dde"}))
        if n.startswith("xl/comments") and n.endswith(".xml"):
            texts = re.findall(r"<t[^>]*>([^<]*)</t>", zf.read(n).decode("utf-8", "replace"))
            if any(len(t) > 200 for t in texts):
                hidden.append("long_comment")
    if "xl/styles.xml" in names:
        st = zf.read("xl/styles.xml").decode("utf-8", "replace")
        if re.search(r'<color rgb="FFFFFFFF"', st) or re.search(r'<sz val="[0-3](\.\d+)?"', st):
            hidden.append("format")
    if hidden:
        hits.append(PlausHit("PLAUS-031", "warn", "hidden content excluded from LLM context",
                             {"file": filename, "kinds": "+".join(sorted(set(hidden)))}))
    if has_formula:
        hits.append(PlausHit("PLAUS-032", "info", "formulas present: cached values only", {"file": filename, "kind": "formula"}))
    return hits


def _zip_ratio(data: bytes) -> float:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return 0.0
    comp = sum(i.compress_size for i in zf.infolist()) or 1
    return sum(i.file_size for i in zf.infolist()) / comp


def scan_file(filename: str, data: bytes, declared_mime: str | None = None) -> FileScan:
    """PLAUS-031/032/033：上传文件扫描（不解析业务内容）。"""
    ext = _ext(filename)
    scan = FileScan(filename=filename, size=len(data), detected_ext=None)
    if ext in ("xlsm", "xlsb"):
        scan.hits.append(PlausHit("PLAUS-032", "block", "macro-enabled workbook rejected", {"file": filename, "kind": "macro"}))
        return scan
    if len(data) > MAX_FILE_BYTES:
        scan.hits.append(PlausHit("PLAUS-033", "block", "file larger than 25 MB", {"file": filename, "reason": "size"}))
        return scan
    if ext not in ALLOWED_EXT:
        scan.hits.append(PlausHit("PLAUS-033", "block", "file type not allowed", {"file": filename, "reason": "type"}))
        return scan
    magics = MAGIC.get(ext, [])
    head = data[:8].lstrip(b"\xef\xbb\xbf \r\n\t")
    if magics and not any(head.startswith(m) for m in magics):
        scan.hits.append(PlausHit("PLAUS-033", "block", "content does not match extension", {"file": filename, "reason": "mime_mismatch"}))
        return scan
    if declared_mime and ext in MIME_BY_EXT and declared_mime not in (MIME_BY_EXT[ext], "application/octet-stream") \
            and not (ext in ("json", "geojson", "csv", "txt", "kml") and declared_mime.startswith(("text/", "application/json"))):
        scan.hits.append(PlausHit("PLAUS-033", "block", "declared MIME does not match extension", {"file": filename, "reason": "mime_mismatch"}))
        return scan
    scan.detected_ext = ext
    if data.startswith(b"PK\x03\x04"):
        ratio = _zip_ratio(data)
        if ratio > MAX_ZIP_RATIO:
            scan.hits.append(PlausHit("PLAUS-033", "block", "compression ratio > 100 (zip bomb)", {"file": filename, "reason": "zip_bomb", "ratio": f"{ratio:.0f}"}))
            return scan
        if ext in ("xlsx", "docx"):
            scan.hits += _ooxml_checks(data, filename)
    if ext == "pdf" and re.search(rb"/(JavaScript|JS)\b", data):
        scan.hits.append(PlausHit("PLAUS-033", "block", "PDF contains JavaScript", {"file": filename, "reason": "pdf_js"}))
    return scan


def plaus_035(*, sender_domain: str | None = None, allowed_domains: list[str] | None = None,
              distinct_ips: int = 0, distinct_devices: int = 0, ip_threshold: int = 5) -> list[PlausHit]:
    """来源不符：发件域不在 Counterparty.domains；或 token 被多方使用的迹象。"""
    hits: list[PlausHit] = []
    if sender_domain and allowed_domains is not None:
        d = sender_domain.lower()
        if not any(d == a.lower() or d.endswith("." + a.lower()) for a in allowed_domains):
            hits.append(PlausHit("PLAUS-035", "block", "sender domain not in Counterparty.domains", {"domain": d}))
    if distinct_ips >= ip_threshold or distinct_devices >= ip_threshold:
        hits.append(PlausHit("PLAUS-035", "block", "token used from many distinct origins", {"ips": str(distinct_ips), "devices": str(distinct_devices)}))
    return hits


CONTENT_RULE_IDS = ["PLAUS-030", "PLAUS-031", "PLAUS-032", "PLAUS-033", "PLAUS-034", "PLAUS-035"]
