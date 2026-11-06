"""Sustained versus bursty saturation, why hours are missing, and the signals that are not independent."""

import json
from collections.abc import Sequence

import pytest

from bicep_cost import rightsize_report
from bicep_cost.armexpr import JsonValue
from bicep_cost.prices import PriceClient
from bicep_cost.rightsize import (
    SUSTAINED_AVG,
    SUSTAINED_SHARE,
    Case,
    Signal,
    Stats,
    build_stats,
    decide,
)
from tests.test_rightsize import (
    APP_T,
    DAYS,
    HOURS,
    PLAN_T,
    SQL_T,
    Azure,
    Series,
    az_with,
    candidate,
    capp,
    case,
    flat,
    hourly,
    plan,
    run,
    sqldb,
)

Metric = dict[str, list[float | None]]
BATCH_HOURS = 4  # a daily batch window: 17% of the hours, below the sustained share


def saturated(
    maxima: Sequence[float | None], averages: Sequence[float | None], *, group: str = "cpu", expected: int = HOURS
) -> Stats:
    return build_stats("CpuPercentage", list(maxima), list(averages), expected, group=group, trigger=True)


def hot_hours(n: int, avg: float = 97.0, rest: float = 5.0) -> list[float | None]:
    """The first ``n`` hours of the window at ``avg``, the rest at ``rest``."""
    return [avg] * n + [rest] * (HOURS - n)


def peaks(n: int) -> list[float | None]:
    return [100.0] * n + [10.0] * (HOURS - n)


def under_case(stats: Stats, *, days: int = DAYS, signals: list[Signal] | None = None) -> Case:
    return case([stats], up=[candidate("P3v3", 0.5, 160.0)], days=days, signals=signals)


# ------------------------------------------------------------------------------------------------ sustained saturation


