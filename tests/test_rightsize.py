import json
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from bicep_cost import cli, rightsize
from bicep_cost.advisor import AdvisorRec, fetch_recommendations
from bicep_cost.armexpr import JsonValue
from bicep_cost.collect.arm import ArmClient, AzureError
from bicep_cost.prices import PriceClient
from bicep_cost.rightsize import Candidate, Case, Signal, Stats, build_stats, decide
from bicep_cost.rightsize_report import render_json, render_markdown, render_text, select
from bicep_cost.rightsize_run import analyse
from tests.conftest import fake_fetcher
from tests.test_collect import FakeAzure

SUB = "sub1"
DAYS = 30
HOURS = DAYS * 24
NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
RG = "rg-app"
BASE = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers"


# ------------------------------------------------------------------------------------------------ synthetic series


def hourly(base: float, peaks: dict[int, float] | None = None, n: int = HOURS) -> list[float | None]:
    """``n`` hourly maxima at ``base``, with single-hour ``peaks`` at the given indexes."""
    values: list[float | None] = [base] * n
    for i, v in (peaks or {}).items():
        values[i] = v
    return values


def first_hours(hours: int, value: float, rest: float | None) -> list[float | None]:
    """``value`` for the first ``hours`` hours of the window, ``rest`` (or no data) afterwards."""
    return [*[value] * hours, *[rest] * (HOURS - hours)]


def gap(values: list[float | None], missing: int) -> list[float | None]:
    return [None] * missing + values[missing:]


def sizing(
    name: str, values: list[float | None], group: str = "cpu", trigger: bool = True, expected: int = HOURS
) -> Stats:
    return build_stats(name, values, None, expected, group=group, trigger=trigger)


def candidate(target: str, factor: float, price: float | None, name: str = "cpu") -> Candidate:
    return Candidate(f"cur -> {target}", target, {name: factor}, lambda: price, "low: test", f"rollback {target}")


def case(
    metrics: list[Stats],
    *,
    current: float | None = 100.0,
    days: int = DAYS,
    down: list[Candidate] | None = None,
    up: list[Candidate] | None = None,
    signals: list[Signal] | None = None,
    advisor: list[AdvisorRec] | None = None,
    exempt_over: str | None = None,
) -> Case:
    return Case(
        f"{BASE}/Microsoft.Web/serverFarms/plan",
        "plan",
        "Microsoft.Web/serverFarms",
        RG,
        "P2v3 x1",
        days,
        metrics,
        lambda: current,
        down or [],
        up or [],
        signals or [],
        advisor or [],
        exempt_over=exempt_over,
    )


# ------------------------------------------------------------------------------------------------ statistics


def test_p95_is_nearest_rank_of_the_hourly_maxima_and_ignores_missing_hours() -> None:
    s = build_stats("cpu", [None, *range(1, 101)], [None, *[v / 2 for v in range(1, 101)]], 101)
    assert s.p95 == 95
    assert s.max == 100
    assert s.mean == pytest.approx(25.25)  # the mean follows the hourly averages when they exist
    assert (s.hours, s.expected) == (100, 101)
    assert s.coverage == pytest.approx(100 / 101)


def test_a_single_spike_is_not_a_meaningful_share_of_hours() -> None:
    assert not sizing("cpu", hourly(10, {5: 100})).hot
    assert sizing("cpu", hourly(10, dict.fromkeys(range(15), 97))).hot  # 15 h of 720 = 2.1%
    assert sizing("cpu", hourly(90)).hot  # p95 >= 85


# ------------------------------------------------------------------------------------------------ decide


def test_clear_over_provisioning_gets_a_priced_proposal() -> None:
    cpu = sizing("CpuPercentage", hourly(8, {3: 31}))
    mem = sizing("MemoryPercentage", hourly(23, {3: 25}), "memory")
    result = decide(case([cpu, mem], down=[candidate("P1v3", 2.0, 50.0, "cpu")]))
    assert result.verdict == "over"
    assert result.proposal is not None
    assert result.proposal.saving == pytest.approx(50.0)
    assert result.proposal.target == "P1v3"
    assert result.proposal.predicted["CpuPercentage"] == pytest.approx((16.0, 62.0))
    assert result.proposal.predicted["MemoryPercentage"] == (23.0, 25.0)  # memory group has no factor: unchanged
    assert result.proposal.risk and result.proposal.rollback
    assert result.coverage == 1.0


def test_target_that_would_exceed_the_limits_is_not_proposed() -> None:
    cpu = sizing("CpuPercentage", hourly(38, {3: 55}))  # doubled: p95 76 > 70
    result = decide(case([cpu], down=[candidate("P1v3", 2.0, 50.0)]))
    assert result.verdict == "ok"
    assert result.proposal is None
    assert "P1v3 would reach" in result.reason
    # the peak is judged too: p95 fine (30 -> 60) but max 55 -> 110
    peaky = sizing("CpuPercentage", hourly(30, {3: 55}))
    assert decide(case([peaky], down=[candidate("P1v3", 2.0, 50.0)])).proposal is None


def test_over_needs_every_sizing_metric_low() -> None:
    cpu = sizing("CpuPercentage", hourly(8))
    mem = sizing("MemoryPercentage", hourly(55), "memory")
    result = decide(case([cpu, mem], down=[candidate("P1v3", 1.5, 50.0)]))
    assert (result.verdict, result.proposal) == ("ok", None)


