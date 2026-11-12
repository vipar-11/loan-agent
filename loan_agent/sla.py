"""
Decision SLA: Apex Financial's published commitment is a credit decision within 3 business days of submission.

Pure functions, deterministic and UI-agnostic (same timestamp format as `loan_agent.db`:
"%Y-%m-%d %H:%M:%S"), so the Applicant Portal, the "My Applications" status page, and any future channel
(partner results files, notifications) all compute the same due date and status from the same two facts:
when the application was submitted, and whether (and when) it was decided.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

DECISION_SLA_BUSINESS_DAYS = 3
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"

_SATURDAY = 5  # datetime.weekday()


def is_business_day(day: datetime) -> bool:
    return day.weekday() < _SATURDAY


def add_business_days(start: datetime, business_days: int) -> datetime:
    """`start` plus `business_days` business days (weekends skipped; federal holidays are not modeled)."""
    day = start
    remaining = business_days
    while remaining > 0:
        day += timedelta(days=1)
        if is_business_day(day):
            remaining -= 1
    return day


def _parse(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FMT)


def business_days_between(start: datetime, end: datetime) -> int:
    """Number of business days crossed walking forward from `start` to `end` (`end` must be >= `start`)."""
    count = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if is_business_day(cursor):
            count += 1
    return count


@dataclass(frozen=True)
class SlaStatus:
    business_days: int
    due_at: Optional[str]            # None once decided — the SLA no longer applies
    business_days_remaining: Optional[int]
    state: str                       # ON_TRACK | DUE_SOON | OVERDUE | DECIDED
    decided_within_sla: Optional[bool]
    message: str


def evaluate(
    submitted_at: str,
    decided_at: Optional[str] = None,
    now: Optional[datetime] = None,
    business_days: int = DECISION_SLA_BUSINESS_DAYS,
) -> SlaStatus:
    """
    The SLA clock for one application. `decided_at` (adjudicated_at) ends it; otherwise it is evaluated as of `now`.
    """
    submitted = _parse(submitted_at)
    due = add_business_days(submitted, business_days)
    due_str = due.strftime(TIMESTAMP_FMT)

    if decided_at:
        decided = _parse(decided_at)
        on_time = decided <= due
        return SlaStatus(
            business_days=business_days, due_at=None, business_days_remaining=None, state="DECIDED",
            decided_within_sla=on_time,
            message=(f"Decision delivered within our {business_days}-business-day service level."
                      if on_time else f"Decision delivered; it took longer than our usual {business_days}-business-day target."),
        )

    moment = now or datetime.now()
    if moment >= due:
        overdue_by = max(business_days_between(due, moment), 1)
        return SlaStatus(
            business_days=business_days, due_at=due_str, business_days_remaining=-overdue_by, state="OVERDUE",
            decided_within_sla=None,
            message="Still under review — this is taking longer than our usual target; thank you for your patience.",
        )

    remaining = business_days_between(moment, due)
    if remaining <= 1:
        return SlaStatus(
            business_days=business_days, due_at=due_str, business_days_remaining=remaining, state="DUE_SOON",
            decided_within_sla=None,
            message=f"A decision is expected by {due_str} (our {business_days}-business-day target).",
        )
    return SlaStatus(
        business_days=business_days, due_at=due_str, business_days_remaining=remaining, state="ON_TRACK",
        decided_within_sla=None,
        message=f"On track: a decision is expected by {due_str} (our {business_days}-business-day target).",
    )
