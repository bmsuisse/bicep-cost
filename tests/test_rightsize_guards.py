"""Regression tests for the ways a proposal could be made on thin, stale or non-transferable evidence."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from bicep_cost import rightsize_report
from bicep_cost.armexpr import JsonValue
from bicep_cost.collect.arm import ArmClient, AzureError, parse_metrics
from bicep_cost.context import ResourceIndex
from bicep_cost.prices import PriceClient
from bicep_cost.rightsize_run import analyse
from bicep_cost.rightsize_types import Env, list_price
from tests import conftest
from tests.conftest import fake_fetcher, make_resource
from tests.test_rightsize import (
    APP_T,
    DAYS,
    HOURS,
    NOW,
    PG_T,
    PLAN_T,
    SQL_T,
    SUB,
    Azure,
    Series,
    az_with,
    capp,
    first_hours,
    flat,
    hourly,
    plan,
    postgres,
    row,
    run,
)


@pytest.fixture
def prices() -> PriceClient:
    return PriceClient(cache_dir=None, fetcher=fake_fetcher)


def sparse(value: float, hours: int, at: int = 0) -> dict[str, list[float | None]]:
    """``value`` in ``hours`` hours starting at ``at``, no data anywhere else."""
    values: list[float | None] = [None] * HOURS
    for i in range(at, at + hours):
        values[i] = value
    return {"maximum": values, "average": list(values)}


def vcore_db(
    name: str, vcores: int = 4, max_gb: int = 250, props: dict[str, JsonValue] | None = None
) -> dict[str, JsonValue]:
    sku: dict[str, JsonValue] = {"name": "GP_Gen5", "tier": "GeneralPurpose", "family": "Gen5", "capacity": vcores}
    return row(
        "Microsoft.Sql/servers/databases",
        name,
        kind="v12.0,user,vcore",
        sku=sku,
        properties={"maxSizeBytes": max_gb * 2**30, **(props or {})},
    )


def vcore_metrics(cpu: float, workers: float, data_io: float, log_io: float) -> Series:
    return {
        "cpu_percent": flat(cpu),
        "workers_percent": flat(workers),
        "physical_data_read_percent": flat(data_io),
        "log_write_percent": flat(log_io),
        "storage_percent": flat(20),
    }


# ------------------------------------------------------------------------------------------------ SQL


def test_a_larger_vcore_size_is_not_credited_with_more_log_or_io_headroom(prices: PriceClient) -> None:
    az = az_with(vcore_db("srv/busy", 4))
    az.give(SQL_T, "srv/busy", vcore_metrics(cpu=95, workers=95, data_io=20, log_io=90))
    a = run(az, prices)["srv/busy"]
    assert a.verdict == "under"
    assert a.proposal is None  # CPU would halve, but the log rate limit may not: 90% stays 90%
    assert "no single larger step" in a.reason


def test_a_larger_vcore_size_is_proposed_when_only_cpu_and_workers_are_the_limit(prices: PriceClient) -> None:
    az = az_with(vcore_db("srv/busy", 2))
    az.give(SQL_T, "srv/busy", vcore_metrics(cpu=95, workers=95, data_io=10, log_io=10))
    a = run(az, prices)["srv/busy"]
    assert a.verdict == "under"
    assert a.proposal is not None
    assert a.proposal.change == "2 -> 4 vCores"
    assert a.proposal.predicted["log_write_percent"] == (10.0, 10.0)
    assert a.proposal.predicted["cpu_percent"] == (47.5, 47.5)


def test_a_smaller_vcore_size_is_judged_with_the_linear_ratio_for_every_limit(prices: PriceClient) -> None:
    az = az_with(vcore_db("srv/quiet", 4))
    az.give(SQL_T, "srv/quiet", vcore_metrics(cpu=10, workers=5, data_io=5, log_io=40))  # log 80% on half the cores
    a = run(az, prices)["srv/quiet"]
    assert (a.verdict, a.proposal) == ("ok", None)
    assert "2 vCores would reach" in a.reason


def test_a_smaller_vcore_size_is_proposed_when_every_limit_stays_comfortable(prices: PriceClient) -> None:
    az = az_with(vcore_db("srv/quiet", 4))
    az.give(SQL_T, "srv/quiet", vcore_metrics(cpu=10, workers=5, data_io=5, log_io=10))
    a = run(az, prices)["srv/quiet"]
    assert a.verdict == "over"
    assert a.proposal is not None
    assert a.proposal.change == "4 -> 2 vCores"
    assert a.proposal.predicted["log_write_percent"] == (20.0, 20.0)


def test_no_smaller_vcore_size_is_proposed_for_a_database_above_one_terabyte(prices: PriceClient) -> None:
    az = az_with(vcore_db("srv/big", 8, max_gb=1536))
    az.give(SQL_T, "srv/big", vcore_metrics(cpu=5, workers=2, data_io=2, log_io=2))
    a = run(az, prices)["srv/big"]
    assert (a.verdict, a.proposal) == ("ok", None)


@pytest.mark.parametrize(
    "props", [{"secondaryType": "Geo"}, {"secondaryType": "Named"}, {"createMode": "Secondary"}], ids=str
)
def test_sql_replicas_are_never_assessed(prices: PriceClient, props: dict[str, JsonValue]) -> None:
    az = az_with(vcore_db("srv/replica", 4, props=props))
    report = analyse(ArmClient(az, days=DAYS, now=NOW), prices, SUB, ())
    assert report.assessments == []
    assert "replica" in report.skipped[0].reason
    assert not any("/metrics?" in c for c in az.calls)


# ------------------------------------------------------------------------------------------------ Container Apps


APP_LOW = {"CpuPercentage": flat(8), "MemoryPercentage": flat(15), "RestartCount": flat(0)}


def test_a_container_app_with_missing_replica_telemetry_is_never_proposed(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=2, memory="4Gi"))
    az.give(
        APP_T,
        "api",
        {
            "Replicas": sparse(1, 168),
            "CpuPercentage": sparse(8, 168),
            "MemoryPercentage": sparse(15, 168),
            "RestartCount": flat(0),
        },
    )
    a = run(az, prices)["api"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert "168 of 720" in a.reason
    assert "scale-to-zero" in a.reason


def test_known_scale_to_zero_hours_are_not_missing_data(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=2, memory="4Gi", min_r=0))
    replicas = {"maximum": first_hours(300, 1, 0), "average": first_hours(300, 1, 0)}
    az.give(
        APP_T,
        "api",
        {
            "Replicas": replicas,
            "CpuPercentage": {"maximum": first_hours(300, 8, None), "average": first_hours(300, 8, None)},
            "MemoryPercentage": {"maximum": first_hours(300, 15, None), "average": first_hours(300, 15, None)},
            "RestartCount": flat(0),
        },
    )
    a = run(az, prices)["api"]
    assert a.verdict == "over"
    assert a.proposal is not None
    # the replica-hours are the average over the whole window (300 of 720 hours), not extrapolated from the busy part
    assert a.current_monthly == pytest.approx((2 * 3.4e-5 + 4 * 4e-6) * 3600 * 730 * 300 / 720)


def test_cpu_and_memory_readings_outside_the_running_hours_do_not_count(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=2, memory="4Gi", min_r=0))
    running = {"maximum": first_hours(300, 1, 0), "average": first_hours(300, 1, 0)}
    elsewhere = {"maximum": [*[None] * 300, *[8.0] * (HOURS - 300)]}  # readings only while no replica ran
    az.give(
        APP_T,
        "api",
        {"Replicas": running, "CpuPercentage": elsewhere, "MemoryPercentage": elsewhere, "RestartCount": flat(0)},
    )
    a = run(az, prices)["api"]
    assert (a.verdict, a.proposal) == ("no-data", None)


def test_replica_hours_are_never_extrapolated_from_the_busy_hours_only(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=2, memory="4Gi", min_r=0))
    replicas = {"maximum": first_hours(300, 1, 0), "average": first_hours(300, 1, None)}  # no average for the zeros
    az.give(
        APP_T,
        "api",
        {
            "Replicas": replicas,
            "CpuPercentage": {"maximum": first_hours(300, 8, None)},
            "MemoryPercentage": {"maximum": first_hours(300, 15, None)},
            "RestartCount": flat(0),
        },
    )
    a = run(az, prices)["api"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert "Replicas average" in a.reason


def test_a_falling_restart_counter_is_a_new_baseline_not_a_restart(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=1, memory="2Gi"))
    counter: list[float | None] = [5.0, *[1.0] * (HOURS - 1)]
    az.give(
        APP_T,
        "api",
        {
            "Replicas": flat(1, 1),
            "CpuPercentage": flat(95, 20),  # peaks inside the hours: not sustained on its own
            "MemoryPercentage": flat(40),
            "RestartCount": {"maximum": counter, "average": list(counter)},
        },
    )
    assert run(az, prices)["api"].verdict == "watch"


def test_a_restart_counter_that_was_already_nonzero_before_the_window_is_not_a_signal(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=1, memory="2Gi"))
    az.give(
        APP_T,
        "api",
        {
            "Replicas": flat(1, 1),
            "CpuPercentage": flat(95, 20),
            "MemoryPercentage": flat(40),
            "RestartCount": {"maximum": hourly(3), "average": hourly(3)},  # cumulative, constant since before
        },
    )
    a = run(az, prices)["api"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert "no second signal" in a.reason


def test_a_rising_restart_counter_is_a_signal(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=1, memory="2Gi"))
    counter = [0.0] * 100 + [1.0] * 100 + [2.0] * (HOURS - 200)
    az.give(
        APP_T,
        "api",
        {
            "Replicas": flat(1, 1),
            "CpuPercentage": flat(95),
            "MemoryPercentage": flat(40),
            "RestartCount": {"maximum": list(counter), "average": list(counter)},
        },
    )
    a = run(az, prices)["api"]
    assert a.verdict == "under"
    assert any("restarts in 2 h" in s.text for s in a.signals)


def env_with_profiles(profiles: bool) -> dict[str, JsonValue]:
    props: dict[str, JsonValue] = {"zoneRedundant": False}
    if profiles:
        props["workloadProfiles"] = [{"name": "Consumption", "workloadProfileType": "Consumption"}]
    return row("Microsoft.App/managedEnvironments", "env", properties=props)


@pytest.mark.parametrize(("profiles", "change"), [(False, None), (True, "2 vCPU / 4 GiB -> 3 vCPU / 6 GiB")])
def test_the_largest_size_follows_the_environment_type(prices: PriceClient, profiles: bool, change: str | None) -> None:
    az = Azure([capp("api", cpu=2, memory="4Gi"), env_with_profiles(profiles)])
    az.give(
        APP_T,
        "api",
        {
            "Replicas": flat(1, 1),
            "CpuPercentage": flat(98),
            "MemoryPercentage": flat(40),
            "RestartCount": {"maximum": hourly(0, {5: 1}), "average": hourly(0)},
        },
    )
    a = run(az, prices)["api"]
    assert a.verdict == "under"
    assert (a.proposal.change if a.proposal else None) == change


# ---------------------------------------------------------------------------------------- stray corroborating points


def test_a_single_queue_point_is_not_a_second_signal(prices: PriceClient) -> None:
    az = az_with(plan("hot", "P1v3"))
    queue = sparse(50, 1)
    az.give(PLAN_T, "hot", {"CpuPercentage": flat(97), "MemoryPercentage": flat(40), "HttpQueueLength": queue})
    a = run(az, prices)["hot"]
    assert (a.verdict, a.proposal) == ("watch", None)


def test_a_single_connection_point_is_not_a_second_signal(prices: PriceClient) -> None:
    az = az_with(postgres("db", "Standard_D2ds_v5", "GeneralPurpose"))
    one = {"maximum": [199.0] + [None] * (HOURS - 1)}
    az.give(
        PG_T,
        "db",
        {
            "cpu_percent": flat(97),
            "memory_percent": flat(50),
            "storage_percent": flat(50),
            "active_connections": one,
            "max_connections": flat(200),
        },
    )
    a = run(az, prices)["db"]
    assert (a.verdict, a.proposal) == ("watch", None)


def test_a_few_stray_exhausted_credit_points_are_not_a_second_signal(prices: PriceClient) -> None:
    az = az_with(postgres("db", "Standard_B1ms", "Burstable"))
    az.give(
        PG_T,
        "db",
        {
            "cpu_percent": flat(100, 20),
            "memory_percent": flat(50),
            "storage_percent": flat(10),
            "active_connections": flat(10),
            "max_connections": flat(50),
            "cpu_credits_remaining": {"minimum": [0.0, 0.0, *[None] * (HOURS - 2)], "maximum": hourly(100)},
        },
    )
    assert run(az, prices)["db"].verdict == "watch"


# ------------------------------------------------------------------------------------------------ pricing and reporting


def test_list_price_ignores_a_free_tier_of_the_meter(prices: PriceClient, monkeypatch: pytest.MonkeyPatch) -> None:
    sku = "Azure App Service Premium v3 Plan - Linux"
    tiered = [
        conftest.item("Azure App Service", sku, "P3 v3", "P3 v3 App", 0.0),
        conftest.item("Azure App Service", sku, "P3 v3", "P3 v3 App", 0.7, tier=100),
    ]
    monkeypatch.setattr(conftest, "CATALOG", [*conftest.CATALOG, *tiered])
    body: dict[str, JsonValue] = {
        "location": "westeurope",
        "kind": "linux",
        "sku": {"name": "P3v3", "capacity": 1},
        "properties": {"reserved": True},
    }
    res = make_resource("Microsoft.Web/serverFarms", "big", body)
    env = Env(DAYS, prices, "westeurope", {}, frozenset(), ResourceIndex([res]))
    # the first 100 hours are free on this meter; the saving of a change must not depend on that
    assert list_price(res, env) == pytest.approx(0.7 * 730)


def test_markdown_shows_warnings_and_skipped_resources(prices: PriceClient) -> None:
    az = az_with(vcore_db("srv/replica", 4, props={"secondaryType": "Geo"}), plan("p1"))
    az.routes.insert(0, ("Microsoft.Advisor", AzureError("HTTP 403: no access")))
    report = analyse(ArmClient(az, days=DAYS, now=NOW), prices, SUB, ())
    report.price_source = "test"
    text = rightsize_report.render_markdown(report, rightsize_report.select(report))
    assert "### Warnings" in text and "Advisor is not available" in text
    assert "### Not assessed (1)" in text and "geo / named replica" in text


# ---------------------------------------------------------------------------------------------------- unknown hours


def test_saturated_readings_during_hours_with_an_unknown_replica_count_block_the_proposal(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=2, memory="4Gi"))
    replicas: dict[str, list[float | None]] = {
        "maximum": [*[1.0] * 648, *[None] * 72],
        "average": [*[1.0] * 648, *[None] * 72],
    }
    hot = [*[8.0] * 648, *[99.0] * 72]
    az.give(
        APP_T,
        "api",
        {
            "Replicas": replicas,
            "CpuPercentage": {"maximum": list(hot)},
            "MemoryPercentage": {"maximum": list(hot)},
            "RestartCount": flat(0),
        },
    )
    a = run(az, prices)["api"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert "replica count is unknown" in a.reason


def test_missing_restart_telemetry_blocks_a_downsize(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=2, memory="4Gi"))
    az.give(APP_T, "api", {"Replicas": flat(1, 1), "CpuPercentage": flat(8), "MemoryPercentage": flat(15)})
    a = run(az, prices)["api"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert "RestartCount" in a.reason


def test_missing_queue_telemetry_blocks_a_plan_downsize(prices: PriceClient) -> None:
    az = az_with(plan("quiet", "P2v3"))
    az.give(PLAN_T, "quiet", {"CpuPercentage": flat(8), "MemoryPercentage": flat(23)})
    a = run(az, prices)["quiet"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert "HttpQueueLength" in a.reason


def test_metrics_are_placed_on_the_requested_hourly_window() -> None:
    def point(hour: int, value: float | None) -> dict[str, object]:
        stamp = {"timeStamp": f"2026-11-06T{hour:02d}:00:00Z"}
        return {**stamp, "maximum": value} if value is not None else stamp

    payload: dict[str, object] = {
        "value": [
            {"name": {"value": "A"}, "timeseries": [{"data": [point(0, 1), point(1, 2), point(2, 3)]}]},
            {"name": {"value": "B"}, "timeseries": [{"data": [point(0, 10), point(2, 30)]}]},  # hour 1 omitted
        ]
    }
    parsed = parse_metrics(payload, ["Maximum"], datetime(2026, 11, 6, tzinfo=UTC), 4)
    assert parsed["a"]["maximum"] == [1, 2, 3, None]  # the window is 4 hours, the last one was not reported
    assert parsed["b"]["maximum"] == [10, None, 30, None]


def test_omitted_replica_records_stay_missing_through_the_one_by_one_fallback(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=2, memory="4Gi"))
    start = datetime(2026, 11, 6, 10, tzinfo=UTC) - timedelta(days=DAYS)

    def stamped(values: list[float | None]) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for i, v in enumerate(values):
            if v is None and i >= 648:
                continue  # Azure leaves the record out altogether
            stamp = (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            out.append({"timeStamp": stamp, **({} if v is None else {"maximum": v, "average": v})})
        return out

    series: dict[str, list[float | None]] = {
        "Replicas": [*[1.0] * 648, *[None] * 72],
        "CpuPercentage": [*[8.0] * 648, *[99.0] * 72],
        "MemoryPercentage": [*[8.0] * 648, *[99.0] * 72],
        "RestartCount": [0.0] * HOURS,
    }

    def metrics(url: str, body: dict[str, object] | None) -> dict[str, object]:
        names = parse_qs(urlparse(url).query)["metricnames"][0].split(",")
        if len(names) > 1:
            raise AzureError("GET x -> HTTP 400: metric not supported")
        return {"value": [{"name": {"value": n}, "timeseries": [{"data": stamped(series[n])}]} for n in names]}

    az.routes.insert(0, ("/metrics?", metrics))
    a = run(az, prices)["api"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert "replica count is unknown" in a.reason