def test_coverage_gap_gives_no_proposal() -> None:
    cpu = sizing("CpuPercentage", gap(hourly(8), 100))  # 620 of 720 h = 86%
    result = decide(case([cpu], down=[candidate("P1v3", 2.0, 50.0)]))
    assert result.verdict == "watch"
    assert result.proposal is None
    assert "620 of 720" in result.reason
    ok = sizing("CpuPercentage", gap(hourly(8), 72))  # exactly 90%
    assert decide(case([ok], down=[candidate("P1v3", 2.0, 50.0)])).verdict == "over"


def test_no_metric_data_is_no_data() -> None:
    result = decide(case([sizing("CpuPercentage", [])]))
    assert result.verdict == "no-data"
    assert decide(case([])).verdict == "no-data"


def test_short_window_cannot_prove_over_because_of_monthly_cycles() -> None:
    cpu = sizing("CpuPercentage", hourly(8, n=14 * 24), expected=14 * 24)
    result = decide(case([cpu], down=[candidate("P1v3", 2.0, 50.0)], days=14))
    assert result.verdict == "watch"
    assert "monthly cycle" in result.reason
    assert result.proposal is None


def test_one_saturated_signal_without_corroboration_is_only_a_watch() -> None:
    cpu = sizing("CpuPercentage", hourly(91, dict.fromkeys(range(50), 100)))
    result = decide(case([cpu], up=[candidate("P3v3", 0.5, 400.0)]))
    assert result.verdict == "watch"
    assert result.proposal is None
    assert "no second signal" in result.reason


def test_saturation_plus_a_second_signal_is_under_with_a_larger_step() -> None:
    cpu = sizing("CpuPercentage", hourly(91, dict.fromkeys(range(50), 100)))
    result = decide(case([cpu], up=[candidate("P3v3", 0.5, 160.0)], signals=[Signal("restarts", "container restarts")]))
    assert result.verdict == "under"
    assert result.proposal is not None
    assert result.proposal.delta == pytest.approx(60.0)
    assert result.proposal.kind == "under"
    assert result.proposal.predicted["CpuPercentage"][0] == pytest.approx(50.0)


def test_a_saturated_but_mostly_idle_resource_is_flagged_as_bursty() -> None:
    cpu = build_stats(
        "CpuPercentage", hourly(5, dict.fromkeys(range(50), 100)), hourly(5), HOURS, group="cpu", trigger=True
    )
    result = decide(case([cpu], up=[candidate("P3v3", 0.5, 160.0)], signals=[Signal("queue", "HTTP queue")]))
    assert result.verdict == "under"
    assert any("bursty load" in n and "50 of 720 h" in n for n in result.notes)
    busy = sizing("CpuPercentage", hourly(91))
    assert not decide(case([busy], up=[candidate("P3v3", 0.5, 160.0)], signals=[Signal("queue", "q")])).notes


def test_an_advisor_performance_recommendation_is_a_second_signal_but_a_cost_one_is_not() -> None:
    cpu = sizing("CpuPercentage", hourly(91))
    cost = AdvisorRec("Cost", "High", "Right-size or shutdown underutilized virtual machines")
    perf = AdvisorRec("Performance", "Medium", "Scale up to improve performance")
    up = [candidate("P3v3", 0.5, 160.0)]
    assert decide(case([cpu], up=up, advisor=[cost])).verdict == "watch"
    result = decide(case([cpu], up=up, advisor=[cost, perf]))
    assert result.verdict == "under"
    assert "Advisor Performance" in result.reason


def test_under_without_a_larger_step_that_helps_has_no_proposal() -> None:
    cpu = sizing("CpuPercentage", hourly(98))
    sig = [Signal("queue", "HTTP queue")]
    result = decide(case([cpu], up=[candidate("P3v3", 0.9, 160.0)], signals=sig))
    assert (result.verdict, result.proposal) == ("under", None)
    assert "no single larger step" in result.reason
    assert decide(case([cpu], signals=sig)).proposal is None


def test_signals_that_contradict_low_load_block_the_proposal() -> None:
    cpu = sizing("CpuPercentage", hourly(8))
    result = decide(
        case([cpu], down=[candidate("P1v3", 2.0, 50.0)], signals=[Signal("restarts", "container restarts")])
    )
    assert result.verdict == "watch"
    assert "contradictory" in result.reason


def test_elastic_resources_are_never_shrunk() -> None:
    cpu = sizing("cpu_percent", hourly(3))
    result = decide(case([cpu], down=[candidate("B1ms", 2.0, 5.0)], exempt_over="Burstable is already elastic"))
    assert (result.verdict, result.proposal) == ("ok", None)
    assert "Burstable is already elastic" in result.reason


def test_an_unpriced_target_gives_a_watch_not_a_guess() -> None:
    cpu = sizing("CpuPercentage", hourly(8))
    assert decide(case([cpu], down=[candidate("P1v3", 2.0, None)])).verdict == "watch"
    assert decide(case([cpu], down=[candidate("P1v3", 2.0, 50.0)], current=None)).verdict == "watch"


def test_a_change_that_saves_nothing_is_not_proposed() -> None:
    cpu = sizing("CpuPercentage", hourly(8))
    result = decide(case([cpu], down=[candidate("P1v3", 2.0, 100.0)]))
    assert (result.verdict, result.proposal) == ("ok", None)


def test_the_cheapest_comfortable_option_wins() -> None:
    cpu = sizing("CpuPercentage", hourly(10))
    result = decide(case([cpu], down=[candidate("size", 2.0, 70.0), candidate("instances", 1.5, 40.0)]))
    assert result.proposal is not None
    assert result.proposal.target == "instances"


