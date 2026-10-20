# Spec: 02 §2.5 (别名解析), §4.2 (tm 引用), §4.4 (命中优先级), §6.1 (目录结构); CONTRACTS.md 跨包接口
"""Loading the dictionary tree and querying it."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from .models import Alias, DataPoint, MappingEntry, TemplateMapping, Unit
from .units import UnitRegistry
from .yamlio import load_yaml

log = logging.getLogger("belegt_dictionary")


class DictionaryError(ValueError):
    """Raised when the dictionary tree cannot be loaded into valid objects."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems[:10]) + (f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""))


def default_root() -> Path:
    """Directory of the dictionary data shipped as package data."""
    return Path(str(resources.files("belegt_dictionary") / "data"))


# --------------------------------------------------------------------------- raw tree
@dataclass
class RawFile:
    path: Path
    rel: str
    data: Any
    text: str


@dataclass
class RawTree:
    """Parsed-but-unvalidated YAML files of a dictionary root (used by the validator)."""

    root: Path
    datapoints: list[RawFile] = field(default_factory=list)
    mappings: list[RawFile] = field(default_factory=list)
    migrations: list[RawFile] = field(default_factory=list)
    enums: dict[str, RawFile] = field(default_factory=dict)
    units: RawFile | None = None
    conversions: RawFile | None = None
    aliases: RawFile | None = None
    methodologies: RawFile | None = None
    abbreviations: RawFile | None = None
    release: RawFile | None = None
    load_errors: list[tuple[str, str]] = field(default_factory=list)  # (rel, message)

    @classmethod
    def read(cls, root: str | Path) -> "RawTree":
        root = Path(root)
        t = cls(root=root)

        def rd(p: Path) -> RawFile | None:
            rel = p.relative_to(root).as_posix()
            try:
                text = p.read_text(encoding="utf-8")
                from .yamlio import load_yaml_text
                return RawFile(p, rel, load_yaml_text(text), text)
            except Exception as e:  # noqa: BLE001 - surfaced as DC-01
                t.load_errors.append((rel, f"YAML parse error: {e}"))
                return None

        for sub, target in (("datapoints", t.datapoints), ("mappings", t.mappings), ("migrations", t.migrations)):
            d = root / sub
            if d.is_dir():
                for p in sorted(d.rglob("*.yaml")):
                    f = rd(p)
                    if f is not None:
                        target.append(f)
        ed = root / "enums"
        if ed.is_dir():
            for p in sorted(ed.glob("*.yaml")):
                f = rd(p)
                if f is not None:
                    t.enums[f.rel] = f
        for attr, rel in (("units", "units/units.yaml"), ("conversions", "units/conversions.yaml"),
                          ("aliases", "aliases/aliases.yaml"), ("methodologies", "refs/methodologies.yaml"),
                          ("abbreviations", "refs/abbreviations.yaml"), ("release", "release.yaml")):
            p = root / rel
            if p.exists():
                setattr(t, attr, rd(p))
        return t


# --------------------------------------------------------------------------- dictionary
@dataclass(frozen=True)
class MappingMatch:
    template_mapping: str
    entry: MappingEntry

    @property
    def ref(self) -> str:
        """``tm:<template_id>@<version>#<entry_id>`` (02 §4.2)."""
        return f"{self.template_mapping}#{self.entry.entry_id}"

    @property
    def datapoint_ids(self) -> list[str]:
        return self.entry.datapoint_ids()


