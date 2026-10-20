# Spec: 02 §4（TemplateMapping 文件格式、template_id 枚举、命中优先级 §4.4）；04 §2.2（附件确定性指纹）
"""已知模板的指纹识别与精确映射（`method=exact_template`）。

映射文件随包分发在 `belegt_io/mappings/<template_id>.yaml`，格式沿用 02 §4.2，额外两个本包字段：
- `fingerprint.markers[]`：`{sheet}` / `{text}` / `{native_id}` / `{json_key}` + `weight`；置信度 = 命中权重 / 总权重。
- `layouts`：模板专用版面（`form` / `table` / `matrix` / `questionnaire`），用于在没有通用表头时切题。
推断置信度 < 0.85 时不使用精确映射（02 §4.4），退回通用解析。
"""
from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources
from typing import Any, Iterable

import yaml

from .models import QuestionDraft, TemplateDetection, TemplateMappingHit

TEMPLATE_IDS = (
    "vsme", "cdp_climate", "pact_pcf", "catenax_pcf", "cbam_communication",
    "eudr_dds", "reach_scip", "cmrt", "eu_battery_passport",
)


@lru_cache(maxsize=1)
def load_templates() -> dict[str, dict[str, Any]]:
    """返回 `{template_id: mapping_doc}`。"""
    out: dict[str, dict[str, Any]] = {}
    base = resources.files("belegt_io") / "mappings"
    for tid in TEMPLATE_IDS:
        doc = yaml.safe_load((base / f"{tid}.yaml").read_text(encoding="utf-8"))
        assert doc["template_id"] == tid, tid
        out[tid] = doc
    return out


def get_template(template_id: str) -> dict[str, Any]:
    return load_templates()[template_id]


def _norm(s: str | None) -> str:
    return re.sub(r"[\s_\-:*()（）.]+", "", (s or "").lower())


# —— 指纹 ——

def _score(doc: dict[str, Any], sheets: Iterable[str], texts: Iterable[str], native_ids: Iterable[str],
           json_keys: Iterable[str]) -> tuple[float, list[str]]:
    fp = doc.get("fingerprint") or {}
    markers = fp.get("markers") or []
    total = sum(float(m.get("weight", 1)) for m in markers) or 1.0
    sheet_set = {_norm(s) for s in sheets}
    text_blob = "\n".join(t for t in texts if t).lower()
    nid_set = {_norm(n) for n in native_ids if n}
    key_set = set(json_keys)
    got = 0.0
    hits: list[str] = []
    for m in markers:
        w = float(m.get("weight", 1))
        if "sheet" in m and _norm(m["sheet"]) in sheet_set:
            got += w
            hits.append(f"sheet:{m['sheet']}")
        elif "text" in m and m["text"].lower() in text_blob:
            got += w
            hits.append(f"text:{m['text']}")
        elif "native_id" in m and _norm(m["native_id"]) in nid_set:
            got += w
            hits.append(f"native_id:{m['native_id']}")
        elif "json_key" in m and m["json_key"] in key_set:
            got += w
            hits.append(f"json_key:{m['json_key']}")
    return round(got / total, 3), hits


def detect_template(sheets: Iterable[str], texts: Iterable[str], native_ids: Iterable[str] = (),
                    json_keys: Iterable[str] = ()) -> TemplateDetection | None:
    """对全部已知模板打分，返回最高分（> 0）的检测结果。是否可用精确映射看 `is_exact`。"""
    sheets, texts, native_ids, json_keys = list(sheets), list(texts), list(native_ids), list(json_keys)
    best: TemplateDetection | None = None
    for tid, doc in load_templates().items():
        conf, hits = _score(doc, sheets, texts, native_ids, json_keys)
        if conf <= 0:
            continue
        det = TemplateDetection(
            template_id=tid, template_version=str(doc["template_version"]),
            template_mapping=doc["template_mapping"], confidence=conf, matched_markers=hits,
        )
        if best is None or det.confidence > best.confidence:
            best = det
    return best