def test_moderate_load_is_ok() -> None:
    result = decide(case([sizing("CpuPercentage", hourly(60, {3: 80}))]))
    assert result.verdict == "ok"
    assert "between" in result.reason


# ------------------------------------------------------------------------------------------------ live shaped inventory


def rid(type_: str, name: str) -> str:
    """Resource id; child resources (type ``Sql/servers/databases``, name ``srv/db``) interleave their segments."""
    provider, *kinds = type_.split("/")
    segments = "/".join(f"{k}/{n}" for k, n in zip(kinds, name.split("/"), strict=True))
    return f"{BASE}/{provider}/{segments}"


def row(type_: str, name: str, **fields: JsonValue) -> dict[str, JsonValue]:
    return {
        "id": rid(type_, name),
        "name": name.rsplit("/", 1)[-1],
        "type": type_.lower(),
        "location": "westeurope",
        **fields,
    }


def plan(name: str, sku: str = "P2v3", capacity: int = 1, **props: JsonValue) -> dict[str, JsonValue]:
    return row(
        "Microsoft.Web/serverFarms",
        name,
        kind="linux",
        sku={"name": sku, "capacity": capacity},
        properties={"reserved": True, **props},
    )


def postgres(name: str, sku: str, tier: str, **props: JsonValue) -> dict[str, JsonValue]:
    return row(
        "Microsoft.DBforPostgreSQL/flexibleServers",
        name,
        sku={"name": sku, "tier": tier},
        properties={"highAvailability": {"mode": "Disabled"}, "storage": {"storageSizeGB": 128}, **props},
    )


def sqldb(name: str, sku: dict[str, JsonValue], kind: str = "v12.0,user", **props: JsonValue) -> dict[str, JsonValue]:
    return row(
        "Microsoft.Sql/servers/databases",
        name,
        kind=kind,
        sku=sku,
        properties={"maxSizeBytes": 250 * 2**30, **props},
    )


def capp(
    name: str,
    cpu: float = 2,
    memory: str = "4Gi",
    min_r: int = 1,
    max_r: int = 3,
    containers: int = 1,
    **props: JsonValue,
) -> dict[str, JsonValue]:
    boxes: list[JsonValue] = [{"name": f"c{i}", "resources": {"cpu": cpu, "memory": memory}} for i in range(containers)]
    return row(
        "Microsoft.App/containerApps",
        name,
        properties={
            "environmentId": rid("Microsoft.App/managedEnvironments", "env"),
            "template": {"containers": boxes, "scale": {"minReplicas": min_r, "maxReplicas": max_r}},
            **props,
        },
    )


ENV_ROW = row("Microsoft.App/managedEnvironments", "env", properties={"zoneRedundant": False})


def payload(series: dict[str, dict[str, list[float | None]]], names: list[str]) -> dict[str, object]:
    """An Azure Monitor response for the metrics asked for; ``series`` maps metric -> aggregation -> hourly values."""
    out = []
    for name in names:
        aggs = series.get(name)
        if aggs is None:
            continue
        points = len(next(iter(aggs.values())))
        data = [{agg: aggs[agg][i] for agg in aggs if aggs[agg][i] is not None} for i in range(points)]
        out.append({"name": {"value": name}, "timeseries": [{"data": data}]})
    return {"value": out}


def flat(maximum: float, average: float | None = None, n: int = HOURS) -> dict[str, list[float | None]]:
    return {"maximum": hourly(maximum, n=n), "average": hourly(maximum if average is None else average, n=n)}


Series = dict[str, dict[str, list[float | None]]]


class Azure(FakeAzure):
    """Routes Resource Graph, Advisor and per-resource Azure Monitor calls."""

    def __init__(self, rows: list[dict[str, JsonValue]]) -> None:
        super().__init__()
        self.metrics: dict[str, Series] = {}
        self.autoscale: list[dict[str, JsonValue]] = []
        self.advisor: list[dict[str, object]] = []
        self.rows = rows
        self.on("Microsoft.ResourceGraph/resources", self._graph)
        self.on("Microsoft.Advisor/recommendations", lambda url, body: {"value": self.advisor})
        self.on("/metrics?", self._metrics)

    def _graph(self, url: str, body: dict[str, object] | None) -> dict[str, object]:
        assert body is not None
        query = str(body["query"])
        return {"data": self.autoscale if "autoscalesettings" in query else self.rows}

    def _metrics(self, url: str, body: dict[str, object] | None) -> dict[str, object]:
        resource = url.split("/providers/microsoft.insights")[0].removeprefix("https://management.azure.com")
        names = parse_qs(urlparse(url).query)["metricnames"][0].split(",")
        return payload(self.metrics.get(resource.lower(), {}), names)

    def give(self, type_: str, name: str, series: Series) -> None:
        self.metrics[rid(type_, name).lower()] = series


def run(az: Azure, prices: PriceClient) -> dict[str, rightsize.Assessment]:
    arm = ArmClient(az, days=DAYS, now=NOW)
    report = analyse(arm, prices, SUB, ())
    return {a.name: a for a in report.assessments}


def az_with(*rows: dict[str, JsonValue]) -> Azure:
    return Azure([*rows, ENV_ROW])


PLAN_T = "Microsoft.Web/serverFarms"
PG_T = "Microsoft.DBforPostgreSQL/flexibleServers"
SQL_T = "Microsoft.Sql/servers/databases"
APP_T = "Microsoft.App/containerApps"


@pytest.fixture
def prices() -> PriceClient:
    return PriceClient(cache_dir=None, fetcher=fake_fetcher)


