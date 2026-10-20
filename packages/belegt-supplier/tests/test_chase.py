# Spec: 08 §4.1–§4.5 chase-policy@1.0.0（供应商时区工作日、静默期、分级、升级、到期）
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from belegt_supplier.chase import (
    POLICY_ID, ChaseState, build_schedule, compute_due_date, next_action, reminder_cap,
)
from belegt_supplier.workcal import BusinessCalendar, QuietPeriod, easter, public_holidays

TR = BusinessCalendar("TR", "Europe/Istanbul")
CN = BusinessCalendar("CN", "Asia/Shanghai")
DE = BusinessCalendar("DE", "Europe/Berlin")
IT = BusinessCalendar("IT", "Europe/Rome")


def steps(s):
    return {x.key: x.on for x in s.steps}


# ---------------------------------------------------------------- calendar basics
def test_easter_and_moveable_holidays():
    assert easter(2026) == date(2026, 4, 5) and easter(2027) == date(2027, 3, 28)
    assert date(2026, 4, 3) in public_holidays("DE", 2026)   # Karfreitag
    assert date(2026, 4, 6) in public_holidays("DE", 2026)   # Ostermontag
    assert date(2026, 10, 29) in public_holidays("TR", 2026)  # Cumhuriyet Bayramı


def test_working_days_skip_weekend_and_holiday():
    # 2026-10-28 (Mi) + 1 Arbeitstag in TR: 10-29 Feiertag → 10-30 (Fr)
    assert TR.add_working_days(date(2026, 10, 28), 1) == date(2026, 10, 30)
    # Freitag + 1 → Montag
    assert DE.add_working_days(date(2026, 11, 6), 1) == date(2026, 11, 9)
    assert DE.add_working_days(date(2026, 11, 9), -1) == date(2026, 11, 6)


def test_quiet_period_shift_reminder_vs_first():
    assert CN.next_business_day(date(2026, 10, 2), "reminder") == date(2026, 10, 8)
    assert IT.next_business_day(date(2027, 8, 12), "reminder") == date(2027, 8, 25)
    assert IT.next_business_day(date(2027, 8, 12), "first") == date(2027, 8, 12)  # Ferragosto 只顺延催办
    assert DE.next_business_day(date(2026, 12, 28), "first") == date(2027, 1, 4)  # 12-23…01-02


def test_send_window_timezone():
    # 2026-11-02 06:30 UTC = 09:30 Istanbul（窗口内）; 13:30 UTC = 16:30（窗口外）
    assert TR.in_send_window(datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc))
    assert not TR.in_send_window(datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc))
    nxt = TR.next_send_window(datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc))
    assert nxt == datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
    # Shanghai：UTC 周五 23:00 = 本地周六 07:00 → 下周一 09:00 本地 = 01:00 UTC
    nxt = CN.next_send_window(datetime(2026, 11, 6, 23, 0, tzinfo=timezone.utc))
    assert nxt == datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
    # 早于 09:00 的工作日 → 当天 09:00
    assert DE.next_send_window(datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)


def test_tenant_custom_quiet_period():
    cal = BusinessCalendar("TR", "Europe/Istanbul", extra_quiet=[QuietPeriod("Inventur", date(2026, 11, 9), date(2026, 11, 11))])
    assert cal.next_business_day(date(2026, 11, 9)) == date(2026, 11, 12)
    s = build_schedule(date(2026, 11, 2), date(2026, 11, 23), cal)
    assert steps(s)["r1"] == date(2026, 11, 12)


# ---------------------------------------------------------------- default schedule
def test_standard_window_schedule_tr():
    s = build_schedule(date(2026, 11, 2), date(2026, 11, 23), TR)
    assert s.policy == POLICY_ID and not s.long_window and s.cap == 3
    assert steps(s) == {"r1": date(2026, 11, 9), "r2": date(2026, 11, 16), "escalate": date(2026, 11, 20),
                        "expire": date(2026, 11, 23)}
    esc = s.step("escalate")
    assert esc.variant == "r3" and esc.external and esc.target_status == "escalated" and "procurement" in esc.notify


