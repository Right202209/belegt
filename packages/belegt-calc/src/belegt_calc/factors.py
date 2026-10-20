# Spec: 05 §7（因子库版本化：结构、冻结、弃用、选择规则 §7.4）；03 §3.13
"""FactorSet 冻结格式、加载、选择与维护操作（import / diff / freeze / deprecate）。

文件格式（YAML，05 §7.1）::

    id: fs:<source>@<version>
    source: <source>
    license: ...
    published_at: YYYY-MM-DD
    valid_from: YYYY-MM-DD
    valid_until: YYYY-MM-DD        # 可选
    upstream: {title, url, retrieved_at}   # 可选
    rows:
      - {factor_key, geography, year, value, unit, source_row_id, quality: {status, gas_scope}}
    row_count: N
    rows_hash: sha256:...
    status: active | deprecated     # 未冻结的导入草稿为 draft
    deprecation: {reason, deprecated_at}   # 弃用后

YAML 以 ``BaseLoader`` 读取（所有标量都是字符串），杜绝 ``0.353`` 被解析为浮点数。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from . import units as U
from .dec import is_dec_str, parse_dec
from .errors import (
    CALC_DEC_INVALID,
    CALC_FACTOR_MISSING,
    CALC_FACTOR_SET_INTEGRITY,
    CALC_PARAM_REQUIRES_DECISION,
    CalcError,
)
from .hashing import rows_hash as compute_rows_hash
from .types import FactorRow, FactorSet

FS_ID_RE = re.compile(r"^fs:([a-z0-9-]+)@([0-9A-Za-z.\-]+)$")
ROW_CORE_KEYS = ("factor_key", "geography", "year", "value", "unit", "source_row_id")
STATUS_RANK = {"final": 0, "preliminary": 1, "estimated": 2}
PLACEHOLDER_FLAG = "rows_hash_placeholder"

# 05 §7.4-1：地理回退 国家 → 上级区域（欧盟）→ GLO
EU27 = frozenset(
    "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split()
)


def parent_regions(geo: str) -> list[str]:
    chain = [geo]
    if geo in EU27:
        chain.append("EU")
    if geo != "GLO":
        chain.append("GLO")
    return chain


# ---------------------------------------------------------------------------
# 加载 / 写出
# ---------------------------------------------------------------------------

def bundled_dir() -> Path:
    return Path(str(resources.files("belegt_calc") / "fs"))


def bundled_path(fs_id: str) -> Path:
    m = FS_ID_RE.match(fs_id)
    if not m:
        raise CalcError(CALC_FACTOR_MISSING, f"invalid FactorSet id {fs_id!r}")
    return bundled_dir() / f"{m.group(1)}@{m.group(2)}.yaml"


def list_bundled() -> list[str]:
    out = []
    for p in sorted(bundled_dir().glob("*.yaml")):
        out.append("fs:" + p.stem)
    return out


def _row_from_mapping(r: Mapping[str, Any], idx: int) -> FactorRow:
    if not isinstance(r, Mapping):
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"row {idx} is not a mapping")
    missing = [k for k in ROW_CORE_KEYS if k not in r]
    if missing:
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"row {idx} missing {missing}")
    for k in ROW_CORE_KEYS:
        if not isinstance(r[k], str):
            raise CalcError(CALC_DEC_INVALID, f"row {idx} field {k} must be a string")
    parse_dec(r["value"], what=f"row {r['source_row_id']} value")
    if not U.is_known(r["unit"]):
        from .errors import CALC_UNIT_INCOMPATIBLE

        raise CalcError(CALC_UNIT_INCOMPATIBLE, f"row {r['source_row_id']}: unknown unit {r['unit']!r} (R-UNIT)")
    if r["year"] != "-" and not re.fullmatch(r"[0-9]{4}", r["year"]):
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"row {r['source_row_id']}: year must be YYYY or '-'")
    quality = r.get("quality")
    if quality is not None and not isinstance(quality, Mapping):
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"row {r['source_row_id']}: quality must be a mapping")
    extra = {k: v for k, v in r.items() if k not in ROW_CORE_KEYS and k != "quality"}
    return FactorRow(
        factor_key=r["factor_key"],
        geography=r["geography"],
        year=r["year"],
        value=r["value"],
        unit=r["unit"],
        source_row_id=r["source_row_id"],
        quality=dict(quality) if quality else None,
        extra=extra or None,
    )


def factor_set_from_dict(doc: Mapping[str, Any], *, allow_draft: bool = False, origin: str = "<dict>") -> FactorSet:
    """由已解析的映射构造并校验 FactorSet。"""
    if not isinstance(doc, Mapping):
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"{origin}: not a mapping")
    fs_id = doc.get("id")
    if not isinstance(fs_id, str) or not FS_ID_RE.match(fs_id):
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"{origin}: invalid FactorSet id {fs_id!r}")
    source = doc.get("source")
    if source != FS_ID_RE.match(fs_id).group(1):
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"{origin}: source {source!r} does not match id {fs_id}")
    raw_rows = doc.get("rows") or []
    if isinstance(raw_rows, str):  # BaseLoader 把空列表 [] 读成 list；保险起见
        raw_rows = []
    rows = [_row_from_mapping(r, i) for i, r in enumerate(raw_rows)]
    ids = [r.source_row_id for r in rows]
    if len(ids) != len(set(ids)):
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"{origin}: duplicate source_row_id")
    status = doc.get("status", "active")
    if status not in ("active", "deprecated") and not (allow_draft and status == "draft"):
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"{origin}: status {status!r} (unfrozen draft cannot be used)")
    computed = compute_rows_hash(r.to_dict() for r in rows)
    declared = doc.get("rows_hash")
    warnings: list[str] = []
    if status != "draft":
        if not declared:
            raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"{origin}: frozen FactorSet without rows_hash")
        if declared != computed:
            if str(doc.get(PLACEHOLDER_FLAG, "")).lower() == "true":
                warnings.append(
                    f"rows_hash {declared} is a declared placeholder and does not equal the JCS rows hash {computed}"
                )
            else:
                raise CalcError(
                    CALC_FACTOR_SET_INTEGRITY,
                    f"{origin}: rows_hash mismatch (declared {declared}, computed {computed})",
                )
        rc = doc.get("row_count")
        if rc is not None and int(rc) != len(rows):
            raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"{origin}: row_count {rc} != {len(rows)}")
    known = {
        "id", "source", "license", "published_at", "valid_from", "valid_until", "upstream", "rows",
        "row_count", "rows_hash", "status", "deprecation",
    }
    meta = {k: v for k, v in doc.items() if k not in known}
    return FactorSet(
        id=fs_id,
        source=source,
        license=str(doc.get("license", "")),
        published_at=str(doc.get("published_at", "")),
        valid_from=str(doc.get("valid_from", "")),
        valid_until=doc.get("valid_until"),
        upstream=doc.get("upstream"),
        rows=rows,
        rows_hash=declared or computed,
        status=status,
        deprecation=doc.get("deprecation"),
        row_count=len(rows),
        meta=meta,
        warnings=warnings,
    )


def load_factor_set(path_or_id: str | Path, *, allow_draft: bool = False) -> FactorSet:
    """从 YAML 文件或内置 ID（如 ``fs:belegt-example@2026.10``）加载并校验。"""
    p = Path(path_or_id)
    if isinstance(path_or_id, str) and path_or_id.startswith("fs:") and not p.exists():
        p = bundled_path(path_or_id)
    if not p.exists():
        raise CalcError(CALC_FACTOR_MISSING, f"FactorSet file not found: {path_or_id}")
    doc = yaml.load(p.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return factor_set_from_dict(doc, allow_draft=allow_draft, origin=str(p))


def factor_set_to_dict(fs: FactorSet) -> dict:
    d: dict[str, Any] = {"id": fs.id, "source": fs.source, "license": fs.license, "published_at": fs.published_at,
                         "valid_from": fs.valid_from}
    if fs.valid_until:
        d["valid_until"] = fs.valid_until
    if fs.upstream:
        d["upstream"] = dict(fs.upstream)
    for k, v in fs.meta.items():
        d[k] = v
    d["rows"] = [r.to_dict() for r in fs.rows]
    d["row_count"] = len(fs.rows)
    if fs.status != "draft":
        d["rows_hash"] = fs.rows_hash
    d["status"] = fs.status
    if fs.deprecation:
        d["deprecation"] = dict(fs.deprecation)
    return d


def dump_factor_set(fs: FactorSet) -> str:
    return yaml.safe_dump(factor_set_to_dict(fs), sort_keys=False, allow_unicode=True, width=120)


# ---------------------------------------------------------------------------
# 选择规则（05 §7.4）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Selection:
    row: FactorRow
    year_gap: int          # 期间年份 − 行年份（无年份行为 0）


def select_row(fs: FactorSet, factor_key: str, *, geography: str | None, year: int | None) -> Selection:
    """按 05 §7.4 选择唯一因子行：地理 → 年份 → 状态；无匹配报 ``CALC_FACTOR_MISSING``。"""
    cands = [r for r in fs.rows if r.factor_key == factor_key]
    if not cands:
        raise CalcError(CALC_FACTOR_MISSING, f"{fs.id}: no row for factor_key {factor_key!r}")
    # 1. 地理
    if geography is not None:
        chosen: list[FactorRow] = []
        for g in parent_regions(geography):
            chosen = [r for r in cands if r.geography == g]
            if chosen:
                break
        if not chosen:
            raise CalcError(CALC_FACTOR_MISSING, f"{fs.id}: {factor_key!r} has no row for geography {geography} (or EU/GLO)")
        cands = chosen
    else:
        geos = {r.geography for r in cands}
        if len(geos) > 1:
            raise CalcError(
                CALC_PARAM_REQUIRES_DECISION,
                f"{fs.id}: {factor_key!r} has rows for several geographies {sorted(geos)}; pass params.country or params.grid_region",
            )
    # 2. 年份：同年 → 不晚于期间的最近年份 → 无年份行；不得使用晚于期间的年份
    gap = 0
    if year is not None:
        dated = [r for r in cands if r.year != "-"]
        same = [r for r in dated if int(r.year) == year]
        earlier = [r for r in dated if int(r.year) < year]
        timeless = [r for r in cands if r.year == "-"]
        if same:
            cands = same
        elif earlier:
            best = max(int(r.year) for r in earlier)
            cands = [r for r in earlier if int(r.year) == best]
            gap = year - best
        elif timeless:
            cands = timeless
        else:
            raise CalcError(CALC_FACTOR_MISSING, f"{fs.id}: {factor_key!r} only has rows later than {year} (no future data)")
    # 3. 状态 final > preliminary > estimated
    best_rank = min(STATUS_RANK.get(r.status, 3) for r in cands)
    cands = [r for r in cands if STATUS_RANK.get(r.status, 3) == best_rank]
    if len(cands) != 1:
        raise CalcError(
            CALC_FACTOR_SET_INTEGRITY,
            f"{fs.id}: {factor_key!r} is ambiguous ({[r.source_row_id for r in cands]})",
        )
    return Selection(row=cands[0], year_gap=gap)


def keys_with_prefix(fs: FactorSet, prefix: str) -> list[str]:
    return sorted({r.factor_key for r in fs.rows if r.factor_key.startswith(prefix)})


# ---------------------------------------------------------------------------
# 维护操作：import / diff / freeze / deprecate（05 §7.3）
# ---------------------------------------------------------------------------

def import_rows_csv(text: str) -> list[dict]:
    """解析 CSV 行（列：factor_key,geography,year,value,unit,source_row_id，可选 quality.status、
    quality.gas_scope、upstream_source）。空单元格省略。数值必须是 DEC-STR，单位须已知（R-UNIT）。"""
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for i, rec in enumerate(reader):
        row: dict[str, Any] = {}
        for k, v in rec.items():
            if k is None:
                continue
            k = k.strip()
            v = (v or "").strip()
            if v == "":
                continue
            if "." in k:
                a, b = k.split(".", 1)
                row.setdefault(a, {})[b] = v
            else:
                row[k] = v
        _row_from_mapping(row, i)  # 校验
        rows.append(row)
    return rows


def make_draft(fs_id: str, rows: list[dict], *, license: str, published_at: str, valid_from: str,
               valid_until: str | None = None, upstream: Mapping[str, Any] | None = None) -> FactorSet:
    m = FS_ID_RE.match(fs_id)
    if not m:
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"invalid FactorSet id {fs_id!r}")
    doc = {"id": fs_id, "source": m.group(1), "license": license, "published_at": published_at,
           "valid_from": valid_from, "rows": rows, "status": "draft"}
    if valid_until:
        doc["valid_until"] = valid_until
    if upstream:
        doc["upstream"] = dict(upstream)
    return factor_set_from_dict(doc, allow_draft=True)


def freeze(fs: FactorSet) -> FactorSet:
    """冻结：计算 rows_hash 与 row_count，status=active。已冻结的库不得改变内容。"""
    computed = compute_rows_hash(r.to_dict() for r in fs.rows)
    if fs.status != "draft":
        if fs.rows_hash != computed and str(fs.meta.get(PLACEHOLDER_FLAG, "")).lower() != "true":
            raise CalcError(CALC_FACTOR_SET_INTEGRITY, f"{fs.id} is frozen and its rows changed; publish a new version")
        return fs
    fs.rows_hash = computed
    fs.row_count = len(fs.rows)
    fs.status = "active"
    return fs


def deprecate(fs: FactorSet, *, reason: str, deprecated_at: str) -> FactorSet:
    """``active → deprecated``（不可逆）；行内容与 rows_hash 不变，历史计算可重放。"""
    if fs.status == "draft":
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, "cannot deprecate an unfrozen draft")
    if not reason:
        raise CalcError(CALC_FACTOR_SET_INTEGRITY, "deprecation.reason is required")
    fs.status = "deprecated"
    fs.deprecation = {"reason": reason, "deprecated_at": deprecated_at}
    return fs


def diff(old: FactorSet, new: FactorSet) -> dict:
    """两版因子库的差异报告（按 ``factor_key + geography + year`` 对齐）。"""
    def key(r: FactorRow) -> tuple[str, str, str]:
        return (r.factor_key, r.geography, r.year)

    a = {key(r): r for r in old.rows}
    b = {key(r): r for r in new.rows}
    added = [b[k].to_dict() for k in sorted(set(b) - set(a))]
    removed = [a[k].to_dict() for k in sorted(set(a) - set(b))]
    changed = []
    for k in sorted(set(a) & set(b)):
        ra, rb = a[k].to_dict(), b[k].to_dict()
        if ra != rb:
            fields = sorted(f for f in set(ra) | set(rb) if ra.get(f) != rb.get(f))
            changed.append({"factor_key": k[0], "geography": k[1], "year": k[2], "fields": fields,
                            "old": ra, "new": rb})
    return {"old": old.id, "new": new.id, "old_rows_hash": old.rows_hash, "new_rows_hash": new.rows_hash,
            "added": added, "removed": removed, "changed": changed,
            "summary": {"added": len(added), "removed": len(removed), "changed": len(changed)}}


def rows_from_iter(rows: Iterable[Mapping[str, Any]]) -> list[FactorRow]:
    return [_row_from_mapping(r, i) for i, r in enumerate(rows)]


__all__ = [
    "EU27", "FS_ID_RE", "Selection", "bundled_dir", "bundled_path", "deprecate", "diff", "dump_factor_set",
    "factor_set_from_dict", "factor_set_to_dict", "freeze", "import_rows_csv", "is_dec_str", "keys_with_prefix",
    "list_bundled", "load_factor_set", "make_draft", "parent_regions", "rows_from_iter", "select_row",
]