def test_over_provisioned_plan_gets_a_proposal_with_evidence(prices: PriceClient) -> None:
    az = az_with(plan("plan1"))
    az.give(
        PLAN_T,
        "plan1",
        {
            "CpuPercentage": {"maximum": hourly(8, {5: 31}), "average": hourly(2)},
            "MemoryPercentage": flat(23),
            "HttpQueueLength": flat(0),
        },
    )
    a = run(az, prices)["plan1"]
    assert a.verdict == "over"
    assert a.proposal is not None
    assert (a.proposal.change, a.proposal.target) == ("P2v3 -> P1v3", "P1v3 x1")
    assert a.proposal.current_monthly == pytest.approx(0.356 * 730)
    assert a.proposal.saving == pytest.approx((0.356 - 0.178) * 730)
    assert a.proposal.rollback == f"az appservice plan update -g {RG} -n plan1 --sku P2V3"
    cpu = next(m for m in a.metrics if m.name == "CpuPercentage")
    assert (cpu.hours, cpu.expected, cpu.max) == (HOURS, HOURS, 31)
    assert cpu.mean == 2


def test_metrics_are_read_with_hourly_granularity_over_the_requested_window(prices: PriceClient) -> None:
    az = az_with(plan("plan1"))
    run(az, prices)
    (call,) = [c for c in az.calls if "/metrics?" in c]
    query = parse_qs(urlparse(call.split(" ", 1)[1]).query)
    assert query["interval"] == ["PT1H"]
    assert query["aggregation"] == ["Average,Maximum,Minimum"]
    assert query["timespan"] == ["2026-10-07T10:00Z/2026-11-06T10:00Z"]


def test_a_metric_gap_gives_a_watch_without_proposal(prices: PriceClient) -> None:
    az = az_with(plan("plan1"))
    az.give(
        PLAN_T,
        "plan1",
        {"CpuPercentage": {"maximum": gap(hourly(8), 300)}, "MemoryPercentage": flat(23), "HttpQueueLength": flat(0)},
    )
    a = run(az, prices)["plan1"]
    assert (a.verdict, a.proposal) == ("watch", None)
    assert "420 of 720" in a.reason


def test_a_resource_without_any_metric_is_no_data(prices: PriceClient) -> None:
    a = run(az_with(plan("plan1")), prices)["plan1"]
    assert a.verdict == "no-data"


def test_autoscaled_plan_is_not_shrunk(prices: PriceClient) -> None:
    az = az_with(plan("plan1"))
    az.autoscale = [{"target": rid(PLAN_T, "plan1").lower(), "enabled": True}]
    az.give(PLAN_T, "plan1", {"CpuPercentage": flat(8), "MemoryPercentage": flat(23), "HttpQueueLength": flat(0)})
    a = run(az, prices)["plan1"]
    assert (a.verdict, a.proposal) == ("ok", None)
    assert "autoscale" in a.reason


def test_a_disabled_autoscale_setting_does_not_exempt_the_plan(prices: PriceClient) -> None:
    az = az_with(plan("plan1"))
    az.autoscale = [{"target": rid(PLAN_T, "plan1").lower(), "enabled": False}]
    az.give(PLAN_T, "plan1", {"CpuPercentage": flat(8), "MemoryPercentage": flat(23), "HttpQueueLength": flat(0)})
    assert run(az, prices)["plan1"].verdict == "over"


def test_zone_redundant_plan_keeps_two_instances(prices: PriceClient) -> None:
    az = az_with(plan("zr", "P1v3", 2, zoneRedundant=True), plan("plain", "P1v3", 2))
    low = {"CpuPercentage": flat(8), "MemoryPercentage": flat(10), "HttpQueueLength": flat(0)}
    az.give(PLAN_T, "zr", low)
    az.give(PLAN_T, "plain", low)
    result = run(az, prices)
    assert result["plain"].proposal is not None
    assert result["plain"].proposal.change in ("2 -> 1 instances", "P1v3 -> P0v3")
    zr = result["zr"].proposal
    assert zr is None or "instances" not in zr.change


def test_instance_reduction_is_priced_per_instance(prices: PriceClient) -> None:
    az = az_with(plan("pair", "P1v3", 2))
    az.give(PLAN_T, "pair", {"CpuPercentage": flat(8), "MemoryPercentage": flat(10), "HttpQueueLength": flat(0)})
    p = run(az, prices)["pair"].proposal
    assert p is not None
    # P0v3 saves 2 x (0.178 - 0.1) per hour, one instance less saves 0.178: the bigger saving wins
    assert p.change == "2 -> 1 instances"
    assert p.saving == pytest.approx(0.178 * 730)


def test_saturated_plan_with_a_queue_is_under_with_a_larger_plan(prices: PriceClient) -> None:
    az = az_with(plan("hot", "P1v3"))
    az.give(
        PLAN_T,
        "hot",
        {
            "CpuPercentage": flat(97),
            "MemoryPercentage": flat(40),
            "HttpQueueLength": {"maximum": hourly(0, dict.fromkeys(range(30), 25)), "average": hourly(0)},
        },
    )
    a = run(az, prices)["hot"]
    assert a.verdict == "under"
    assert any("HTTP queue" in s.text for s in a.signals)
    assert a.proposal is not None
    assert a.proposal.change == "P1v3 -> P2v3"
    assert a.proposal.delta == pytest.approx((0.356 - 0.178) * 730)


