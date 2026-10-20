# Spec: 02 §6.2 (校验 CI DC-01…DC-12), §1.1 字段约束补充, §2.2 命名禁忌, §2.3 qualifier, §2.5 别名, §6.3 版本策略
"""Dictionary CI checks.

``validate_tree(root, baseline=None, corpus=None)`` runs DC-01…DC-12 and returns a :class:`Report`.
Levels follow 02 §6.2: every DC is ``error`` except DC-12 (``info``); a few sub-checks emit ``warn``
where 02 says so (DC-04 deprecated reference) or where the doc leaves severity open.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Literal

from jsonschema import Draft202012Validator

from .cel import DP_REF, ExprError, check_expr
from .dictionary import Dictionary, RawTree, build_dictionary, enum_codes
from .models import DataPoint, MappingEntry, Transform
from .versioning import classify_change, compare_semver
from .yamlio import dump_yaml

Level = Literal["error", "warn", "info"]
RULES = {
    "DC-01": "schema lint", "DC-02": "ID 唯一", "DC-03": "ID 规范", "DC-04": "映射引用存在", "DC-05": "单位可换算",
    "DC-06": "枚举映射完备", "DC-07": "别名无环", "DC-08": "i18n 完整", "DC-09": "版本递增", "DC-10": "原文检测",
    "DC-11": "关系一致", "DC-12": "覆盖报告",
}
TEMPLATE_IDS = ["vsme", "cdp_climate", "pact_pcf", "catenax_pcf", "cbam_communication", "eudr_dds", "reach_scip", "cmrt",
                "eu_battery_passport"]
EMISSION_DIMENSIONS = {"mass_co2e", "emission_intensity_mass", "emission_intensity_energy", "emission_intensity_currency",
                       "emission_intensity_volume"}
HUMAN_OK_TYPES = {"bool", "enum", "date", "text", "file"}
HUMAN_NUMBER_TAG = "human_confirmable_number"
# 02 §2.2 naming taboos
TEMPLATE_WORDS = {"vsme", "cdp", "pact", "catenax", "cmrt", "passport", "wbcsd", "tfs", "cbam", "eudr", "scip", "reach", "rohs"}
REGULATION_TOPICS = {"cbam", "eudr", "scip", "reach", "rohs"}
UNIT_TOKENS = {"t", "kg", "g", "tco2e", "kgco2e", "tco2", "kwh", "mwh", "gwh", "gj", "mj", "m3", "l", "m2", "ha", "eur", "usd",
               "ppm", "tonnes", "tons", "tonne", "liters", "litres"}
YEAR_RX = re.compile(r"^((19|20)\d{2}|fy\d{2,4}|q[1-4]|(19|20)\d{2}q[1-4]|h[12])$")
NEGATION_TOKENS = {"no", "not", "non", "without"}
TENANT_RX = re.compile(r"(^|_)(site|plant|customer|tenant)_[a-z0-9]{1,2}($|_)")
VOWELS = set("aeiouy")


@dataclass
class Finding:
    rule: str
    level: Level
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.level.upper():5} {self.path}: {self.message}"


@dataclass
class Report:
    root: str
    findings: list[Finding] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, rule: str, level: Level, path: str, message: str) -> None:
        self.findings.append(Finding(rule, level, path, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def by_rule(self, rule: str, level: Level | None = None) -> list[Finding]:
        return [f for f in self.findings if f.rule == rule and (level is None or f.level == level)]

    def to_json(self) -> str:
        return json.dumps({"root": self.root, "ok": self.ok, "counts": self.counts, "coverage": self.coverage,
                           "findings": [asdict(f) for f in self.findings]}, ensure_ascii=False, indent=2)

    def to_text(self) -> str:
        lines = [str(f) for f in self.findings if f.level != "info"]
        lines.append("")
        lines.append("DC-12 覆盖报告：")
        for tid, c in self.coverage.get("templates", {}).items():
            lines.append(f"  {tid:22} files={c['files']} entries={c['entries']} datapoints={c['datapoints_mapped']} "
                         f"tagged={c['datapoints_tagged']} coverage={c['coverage_pct']}% verify={c['entries_to_verify']}")
        ext = self.coverage.get("external_contribution_share_pct")
        lines.append(f"  外部贡献占比: {ext if ext is not None else 'n/a（无 contributions.yaml）'}")
        lines.append("")
        lines.append(f"{self.counts.get('datapoints', 0)} datapoints, {self.counts.get('template_mappings', 0)} template mappings; "
                     f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        return "\n".join(lines)


# --------------------------------------------------------------------------- helpers
def _schema(name: str) -> dict:
    return json.loads((resources.files("belegt_dictionary") / "data" / "schema" / name).read_text(encoding="utf-8"))


def _schema_for(root: Path, name: str) -> dict:
    p = root / "schema" / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else _schema(name)


def _jpath(err) -> str:
    return "/".join(str(p) for p in err.absolute_path) or "<root>"


def _id_parts(dp_id: str) -> list[str]:
    return dp_id[3:].split(".") if dp_id.startswith("dp:") else dp_id.split(".")


def expected_dp_path(dp_id: str) -> str:
    p = _id_parts(dp_id)
    return f"datapoints/{p[0]}/{p[1]}/{'.'.join(p[2:])}.yaml"


def _iter_steps(t: Transform | None) -> Iterable[Transform]:
    if t is not None:
        yield from t.walk()


class _Ctx:
    def __init__(self, tree: RawTree, d: Dictionary, report: Report, baseline: RawTree | None,
                 corpus: dict[int, set[str]] | None, contributions: list[dict] | None):
        self.tree, self.d, self.r, self.baseline, self.corpus, self.contrib = tree, d, report, baseline, corpus, contributions
        self.dp_files = {f.data.get("id"): f for f in tree.datapoints if isinstance(f.data, dict)}
        self.tm_files = {f.data.get("template_mapping"): f for f in tree.mappings if isinstance(f.data, dict)}

    def dp_path(self, dp_id: str) -> str:
        f = self.dp_files.get(dp_id)
        return f.rel if f else dp_id

    def tm_path(self, tm_id: str) -> str:
        f = self.tm_files.get(tm_id)
        return f.rel if f else tm_id


# --------------------------------------------------------------------------- DC-01
def dc01(c: _Ctx) -> None:
    r = c.r
    for rel, msg in c.tree.load_errors:
        r.add("DC-01", "error", rel, msg)
    root = c.tree.root
    for files, schema_name in ((c.tree.datapoints, "datapoint.schema.json"), (c.tree.mappings, "template_mapping.schema.json"),
                               (c.tree.migrations, "migration.schema.json")):
        v = Draft202012Validator(_schema_for(root, schema_name))
        for f in files:
            for err in sorted(v.iter_errors(f.data), key=lambda e: list(e.absolute_path)):
                r.add("DC-01", "error", f.rel, f"schema: {_jpath(err)}: {err.message}")
    # YAML formatting consistency (canonical dump)
    fmt_files = list(c.tree.datapoints) + list(c.tree.mappings) + list(c.tree.migrations) + list(c.tree.enums.values())
    fmt_files += [f for f in (c.tree.units, c.tree.conversions, c.tree.aliases, c.tree.methodologies, c.tree.abbreviations) if f]
    for f in fmt_files:
        if f.text != dump_yaml(f.data):
            r.add("DC-01", "error", f.rel, "YAML not in canonical format (run `belegt-dict fmt`)")
    # 02 §1.1 字段约束补充
    for dp in c.d.datapoints.values():
        p = c.dp_path(dp.id)
        ev = dp.evidence_requirements
        if dp.value_type == "number" and not ev.requires_calculation and not ev.accepted_source_types:
            r.add("DC-01", "error", p, "number datapoint needs requires_calculation or accepted_source_types (§1.1)")
        dim = c.d.units.dimension(dp.unit) if dp.unit and dp.unit in c.d.units else None
        if ev.human_confirmation_allowed:
            if dim in EMISSION_DIMENSIONS:
                r.add("DC-01", "error", p, "emission numbers must have human_confirmation_allowed=false (§1.1, FR-401)")
            elif dp.value_type == "number" and HUMAN_NUMBER_TAG not in (dp.tags or []):
                r.add("DC-01", "error", p, f"human_confirmation_allowed=true on a number requires tag {HUMAN_NUMBER_TAG} (§1.1)")
            elif dp.value_type not in HUMAN_OK_TYPES | {"number"}:
                r.add("DC-01", "error", p, f"human_confirmation_allowed=true not allowed for value_type={dp.value_type} (§1.1)")
        if "human_confirmation" in ev.accepted_source_types and not ev.human_confirmation_allowed:
            r.add("DC-01", "error", p, "accepted_source_types contains human_confirmation but human_confirmation_allowed=false")
        if "product_per_unit" in dp.boundary and (not dp.unit or "/" not in dp.unit):
            r.add("DC-01", "error", p, "boundary product_per_unit requires a per-declared-unit composite unit (§1.1)")
        if "organization" in dp.boundary and not dp.consolidation:
            r.add("DC-01", "error", p, "boundary contains organization: consolidation required (§1.1)")
        if dp.cardinality == "one" and dp.record_fields:
            r.add("DC-01", "warn", p, "record_fields given but cardinality=one")
        if dp.value_type != "number" and (dp.unit or dp.allowed_units):
            r.add("DC-01", "warn", p, "unit/allowed_units on a non-number datapoint")
        vr = dp.validity_rule
        if vr and vr.regex:
            try:
                re.compile(vr.regex)
            except re.error as e:
                r.add("DC-01", "error", p, f"validity_rule.regex invalid: {e}")
        if vr and vr.expires_by == "period_end_plus_days" and vr.expires_after_days is None:
            r.add("DC-01", "error", p, "expires_by=period_end_plus_days requires expires_after_days")
        if vr and vr.min is not None and vr.max is not None and vr.min > vr.max:
            r.add("DC-01", "error", p, "validity_rule.min > max")


# --------------------------------------------------------------------------- DC-02
def dc02(c: _Ctx) -> None:
    r = c.r
    seen: dict[str, str] = {}
    for f in c.tree.datapoints:
        if not isinstance(f.data, dict) or "id" not in f.data:
            continue
        i = f.data["id"]
        if i in seen:
            r.add("DC-02", "error", f.rel, f"duplicate DataPoint id {i} (also {seen[i]})")
        seen[i] = f.rel
        if isinstance(i, str) and i.startswith("dp:") and f.rel != expected_dp_path(i):
            r.add("DC-02", "error", f.rel, f"path does not match id; expected {expected_dp_path(i)}")
    if c.baseline is not None:
        for f in c.baseline.datapoints:
            if isinstance(f.data, dict) and f.data.get("status") == "retired":
                cur = c.dp_files.get(f.data["id"])
                if cur is not None and cur.data.get("status") != "retired":
                    r.add("DC-02", "error", cur.rel, f"retired id {f.data['id']} must not be reused")
    tms: dict[str, str] = {}
    for f in c.tree.mappings:
        if not isinstance(f.data, dict):
            continue
        tid, ver, tm = f.data.get("template_id"), f.data.get("template_version"), f.data.get("template_mapping")
        if tm in tms:
            r.add("DC-02", "error", f.rel, f"duplicate TemplateMapping {tm} (also {tms[tm]})")
        tms[tm] = f.rel
        if tid and ver and f.rel != f"mappings/{tid}/{ver}.yaml":
            r.add("DC-02", "error", f.rel, f"path does not match template; expected mappings/{tid}/{ver}.yaml")
        ids = [e.get("entry_id") for e in f.data.get("entries") or [] if isinstance(e, dict)]
        for dup in sorted({x for x in ids if ids.count(x) > 1}):
            r.add("DC-02", "error", f.rel, f"duplicate entry_id {dup}")


# --------------------------------------------------------------------------- DC-03
def _abbrev(c: _Ctx) -> tuple[set[str], set[str], list[str]]:
    a = c.tree.abbreviations.data if c.tree.abbreviations else {}
    allowed: set[str] = set()
    phrases = [w for w in a.get("whitelist", []) if "_" in w]
    allowed |= {w for w in a.get("whitelist", []) if "_" not in w}
    allowed |= {x["abbr"] if isinstance(x, dict) else x for x in a.get("registered", [])}
    return allowed, set(a.get("short_words", [])), phrases


def _qualifiers(c: _Ctx) -> set[str]:
    q = c.tree.enums.get("enums/qualifiers.yaml")
    vals: set[str] = set()
    if q:
        for dim in q.data.get("dimensions", []):
            vals |= set(dim.get("values", []))
    return vals


def id_naming_problems(dp_id: str, *, abbreviations: set[str], short_words: set[str], phrases: list[str],
                       qualifiers: set[str]) -> list[str]:
    """02 §2.1–§2.3 naming checks for one id (regex, length, taboos, abbreviations, qualifier)."""
    from .models import ID_PATTERN
    probs: list[str] = []
    if not re.match(ID_PATTERN, dp_id):
        return [f"id {dp_id!r} does not match {ID_PATTERN}"]
    if len(dp_id) > 120:
        probs.append("id longer than 120 characters")
    parts = _id_parts(dp_id)
    if any(len(s) > 48 for s in parts):
        probs.append("segment longer than 48 characters")
    topic, metric = parts[1], parts[2]
    qualifier = parts[3] if len(parts) == 4 else None
    if qualifier is not None and qualifier not in qualifiers:
        probs.append(f"qualifier {qualifier!r} not registered in enums/qualifiers.yaml (§2.3)")
    for seg_name, seg in (("topic", topic), ("metric", metric)):
        toks = seg.split("_")
        phrase_toks: set[str] = set()
        for ph in phrases:
            if ph in seg:
                phrase_toks |= set(ph.split("_"))
        for i, tok in enumerate(toks):
            if tok in TEMPLATE_WORDS and not (tok in REGULATION_TOPICS and tok == topic):
                probs.append(f"{seg_name} contains template/regulation name {tok!r} (§2.2-1)")
            if tok in UNIT_TOKENS and not (i > 0 and toks[i - 1] == "per"):
                probs.append(f"{seg_name} contains unit {tok!r} (§2.2-2)")
            if YEAR_RX.match(tok):
                probs.append(f"{seg_name} contains year/period {tok!r} (§2.2-3)")
            alpha = tok.rstrip("0123456789")
            if not alpha or tok in UNIT_TOKENS or tok in TEMPLATE_WORDS or tok in phrase_toks:
                continue
            looks_abbrev = (len(alpha) <= 4 and alpha not in short_words) or not (set(alpha) & VOWELS)
            if looks_abbrev and alpha not in abbreviations:
                probs.append(f"{seg_name} token {tok!r} looks like an unregistered abbreviation (§2.2-5, refs/abbreviations.yaml)")
        if TENANT_RX.search(seg):
            probs.append(f"{seg_name} contains boundary/tenant information (§2.2-4)")
    if metric.split("_")[0] in NEGATION_TOKENS:
        probs.append("metric uses a negative form (§2.2-6)")
    return probs


def dc03(c: _Ctx) -> None:
    abbr, short, phrases = _abbrev(c)
    quals = _qualifiers(c)
    for f in c.tree.datapoints:
        if not isinstance(f.data, dict) or not isinstance(f.data.get("id"), str):
            c.r.add("DC-03", "error", f.rel, "missing id")
            continue
        for p in id_naming_problems(f.data["id"], abbreviations=abbr, short_words=short, phrases=phrases, qualifiers=quals):
            c.r.add("DC-03", "error", f.rel, p)


# --------------------------------------------------------------------------- DC-04
def _entries(c: _Ctx) -> Iterable[tuple[str, str, MappingEntry]]:
    for tm in c.d.template_mappings.values():
        for e in tm.entries:
            yield tm.id, c.tm_path(tm.id), e


def dc04(c: _Ctx) -> None:
    for tm_id, path, e in _entries(c):
        where = f"{path}#{e.entry_id}"
        for dp_id in e.datapoint_ids():
            dp = c.d.datapoints.get(dp_id)
            if dp is None:
                c.r.add("DC-04", "error", where, f"datapoint {dp_id} does not exist")
            elif dp.status == "retired":
                c.r.add("DC-04", "error", where, f"datapoint {dp_id} is retired")
            elif dp.status == "deprecated":
                c.r.add("DC-04", "warn", where, f"datapoint {dp_id} is deprecated; replaced_by "
                        f"{dp.deprecation.replaced_by if dp.deprecation else []}")
        if e.condition:
            try:
                for ref in check_expr(e.condition):
                    if ref not in c.d.datapoints:
                        c.r.add("DC-04", "error", where, f"condition references unknown datapoint {ref}")
            except ExprError as ex:
                c.r.add("DC-04", "error", where, f"condition not in CEL subset: {ex}")
        dp = c.d.datapoints.get(e.datapoint_id or "")
        if dp is not None:
            if e.boundary_hint and e.boundary_hint not in dp.boundary:
                c.r.add("DC-04", "error", where, f"boundary_hint {e.boundary_hint} not allowed by {dp.id} {dp.boundary}")
            if e.period_hint and e.period_hint != dp.period_type:
                c.r.add("DC-04", "warn", where, f"period_hint {e.period_hint} differs from {dp.id} period_type {dp.period_type} (§3.4 alignment)")


# --------------------------------------------------------------------------- DC-05
def dc05(c: _Ctx) -> None:
    U = c.d.units
    for dp in c.d.datapoints.values():
        p = c.dp_path(dp.id)
        if dp.unit is not None:
            if dp.unit not in U:
                c.r.add("DC-05", "error", p, f"unit {dp.unit!r} not in units/units.yaml")
                continue
            if U.units[dp.unit].calc_internal:
                c.r.add("DC-05", "error", p, f"unit {dp.unit!r} is a calc-internal unit, not a DataPoint unit (§3.1)")
            for au in dp.allowed_units or []:
                if au not in U:
                    c.r.add("DC-05", "error", p, f"allowed unit {au!r} not in units table")
                elif not U.same_dimension(au, dp.unit):
                    c.r.add("DC-05", "error", p, f"allowed unit {au} ({U.dimension(au)}) not same dimension as {dp.unit} ({U.dimension(dp.unit)})")
                elif not U.has_path(au, dp.unit):
                    c.r.add("DC-05", "error", p, f"no conversion path {au} → {dp.unit} in units/conversions.yaml")
        for rf in dp.record_fields or []:
            if rf.unit and rf.unit not in U:
                c.r.add("DC-05", "error", p, f"record_fields.{rf.name}.unit {rf.unit!r} not in units table")
    for tm_id, path, e in _entries(c):
        where = f"{path}#{e.entry_id}"
        dp = c.d.datapoints.get(e.datapoint_id or "")
        target_units: list[str] = []
        if dp is not None and dp.unit:
            target_units = [dp.unit]
        else:
            target_units = [c.d.datapoints[x].unit for x in e.datapoint_ids()
                            if x in c.d.datapoints and c.d.datapoints[x].unit]  # type: ignore[misc]
        if e.unit_hint:
            if e.unit_hint not in U:
                c.r.add("DC-05", "error", where, f"unit_hint {e.unit_hint!r} not in units table")
            else:
                for tu in target_units:
                    if tu in U and not U.has_path(e.unit_hint, tu):
                        c.r.add("DC-05", "error", where, f"unit_hint {e.unit_hint} not convertible to {tu}")
        for step in _iter_steps(e.transform):
            if step.type != "unit_convert":
                continue
            fu, tu = step.from_unit, step.to_unit
            bad = [u for u in (fu, tu) if u not in U]
            if bad:
                c.r.add("DC-05", "error", where, f"unit_convert unknown unit(s) {bad}")
                continue
            if not U.same_dimension(fu, tu):  # type: ignore[arg-type]
                c.r.add("DC-05", "error", where, f"unit_convert across dimensions {fu} → {tu} is a calculation (§3.2)")
                continue
            if not U.has_path(fu, tu, allow_export_only=True):  # type: ignore[arg-type]
                c.r.add("DC-05", "error", where, f"unit_convert {fu} → {tu} has no path in conversions.yaml")
            for dpu in target_units:
                if dpu in U and not U.same_dimension(fu, dpu):  # type: ignore[arg-type]
                    c.r.add("DC-05", "error", where, f"unit_convert {fu} → {tu} not same dimension as datapoint unit {dpu}")


# --------------------------------------------------------------------------- DC-06
def dc06(c: _Ctx) -> None:
    def codes(ref: str, where: str) -> set[str] | None:
        if ref not in c.d.enums:
            c.r.add("DC-06", "error", where, f"enum_ref {ref} not found")
            return None
        return enum_codes(c.d.enums[ref])

    for dp in c.d.datapoints.values():
        p = c.dp_path(dp.id)
        if dp.enum_ref:
            codes(dp.enum_ref, p)
        for rf in dp.record_fields or []:
            if rf.value_type == "enum" and not rf.enum_ref:
                c.r.add("DC-06", "error", p, f"record_fields.{rf.name} is enum without enum_ref")
            if rf.enum_ref:
                codes(rf.enum_ref, p)
    for tm_id, path, e in _entries(c):
        where = f"{path}#{e.entry_id}"
        dp = c.d.datapoints.get(e.datapoint_id or "")
        for step in _iter_steps(e.transform):
            if step.type != "enum_map" or dp is None:
                continue
            targets = list((step.map or {}).values()) + ([step.default] if step.default is not None else [])
            if dp.value_type == "enum" and dp.enum_ref:
                allowed = enum_codes(c.d.enums.get(dp.enum_ref, {}))
                for t in targets:
                    if str(t) not in allowed:
                        c.r.add("DC-06", "error", where, f"enum_map target {t!r} not in {dp.enum_ref}")
            elif dp.value_type == "bool":
                for t in targets:
                    if not isinstance(t, bool):
                        c.r.add("DC-06", "error", where, f"enum_map target {t!r} for bool datapoint must be true/false")
            # number/text targets (e.g. SCIP concentration ranges) have no enum to check against


# --------------------------------------------------------------------------- DC-07
def dc07(c: _Ctx) -> None:
    path = c.tree.aliases.rel if c.tree.aliases else "aliases/aliases.yaml"
    raw = c.tree.aliases.data.get("aliases", []) if c.tree.aliases else []
    seen: set[str] = set()
    for a in raw:
        al = a.get("alias")
        if al in seen:
            c.r.add("DC-07", "error", path, f"alias {al} listed twice")
        seen.add(al)
    for al in c.d.aliases.values():
        if al.target in c.d.aliases:
            c.r.add("DC-07", "error", path, f"alias {al.alias} points to another alias {al.target} (no chains)")
        if al.alias in c.d.datapoints:
            c.r.add("DC-07", "error", path, f"alias {al.alias} collides with a current DataPoint id")
        if al.target not in c.d.datapoints:
            c.r.add("DC-07", "error", path, f"alias {al.alias} target {al.target} does not exist")
        elif c.d.datapoints[al.target].status == "retired":
            c.r.add("DC-07", "warn", path, f"alias {al.alias} target {al.target} is retired")
        if al.alias == al.target:
            c.r.add("DC-07", "error", path, f"alias {al.alias} points to itself")
    for dp in c.d.datapoints.values():
        for al in dp.aliases or []:
            if al in c.d.datapoints:
                c.r.add("DC-07", "error", c.dp_path(dp.id), f"alias {al} collides with a current DataPoint id")
            t = c.d.aliases.get(al)
            if t is not None and t.target != dp.id:
                c.r.add("DC-07", "error", c.dp_path(dp.id), f"alias {al} also declared in aliases.yaml → {t.target}")
            for other in c.d.datapoints.values():
                if other.id != dp.id and al in (other.aliases or []):
                    c.r.add("DC-07", "error", c.dp_path(dp.id), f"alias {al} declared on both {dp.id} and {other.id}")


# --------------------------------------------------------------------------- DC-08
def dc08(c: _Ctx) -> None:
    for f in c.tree.datapoints:
        d = f.data if isinstance(f.data, dict) else {}
        label = d.get("label") or {}
        for lang in ("de", "en", "zh"):
            v = label.get(lang) if isinstance(label, dict) else None
            if not isinstance(v, str) or not v.strip():
                c.r.add("DC-08", "error", f.rel, f"label.{lang} missing or empty")
        defin = d.get("definition") or {}
        en = defin.get("en") if isinstance(defin, dict) else None
        if not isinstance(en, str) or not en.strip():
            c.r.add("DC-08", "error", f.rel, "definition.en missing or empty")
        for lang in ("de", "zh"):
            if isinstance(defin, dict) and not (isinstance(defin.get(lang), str) and defin[lang].strip()):
                c.r.add("DC-08", "info", f.rel, f"definition.{lang} not yet provided")


# --------------------------------------------------------------------------- DC-09
def dc09(c: _Ctx) -> None:
    if c.baseline is None:
        return
    base, _ = build_dictionary(c.baseline)
    if base is None:
        return
    for old in base.datapoints.values():
        new = c.d.datapoints.get(old.id)
        p = c.dp_path(old.id)
        if new is None:
            c.r.add("DC-09", "error", old.id, "published DataPoint removed; deprecate/retire instead (§2.4, §6.4)")
            continue
        change = classify_change(old, new)
        cmp = compare_semver(old.version, new.version)
        if change.level == "redefine":
            c.r.add("DC-09", "error", p, f"semantic change ({', '.join(change.reasons)}) requires a new id (§2.4)")
            continue
        if cmp < 0:
            c.r.add("DC-09", "error", p, f"version decreased {old.version} → {new.version}")
            continue
        if change.level == "none":
            if new.version != old.version:
                c.r.add("DC-09", "warn", p, f"version bumped {old.version} → {new.version} without content change")
            continue
        bumped = bump_level(old.version, new.version)
        order = {"none": 0, "patch": 1, "minor": 2, "major": 3}
        if order[bumped] < order[change.level]:
            c.r.add("DC-09", "error", p, f"{change.level.upper()} change ({', '.join(change.reasons)}) but version "
                    f"{old.version} → {new.version} is {bumped}")
        elif order[bumped] > order[change.level]:
            c.r.add("DC-09", "warn", p, f"version bump {bumped} larger than required {change.level}")
    for f in c.baseline.mappings:
        if isinstance(f.data, dict) and f.data.get("status") == "frozen":
            cur = c.tm_files.get(f.data.get("template_mapping"))
            if cur is None:
                c.r.add("DC-09", "error", f.rel, "frozen mapping file removed (§6.3: old template files stay read-only)")
            elif cur.data != f.data:
                c.r.add("DC-09", "error", cur.rel, "frozen mapping file modified (§6.3)")


def bump_level(old: str, new: str) -> str:
    o, n = [int(x) for x in old.split(".")], [int(x) for x in new.split(".")]
    if n[0] > o[0]:
        return "major"
    if n[1] > o[1] and n[0] == o[0]:
        return "minor"
    if n[2] > o[2] and n[:2] == o[:2]:
        return "patch"
    return "none"


# --------------------------------------------------------------------------- DC-10
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def _is_cjk(ch: str) -> bool:
    return "CJK" in unicodedata.name(ch, "")


def text_tokens(text: str) -> list[str]:
    """Lower-cased word tokens; CJK characters are one token each."""
    t = _PUNCT.sub(" ", unicodedata.normalize("NFKC", text).lower())
    out: list[str] = []
    for w in t.split():
        buf = ""
        for ch in w:
            if _is_cjk(ch):
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(ch)
            else:
                buf += ch
        if buf:
            out.append(buf)
    return out


def ngram_hashes(text: str, n: int) -> set[str]:
    toks = text_tokens(text)
    return {hashlib.sha256(" ".join(toks[i:i + n]).encode()).hexdigest()[:16] for i in range(len(toks) - n + 1)}


DC10_N_LABEL, DC10_N_DEF, DC10_THRESHOLD = 4, 5, 0.6


def build_corpus(texts: Iterable[str]) -> dict[int, set[str]]:
    corpus: dict[int, set[str]] = {DC10_N_LABEL: set(), DC10_N_DEF: set()}
    for t in texts:
        for n in corpus:
            corpus[n] |= ngram_hashes(t, n)
    return corpus


def load_corpus(root: Path, extra: Path | None = None) -> dict[int, set[str]]:
    """Fingerprints from ``refs/text_fingerprints.yaml`` (hashes only, no source text) plus optional .txt files."""
    from .yamlio import load_yaml
    corpus: dict[int, set[str]] = {DC10_N_LABEL: set(), DC10_N_DEF: set()}
    fp = root / "refs" / "text_fingerprints.yaml"
    if fp.exists():
        data = load_yaml(fp) or {}
        for n, hs in (data.get("fingerprints") or {}).items():
            corpus.setdefault(int(n), set()).update(hs or [])
    if extra is not None:
        files = [extra] if extra.is_file() else sorted(extra.rglob("*.txt"))
        more = build_corpus(f.read_text(encoding="utf-8") for f in files)
        for n, hs in more.items():
            corpus.setdefault(n, set()).update(hs)
    return corpus


def similarity(text: str, corpus: dict[int, set[str]], n: int) -> float:
    grams = ngram_hashes(text, n)
    if not grams or not corpus.get(n):
        return 0.0
    return len(grams & corpus[n]) / len(grams)


def dc10(c: _Ctx) -> None:
    corpus = c.corpus or {}
    if not any(corpus.values()):
        return
    for dp in c.d.datapoints.values():
        for lang in ("en", "de", "zh"):
            txt = getattr(dp.definition, lang)
            if txt:
                s = similarity(txt, corpus, DC10_N_DEF)
                if s >= DC10_THRESHOLD:
                    c.r.add("DC-10", "error", c.dp_path(dp.id), f"definition.{lang} matches template text corpus ({s:.0%} {DC10_N_DEF}-grams)")
    for tm_id, path, e in _entries(c):
        s = similarity(e.short_label, corpus, DC10_N_LABEL)
        if s >= DC10_THRESHOLD:
            c.r.add("DC-10", "error", f"{path}#{e.entry_id}", f"short_label matches template text corpus ({s:.0%} {DC10_N_LABEL}-grams)")


# --------------------------------------------------------------------------- DC-11
def dc11(c: _Ctx) -> None:
    D = c.d.datapoints
    for dp in D.values():
        p = c.dp_path(dp.id)
        for rel in dp.related_datapoints or []:
            if rel.id == dp.id:
                c.r.add("DC-11", "error", p, f"related_datapoints references itself ({rel.relation})")
            elif rel.id not in D:
                c.r.add("DC-11", "error", p, f"related datapoint {rel.id} does not exist")
        for comp in dp.related("sum_of"):
            o = D.get(comp)
            if o is None:
                continue
            if o.unit != dp.unit:
                c.r.add("DC-11", "error", p, f"sum_of component {comp} unit {o.unit} ≠ {dp.unit}")
            if dp.id not in o.related("component_of"):
                c.r.add("DC-11", "warn", c.dp_path(comp), f"missing reciprocal component_of → {dp.id}")
        if dp.deprecation:
            for rb in dp.deprecation.replaced_by:
                if rb not in D:
                    c.r.add("DC-11", "error", p, f"replaced_by {rb} does not exist")
                elif rb == dp.id:
                    c.r.add("DC-11", "error", p, "replaced_by references itself")
        for pl in (dp.validity_rule.plausibility if dp.validity_rule else []):
            try:
                for ref in check_expr(pl.expr):
                    if ref not in D:
                        c.r.add("DC-11", "error", p, f"plausibility expr references unknown datapoint {ref}")
            except ExprError as ex:
                c.r.add("DC-11", "error", p, f"plausibility expr not in CEL subset: {ex}")
        for m in dp.methodology_refs or []:
            if m.ref not in c.d.methodologies:
                c.r.add("DC-11", "error", p, f"methodology ref {m.ref!r} not registered in refs/methodologies.yaml")
    # qualifier slices should be linked from the parent total when the parent exists (§2.3)
    for dp in D.values():
        parent = dp.parent_id
        if parent and parent in D and dp.id not in D[parent].related("sum_of") and \
                dp.id.rsplit(".", 1)[-1] not in ("excluding_biogenic", "including_biogenic"):
            c.r.add("DC-11", "warn", c.dp_path(parent), f"parent total should list slice {dp.id} as sum_of (§2.3)")


# --------------------------------------------------------------------------- DC-12
def dc12(c: _Ctx) -> None:
    templates: dict[str, Any] = {}
    all_tids = sorted(set(TEMPLATE_IDS) | {tm.template_id for tm in c.d.template_mappings.values()})
    for tid in all_tids:
        files = c.d.mappings_for(tid)
        mapped = c.d.datapoints_for_template(tid)
        tagged = {d.id for d in c.d.by_tag(tid)}
        entries = [e for m in files for e in m.entries]
        cov = round(100 * len(tagged & mapped) / len(tagged), 1) if tagged else None
        templates[tid] = {"files": len(files), "versions": sorted(m.template_version for m in files), "entries": len(entries),
                          "datapoints_mapped": len(mapped), "datapoints_tagged": len(tagged), "coverage_pct": cov,
                          "unmapped_tagged": sorted(tagged - mapped), "entries_to_verify": sum(1 for e in entries if e.locator.verify)}
        c.r.add("DC-12", "info", f"mappings/{tid}", f"{len(entries)} entries, {len(mapped)} datapoints, coverage of tagged datapoints {cov}%")
    ext = None
    if c.contrib:
        n = len(c.contrib)
        ext = round(100 * sum(1 for x in c.contrib if x.get("external")) / n, 1) if n else None
    c.r.coverage = {"templates": templates, "external_contribution_share_pct": ext}


CHECKS = [("DC-01", dc01), ("DC-02", dc02), ("DC-03", dc03), ("DC-04", dc04), ("DC-05", dc05), ("DC-06", dc06),
          ("DC-07", dc07), ("DC-08", dc08), ("DC-09", dc09), ("DC-10", dc10), ("DC-11", dc11), ("DC-12", dc12)]


def validate_tree(root: str | Path | None = None, *, baseline: str | Path | None = None,
                  corpus: str | Path | dict[int, set[str]] | None = None, only: Iterable[str] | None = None) -> Report:
    """Run DC-01…DC-12 on a dictionary root (default: shipped package data)."""
    from .dictionary import default_root
    from .yamlio import load_yaml
    rootp = Path(root) if root is not None else default_root()
    tree = RawTree.read(rootp)
    d, problems = build_dictionary(tree)
    rep = Report(root=str(rootp))
    for pr in problems:
        rel, _, msg = pr.partition(": ")
        if not any(rel == le[0] for le in tree.load_errors):
            rep.add("DC-01", "error", rel, f"model: {msg}")
    base = RawTree.read(baseline) if baseline is not None else None
    corp = corpus if isinstance(corpus, dict) else load_corpus(rootp, Path(corpus) if corpus else None)
    cpath = rootp / "contributions.yaml"
    contrib = (load_yaml(cpath) or {}).get("contributions") if cpath.exists() else None
    assert d is not None
    ctx = _Ctx(tree, d, rep, base, corp, contrib)
    wanted = set(only) if only else None
    for rule, fn in CHECKS:
        if wanted is None or rule in wanted:
            fn(ctx)
    rep.counts = {"datapoints": len(d.datapoints), "template_mappings": len(d.template_mappings),
                  "errors": len(rep.errors), "warnings": len(rep.warnings)}
    return rep