def _flatten_keys(obj: Any, prefix: str = "") -> list[str]:
    keys: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            keys.append(p)
            keys.extend(_flatten_keys(v, p))
    elif isinstance(obj, list) and obj:
        keys.extend(_flatten_keys(obj[0], prefix))
    return keys


def detect_json_template(doc: Any) -> TemplateDetection | None:
    """JSON 模板（PACT、Catena-X、电池护照）的键指纹识别。"""
    keys = _flatten_keys(doc)
    if isinstance(doc, dict) and isinstance(doc.get("data"), list) and doc["data"]:
        keys += _flatten_keys(doc["data"][0])  # PACT ListFootprints 响应
    return detect_template([], [], [], keys)


# —— 精确映射 ——

def _hit(doc: dict[str, Any], entry: dict[str, Any], conf: float, datapoint_id: str | None = None) -> TemplateMappingHit:
    return TemplateMappingHit(
        template_mapping_ref=f"{doc['template_mapping']}#{entry['entry_id']}",
        datapoint_id=datapoint_id or entry["datapoint_id"],
        confidence=conf,
        short_label=entry.get("short_label"),
        unit_hint=entry.get("unit_hint"),
        period_hint=entry.get("period_hint"),
        boundary_hint=entry.get("boundary_hint"),
        transform=entry.get("transform"),
        direction=entry.get("direction", "both"),
    )


def match_entries(questions: list[QuestionDraft], detection: TemplateDetection) -> int:
    """按 02 §4.4"先按 locator 精确匹配"把条目挂到 `QuestionDraft.exact_mappings`；返回命中题数。"""
    if not detection.is_exact:
        return 0
    doc = get_template(detection.template_id)
    conf = min(1.0, max(0.85, detection.confidence))
    n = 0
    for q in questions:
        hits: list[TemplateMappingHit] = []
        base_id = _norm(q.hints.get("base_id") or q.native_id)
        sheet = q.locator.sheet
        label = _norm(q.hints.get("label") or next(iter(q.text.values()), ""))
        column = q.hints.get("column")
        for e in doc.get("entries", []):
            loc = e.get("locator") or {}
            kind, ref = loc.get("kind"), str(loc.get("ref", ""))
            ok = False
            if kind in ("question", "paragraph"):
                r_sheet, _, r_id = ref.rpartition("!") if "!" in ref else ("", "", ref)
                ids = {_norm(r_id)} | ({_norm(loc["legacy_ref"])} if loc.get("legacy_ref") else set())
                ok = bool(base_id) and base_id in ids and (not r_sheet or r_sheet == sheet)
                if ok and loc.get("column") and column and _norm(loc["column"]) != _norm(column):
                    ok = False
            elif kind == "cell":
                r_sheet, _, r_cell = ref.rpartition("!")
                if r_cell.startswith("<") and r_cell.endswith(">"):
                    ok = r_sheet == sheet and label == _norm(r_cell[1:-1])
                else:
                    ok = r_sheet == sheet and r_cell.replace("$", "") == q.locator.cell
            elif kind == "column":
                cols = loc.get("columns") or [loc.get("column")]
                ok = ref == sheet and column is not None and _norm(column) in {_norm(c) for c in cols if c}
            elif kind == "field":
                ok = bool(label) and (_norm(ref) == label or _norm(ref) in label)
            if not ok:
                continue
            dp = None
            per_col = e.get("per_column")
            if per_col and column:
                dp = per_col.get(column)
            hits.append(_hit(doc, e, conf, dp))
        if hits:
            q.exact_mappings = hits
            n += 1
    return n


def layouts_for(template_id: str) -> dict[str, dict[str, Any]]:
    """返回 `{sheet_name: [layout, ...]}` 形式的版面定义（同一工作表可有多个版面）。"""
    doc = get_template(template_id)
    out: dict[str, list[dict[str, Any]]] = {}
    for key, lay in (doc.get("layouts") or {}).items():
        sheet = lay.get("sheet") or key.split("#", 1)[0]
        out.setdefault(sheet, []).append(lay)
    return out  # type: ignore[return-value]