def test_postgres_burstable_is_not_shrunk_but_can_be_under(prices: PriceClient) -> None:
    az = az_with(postgres("quiet", "Standard_B1ms", "Burstable"), postgres("busy", "Standard_B1ms", "Burstable"))
    quiet = {"cpu_percent": flat(2), "memory_percent": flat(20), "storage_percent": flat(10)}
    az.give(PG_T, "quiet", {**quiet, "active_connections": flat(3), "max_connections": flat(50)})
    remaining = {"maximum": hourly(100), "average": hourly(100), "minimum": hourly(100, dict.fromkeys(range(40), 0))}
    az.give(
        PG_T,
        "busy",
        {
            "cpu_percent": flat(100),
            "memory_percent": flat(60),
            "storage_percent": flat(10),
            "active_connections": flat(20),
            "max_connections": flat(50),
            "cpu_credits_remaining": remaining,
        },
    )
    result = run(az, prices)
    assert (result["quiet"].verdict, result["quiet"].proposal) == ("ok", None)
    assert "Burstable" in result["quiet"].reason
    busy = result["busy"]
    assert busy.verdict == "under"
    assert busy.proposal is not None
    assert busy.proposal.target == "B2s"
    assert busy.proposal.delta == pytest.approx((0.0796 - 0.0199) * 730)
    assert any("credits" in s.text for s in busy.signals)


def test_postgres_general_purpose_steps_down_within_its_family(prices: PriceClient) -> None:
    az = az_with(postgres("big", "Standard_D4ds_v5", "GeneralPurpose"))
    az.give(
        PG_T,
        "big",
        {
            "cpu_percent": flat(10, 5),
            "memory_percent": flat(25, 20),
            "storage_percent": flat(70),
            "active_connections": flat(10),
            "max_connections": flat(400),
        },
    )
    a = run(az, prices)["big"]
    assert a.verdict == "over"
    assert a.proposal is not None
    assert a.proposal.change == "D4ds_v5 -> D2ds_v5"
    assert a.proposal.predicted["cpu_percent"] == (20.0, 20.0)
    assert "--sku-name Standard_D4ds_v5 --tier GeneralPurpose" in a.proposal.rollback
    # storage is shown but never judged
    assert not next(m for m in a.metrics if m.name == "storage_percent").group


def test_postgres_connections_near_the_limit_corroborate_saturation(prices: PriceClient) -> None:
    az = az_with(postgres("db", "Standard_D2ds_v5", "GeneralPurpose"))
    az.give(
        PG_T,
        "db",
        {
            "cpu_percent": flat(95, 80),
            "memory_percent": flat(50),
            "storage_percent": flat(50),
            "active_connections": flat(195),
            "max_connections": flat(200),
        },
    )
    a = run(az, prices)["db"]
    assert a.verdict == "under"
    assert any("max_connections" in s.text for s in a.signals)


def test_postgres_cpu_alone_stays_a_watch(prices: PriceClient) -> None:
    az = az_with(postgres("db", "Standard_D2ds_v5", "GeneralPurpose"))
    az.give(
        PG_T,
        "db",
        {
            "cpu_percent": flat(95, 80),
            "memory_percent": flat(50),
            "storage_percent": flat(50),
            "active_connections": flat(20),
            "max_connections": flat(200),
        },
    )
    a = run(az, prices)["db"]
    assert (a.verdict, a.proposal) == ("watch", None)


def test_read_replicas_are_not_assessed(prices: PriceClient) -> None:
    report = analyse(
        ArmClient(
            az_with(postgres("rep", "Standard_D4ds_v5", "GeneralPurpose", replicationRole="AsyncReplica")),
            days=DAYS,
            now=NOW,
        ),
        prices,
        SUB,
        (),
    )
    assert report.assessments == []
    assert report.skipped[0].reason.startswith("read replica")


def test_sql_dtu_database_steps_down_one_level(prices: PriceClient) -> None:
    az = az_with(sqldb("srv/app", {"name": "Standard", "tier": "Standard", "capacity": 20}))
    az.give(
        SQL_T,
        "srv/app",
        {
            "dtu_consumption_percent": flat(12, 4),
            "cpu_percent": flat(12),
            "physical_data_read_percent": flat(2),
            "log_write_percent": flat(1),
            "workers_percent": flat(3),
            "storage_percent": flat(40),
        },
    )
    a = run(az, prices)["srv/app"]
    assert a.verdict == "over"
    assert a.proposal is not None
    assert (a.proposal.change, a.proposal.target) == ("S1 -> S0", "S0 (10 DTU)")
    assert a.proposal.predicted["dtu_consumption_percent"] == (24.0, 24.0)
    assert a.proposal.saving == pytest.approx((0.9678 - 0.4839) * 365 / 12)
    assert "--service-objective S1" in a.proposal.rollback


def test_sql_database_that_would_not_fit_the_smaller_level_is_kept(prices: PriceClient) -> None:
    big = sqldb(
        "srv/big", {"name": "Standard", "tier": "Standard", "capacity": 100}, maxSizeBytes=500 * 2**30
    )  # S3 holds 1 TB, S2 only 250 GB
    az = az_with(big)
    az.give(
        SQL_T,
        "srv/big",
        {
            "dtu_consumption_percent": flat(5),
            "cpu_percent": flat(5),
            "physical_data_read_percent": flat(1),
            "log_write_percent": flat(1),
            "workers_percent": flat(1),
            "storage_percent": flat(30),
        },
    )
    a = run(az, prices)["srv/big"]
    assert (a.verdict, a.proposal) == ("ok", None)


