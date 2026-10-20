# Spec: 08 §4.1（T+n 为供应商时区工作日；09:00–16:00 发送窗口）、§4.4（静默期与节假日）
"""供应商工作日历（纯函数，无 I/O 除包内数据）。

节假日来源：若安装了开源 `holidays` 库则使用之；否则用内置的固定日期 + 复活节推算的最小表。
商务静默期：包内 `data/quiet_periods.yaml` + 租户自定义（`extra_quiet`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from importlib import resources
from typing import Literal
from zoneinfo import ZoneInfo

import yaml

Kind = Literal["first", "reminder"]
SEND_WINDOW_START = time(9, 0)
SEND_WINDOW_END = time(16, 0)
EU_MEMBERS = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "GR", "HU", "IE", "LV", "LT", "LU", "MT",
              "NL", "PT", "RO", "SK", "SI", "ES", "SE"}


def easter(year: int) -> date:
    """格里高利历复活节（Anonymous Gregorian algorithm）。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


_FIXED = {
    "DE": ["01-01", "05-01", "10-03", "12-25", "12-26"],
    "IT": ["01-01", "01-06", "04-25", "05-01", "06-02", "08-15", "11-01", "12-08", "12-25", "12-26"],
    "PL": ["01-01", "01-06", "05-01", "05-03", "08-15", "11-01", "11-11", "12-25", "12-26"],
    "TR": ["01-01", "04-23", "05-01", "05-19", "07-15", "08-30", "10-29"],
    "CN": ["01-01", "05-01", "10-01", "10-02", "10-03"],
    "IN": ["01-26", "08-15", "10-02"],
    "VN": ["01-01", "04-30", "05-01", "09-02"],
}
_EASTER_OFFSETS = {"DE": [-2, 1, 39, 50], "IT": [1], "PL": [1, 60]}


@lru_cache(maxsize=256)
def public_holidays(country: str, year: int) -> frozenset[date]:
    country = (country or "").upper()
    try:  # pragma: no cover - 可选依赖
        import holidays as _h  # type: ignore

        return frozenset(_h.country_holidays(country, years=year).keys())
    except Exception:  # noqa: BLE001
        pass
    days = {date(year, int(md[:2]), int(md[3:])) for md in _FIXED.get(country, [])}
    e = easter(year)
    days |= {e + timedelta(days=o) for o in _EASTER_OFFSETS.get(country, [])}
    return frozenset(days)


@dataclass(frozen=True)
class QuietPeriod:
    name: str
    start: date
    end: date
    applies_to: Literal["all", "reminders_only"] = "all"
    due_min_business_days_after: int = 0
    lead_days: int = 0

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def applies(self, kind: Kind) -> bool:
        return self.applies_to == "all" or kind == "reminder"


@lru_cache(maxsize=1)
def _quiet_table() -> dict:
    return yaml.safe_load(resources.files("belegt_supplier").joinpath("data/quiet_periods.yaml").read_text("utf-8"))["countries"]


def quiet_periods(country: str, years: range) -> list[QuietPeriod]:
    country = (country or "").upper()
    entries = list(_quiet_table().get(country, []))
    if country in EU_MEMBERS:
        entries += _quiet_table().get("EU", [])
    out: list[QuietPeriod] = []
    for e in entries:
        common = dict(name=e["name"], applies_to=e.get("applies_to", "all"),
                      due_min_business_days_after=e.get("due_min_business_days_after", 0), lead_days=e.get("lead_days", 0))
        if "annual" in e:
            sm, sd = map(int, e["annual"]["start"].split("-"))
            em, ed = map(int, e["annual"]["end"].split("-"))
            for y in years:
                s = date(y, sm, sd)
                end = date(y + (1 if (em, ed) < (sm, sd) else 0), em, ed)
                out.append(QuietPeriod(start=s, end=end, **common))
        else:
            out.append(QuietPeriod(start=e["start"], end=e["end"], **common))
    return out


