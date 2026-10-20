import pytest

from bicep_cost.estimators.cron import active_hours_per_month


def test_daily_window():
    # 04:00-23:00 every day = 19 h/day
    assert active_hours_per_month("0 4 * * *", "0 23 * * *") == pytest.approx(19 * 730 / 24)


def test_weekdays_only():
    assert active_hours_per_month("0 7 * * 1-5", "0 19 * * 1-5") == pytest.approx(12 * 5 * 730 / 168)


def test_window_crossing_midnight():
    assert active_hours_per_month("0 22 * * *", "0 6 * * *") == pytest.approx(8 * 730 / 24)


def test_unsupported_day_of_month():
    with pytest.raises(ValueError, match="not supported"):
        active_hours_per_month("0 7 1 * *", "0 8 1 * *")


def test_overlapping_rules_take_the_highest_target_not_the_sum():
    from bicep_cost.estimators.cron import extra_replica_hours_per_month

    day = ("0 8 * * *", "0 20 * * *")  # 12 h
    overlap = ("0 12 * * *", "0 16 * * *")  # 4 h, inside the first window
    hours = extra_replica_hours_per_month([(*day, 2), (*overlap, 2)], baseline=0)
    assert hours == pytest.approx(2 * 12 * 730 / 24)  # not 2*12 + 2*4
    hours = extra_replica_hours_per_month([(*day, 1), (*overlap, 3)], baseline=1)
    assert hours == pytest.approx(2 * 4 * 730 / 24)  # only the overlap exceeds the baseline