def test_sql_dtu_at_its_limit_with_a_saturated_component_is_under_when_sustained(prices: PriceClient) -> None:
    az = az_with(sqldb("srv/hot", {"name": "Standard", "tier": "Standard", "capacity": 20}))
    az.give(
        SQL_T,
        "srv/hot",
        {
            "dtu_consumption_percent": flat(100),
            "cpu_percent": flat(40),
            "physical_data_read_percent": flat(10),
            "log_write_percent": flat(100),
            "workers_percent": flat(2),
            "storage_percent": flat(10),
        },
    )
    a = run(az, prices)["srv/hot"]
    assert a.verdict == "under"
    assert "sustained" in a.reason
    assert any("log IO" in n for n in a.notes)  # what saturates is shown, but it is not a second signal
    assert a.signals == []


def test_sql_serverless_is_excluded_from_shrinking(prices: PriceClient) -> None:
    sku: dict[str, JsonValue] = {"name": "GP_S_Gen5", "tier": "GeneralPurpose", "family": "Gen5", "capacity": 4}
    az = az_with(sqldb("srv/sl", sku, kind="v12.0,user,vcore,serverless", minCapacity=0.5))
    az.give(
        SQL_T,
        "srv/sl",
        {
            "cpu_percent": flat(3),
            "physical_data_read_percent": flat(1),
            "log_write_percent": flat(1),
            "workers_percent": flat(1),
            "storage_percent": flat(5),
        },
    )
    a = run(az, prices)["srv/sl"]
    assert (a.verdict, a.proposal) == ("ok", None)
    assert "serverless" in a.reason


def test_system_databases_and_pool_members_are_skipped_with_a_reason(prices: PriceClient) -> None:
    rows = [
        sqldb("srv/master", {"name": "GP_SYSTEM", "tier": "System", "capacity": 4}, kind="v12.0,system,serverless"),
        sqldb("srv/pooled", {"name": "ElasticPool"}, elasticPoolId="pool"),
    ]
    report = analyse(ArmClient(az_with(*rows), days=DAYS, now=NOW), prices, SUB, ())
    assert report.assessments == []
    assert {s.name: s.reason for s in report.skipped}["srv/master"] == "system database"
    assert "elastic pool" in {s.name: s.reason for s in report.skipped}["srv/pooled"]


def test_container_app_gets_a_smaller_size_and_a_priced_saving(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=2, memory="4Gi", min_r=1, max_r=3))
    az.give(
        APP_T,
        "api",
        {
            "Replicas": flat(1, 1),
            "CpuPercentage": flat(10, 4),
            "MemoryPercentage": flat(20, 15),
            "RestartCount": flat(0),
        },
    )
    a = run(az, prices)["api"]
    assert a.verdict == "over"
    assert a.proposal is not None
    assert a.proposal.change == "2 vCPU / 4 GiB -> 1.75 vCPU / 3.5 GiB"
    assert a.proposal.predicted["CpuPercentage"] == pytest.approx((11.43, 11.43), abs=0.01)
    assert a.proposal.saving > 0
    assert "--cpu 2 --memory 4Gi" in a.proposal.rollback


def test_container_app_min_replicas_that_were_never_needed_are_reduced(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=0.25, memory="0.5Gi", min_r=3, max_r=5))
    az.give(
        APP_T,
        "api",
        {"Replicas": flat(3, 3), "CpuPercentage": flat(8), "MemoryPercentage": flat(15), "RestartCount": flat(0)},
    )
    a = run(az, prices)["api"]
    assert a.proposal is not None
    assert a.proposal.change == "min replicas 3 -> 2"
    assert a.proposal.saving == pytest.approx(
        0.25 * 3600 * 730 * 3.4e-5 + 0.5 * 3600 * 730 * 4e-6
    )  # one replica's hours


def test_container_app_replicas_that_scaled_out_keep_their_minimum(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=0.25, memory="0.5Gi", min_r=2, max_r=5))
    scaled = {"maximum": hourly(2, dict.fromkeys(range(20), 4)), "average": hourly(2)}
    az.give(
        APP_T,
        "api",
        {"Replicas": scaled, "CpuPercentage": flat(8), "MemoryPercentage": flat(15), "RestartCount": flat(0)},
    )
    a = run(az, prices)["api"]
    assert a.proposal is None  # 0.25 vCPU is the smallest size and the replicas did scale out


def test_container_app_pinned_at_max_with_restarts_is_under(prices: PriceClient) -> None:
    az = az_with(capp("api", cpu=1, memory="2Gi", min_r=1, max_r=3))
    az.give(
        APP_T,
        "api",
        {
            "Replicas": flat(3, 3),
            "CpuPercentage": flat(90),
            "MemoryPercentage": flat(40),
            "RestartCount": {"maximum": hourly(0, {5: 1}), "average": hourly(0)},
        },
    )
    a = run(az, prices)["api"]
    assert a.verdict == "under"
    assert {s.name for s in a.signals} == {"restarts", "pinned"}
    assert a.proposal is not None
    assert a.proposal.change == "1 vCPU / 2 GiB -> 1.5 vCPU / 3 GiB"
    assert a.proposal.delta > 0


def test_container_app_with_several_containers_is_never_resized(prices: PriceClient) -> None:
    az = az_with(capp("multi", cpu=1, memory="2Gi", containers=2))
    az.give(
        APP_T,
        "multi",
        {"Replicas": flat(1, 1), "CpuPercentage": flat(5), "MemoryPercentage": flat(10), "RestartCount": flat(0)},
    )
    a = run(az, prices)["multi"]
    assert (a.verdict, a.proposal) == ("ok", None)
    assert "several containers" in a.reason


