# Spec: 02 §3.3 (期间类型), §3.4 (期间对齐规则：财年 vs 日历年), _baseline F (风险等级)
"""Deterministic period checks and the 5-step alignment decision of 02 §3.4.

This module only *decides* which step applies. It never produces numbers: step 2 and step 4
are executed by ``calc-service`` (``method=time_proration`` for step 4), step 5 is a
``ClosedQuestion`` or an upstream request.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, Sequence

from .models import PeriodType

RiskLevel = Literal["L0", "L1", "L2", "L3"]


@dataclass(frozen=True, order=True)
class Period:
    """Closed date interval ``[start, end]`` (``Answer.period``)."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"period end {self.end} before start {self.start}")

    @classmethod
    def parse(cls, obj: "Period | dict | tuple") -> "Period":
        if isinstance(obj, Period):
            return obj
        if isinstance(obj, dict):
            s, e = obj["start"], obj["end"]
        else:
            s, e = obj
        return cls(_d(s), _d(e))

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def overlap(self, other: "Period") -> "Period | None":
        s, e = max(self.start, other.start), min(self.end, other.end)
        return Period(s, e) if s <= e else None

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


def _d(x: date | str) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def months_covered(p: Period) -> int:
    """Number of whole months contained in ``p`` counted from its start (a 12-month FY → 12)."""
    n = 0
    while add_months(p.start, n + 1) - timedelta(days=1) <= p.end:
        n += 1
    return n


def is_calendar_year(p: Period) -> bool:
    return p.start == date(p.start.year, 1, 1) and p.end == date(p.start.year, 12, 31)


def is_twelve_months(p: Period) -> bool:
    return add_months(p.start, 12) - timedelta(days=1) == p.end


def validate_period(period_type: PeriodType, period: Period | dict, *,
                    fiscal_year_start: tuple[int, int] | None = None) -> list[str]:
    """Return problems of ``period`` for a DataPoint of ``period_type`` (02 §3.3); empty list = valid.

    ``fiscal_year_start`` is the organization's FY start as ``(month, day)`` (``Organization`` setting).
    """
    p = Period.parse(period)
    problems: list[str] = []
    if period_type == "calendar_year" and not is_calendar_year(p):
        problems.append("calendar_year requires 01-01..12-31 of one year")
    elif period_type == "fiscal_year":
        if not is_twelve_months(p):
            problems.append("fiscal_year must span exactly 12 months")
        if fiscal_year_start and (p.start.month, p.start.day) != tuple(fiscal_year_start):
            problems.append(f"fiscal_year must start on organization FY start {fiscal_year_start}")
    elif period_type == "reporting_period" and not is_twelve_months(p):
        problems.append("reporting_period must be a 12-month interval")
    elif period_type == "point_in_time" and p.start != p.end:
        problems.append("point_in_time requires start == end")
    return problems


@dataclass(frozen=True)
class EvidencePeriod:
    """A period for which a value already exists (an Evidence / Answer), with a caller-chosen key."""

    period: Period
    ref: str = ""
    granularity: Literal["month", "quarter", "year", "other"] = "other"


@dataclass(frozen=True)
class AlignmentDecision:
    step: int  # 1..5 as numbered in 02 §3.4
    action: Literal["reuse", "recompose", "use_reporting_period", "time_proration", "closed_question"]
    is_estimate: bool
    min_risk_level: RiskLevel | None
    method: str | None = None
    used: tuple[str, ...] = field(default_factory=tuple)
    actual_period: Period | None = None
    note: str = ""


def _tiles(target: Period, parts: Sequence[EvidencePeriod]) -> list[EvidencePeriod] | None:
    """Pick sub-period evidence that covers ``target`` exactly, contiguous and non-overlapping."""
    inside = sorted((p for p in parts if p.granularity in ("month", "quarter")
                     and p.period.start >= target.start and p.period.end <= target.end),
                    key=lambda p: (p.period.start, -p.period.days))
    chosen: list[EvidencePeriod] = []
    cursor = target.start
    for ep in inside:
        if ep.period.start == cursor:
            chosen.append(ep)
            cursor = ep.period.end + timedelta(days=1)
            if cursor > target.end:
                return chosen
    return None


def align_period(target: Period | dict, available: Sequence[EvidencePeriod], *,
                 period_type: PeriodType = "reporting_period",
                 customer_accepts_reporting_period: bool = False,
                 proration_allowed_by_datapoint: bool = True,
                 proration_allowed_by_customer: bool = False,
                 is_emission: bool = False,
                 first_submission: bool = False) -> AlignmentDecision:
    """Apply 02 §3.4 steps 1–5 in order and return the first that applies."""
    t = Period.parse(target)
    if period_type == "point_in_time":
        raise ValueError("use align_point_in_time for point_in_time datapoints")
    # 1. exact match
    for ep in available:
        if ep.period == t:
            return AlignmentDecision(1, "reuse", False, None, used=(ep.ref,), actual_period=t)
    # 2. re-composable from monthly/quarterly evidence
    tiles = _tiles(t, available)
    if tiles:
        return AlignmentDecision(2, "recompose", False, None, method="sum_subperiods",
                                 used=tuple(e.ref for e in tiles), actual_period=t,
                                 note="calc-service sums sub-period evidence into a new Calculation")
    # 3. overlap >= 9 months and customer accepts reporting-period basis
    if customer_accepts_reporting_period:
        best = None
        for ep in available:
            ov = ep.period.overlap(t)
            if ov and months_covered(ov) >= 9 and (best is None or ov.days > best[1].days):
                best = (ep, ov)
        if best:
            ep = best[0]
            return AlignmentDecision(3, "use_reporting_period", False, "L2", used=(ep.ref,), actual_period=ep.period,
                                     note=f"按报告期 {ep.period.start.isoformat()} 至 {ep.period.end.isoformat()} 报告")
    # 4. time proration (calc-service, method=time_proration)
    if proration_allowed_by_datapoint and proration_allowed_by_customer:
        cands = [ep for ep in available if ep.period.overlap(t)]
        if cands:
            ep = max(cands, key=lambda e: e.period.overlap(t).days)  # type: ignore[union-attr]
            risk: RiskLevel = "L3" if (is_emission and first_submission) else "L2"
            return AlignmentDecision(4, "time_proration", True, risk, method="time_proration", used=(ep.ref,),
                                     actual_period=t)
    # 5. nothing applies
    return AlignmentDecision(5, "closed_question", False, None,
                             note="ClosedQuestion（是否接受以财年数据代替？）或转 upstream-chaser / contributor 补数据")


@dataclass(frozen=True)
class PointInTimeDecision:
    ref: str | None
    as_of: date | None
    expired: bool
    gap_days: int | None


def align_point_in_time(target: date | str, available: Sequence[tuple[date | str, str]],
                        expires_after_days: int | None = None) -> PointInTimeDecision:
    """Latest value not later than ``target``; expired if older than ``expires_after_days`` (02 §3.4, FR-404)."""
    t = _d(target)
    cands = [(_d(d), r) for d, r in available if _d(d) <= t]
    if not cands:
        return PointInTimeDecision(None, None, False, None)
    d, r = max(cands)
    gap = (t - d).days
    expired = expires_after_days is not None and gap > expires_after_days
    return PointInTimeDecision(r, d, expired, gap)