@dataclass
class BusinessCalendar:
    """某供应商的工作日历。`tz` 为 IANA 时区；`country` 为 ISO 3166-1 alpha-2。"""
    country: str = ""
    tz: str = "UTC"
    extra_holidays: frozenset[date] = frozenset()
    extra_quiet: list[QuietPeriod] = field(default_factory=list)
    use_builtin_quiet: bool = True

    def __post_init__(self) -> None:
        self._zone = ZoneInfo(self.tz)
        self._quiet_cache: dict[int, list[QuietPeriod]] = {}

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    # ---- days
    def is_holiday(self, d: date) -> bool:
        return d in self.extra_holidays or d in public_holidays(self.country, d.year)

    def is_working_day(self, d: date) -> bool:
        """周一至周五且非公共假日（不考虑商务静默期）。"""
        return d.weekday() < 5 and not self.is_holiday(d)

    def quiet_list(self, year: int) -> list[QuietPeriod]:
        if year not in self._quiet_cache:
            base = quiet_periods(self.country, range(year - 1, year + 2)) if self.use_builtin_quiet else []
            self._quiet_cache[year] = base + list(self.extra_quiet)
        return self._quiet_cache[year]

    def quiet_period_at(self, d: date, kind: Kind = "reminder") -> QuietPeriod | None:
        for q in self.quiet_list(d.year):
            if q.contains(d) and q.applies(kind):
                return q
        return None

    def is_business_day(self, d: date, kind: Kind = "reminder") -> bool:
        """可发送日：工作日且不在（适用的）静默期内。"""
        return self.is_working_day(d) and self.quiet_period_at(d, kind) is None

    def add_working_days(self, start: date, n: int) -> date:
        """T+n：从 start 起数 n 个工作日（n 可为负）。"""
        d, step, left = start, (1 if n >= 0 else -1), abs(n)
        while left:
            d += timedelta(days=step)
            if self.is_working_day(d):
                left -= 1
        return d

    def next_business_day(self, d: date, kind: Kind = "reminder", *, inclusive: bool = True) -> date:
        """d 当天（inclusive）或之后的第一个可发送日；落在静默期则顺延到静默期结束后的第一个工作日（§4.4）。"""
        if not inclusive:
            d += timedelta(days=1)
        for _ in range(800):
            q = self.quiet_period_at(d, kind)
            if q is not None:
                d = q.end + timedelta(days=1)
                continue
            if self.is_working_day(d):
                return d
            d += timedelta(days=1)
        raise RuntimeError("no business day found within 800 days")

    def working_days_between(self, start: date, end: date, *, only_quiet: bool = False, kind: Kind = "first") -> int:
        """(start, end] 中的工作日数；only_quiet=True 时只数落在静默期内的工作日。"""
        n, d = 0, start
        while d < end:
            d += timedelta(days=1)
            if self.is_working_day(d) and (not only_quiet or self.quiet_period_at(d, kind) is not None):
                n += 1
        return n

    # ---- times
    def local(self, dt_utc: datetime) -> datetime:
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(self._zone)

    def local_date(self, dt_utc: datetime) -> date:
        return self.local(dt_utc).date()

    def at_local(self, d: date, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=self._zone).astimezone(timezone.utc)

    def in_send_window(self, dt_utc: datetime, kind: Kind = "reminder") -> bool:
        loc = self.local(dt_utc)
        return self.is_business_day(loc.date(), kind) and SEND_WINDOW_START <= loc.time() < SEND_WINDOW_END

    def next_send_window(self, dt_utc: datetime, kind: Kind = "reminder") -> datetime:
        """UPC-LIM-04：返回 ≥ dt_utc 的最早可发送时刻（UTC）。"""
        if self.in_send_window(dt_utc, kind):
            return dt_utc if dt_utc.tzinfo else dt_utc.replace(tzinfo=timezone.utc)
        loc = self.local(dt_utc)
        d = loc.date()
        if self.is_business_day(d, kind) and loc.time() < SEND_WINDOW_START:
            return self.at_local(d, SEND_WINDOW_START)
        return self.at_local(self.next_business_day(d, kind, inclusive=False), SEND_WINDOW_START)