class Dictionary:
    """Validated, queryable dictionary (CONTRACTS.md: ``.datapoints``, ``.template_mappings``, ``.units``, ``.convert``)."""

    def __init__(self, *, root: Path, datapoints: dict[str, DataPoint], template_mappings: dict[str, TemplateMapping],
                 units: UnitRegistry, aliases: dict[str, Alias], enums: dict[str, dict], release: dict | None = None,
                 methodologies: dict[str, dict] | None = None):
        self.root = root
        self.datapoints = datapoints
        self.template_mappings = template_mappings
        self.units = units
        self.aliases = aliases
        self.enums = enums
        self.release = release or {}
        self.methodologies = methodologies or {}

    # ------------------------------------------------------------ units
    def convert(self, value: Decimal | str | int, from_unit: str, to_unit: str, *,
                round_digits: int | None = None, allow_export_only: bool = False) -> Decimal:
        return self.units.convert(value, from_unit, to_unit, round_digits=round_digits,
                                  allow_export_only=allow_export_only)

    def to_canonical(self, datapoint_id: str, value: Decimal | str | int, unit: str) -> Decimal:
        """Convert a collected value into the DataPoint's canonical ``unit`` (02 §1.1 ``allowed_units``)."""
        dp = self.get(datapoint_id)
        if dp.unit is None:
            raise ValueError(f"{dp.id} has no unit")
        allowed = set(dp.allowed_units or []) | {dp.unit}
        if unit not in allowed:
            raise ValueError(f"unit {unit!r} not in allowed_units of {dp.id}: {sorted(allowed)}")
        return self.convert(value, unit, dp.unit)

    # ------------------------------------------------------------ ids & aliases
    def resolve_id(self, ref: str) -> str:
        """Resolve an id or ``id@version`` through the alias table (single hop, 02 §2.5); unknown ids raise KeyError."""
        dp_id, _, _ver = ref.partition("@")
        if dp_id in self.datapoints:
            return dp_id
        a = self.aliases.get(dp_id)
        if a is not None:
            log.warning("alias %s (%s) resolved to %s", dp_id, a.kind, a.target)
            return a.target
        for dp in self.datapoints.values():  # aliases declared on the DataPoint itself
            if dp_id in (dp.aliases or []):
                log.warning("alias %s resolved to %s (datapoint.aliases)", dp_id, dp.id)
                return dp.id
        raise KeyError(ref)

    def get(self, ref: str, *, follow_aliases: bool = True) -> DataPoint:
        """DataPoint by id / ``id@version``; a pinned version must equal the loaded one."""
        dp_id, _, ver = ref.partition("@")
        real = self.resolve_id(dp_id) if follow_aliases else dp_id
        dp = self.datapoints[real]
        if ver and real == dp_id and ver != dp.version:
            raise KeyError(f"{dp_id}@{ver} not in this dictionary (loaded {dp.version})")
        if dp.status == "deprecated":
            log.warning("%s is deprecated; replaced_by=%s", dp.id, dp.deprecation.replaced_by if dp.deprecation else [])
        return dp

    def __contains__(self, ref: str) -> bool:
        try:
            self.resolve_id(ref)
            return True
        except KeyError:
            return False

    def by_tag(self, tag: str) -> list[DataPoint]:
        return [d for d in self.datapoints.values() if tag in (d.tags or [])]

    def enum_values(self, enum_ref: str) -> set[str]:
        e = self.enums.get(enum_ref)
        if e is None:
            raise KeyError(enum_ref)
        return enum_codes(e)

    # ------------------------------------------------------------ template mappings
    def mapping(self, tm_id: str) -> TemplateMapping:
        return self.template_mappings[tm_id]

    def mappings_for(self, template_id: str) -> list[TemplateMapping]:
        return [m for m in self.template_mappings.values() if m.template_id == template_id]

    def entry(self, ref: str) -> MappingMatch:
        """Resolve ``tm:<template_id>@<version>#<entry_id>``."""
        tm_id, sep, entry_id = ref.partition("#")
        if not sep:
            raise KeyError(f"not an entry reference: {ref}")
        e = self.template_mappings[tm_id].entry(entry_id)
        if e is None:
            raise KeyError(ref)
        return MappingMatch(tm_id, e)

    def find_by_template_field(self, template_id: str, locator_ref: str | None = None, *,
                               template_version: str | None = None, entry_id: str | None = None,
                               column: str | None = None, row: str | None = None,
                               direction: str | None = None, include_legacy: bool = True) -> list[MappingMatch]:
        """Exact template lookup for ``question-parser`` (``Mapping.method=exact_template``) and ``filler-file``.

        Matches on ``locator.ref`` (or ``legacy_ref`` when ``include_legacy``), optionally narrowed by column/row,
        entry id, template version and direction (``import`` matches import+both, ``export`` matches export+both).
        """
        out: list[MappingMatch] = []
        for m in self.mappings_for(template_id):
            if template_version is not None and m.template_version != template_version:
                continue
            for e in m.entries:
                if entry_id is not None and e.entry_id != entry_id:
                    continue
                if locator_ref is not None:
                    refs = {e.locator.ref} | ({e.locator.legacy_ref} if include_legacy and e.locator.legacy_ref else set())
                    if locator_ref not in refs:
                        continue
                if column is not None and e.locator.column != column and column not in (e.locator.columns or []):
                    continue
                if row is not None and e.locator.row != row:
                    continue
                if direction is not None and e.direction not in (direction, "both"):
                    continue
                out.append(MappingMatch(m.id, e))
        return out

    def datapoints_for_template(self, template_id: str, template_version: str | None = None) -> set[str]:
        ids: set[str] = set()
        for m in self.mappings_for(template_id):
            if template_version is None or m.template_version == template_version:
                for e in m.entries:
                    ids.update(e.datapoint_ids())
        return ids


