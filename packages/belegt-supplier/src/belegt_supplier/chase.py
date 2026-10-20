# Spec: 08 §4.1（默认时间表 chase-policy@1.0.0）、§4.2（分级调整）、§4.4（静默期顺延、due 顺延）、§4.5（升级）
"""催办策略 `chase-policy@1.0.0`：纯函数。

- 所有 T+n 为**供应商时区工作日**（`BusinessCalendar.add_working_days`），落在静默期内顺延（§4.4）。
- `build_schedule()` 给出整套日程；`next_action()` 在给定时刻判断该做什么（由调用方工作流定时调用）。
- 不做 I/O；发送由调用方经 `EgressGateway`，状态转移由 `statemachine` 执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from .workcal import SEND_WINDOW_START, BusinessCalendar

POLICY_ID = "chase-policy@1.0.0"
LONG_WINDOW_DAYS = 42
DEFAULT_WINDOW_DAYS = 21
DUE_BUFFER_WORKING_DAYS = 10
DEFAULT_CAP = 3  # 1+3（UPC-LIM-01）
TIER_A_CAP = 5   # 1+5
PAUSE_DAYS_I_WILL_CONTACT = 14

Tier = Literal["A", "B", "C", "good_responder", "complained"]
StepKind = Literal["remind", "escalate", "expire", "internal_notify", "recommend_close", "continue_check"]


@dataclass(frozen=True)
class ChaseStep:
    key: str                     # 稳定键，如 "r1"、"escalate"、"expire"、"check@T+14"
    kind: StepKind
    on: date                     # 供应商本地日期
    variant: str | None = None   # 外发模板变体 r1/r2/r3/r1_continue
    external: bool = False       # 是否对外发信
    notify: tuple[str, ...] = ()  # 内部通知对象：owner/procurement/executive
    target_status: str | None = None

    def not_before_utc(self, cal: BusinessCalendar) -> datetime:
        return cal.at_local(self.on, SEND_WINDOW_START)


@dataclass(frozen=True)
class ChaseSchedule:
    policy: str
    tier: Tier
    sent_on: date
    due_date: date
    long_window: bool
    cap: int
    steps: tuple[ChaseStep, ...]

    def step(self, key: str) -> ChaseStep | None:
        return next((s for s in self.steps if s.key == key), None)


def reminder_cap(tier: Tier, tenant_cap: int | None = None) -> int:
    """UPC-LIM-01：默认 1+3；A 级允许 1+5；租户可调（不超过 5）。"""
    if tenant_cap is not None:
        return max(0, min(5, tenant_cap))
    return TIER_A_CAP if tier == "A" else DEFAULT_CAP


def compute_due_date(sent_on: date, cal: BusinessCalendar, request_due_date: date | None = None,
                     window_days: int = DEFAULT_WINDOW_DAYS) -> date:
    """默认 due = 发送日 + 21 天，但不晚于 Request.due_date 前 10 个工作日；
    与静默期重叠时顺延同等工作日数；春节前 lead_days 内发出的请求，due 不早于节后 N 个工作日（§4.4）。"""
    due = sent_on + timedelta(days=window_days)
    if request_due_date is not None:
        cap = cal.add_working_days(request_due_date, -DUE_BUFFER_WORKING_DAYS)
        due = min(due, cap)
    overlap = cal.working_days_between(sent_on, due, only_quiet=True, kind="first")
    if overlap:
        due = cal.add_working_days(due, overlap)
    for q in cal.quiet_list(sent_on.year) + cal.quiet_list(sent_on.year + 1):
        if q.due_min_business_days_after and q.start - timedelta(days=q.lead_days) <= sent_on <= q.end:
            due = max(due, cal.add_working_days(q.end, q.due_min_business_days_after))
    return due


def _shift(cal: BusinessCalendar, d: date) -> date:
    return cal.next_business_day(d, "reminder")


def build_schedule(sent_on: date, due_date: date, cal: BusinessCalendar, tier: Tier = "B",
                   tenant_cap: int | None = None) -> ChaseSchedule:
    """生成 chase-policy@1.0.0 日程（不含 T+0 首次发送本身）。"""
    long_window = (due_date - sent_on).days >= LONG_WINDOW_DAYS
    cap = reminder_cap(tier, tenant_cap)
    T = lambda n: _shift(cal, cal.add_working_days(sent_on, n))  # noqa: E731
    due_minus = lambda n: _shift(cal, cal.add_working_days(due_date, -n))  # noqa: E731
    last_before_due = cal.add_working_days(due_date, -1)
    steps: list[ChaseStep] = []

    if long_window:
        r1, r2, esc = T(7), T(21), due_minus(7)
    else:
        r1, r2 = T(5), T(10)
        esc = T(14)
        if esc >= due_date:  # 窗口过短：升级点不晚于 due 前一个工作日
            esc = last_before_due
    # 顺延后保持节奏间隔（标准窗口 r1→r2 ≥ 5、r2→升级 ≥ 4 个工作日；长窗口 ≥ 14/≥ 5），避免两封合并到同一天
    gap1, gap2 = (14, 5) if long_window else (5, 4)
    r2 = max(r2, _shift(cal, cal.add_working_days(r1, gap1)))
    esc = max(esc, _shift(cal, cal.add_working_days(r2, gap2)))
    if esc >= due_date:
        esc = max(r2, last_before_due)
    esc_notify = ("owner", "procurement") + (("executive",) if tier == "A" else ())

    if tier in ("A", "B"):
        steps.append(ChaseStep("r1", "remind", r1, "r1", True, (), "reminded"))
        steps.append(ChaseStep("r2", "remind", r2, "r2", True,
                               ("owner", "procurement") if tier == "A" else ("owner",), "reminded"))
        steps.append(ChaseStep("escalate", "escalate", esc, "r3", True, esc_notify, "escalated"))
        if long_window:
            d = sent_on + timedelta(days=14)
            while d < esc:
                cd = _shift(cal, d)
                if r1 < cd < esc and cd != r2:
                    steps.append(ChaseStep(f"check@{(d - sent_on).days}", "continue_check", cd, "r1_continue", True, (), "reminded"))
                d += timedelta(days=14)
    elif tier == "C":
        steps.append(ChaseStep("r1", "remind", T(10), "r1", True, (), "reminded"))
        steps.append(ChaseStep("recommend_close", "recommend_close", min(T(21), last_before_due), None, False, ("owner",), None))
    elif tier == "good_responder":
        steps.append(ChaseStep("r1", "remind", r1, "r1", True, (), "reminded"))
        steps.append(ChaseStep("escalate", "escalate", esc, None, False, esc_notify, "escalated"))
    elif tier == "complained":
        steps.append(ChaseStep("internal@r1", "internal_notify", r1, None, False, ("procurement",), None))
        steps.append(ChaseStep("internal@r2", "internal_notify", r2, None, False, ("procurement",), None))
    steps.append(ChaseStep("expire", "expire", due_date, None, False, ("owner",), "expired"))
    steps.sort(key=lambda s: (s.on, s.kind == "expire"))
    return ChaseSchedule(POLICY_ID, tier, sent_on, due_date, long_window, cap, tuple(steps))


@dataclass(frozen=True)
class ChaseState:
    """next_action 的输入：SupplierRequest 的相关切片 + 已执行步骤。"""
    status: str
    reminder_count: int
    done_steps: frozenset[str] = frozenset()
    link_opened: bool = False
    submitted: bool = False
    paused_until: date | None = None   # 采购"我来联系"（14 天）
    reminders_stopped: bool = False    # UPC-LIM-07 暂停提醒 / 硬退信


@dataclass(frozen=True)
class ChaseAction:
    step: ChaseStep
    variant: str | None
    external: bool
    target_status: str | None
    reason: str = ""
    notify: tuple[str, ...] = field(default=())


def next_action(schedule: ChaseSchedule, state: ChaseState, now_utc: datetime, cal: BusinessCalendar) -> ChaseAction | None:
    """返回此刻应执行的一个动作；无事可做返回 None。"""
    if state.status not in ("sent", "reminded", "escalated") or state.submitted:
        return None
    today = cal.local_date(now_utc)
    if today >= schedule.due_date and "expire" not in state.done_steps:
        exp = schedule.step("expire")
        assert exp is not None
        if state.status == "escalated":
            return None  # 到期时保持 escalated（§4.1 表）
        return ChaseAction(exp, None, False, "expired", "due_date reached", exp.notify)
    for s in schedule.steps:
        if s.kind == "expire" or s.key in state.done_steps or s.on > today:
            continue
        if state.status == "escalated":
            return None
        stopped = state.reminders_stopped or (state.paused_until is not None and today < state.paused_until)
        if s.kind in ("remind", "continue_check"):
            if s.kind == "continue_check" and not state.link_opened:
                continue
            if s.kind == "continue_check" and any(k.startswith("check@") for k in state.done_steps):
                continue  # 打开未提交只发一次"继续填写"
            if stopped:
                return ChaseAction(s, None, False, None, "reminders paused → internal notice", ("procurement",))
            if state.reminder_count >= schedule.cap:
                esc = schedule.step("escalate") or s
                return ChaseAction(esc, None, False, "escalated" if state.status == "reminded" else None,
                                   "UPC-LIM-01 cap reached", ("owner", "procurement"))
            variant = s.variant
            if s.key == "r1" and state.link_opened:
                variant = "r1_continue"  # §4.1：已打开未提交改用"继续填写"
            return ChaseAction(s, variant, True, "reminded", "", s.notify)
        if s.kind == "escalate":
            external = s.external and not stopped and state.reminder_count < schedule.cap
            target = "escalated" if (state.status == "reminded" or external) else None
            return ChaseAction(s, s.variant if external else None, external, target, "", s.notify)
        return ChaseAction(s, None, False, s.target_status, "", s.notify)
    return None