def test_container_app_that_never_ran_has_no_data_and_one_that_ran_briefly_is_a_watch(prices: PriceClient) -> None:
    az = az_with(capp("zero", min_r=0), capp("brief", min_r=0))
    az.give(
        APP_T,
        "zero",
        {
            "Replicas": flat(0, 0),
            "CpuPercentage": flat(0, n=0),
            "MemoryPercentage": flat(0, n=0),
            "RestartCount": flat(0),
        },
    )
    short = {"maximum": first_hours(60, 1, 0), "average": first_hours(60, 1, 0)}
    az.give(
        APP_T,
        "brief",
        {
            "Replicas": short,
            "CpuPercentage": {"maximum": first_hours(60, 5, None), "average": first_hours(60, 5, None)},
            "MemoryPercentage": {"maximum": first_hours(60, 9, None)},
            "RestartCount": flat(0),
        },
    )
    result = run(az, prices)
    assert result["zero"].verdict == "no-data"
    assert result["brief"].verdict == "watch"
    assert "ran in only 60 h" in result["brief"].reason


def test_unsupported_types_are_listed_as_not_covered_and_free_ones_are_not(prices: PriceClient) -> None:
    rows = [
        row("Microsoft.Storage/storageAccounts", "st1"),
        row("Microsoft.Storage/storageAccounts", "st2"),
        row("Microsoft.Compute/virtualMachines", "vm1"),
        row("Microsoft.Network/virtualNetworks", "vnet"),
    ]
    az = az_with(*rows)
    report = analyse(ArmClient(az, days=DAYS, now=NOW), prices, SUB, ())
    assert report.not_covered == {
        "microsoft.storage/storageaccounts": 2,
        "microsoft.compute/virtualmachines": 1,
        "microsoft.app/managedenvironments": 1,
    }
    assert not any("/metrics?" in c for c in az.calls)  # nothing is measured for them


def test_advisor_is_a_second_opinion_matched_by_resource_id(prices: PriceClient) -> None:
    az = az_with(plan("plan1", "P1v3"))
    az.give(PLAN_T, "plan1", {"CpuPercentage": flat(91), "MemoryPercentage": flat(40), "HttpQueueLength": flat(0)})
    az.advisor = [
        {
            "properties": {
                "category": "Performance",
                "impact": "High",
                "shortDescription": {"problem": "Plan is overloaded"},
                "resourceMetadata": {"resourceId": rid(PLAN_T, "plan1").upper()},
            }
        },
        {
            "properties": {
                "category": "Cost",
                "impact": "High",
                "shortDescription": {"problem": "Buy a reservation"},
                "resourceMetadata": {"resourceId": f"/subscriptions/{SUB}"},
            }
        },
        {"properties": {"category": "Security", "resourceMetadata": {"resourceId": rid(PLAN_T, "plan1")}}},
    ]
    a = run(az, prices)["plan1"]
    assert [(r.category, r.problem) for r in a.advisor] == [("Performance", "Plan is overloaded")]
    assert a.verdict == "under"
    assert "Advisor Performance" in a.reason


def test_advisor_query_asks_for_cost_and_performance_only() -> None:
    az = FakeAzure()
    az.on("Microsoft.Advisor", {"value": []})
    assert fetch_recommendations(az, SUB) == {}
    (call,) = az.calls
    assert "Category%20eq%20%27Cost%27%20or%20Category%20eq%20%27Performance%27" in call


def test_advisor_pages_are_followed() -> None:
    pages: Iterator[dict[str, object]] = iter(
        [
            {"value": [], "nextLink": "https://management.azure.com/next?page=2"},
            {
                "value": [
                    {
                        "properties": {
                            "category": "Cost",
                            "impact": "Low",
                            "shortDescription": {"problem": "p"},
                            "resourceMetadata": {"resourceId": "/X"},
                        }
                    }
                ]
            },
        ]
    )
    az = FakeAzure()
    az.on("management.azure.com", lambda url, body: next(pages))
    assert fetch_recommendations(az, SUB) == {"/x": [AdvisorRec("Cost", "Low", "p")]}


def test_advisor_failure_is_a_warning_not_an_error(prices: PriceClient) -> None:
    az = az_with(plan("plan1"))
    az.routes.insert(0, ("Microsoft.Advisor", AzureError("HTTP 403: no access")))
    report = analyse(ArmClient(az, days=DAYS, now=NOW), prices, SUB, ())
    assert report.warnings and "Advisor is not available" in report.warnings[0]
    assert len(report.assessments) == 1


def test_a_metric_the_sku_lacks_is_retried_one_by_one(prices: PriceClient) -> None:
    az = az_with(plan("plan1"))
    calls: list[str] = []

    def metrics(url: str, body: dict[str, object] | None) -> dict[str, object]:
        names = parse_qs(urlparse(url).query)["metricnames"][0].split(",")
        calls.append(",".join(names))
        if len(names) > 1:
            raise AzureError("GET x -> HTTP 400: metric not supported")
        return payload({"CpuPercentage": flat(8), "MemoryPercentage": flat(23)}, names)

    az.routes.insert(0, ("/metrics?", metrics))
    a = run(az, prices)["plan1"]
    assert calls == [
        "CpuPercentage,MemoryPercentage,HttpQueueLength",
        "CpuPercentage",
        "MemoryPercentage",
        "HttpQueueLength",
    ]
    assert [m.hours for m in a.metrics if m.group] == [HOURS, HOURS]


def test_an_unreadable_resource_becomes_no_data_and_other_errors_do_not_hide_it(prices: PriceClient) -> None:
    az = az_with(plan("plan1"))
    az.routes.insert(0, ("/metrics?", AzureError("GET x -> HTTP 403: no Monitoring Reader")))
    a = run(az, prices)["plan1"]
    assert a.verdict == "no-data"
    assert "403" in a.reason


