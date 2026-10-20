# Spec: 02 §3.3 期间类型、§3.4 期间对齐规则（财年 vs 日历年）
from datetime import date

import pytest

from belegt_dictionary import EvidencePeriod, Period, align_period, align_point_in_time, validate_period
from belegt_dictionary.periods import months_covered

CY2026 = Period(date(2026, 1, 1), date(2026, 12, 31))
FY = Period(date(2025, 10, 1), date(2026, 9, 30))


def months(year: int, m_from: int, m_to: int, ref="m"):
    import calendar
    return [EvidencePeriod(Period(date(year, m, 1), date(year, m, calendar.monthrange(year, m)[1])), f"{ref}{m:02d}", "month")
            for m in range(m_from, m_to + 1)]


def test_step1_exact_match():
    d = align_period(CY2026, [EvidencePeriod(CY2026, "ans_1", "year")])
    assert (d.step, d.action, d.is_estimate) == (1, "reuse", False)


def test_step2_recompose_from_months():
    d = align_period(CY2026, months(2026, 1, 12) + [EvidencePeriod(FY, "fy", "year")])
    assert (d.step, d.action, d.method, d.is_estimate) == (2, "recompose", "sum_subperiods", False)
    assert len(d.used) == 12


def test_step2_quarters():
    q = [EvidencePeriod(Period(date(2026, 1, 1), date(2026, 3, 31)), "q1", "quarter"),
         EvidencePeriod(Period(date(2026, 4, 1), date(2026, 6, 30)), "q2", "quarter"),
         EvidencePeriod(Period(date(2026, 7, 1), date(2026, 9, 30)), "q3", "quarter"),
         EvidencePeriod(Period(date(2026, 10, 1), date(2026, 12, 31)), "q4", "quarter")]
    assert align_period(CY2026, q).step == 2


def test_step2_gap_falls_through():
    ev = months(2026, 1, 5) + months(2026, 7, 12)
    assert align_period(CY2026, ev).step == 5


def test_step3_fiscal_year_overlap_accepted():
    d = align_period(CY2026, [EvidencePeriod(FY, "fy2026", "year")], customer_accepts_reporting_period=True)
    assert (d.step, d.action, d.min_risk_level, d.is_estimate) == (3, "use_reporting_period", "L2", False)
    assert d.actual_period == FY and "2025-10-01" in d.note


def test_step3_requires_nine_months():
    short_overlap = Period(date(2025, 6, 1), date(2026, 5, 31))  # 5 months overlap with CY2026
    d = align_period(CY2026, [EvidencePeriod(short_overlap, "x", "year")], customer_accepts_reporting_period=True)
    assert d.step == 5


def test_step4_time_proration():
    short_overlap = Period(date(2025, 6, 1), date(2026, 5, 31))
    d = align_period(CY2026, [EvidencePeriod(short_overlap, "x", "year")], proration_allowed_by_customer=True)
    assert (d.step, d.method, d.is_estimate, d.min_risk_level) == (4, "time_proration", True, "L2")
    d3 = align_period(CY2026, [EvidencePeriod(short_overlap, "x", "year")], proration_allowed_by_customer=True,
                      is_emission=True, first_submission=True)
    assert d3.min_risk_level == "L3"


def test_step4_blocked_by_datapoint_rule():
    d = align_period(CY2026, [EvidencePeriod(FY, "fy", "year")], proration_allowed_by_customer=True,
                     proration_allowed_by_datapoint=False)
    assert d.step == 5 and d.action == "closed_question"


def test_step5_nothing():
    assert align_period(CY2026, []).action == "closed_question"


def test_point_in_time_alignment():
    av = [(date(2025, 12, 31), "a"), (date(2026, 6, 30), "b"), (date(2027, 1, 31), "c")]
    d = align_point_in_time(date(2026, 12, 31), av, expires_after_days=400)
    assert (d.ref, d.expired) == ("b", False)
    d2 = align_point_in_time("2028-01-01", av[:2], expires_after_days=400)
    assert d2.expired is True
    assert align_point_in_time("2020-01-01", av).ref is None
    with pytest.raises(ValueError):
        align_period(CY2026, [], period_type="point_in_time")


@pytest.mark.parametrize("ptype,period,ok", [
    ("calendar_year", CY2026, True),
    ("calendar_year", FY, False),
    ("fiscal_year", FY, True),
    ("fiscal_year", Period(date(2025, 10, 1), date(2026, 6, 30)), False),
    ("reporting_period", FY, True),
    ("point_in_time", Period(date(2026, 12, 31), date(2026, 12, 31)), True),
    ("point_in_time", CY2026, False),
    ("validity_range", Period(date(2026, 3, 1), date(2029, 2, 28)), True),
])
def test_validate_period(ptype, period, ok):
    assert (validate_period(ptype, period) == []) is ok


def test_fiscal_year_start_setting():
    assert validate_period("fiscal_year", FY, fiscal_year_start=(10, 1)) == []
    assert validate_period("fiscal_year", FY, fiscal_year_start=(1, 1))


def test_period_parse_and_months():
    p = Period.parse({"start": "2025-10-01", "end": "2026-09-30"})
    assert p == FY and months_covered(p) == 12 and p.days == 365
    with pytest.raises(ValueError):
        Period(date(2026, 2, 1), date(2026, 1, 1))