def test_schedule_skips_holiday():
    # 2026-10-22 (Do) + 5 Arbeitstage TR: 23,26,27,28,(29 Feiertag),30 → r1 = 10-30
    s = build_schedule(date(2026, 10, 22), date(2026, 11, 12), TR)
    assert steps(s)["r1"] == date(2026, 10, 30)


def test_quiet_period_pushes_reminders_and_keeps_gaps_cn():
    s = build_schedule(date(2026, 9, 24), date(2026, 10, 22), CN)
    st = steps(s)
    assert st["r1"] == date(2026, 10, 8)            # 国庆后第一个工作日
    assert st["r2"] > st["r1"] and st["escalate"] > st["r2"] and st["escalate"] < st["expire"]


def test_long_window_schedule():
    s = build_schedule(date(2027, 1, 4), date(2027, 3, 31), DE)
    st = steps(s)
    assert s.long_window
    assert st["r1"] == DE.add_working_days(date(2027, 1, 4), 7)
    assert st["r2"] == DE.add_working_days(date(2027, 1, 4), 21)
    assert st["escalate"] == DE.add_working_days(date(2027, 3, 31), -7)  # due−7（Karfreitag/Ostermontag 跳过）
    assert any(k.startswith("check@") for k in st)


@pytest.mark.parametrize("tier,keys,cap", [
    ("A", {"r1", "r2", "escalate", "expire"}, 5),
    ("B", {"r1", "r2", "escalate", "expire"}, 3),
    ("C", {"r1", "recommend_close", "expire"}, 3),
    ("good_responder", {"r1", "escalate", "expire"}, 3),
    ("complained", {"internal@r1", "internal@r2", "expire"}, 3),
])
def test_tiers(tier, keys, cap):
    s = build_schedule(date(2026, 11, 2), date(2026, 11, 23), TR, tier)
    assert set(steps(s)) == keys and s.cap == cap
    if tier == "A":
        assert "executive" in s.step("escalate").notify and "procurement" in s.step("r2").notify
    if tier == "C":
        assert s.step("r1").on == TR.add_working_days(date(2026, 11, 2), 10)
    if tier == "complained":
        assert not any(x.external for x in s.steps)


def test_reminder_cap_tenant_override():
    assert reminder_cap("B") == 3 and reminder_cap("A") == 5 and reminder_cap("B", 5) == 5 and reminder_cap("B", 9) == 5


# ---------------------------------------------------------------- due date
def test_due_default_21_days_and_capped_by_request_due():
    assert compute_due_date(date(2026, 11, 2), TR) == date(2026, 11, 23)
    # Request.due_date 2026-11-30 − 10 Arbeitstage = 2026-11-16
    assert compute_due_date(date(2026, 11, 2), TR, request_due_date=date(2026, 11, 30)) == date(2026, 11, 16)


def test_due_extended_by_quiet_overlap():
    # 2026-09-24 + 21 = 10-15；国庆静默中的工作日 10-05/06/07 → 顺延 3 个工作日
    assert compute_due_date(date(2026, 9, 24), CN) == date(2026, 10, 20)


def test_spring_festival_due_not_before_10_working_days_after():
    assert compute_due_date(date(2027, 1, 25), CN) == CN.add_working_days(date(2027, 2, 15), 10)


# ---------------------------------------------------------------- next_action
def at(d: date, hour_utc: int = 8) -> datetime:
    return datetime(d.year, d.month, d.day, hour_utc, tzinfo=timezone.utc)


SCHED = build_schedule(date(2026, 11, 2), date(2026, 11, 23), TR)


def test_next_action_none_before_first_reminder():
    assert next_action(SCHED, ChaseState("sent", 0), at(date(2026, 11, 6)), TR) is None