def enum_codes(enum_doc: dict) -> set[str]:
    vals = enum_doc.get("values") or []
    return {str(v["code"]) if isinstance(v, dict) else str(v) for v in vals}


def _index_by(items: Iterable[Any], key: str) -> dict[str, Any]:
    return {getattr(i, key): i for i in items}


def build_dictionary(tree: RawTree) -> tuple[Dictionary | None, list[str]]:
    """Validate a RawTree into a Dictionary; returns (dictionary or None, problems)."""
    problems = [f"{rel}: {msg}" for rel, msg in tree.load_errors]
    dps: dict[str, DataPoint] = {}
    for f in tree.datapoints:
        try:
            dp = DataPoint.model_validate(f.data)
        except ValidationError as e:
            problems.append(f"{f.rel}: {e.errors()[0]['loc']} {e.errors()[0]['msg']}")
            continue
        if dp.id in dps:
            problems.append(f"{f.rel}: duplicate id {dp.id}")
        dps[dp.id] = dp
    tms: dict[str, TemplateMapping] = {}
    for f in tree.mappings:
        try:
            tm = TemplateMapping.model_validate(f.data)
        except ValidationError as e:
            problems.append(f"{f.rel}: {e.errors()[0]['loc']} {e.errors()[0]['msg']}")
            continue
        if tm.id in tms:
            problems.append(f"{f.rel}: duplicate template mapping {tm.id}")
        tms[tm.id] = tm
    units_doc = tree.units.data if tree.units else {"units": []}
    conv_doc = tree.conversions.data if tree.conversions else {"conversions": []}
    try:
        units = UnitRegistry([Unit.model_validate(u) for u in units_doc.get("units", [])], conv_doc.get("conversions", []))
    except Exception as e:  # noqa: BLE001
        problems.append(f"units: {e}")
        units = UnitRegistry([], [])
    aliases: dict[str, Alias] = {}
    for a in (tree.aliases.data.get("aliases", []) if tree.aliases else []):
        try:
            al = Alias.model_validate(a)
            aliases[al.alias] = al
        except ValidationError as e:
            problems.append(f"aliases: {a!r}: {e.errors()[0]['msg']}")
    enums = {rel: f.data for rel, f in tree.enums.items()}
    meth = {m["ref"]: m for m in (tree.methodologies.data.get("methodologies", []) if tree.methodologies else [])}
    d = Dictionary(root=tree.root, datapoints=dict(sorted(dps.items())), template_mappings=dict(sorted(tms.items())),
                   units=units, aliases=aliases, enums=enums,
                   release=tree.release.data if tree.release else None, methodologies=meth)
    return d, problems


def load_dictionary(path: str | Path | None = None, *, strict: bool = True) -> Dictionary:
    """Load the dictionary (default: the package data shipped with ``belegt-dictionary``).

    With ``strict=True`` any object failing its model raises :class:`DictionaryError`.
    Full CI checks (DC-01…DC-12) are in :func:`belegt_dictionary.validate.validate_tree`.
    """
    root = Path(path) if path is not None else default_root()
    if not root.is_dir():
        raise DictionaryError([f"dictionary root not found: {root}"])
    d, problems = build_dictionary(RawTree.read(root))
    if strict and problems:
        raise DictionaryError(problems)
    assert d is not None
    return d