def test_sustained_saturation_is_under_without_a_second_signal() -> None:
    cpu = saturated(peaks(HOURS // 2), hot_hours(HOURS // 2))
    result = decide(under_case(cpu))
    assert result.verdict == "under"
    assert result.proposal is not None
    assert result.proposal.delta == pytest.approx(60.0)
    assert "sustained" in result.reason
    assert f"{SUSTAINED_AVG:.0f}%" in result.reason
    assert "360 of 720 h" in result.reason


def test_a_daily_batch_window_is_not_sustained() -> None:
    averages: list[float | None] = [97.0 if h % 24 < BATCH_HOURS else 5.0 for h in range(HOURS)]
    maxima: list[float | None] = [100.0 if h % 24 < BATCH_HOURS else 10.0 for h in range(HOURS)]
    result = decide(under_case(saturated(maxima, averages)))
    assert (result.verdict, result.proposal) == ("watch", None)
    assert "not sustained" in result.reason
    assert f"in {BATCH_HOURS * DAYS} of 720 h" in result.reason
    assert any("bursty load" in n for n in result.notes)


def test_minute_spikes_inside_the_hours_are_not_sustained() -> None:
    """The hourly maximum is the busiest minute: 100% in 12% of the hours while the average never passes 40%."""
    maxima = peaks(HOURS // 8)
    averages = [35.0] * (HOURS // 8) + [10.0] * (HOURS - HOURS // 8)
    result = decide(under_case(saturated(maxima, averages)))
    assert (result.verdict, result.proposal) == ("watch", None)
    assert "hourly average at or above 90% in 0 of 720 h" in result.reason
    assert "highest 35%" in result.reason


def test_a_single_spike_never_qualifies() -> None:
    result = decide(under_case(saturated(hourly(30, {5: 100}), hourly(20, {5: 99}))))
    assert result.verdict == "ok"  # not even hot


def test_the_sustained_share_is_a_hard_edge() -> None:
    edge = round(SUSTAINED_SHARE * HOURS)
    at = decide(under_case(saturated(peaks(edge), hot_hours(edge))))
    below = decide(under_case(saturated(peaks(edge - 1), hot_hours(edge - 1))))
    assert (at.verdict, below.verdict) == ("under", "watch")


def test_sustained_needs_every_hour_of_a_full_window() -> None:
    maxima, averages = peaks(HOURS // 2), hot_hours(HOURS // 2)
    # 1 of 720 h missing: still above the coverage minimum, but not "every hour"
    holes_max, holes_avg = [None, *maxima[1:]], [None, *averages[1:]]
    result = decide(under_case(saturated(holes_max, holes_avg)))
    assert result.verdict == "watch"
    assert "complete 30 d window of every hour" in result.reason
    short = decide(under_case(saturated(maxima, averages), days=14))
    assert short.verdict == "watch"
    assert "complete 30 d window of every hour" in short.reason


def test_without_hourly_averages_nothing_is_sustained() -> None:
    cpu = build_stats("CpuPercentage", peaks(HOURS // 2), None, HOURS, group="cpu", trigger=True)
    assert decide(under_case(cpu)).verdict == "watch"


def test_sustained_load_and_a_second_signal_are_both_in_the_reason() -> None:
    cpu = saturated(peaks(HOURS // 2), hot_hours(HOURS // 2))
    result = decide(under_case(cpu, signals=[Signal("queue", "HTTP queue of at least 10 requests in 40 h")]))
    assert result.verdict == "under"
    assert "HTTP queue" in result.reason
    assert "sustained" in result.reason


def test_sustained_memory_counts_too() -> None:
    mem = saturated(peaks(HOURS // 2), hot_hours(HOURS // 2), group="memory")
    assert decide(case([mem], up=[candidate("P3v3", 0.5, 160.0, "memory")])).verdict == "under"


# ------------------------------------------------------------------------------------------------ why hours are missing


def test_a_resource_younger_than_the_window_says_so() -> None:
    cpu = saturated([None] * 527 + [50.0] * 193, [None] * 527 + [20.0] * 193)
    result = decide(case([cpu]))
    assert result.verdict == "watch"
    assert "only 193 of 720 hourly points (27%)" in result.reason
    assert "no points in the first 527 h" in result.reason
    assert "younger" in result.reason


def test_a_resource_that_stopped_reporting_and_a_gap_in_the_middle_are_told_apart() -> None:
    stopped = saturated([50.0] * 400 + [None] * 320, [20.0] * 400 + [None] * 320)
    assert "no points in the last 320 h" in decide(case([stopped])).reason
    middle = saturated([50.0] * 100 + [None] * 300 + [50.0] * 320, [20.0] * 100 + [None] * 300 + [20.0] * 320)
    assert "300 h between points" in decide(case([middle])).reason


def test_missing_hours_are_located_from_a_real_azure_series(prices: PriceClient) -> None:
    """A plan created inside the window: Azure returns the whole hourly grid, the early records are empty."""
    az = az_with(plan("young", sku="P0v3"))
    series = {"maximum": first_hours_none(527, 60.0), "average": first_hours_none(527, 20.0)}
    az.give(
        PLAN_T,
        "young",
        {"CpuPercentage": series, "MemoryPercentage": series, "HttpQueueLength": flat(0)},
    )
    a = run(az, prices)["young"]
    assert a.verdict == "watch"
    assert "no points in the first 527 h" in a.reason


def first_hours_none(missing: int, value: float) -> list[float | None]:
    return [None] * missing + [value] * (HOURS - missing)


# ------------------------------------------------------------------------------------------------ Azure SQL DTU


def dtu_db(name: str) -> Azure:
    return az_with(sqldb(name, {"name": "Standard", "tier": "Standard", "capacity": 50}))


def dtu_series(dtu: Metric, **parts: Metric) -> Series:
    base: Series = {
        "cpu_percent": flat(5),
        "physical_data_read_percent": flat(5),
        "log_write_percent": flat(5),
        "workers_percent": flat(2),
        "storage_percent": flat(10),
    }
    return {"dtu_consumption_percent": dtu, **base, **parts}


def nightly(avg_in: float, hours_per_day: int) -> Metric:
    inside = [h % 24 < hours_per_day for h in range(HOURS)]
    return {
        "maximum": [100.0 if i else 8.0 for i in inside],
        "average": [avg_in if i else 3.0 for i in inside],
    }


def test_a_bursty_dtu_database_is_watch_not_under(prices: PriceClient) -> None:
    """A nightly window pins the DTUs (also its log IO) but the database idles around it: no larger size is proposed."""
    az = dtu_db("srv/bursty")
    az.give(SQL_T, "srv/bursty", dtu_series(nightly(92, 3), log_write_percent=nightly(92, 3)))
    a = run(az, prices)["srv/bursty"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert a.signals == []  # a saturated component is the DTU itself, not a second signal
    assert "not sustained" in a.reason
    assert any("log IO is at or above 95% in 90 h" in n for n in a.notes)
    assert any("scale up only around the busy hours" in n for n in a.notes)


def test_a_dtu_database_saturated_for_most_of_the_window_is_under(prices: PriceClient) -> None:
    az = dtu_db("srv/busy")
    az.give(SQL_T, "srv/busy", dtu_series(flat(100), log_write_percent=flat(100)))
    a = run(az, prices)["srv/busy"]
    assert a.verdict == "under"
    assert "sustained" in a.reason
    assert any("log IO" in n for n in a.notes)


def test_dtu_worker_threads_at_the_limit_are_an_independent_signal(prices: PriceClient) -> None:
    az = dtu_db("srv/workers")
    az.give(SQL_T, "srv/workers", dtu_series(nightly(92, 3), workers_percent=nightly(92, 3)))
    a = run(az, prices)["srv/workers"]
    assert a.verdict == "under"
    assert {s.name for s in a.signals} == {"workers"}


def test_a_saturated_io_limit_is_never_ok_nothing_to_do(prices: PriceClient) -> None:
    """vCore database: data IO is not sized on (its limit does not scale linearly), but it must not read as fine."""
    sku: dict[str, JsonValue] = {"name": "GP_Gen5", "tier": "GeneralPurpose", "family": "Gen5", "capacity": 4}
    az = az_with(sqldb("srv/io", sku, kind="v12.0,user,vcore"))
    az.give(
        SQL_T,
        "srv/io",
        {
            "cpu_percent": flat(50),
            "physical_data_read_percent": flat(100),
            "log_write_percent": flat(5),
            "workers_percent": flat(3),
            "storage_percent": flat(10),
        },
    )
    a = run(az, prices)["srv/io"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert "physical_data_read_percent" in a.reason
    assert "by hand" in a.reason


# ------------------------------------------------------------------------------------------------ Container Apps


def test_a_single_replica_app_is_not_pinned_at_its_maximum(prices: PriceClient) -> None:
    az = az_with(capp("solo", cpu=1, memory="2Gi", min_r=0, max_r=1))
    az.give(
        APP_T,
        "solo",
        {
            "Replicas": flat(1, 1),
            "CpuPercentage": flat(3),
            "MemoryPercentage": flat(10),
            "RestartCount": flat(0),
        },
    )
    a = run(az, prices)["solo"]
    assert a.signals == []
    assert a.verdict == "over"


def test_a_stopped_or_failed_app_says_why_it_has_no_replicas(prices: PriceClient) -> None:
    empty = {
        "Replicas": {"maximum": [None] * HOURS, "average": [None] * HOURS},
        "CpuPercentage": flat(0, n=0),
        "MemoryPercentage": flat(0, n=0),
        "RestartCount": flat(0),
    }
    stopped = capp("stopped", min_r=1, max_r=1, runningStatus="Stopped", workloadProfileName="Dedicated-D4")
    failed = capp("failed", min_r=0, max_r=1, runningStatus="Running", provisioningState="Failed")
    az = az_with(stopped, failed)
    az.give(APP_T, "stopped", empty)
    az.give(APP_T, "failed", empty)
    result = run(az, prices)
    assert result["stopped"].verdict == "no-data"
    assert "stopped (runningStatus Stopped)" in result["stopped"].reason
    assert result["failed"].verdict == "no-data"
    assert "provisioning failed" in result["failed"].reason


# ------------------------------------------------------------------------------------------------ report


def test_the_report_carries_the_sustained_thresholds_and_evidence(
    prices: PriceClient,
) -> None:
    assert rightsize_report.THRESHOLDS["sustained_avg"] == SUSTAINED_AVG
    assert rightsize_report.THRESHOLDS["sustained_share"] == SUSTAINED_SHARE
    az = dtu_db("srv/busy")
    az.give(SQL_T, "srv/busy", dtu_series(flat(100)))
    a = run(az, prices)["srv/busy"]
    metric = rightsize_report._stats_json(a.metrics[0])
    assert metric["hours_avg_at_least_sustained"] == HOURS
    assert metric["peak_hourly_average"] == 100.0
    assert json.dumps(metric)