def test_next_action_r1_and_continue_variant():
    a = next_action(SCHED, ChaseState("sent", 0), at(date(2026, 11, 9)), TR)
    assert a.step.key == "r1" and a.variant == "r1" and a.external and a.target_status == "reminded"
    a = next_action(SCHED, ChaseState("sent", 0, link_opened=True), at(date(2026, 11, 9)), TR)
    assert a.variant == "r1_continue"


def test_next_action_r2_then_escalate():
    st = ChaseState("reminded", 1, frozenset({"r1"}))
    a = next_action(SCHED, st, at(date(2026, 11, 16)), TR)
    assert a.step.key == "r2" and a.variant == "r2" and "owner" in a.notify
    st = ChaseState("reminded", 2, frozenset({"r1", "r2"}))
    a = next_action(SCHED, st, at(date(2026, 11, 20)), TR)
    assert a.step.key == "escalate" and a.variant == "r3" and a.target_status == "escalated"


def test_next_action_cap_reached_escalates_without_sending():
    sched = build_schedule(date(2026, 11, 2), date(2026, 11, 23), TR, tenant_cap=1)
    a = next_action(sched, ChaseState("reminded", 1, frozenset({"r1"})), at(date(2026, 11, 16)), TR)
    assert not a.external and a.target_status == "escalated" and "UPC-LIM-01" in a.reason


def test_next_action_expire_and_escalated_stays():
    a = next_action(SCHED, ChaseState("reminded", 2, frozenset({"r1", "r2"})), at(date(2026, 11, 23)), TR)
    assert a.step.key == "expire" and a.target_status == "expired"
    assert next_action(SCHED, ChaseState("escalated", 3, frozenset({"r1", "r2", "escalate"})), at(date(2026, 11, 24)), TR) is None


def test_next_action_uses_supplier_local_date():
    # 2026-11-08 22:30 UTC = 2026-11-09 01:30 Istanbul → r1 已到期
    a = next_action(SCHED, ChaseState("sent", 0), datetime(2026, 11, 8, 22, 30, tzinfo=timezone.utc), TR)
    assert a is not None and a.step.key == "r1"
    # 同一时刻在 UTC-5 的供应商仍是 11-08
    us = BusinessCalendar("US", "America/New_York")
    s = build_schedule(date(2026, 11, 2), date(2026, 11, 23), us)
    assert next_action(s, ChaseState("sent", 0), datetime(2026, 11, 8, 22, 30, tzinfo=timezone.utc), us) is None


def test_next_action_paused_and_stopped():
    a = next_action(SCHED, ChaseState("sent", 0, paused_until=date(2026, 11, 20)), at(date(2026, 11, 9)), TR)
    assert not a.external and "paused" in a.reason
    a = next_action(SCHED, ChaseState("sent", 0, reminders_stopped=True), at(date(2026, 11, 9)), TR)
    assert not a.external


def test_next_action_noop_when_responded_or_submitted():
    assert next_action(SCHED, ChaseState("responded", 1), at(date(2026, 11, 16)), TR) is None
    assert next_action(SCHED, ChaseState("sent", 0, submitted=True), at(date(2026, 11, 9)), TR) is None


def test_long_window_continue_check_once():
    s = build_schedule(date(2027, 1, 4), date(2027, 3, 31), DE)
    checks = [x for x in s.steps if x.key.startswith("check@")]
    st = ChaseState("reminded", 1, frozenset({"r1"}), link_opened=True)
    a = next_action(s, st, at(checks[0].on), DE)
    assert a.step.key == checks[0].key and a.variant == "r1_continue"
    st2 = ChaseState("reminded", 2, frozenset({"r1", checks[0].key}), link_opened=True)
    a2 = next_action(s, st2, at(checks[1].on), DE)
    assert a2 is None or not a2.step.key.startswith("check@")
    # 未打开链接不发"继续填写"
    assert next_action(s, ChaseState("reminded", 1, frozenset({"r1"})), at(checks[0].on), DE) is None
