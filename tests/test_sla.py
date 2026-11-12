"""
test_sla.py
Decision SLA math (loan_agent/sla.py): Apex Financial's published commitment of a decision within 3 business days.
"""
from datetime import datetime, timedelta

import pytest

from loan_agent import sla


def ts(dt: datetime) -> str:
    return dt.strftime(sla.TIMESTAMP_FMT)


class TestAddBusinessDays:
    def test_monday_plus_three_is_thursday(self):
        monday = datetime(2026, 1, 5, 9, 0, 0)
        assert monday.weekday() == 0
        assert sla.add_business_days(monday, 3) == datetime(2026, 1, 8, 9, 0, 0)

    def test_friday_plus_three_skips_the_weekend(self):
        friday = datetime(2026, 1, 9, 9, 0, 0)
        assert friday.weekday() == 4
        assert sla.add_business_days(friday, 3) == datetime(2026, 1, 14, 9, 0, 0)  # Wed, not Wed-2-days-early

    def test_weekend_submission_also_skips_the_weekend(self):
        saturday = datetime(2026, 1, 10, 10, 0, 0)
        assert saturday.weekday() == 5
        # Sat -> Sun(skip) -> Mon(1) -> Tue(2) -> Wed(3)
        assert sla.add_business_days(saturday, 3) == datetime(2026, 1, 14, 10, 0, 0)

    def test_zero_business_days_is_a_no_op(self):
        now = datetime(2026, 1, 5, 9, 0, 0)
        assert sla.add_business_days(now, 0) == now


class TestEvaluate_Pending:
    MON = datetime(2026, 1, 5, 9, 0, 0)

    def test_on_track_right_after_submission(self):
        status = sla.evaluate(ts(self.MON), now=self.MON + timedelta(hours=1))
        assert status.state == "ON_TRACK" and status.business_days_remaining == 3
        assert status.due_at == ts(datetime(2026, 1, 8, 9, 0, 0))
        assert str(status.business_days) in status.message and status.due_at in status.message

    def test_due_soon_with_one_business_day_left(self):
        status = sla.evaluate(ts(self.MON), now=self.MON + timedelta(days=2, hours=1))  # Wednesday
        assert status.state == "DUE_SOON" and status.business_days_remaining == 1

    def test_overdue_past_the_due_date(self):
        status = sla.evaluate(ts(self.MON), now=self.MON + timedelta(days=3, hours=1))
        assert status.state == "OVERDUE"
        assert status.business_days_remaining is not None and status.business_days_remaining < 0
        assert "thank you for your patience" in status.message.lower()

    def test_exactly_at_the_due_instant_is_overdue_not_on_track(self):
        due = sla.add_business_days(self.MON, 3)
        status = sla.evaluate(ts(self.MON), now=due)
        assert status.state == "OVERDUE"

    def test_custom_sla_window(self):
        status = sla.evaluate(ts(self.MON), now=self.MON + timedelta(hours=1), business_days=1)
        assert status.due_at == ts(datetime(2026, 1, 6, 9, 0, 0)) and status.business_days == 1


class TestEvaluate_Decided:
    MON = datetime(2026, 1, 5, 9, 0, 0)

    def test_decided_within_sla(self):
        decided = self.MON + timedelta(days=1)
        status = sla.evaluate(ts(self.MON), decided_at=ts(decided))
        assert status.state == "DECIDED" and status.decided_within_sla is True
        assert status.due_at is None and status.business_days_remaining is None
        assert "within our 3-business-day" in status.message.lower()

    def test_decided_after_the_due_date(self):
        decided = sla.add_business_days(self.MON, 3) + timedelta(days=5)
        status = sla.evaluate(ts(self.MON), decided_at=ts(decided))
        assert status.state == "DECIDED" and status.decided_within_sla is False
        assert "longer than" in status.message.lower()

    def test_decided_exactly_at_the_due_instant_counts_as_within_sla(self):
        decided = sla.add_business_days(self.MON, 3)
        status = sla.evaluate(ts(self.MON), decided_at=ts(decided))
        assert status.decided_within_sla is True

    @pytest.mark.parametrize("now", [None, datetime(2099, 1, 1)])
    def test_now_is_irrelevant_once_decided(self, now):
        decided = self.MON + timedelta(days=1)
        status = sla.evaluate(ts(self.MON), decided_at=ts(decided), now=now)
        assert status.state == "DECIDED"


def test_is_business_day():
    assert sla.is_business_day(datetime(2026, 1, 5))  # Monday
    assert sla.is_business_day(datetime(2026, 1, 9))  # Friday
    assert not sla.is_business_day(datetime(2026, 1, 10))  # Saturday
    assert not sla.is_business_day(datetime(2026, 1, 11))  # Sunday