# ------------------------------------------------------------------------------------------------ report and CLI


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> Azure:
    az = az_with(
        plan("big", "P2v3"),
        plan("small", "P1v3"),
        postgres("hot", "Standard_D2ds_v5", "GeneralPurpose"),
        row("Microsoft.Compute/virtualMachines", "vm1"),
    )
    az.give(PLAN_T, "big", {"CpuPercentage": flat(8), "MemoryPercentage": flat(23), "HttpQueueLength": flat(0)})
    az.give(PLAN_T, "small", {"CpuPercentage": flat(2), "MemoryPercentage": flat(10), "HttpQueueLength": flat(0)})
    az.give(
        PG_T,
        "hot",
        {
            "cpu_percent": flat(97),
            "memory_percent": flat(50),
            "storage_percent": flat(50),
            "active_connections": flat(199),
            "max_connections": flat(200),
        },
    )
    monkeypatch.setattr(cli, "http_transport", lambda *a, **k: az)
    monkeypatch.setattr(cli, "az_current_subscription", lambda: SUB)
    monkeypatch.setattr(
        cli,
        "_client",
        lambda args, currency: PriceClient(currency=currency.upper(), cache_dir=None, fetcher=fake_fetcher),
    )
    return az


def test_json_report_shape(azure: Azure, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["rightsize", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {
        "subscription_id",
        "window",
        "currency",
        "price_source",
        "thresholds",
        "summary",
        "resources",
    } <= data.keys()
    assert {"not_assessed", "not_covered", "warnings", "resource_groups"} <= data.keys()
    assert data["window"]["days"] == 30
    assert data["window"]["granularity"] == "PT1H"
    assert data["window"]["expected_hours"] == 720
    assert data["thresholds"]["over_p95"] == 40
    assert data["summary"]["over"] == 2
    assert data["summary"]["under"] == 1
    assert data["not_covered"] == {"microsoft.compute/virtualmachines": 1, "microsoft.app/managedenvironments": 1}
    first = data["resources"][1]
    assert first["name"] == "big"  # ranked by absolute monthly money, increases and savings alike
    assert {
        "id",
        "type",
        "sku",
        "verdict",
        "reason",
        "coverage",
        "metrics",
        "signals",
        "advisor",
        "proposal",
    } <= first.keys()
    assert {"name", "hours", "expected_hours", "coverage", "p95", "max", "mean", "sizing"} <= first["metrics"][0].keys()
    assert {
        "change",
        "target",
        "current_monthly",
        "target_monthly",
        "monthly_delta",
        "predicted",
        "risk",
        "rollback",
    } <= (first["proposal"].keys())
    assert first["proposal"]["monthly_delta"] < 0
    assert [r["name"] for r in data["resources"]][:3] == ["hot", "big", "small"]


def test_only_and_min_saving_filter_the_report(azure: Azure, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["rightsize", "--format", "json", "--only", "under"]) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)["resources"]] == ["hot"]
    assert cli.main(["rightsize", "--format", "json", "--only", "over", "--min-saving", "100"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in data["resources"]] == ["big"]  # `small` saves less than 100
    assert data["summary"]["hidden_below_min_saving"] == 1
    assert data["summary"]["over"] == 2  # the summary still counts everything


def test_text_and_markdown_show_the_evidence(azure: Azure, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["rightsize"]) == 0
    text = capsys.readouterr().out
    assert "window 30 d ending 2026" in text
    assert "720/720 h (100%)" in text
    assert "P2v3 -> P1v3" in text
    assert "predicted after the change" in text
    assert "rollback: az appservice plan update" in text
    assert "NOT COVERED" in text and "microsoft.compute/virtualmachines" in text
    assert cli.main(["rightsize", "--format", "markdown"]) == 0
    md = capsys.readouterr().out
    assert "### Proposals" in md and "| 2 | over |" in md


def test_fail_on_exits_with_3_and_default_is_always_0(azure: Azure) -> None:
    assert cli.main(["rightsize"]) == 0
    assert cli.main(["rightsize", "--fail-on", "under"]) == 3
    assert cli.main(["rightsize", "--fail-on", "over"]) == 3


def test_window_must_be_between_14_and_90_days(azure: Azure, capsys: pytest.CaptureFixture[str]) -> None:
    for days in ("13", "91"):
        with pytest.raises(SystemExit):
            cli.main(["rightsize", "--days", days])
        assert "between 14 and 90" in capsys.readouterr().err


def test_rightsize_never_writes_to_azure(azure: Azure) -> None:
    cli.main(["rightsize", "--format", "json"])
    assert {c.split(" ", 1)[0] for c in azure.calls} <= {"GET", "POST"}
    assert all("Microsoft.ResourceGraph" in c for c in azure.calls if c.startswith("POST"))


def test_select_ranks_proposals_by_absolute_money(prices: PriceClient) -> None:
    az = az_with(plan("small", "P1v3"), plan("big", "P2v3"))
    low = {"CpuPercentage": flat(2), "MemoryPercentage": flat(10), "HttpQueueLength": flat(0)}
    az.give(PLAN_T, "small", low)
    az.give(PLAN_T, "big", low)
    report = analyse(ArmClient(az, days=DAYS, now=NOW), prices, SUB, ())
    report.price_source = "test"
    names = [a.name for a in select(report).shown]
    assert names == ["big", "small"]
    assert "big" in render_text(report, select(report))
    assert json.loads(render_json(report, select(report)))["resources"][0]["name"] == "big"
    assert "`big`" in render_markdown(report, select(report))
