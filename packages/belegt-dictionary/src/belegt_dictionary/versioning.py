# Spec: 02 §2.4 (同 ID 内允许的 MAJOR 变更 / 改义须新 ID), §6.3 (DataPoint SemVer 规则)
"""Classify the difference between two versions of one DataPoint into the SemVer level it requires."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .models import DataPoint

ChangeLevel = Literal["none", "patch", "minor", "major", "redefine"]
_ORDER = {"none": 0, "patch": 1, "minor": 2, "major": 3, "redefine": 4}

# fields whose change means "改义" → new id required (§2.4)
REDEFINING = ("value_type", "unit", "cardinality", "period_type", "enum_ref")
# lifecycle fields: governed by §6.4, not by SemVer
LIFECYCLE = ("status", "deprecation", "version", "introduced_in")


@dataclass
class Change:
    level: ChangeLevel = "none"
    reasons: list[str] = field(default_factory=list)

    def bump(self, level: ChangeLevel, reason: str) -> None:
        self.reasons.append(f"{reason}:{level}")
        if _ORDER[level] > _ORDER[self.level]:
            self.level = level


def compare_semver(a: str, b: str) -> int:
    ta, tb = tuple(int(x) for x in a.split(".")), tuple(int(x) for x in b.split("."))
    return (tb > ta) - (tb < ta)


def _set(x) -> set:
    return set(x or [])


def _rule_relaxed(old: dict, new: dict) -> tuple[bool, bool]:
    """Return (relaxed, tightened) for a validity_rule change."""
    relaxed = tightened = False
    for k, looser in (("min", lambda o, n: n is None or (o is not None and n < o)),
                      ("max", lambda o, n: n is None or (o is not None and n > o))):
        o, n = old.get(k), new.get(k)
        if o == n:
            continue
        if looser(o, n):
            relaxed = True
        else:
            tightened = True
    for k in ("regex", "enum_ref"):
        o, n = old.get(k), new.get(k)
        if o != n:
            if n is None:
                relaxed = True
            else:
                tightened = True
    op = {p["expr"]: p.get("severity") for p in old.get("plausibility") or []}
    np_ = {p["expr"]: p.get("severity") for p in new.get("plausibility") or []}
    for e in set(op) | set(np_):
        if e not in np_:
            relaxed = True
        elif e not in op:
            # new warn-level check is advisory; a new block-level check narrows what can be filled
            tightened = tightened or np_[e] == "block"
            relaxed = relaxed or np_[e] != "block"
        elif op[e] != np_[e]:
            if np_[e] == "block":
                tightened = True
            else:
                relaxed = True
    for k in ("expires_by", "expires_after_days"):
        if old.get(k) != new.get(k):
            relaxed = True  # expiry metadata: treated as MINOR (doc silent)
    return relaxed, tightened


def classify_change(old: DataPoint, new: DataPoint) -> Change:
    """SemVer level required by the diff old → new (§6.3); ``redefine`` = must become a new id (§2.4)."""
    ch = Change()
    o, n = old.model_dump(exclude_none=True), new.model_dump(exclude_none=True)
    for f in REDEFINING:
        if o.get(f) != n.get(f):
            ch.bump("redefine", f)
    if [(r["name"], r["value_type"]) for r in o.get("record_fields", [])] != \
            [(r["name"], r["value_type"]) for r in n.get("record_fields", [])]:
        # dropping/adding optional record fields could be MINOR, but structure changes are treated as redefinition
        ch.bump("redefine", "record_fields")
    # PATCH: wording
    for f in ("label", "definition", "tags"):
        if o.get(f) != n.get(f):
            ch.bump("patch", f)
    # allowed_units: add → MINOR, remove → MAJOR
    ou, nu = _set(o.get("allowed_units")), _set(n.get("allowed_units"))
    if nu - ou:
        ch.bump("minor", "allowed_units+")
    if ou - nu:
        ch.bump("major", "allowed_units-")
    # boundary / consolidation: narrow → MAJOR, widen → MINOR
    for f in ("boundary", "consolidation"):
        ob, nb = _set(o.get(f)), _set(n.get(f))
        if ob - nb:
            ch.bump("major", f"{f}-")
        if nb - ob:
            ch.bump("minor", f"{f}+")
        elif o.get(f) != n.get(f) and ob == nb:
            ch.bump("patch", f"{f} order")
    for f in ("methodology_refs", "related_datapoints"):
        if o.get(f) != n.get(f):
            ch.bump("minor", f)
    if o.get("aliases") != n.get("aliases"):
        ch.bump("minor", "aliases")
    if o.get("validity_rule") != n.get("validity_rule"):
        relaxed, tightened = _rule_relaxed(o.get("validity_rule") or {}, n.get("validity_rule") or {})
        if tightened:
            ch.bump("major", "validity_rule tightened")
        elif relaxed:
            ch.bump("minor", "validity_rule relaxed")
    for f in ("sensitivity", "evidence_requirements"):
        if o.get(f) != n.get(f):
            ch.bump("minor", f)  # doc silent → MINOR (see README 取舍)
    return ch
