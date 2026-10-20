# Spec: 04 §6.1（文档库 docs.search / docs.get_page 示例）、§2.4 evidence-miner（版面用确定性工具）；
#       03 §3.5 Evidence/Locator（page + bbox + text_excerpt_hash）、R-LOC；FR-202
"""电费单 / 燃气单 / 供热单 PDF 文件夹连接器。

- 优先调用 `belegt_io.extract_pdf_evidence`（页码 + 估算 bbox + `text_excerpt_hash`，德式数字清洗、单位归一）；
- `belegt_io` 不可导入时退回内置提取（pypdf 文本层或原始内容流 `Tj` 解析 + 简化数字/单位正则），
  locator 只有页码（仍满足 R-LOC 的 page 条件），置信度封顶 0.85（不满足 `EV-AUTO-0.90-LOC`，必经人确认）。

工具：
- `docs.utility_bills.files.list`   账单清单：供应商（首行）、品类（电/气/热）、账单期、页数；增量同步
- `docs.utility_bills.facts.query`  逐个带单位数字的候选事实（`fact_key`、DEC-STR 值、单位、期间、locator）
- `docs.utility_bills.pages.get`    单页文本（外部数据），供 LLM 抽取时“每次只给一个文档块”
"""
from __future__ import annotations

import re
import zlib
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..base import BaseConnector, CallContext, safe_join
from ..spec import CURSOR_PROPS, SYNC_PROPS, ConnectorError, dec_str, object_schema, sha256_bytes

PDF_MIME = "application/pdf"
FALLBACK_CONFIDENCE_CAP = 0.85

COMMODITY_RULES = [
    (re.compile(r"fernw(ä|ae)rme|district heat", re.I), "district_heat"),
    (re.compile(r"erdgas|natural gas|\bgas\b|gasversorgung", re.I), "natural_gas"),
    (re.compile(r"strom|electricity|wirkarbeit|power", re.I), "electricity"),
    (re.compile(r"wasser|water", re.I), "water"),
]
_PERIOD_RE = re.compile(
    r"(?:abrechnungszeitraum|leistungszeitraum|billing period|zeitraum)[:\s]*"
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(?:-|–|bis|to)\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I)
_PERIOD_ISO_RE = re.compile(r"(?:billing period|period)[:\s]*(\d{4}-\d{2}-\d{2})\s*(?:-|–|to)\s*(\d{4}-\d{2}-\d{2})",
                            re.I)

# —— 内置退回提取（belegt_io 不可用时） ——
_UNITS = [("t CO2e", "tCO2e"), ("tCO2e", "tCO2e"), ("kWh/m3", None), ("kWh", "kWh"), ("MWh", "MWh"),
          ("m3", "m3"), ("m³", "m3"), ("EUR", "EUR"), ("Liter", "L")]
_NUM_UNIT_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?|\d+(?:\.\d+)?)\s*"
                          r"(t CO2e|tCO2e|kWh/m3|kWh|MWh|m3|m³|EUR|Liter)\b")
_FACT_RULES = [
    (re.compile(r"strom|wirkarbeit|electricity", re.I), {"kWh", "MWh"}, "activity.electricity.purchased"),
    (re.compile(r"erdgas|natural gas|\bgas\b", re.I), {"kWh", "MWh", "m3"}, "activity.natural_gas.consumed"),
    (re.compile(r"fernw(ä|ae)rme|district heat", re.I), {"kWh", "MWh"}, "activity.district_heat.purchased"),
]


def _fallback_number(tok: str) -> str | None:
    t = tok
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", t):
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return dec_str(Decimal(t))
    except (InvalidOperation, ValueError):
        return None


def _pages_text_fallback(data: bytes) -> list[str]:
    try:
        import io

        from pypdf import PdfReader

        return [(p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages]
    except ImportError:  # 纯标准库：解析未压缩内容流里的 (..) Tj（仅适用于简单 PDF，如本包样例）
        pages = []
        for m in re.finditer(rb"stream\n(.*?)\nendstream", data, re.S):
            raw = m.group(1)
            if b"/FlateDecode" in data[max(0, m.start() - 200): m.start()]:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    continue
            lines = [re.sub(rb"\\(.)", rb"\1", t).decode("latin-1") for t in re.findall(rb"\((.*?)(?<!\\)\)\s*Tj", raw)]
            if lines:
                pages.append("\n".join(lines))
        return pages


def _detect_period(text: str) -> dict[str, str] | None:
    m = _PERIOD_RE.search(text)
    if m:
        try:
            s = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            e = date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
            return {"start": s.isoformat(), "end": e.isoformat()}
        except ValueError:
            return None
    m = _PERIOD_ISO_RE.search(text)
    if m:
        return {"start": m.group(1), "end": m.group(2)}
    return None


def _commodity(text: str) -> str:
    for rx, c in COMMODITY_RULES:
        if rx.search(text):
            return c
    return "unknown"


def belegt_io_available() -> bool:
    try:
        import belegt_io  # noqa: F401
        return True
    except ImportError:
        return False


class UtilityBillPdfConnector(BaseConnector):
    kind = "utility-bills"
    category = "docs"
    system = "utility_bills"
    title = "Utility bill PDFs (electricity / gas / heat), read-only"
    source_system = "document-folder"
    attested = False

    def __init__(self, root: str | Path, connector_id: str | None = None, *, use_belegt_io: bool | None = None,
                 **kw: Any) -> None:
        super().__init__(connector_id or f"conn:utility-bills:{Path(root).name or 'root'}", **kw)
        self.root = Path(root)
        if not self.root.is_dir():
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"root folder {root!s} does not exist")
        self.use_belegt_io = belegt_io_available() if use_belegt_io is None else use_belegt_io
        if self.use_belegt_io and not belegt_io_available():
            raise ConnectorError("CONNECTOR_SOURCE_ERROR", "belegt_io requested but not importable")
        ref = {"path": {"type": "string"}, "blob_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}}
        self.register("docs.utility_bills.files.list", "账单 PDF 清单：供应商、品类、账单期、页数；支持增量同步",
                      object_schema({**CURSOR_PROPS, **SYNC_PROPS}), self._files, paginated=True, incremental=True)
        self.register(
            "docs.utility_bills.facts.query",
            "候选事实：每个带单位数字一条（fact_key、DEC-STR 值、单位、期间、置信度、locator=页码+bbox）。"
            "status 恒为 candidate，是否 verified 由 evidence-miner/guard 按 EV-AUTO-0.90-LOC 决定。",
            object_schema({**ref, "fact_key": {"type": "string"},
                           "min_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                           "include_unclassified": {"type": "boolean", "default": True}, **CURSOR_PROPS}),
            self._facts, paginated=True, evidence_source_type="document")
        self.register("docs.utility_bills.pages.get", "单页文本（外部数据，external_data=true）",
                      object_schema({**ref, "page": {"type": "integer", "minimum": 1}}, ["page"]), self._page)

    def compliance_probes(self) -> dict[str, dict[str, Any]]:
        files = self._scan()
        probes: dict[str, dict[str, Any]] = {"docs.utility_bills.files.list": {}}
        if files:
            rel = files[0].relative_to(self.root.resolve()).as_posix()
            probes["docs.utility_bills.facts.query"] = {"path": rel}
            probes["docs.utility_bills.pages.get"] = {"path": rel, "page": 1}
        return probes

    # —— helpers ——
    def _scan(self) -> list[Path]:
        root = self.root.resolve()
        return [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() == ".pdf"]

    def _resolve(self, args: dict[str, Any]) -> list[tuple[Path, bytes, str]]:
        if args.get("path"):
            p = safe_join(self.root, args["path"])
            if not p.is_file():
                raise ConnectorError("CONNECTOR_NOT_FOUND", f"file {args['path']!r} not found")
            data = p.read_bytes()
            return [(p, data, sha256_bytes(data))]
        files = [(p, p.read_bytes()) for p in self._scan()]
        out = [(p, d, sha256_bytes(d)) for p, d in files]
        if args.get("blob_sha256"):
            out = [x for x in out if x[2] == args["blob_sha256"]]
            if not out:
                raise ConnectorError("CONNECTOR_NOT_FOUND", "no PDF with this blob_sha256")
        return out

    def pages_text(self, path: Path, data: bytes) -> list[str]:
        if self.use_belegt_io:
            from belegt_io.evidence import pdf_pages_text
            return pdf_pages_text(path)
        return _pages_text_fallback(data)

    # —— handlers ——
    def _files(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        root = self.root.resolve()
        out = []
        for p in self._scan():
            data = p.read_bytes()
            sha = sha256_bytes(data)
            pages = self.pages_text(p, data)
            full = "\n".join(pages)
            first = next((ln.strip() for ln in full.splitlines() if ln.strip()), "")
            rel = p.relative_to(root).as_posix()
            st = p.stat()
            out.append({"record_key": rel,
                        "fields": {"path": rel, "blob_sha256": sha, "pages": len(pages), "supplier": first[:120],
                                   "commodity": _commodity(full), "billing_period": _detect_period(full),
                                   "size": st.st_size, "extractor": "belegt_io" if self.use_belegt_io else "fallback"},
                        "period": _detect_period(full),
                        "locator": {"blob_sha256": sha, "mime": PDF_MIME},
                        "_wm": f"{st.st_mtime_ns:020d}|{rel}"})
        out.sort(key=lambda r: r["_wm"])
        return out

    def _facts(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p, data, sha in self._resolve(args):
            full = "\n".join(self.pages_text(p, data))
            bill_period = _detect_period(full)
            cands = self._extract_io(p, sha) if self.use_belegt_io else self._extract_fallback(data, sha)
            for n, c in enumerate(cands):
                if c["fields"].get("fact_key") is None and not args.get("include_unclassified", True):
                    continue
                if args.get("fact_key") and c["fields"].get("fact_key") != args["fact_key"]:
                    continue
                if args.get("min_confidence") is not None and c["fields"]["confidence"] < args["min_confidence"]:
                    continue
                if c.get("period") is None and bill_period:
                    c["period"] = bill_period
                c["record_key"] = f"{sha}#p{c['locator']['page']}#{n}"
                out.append(c)
        return out

    def _extract_io(self, path: Path, sha: str) -> list[dict[str, Any]]:
        from belegt_io import extract_pdf_evidence

        out = []
        for c in extract_pdf_evidence(path):
            loc = c.locator.to_json() if hasattr(c.locator, "to_json") else c.locator.model_dump(exclude_none=True)
            loc = {k: (list(v) if isinstance(v, tuple) else v) for k, v in loc.items() if v is not None}
            if loc.get("blob_sha256") != sha:  # 防御：belegt_io 与本连接器哈希口径一致
                raise ConnectorError("CONNECTOR_SOURCE_ERROR", "blob hash mismatch")
            period = ({"start": c.period.start.isoformat(), "end": c.period.end.isoformat()}
                      if c.period is not None and c.period.start and c.period.end else None)
            fact_key, reasons = c.fact_key, list(c.reasons)
            if fact_key is None and c.label:  # 连接器补充规则（如 “Fernwaerme” 的 ae 写法）
                fact_key = next((k for rx, us, k in _FACT_RULES if rx.search(c.label) and c.unit in us), None)
                if fact_key:
                    reasons.append("fact_key_from_connector_rules")
            out.append({
                "fields": {"fact_key": fact_key, "extracted_value": c.extracted_value, "value_type": c.value_type,
                           "unit": c.unit, "label": c.label, "confidence": c.confidence,
                           "needs_confirmation": c.needs_confirmation, "is_estimate": c.is_estimate,
                           "reasons": reasons, "extractor": "belegt_io", "external_data": True},
                "unit": c.unit, "period": period, "locator": loc,
            })
        return out

    def _extract_fallback(self, data: bytes, sha: str) -> list[dict[str, Any]]:
        out = []
        for pno, text in enumerate(_pages_text_fallback(data), start=1):
            for line in text.splitlines():
                for m in _NUM_UNIT_RE.finditer(line):
                    raw_unit = m.group(2)
                    unit = dict(_UNITS).get(raw_unit)
                    if unit is None:
                        continue
                    val = _fallback_number(m.group(1))
                    if val is None:
                        continue
                    label = line[: m.start()].strip(" :;-–\t") or line
                    fact = next((k for rx, us, k in _FACT_RULES if rx.search(label) and unit in us), None)
                    amb = bool(re.fullmatch(r"\d{1,3}\.\d{3}", m.group(1)))  # 1.234 歧义（千分位或小数）
                    conf = min(FALLBACK_CONFIDENCE_CAP, 0.55 if amb else 0.85)
                    out.append({
                        "fields": {"fact_key": fact, "extracted_value": val, "value_type": "number", "unit": unit,
                                   "label": label, "confidence": conf, "needs_confirmation": amb or conf < 0.9,
                                   "is_estimate": False, "reasons": ["fallback_extractor"] + (["ambiguous"] if amb else []),
                                   "extractor": "fallback", "external_data": True},
                        "unit": unit, "period": None,
                        "locator": {"blob_sha256": sha, "mime": PDF_MIME, "page": pno,
                                    "text_excerpt_hash": sha256_bytes(line.encode("utf-8"))},
                    })
        return out

    def _page(self, args: dict[str, Any], ctx: CallContext) -> list[dict[str, Any]]:
        files = self._resolve(args)
        if len(files) != 1:
            raise ConnectorError("CONNECTOR_INPUT_INVALID", "path or blob_sha256 must identify exactly one PDF")
        p, data, sha = files[0]
        pages = self.pages_text(p, data)
        n = int(args["page"])
        if n > len(pages):
            raise ConnectorError("CONNECTOR_NOT_FOUND", f"page {n} > {len(pages)}")
        return [{"record_key": f"{sha}#p{n}", "fields": {"page": n, "pages": len(pages), "text": pages[n - 1],
                                                         "external_data": True},
                 "locator": {"blob_sha256": sha, "mime": PDF_MIME, "page": n}}]
