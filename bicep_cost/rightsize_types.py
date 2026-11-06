"""Per resource type: which Azure Monitor metrics to read and which same-family sizes to compare.

Every handler turns the live resource plus its hourly metrics into a `Case` for `rightsize.decide`. A handler never
guesses: when a size, a price or a ladder is not known it offers no candidate.
"""

import copy
import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise

from bicep_cost.advisor import AdvisorRec
from bicep_cost.armexpr import JsonValue
from bicep_cost.calc import estimate_resources
from bicep_cost.context import HOURS_PER_MONTH, ResourceIndex, UnresolvedError, as_number
from bicep_cost.engine import Resource
from bicep_cost.estimators.aca import parse_memory_gib
from bicep_cost.estimators.appservice import parse_plan_sku
from bicep_cost.estimators.sql import parse_sku as parse_sql_sku
from bicep_cost.prices import PriceClient, PriceError
from bicep_cost.rightsize import (
    HOT_SHARE,
    MIN_COVERAGE,
    PINNED_SHARE,
    UNDER_MAX,
    Candidate,
    Case,
    Signal,
    Stats,
    build_stats,
)
from bicep_cost.usage import UsageConfig

Metrics = dict[str, dict[str, list[float | None]]]
MIN_RUNNING_HOURS = 168  # a scale-to-zero app must have run a week in the window to be judged
QUEUE_LIMIT = 10.0  # requests waiting in an App Service plan's HTTP queue
CONNECTION_LIMIT = 90.0  # percent of max_connections


@dataclass(frozen=True)
class Env:
    days: int
    prices: PriceClient
    region: str
    advisor: dict[str, list[AdvisorRec]]
    autoscaled: frozenset[str]  # lower-cased ids of resources with an enabled autoscale setting
    index: ResourceIndex

    @property
    def expected(self) -> int:
        return self.days * 24


@dataclass(frozen=True)
class Handler:
    skip: Callable[[Resource], str | None]  # reason the resource is not assessed at all
    names: Callable[[Resource], list[str]]  # metrics to read
    case: Callable[[Resource, Env, Metrics], Case]


# ---------------------------------------------------------------------------------------------------- pricing helpers


def _step(node: JsonValue, part: str) -> JsonValue:
    if isinstance(node, dict):
        return node.get(part)
    if isinstance(node, list) and part.isdigit() and int(part) < len(node):
        return node[int(part)]
    return None


def with_value(res: Resource, path: str, value: JsonValue) -> Resource:
    """A copy of the resource with one value changed (dotted path, list indexes as numbers)."""
    body = copy.deepcopy(res.body)
    *parents, last = path.split(".")
    node: JsonValue = body
    for part in parents:
        node = _step(node, part)
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list) and last.isdigit():
        node[int(last)] = value
    return dataclasses.replace(res, body=body)


def lazy(fn: Callable[[], float | None]) -> Callable[[], float | None]:
    box: list[float | None] = []

    def get() -> float | None:
        if not box:
            box.append(fn())
        return box[0]

    return get


def list_price(res: Resource, env: Env, usage: dict[str, object] | None = None) -> float | None:
    """Monthly list price of the resource as configured, or None when it cannot be priced.

    Free grants and tier ladders are ignored on purpose (every meter is priced at its headline unit price): they are
    shared across the subscription, so what a change saves is the plain price of the units it removes.
    """
    try:
        est = estimate_resources([res], env.prices, UsageConfig(usage or {}), default_region=env.region)
    except (PriceError, ValueError):
        return None
    (first,) = est.resources
    if first.status == "free":
        return 0.0
    if first.status != "priced" or not first.items:
        return None
    total = 0.0
    for item in first.items:
        if item.fixed_amount is not None:
            total += item.fixed_amount
        elif item.meter is not None:
            total += item.meter.unit_price * item.quantity  # headline price: no free tier, no ladder
        else:
            return None
    return total


def _stat(
    data: Metrics,
    name: str,
    expected: int,
    *,
    group: str = "",
    trigger: bool = False,
    unit: str = "%",
    required: bool = False,
) -> Stats:
    d = data.get(name.lower(), {})
    return build_stats(
        name,
        d.get("maximum", []),
        d.get("average"),
        expected,
        unit=unit,
        group=group,
        trigger=trigger,
        required=required,
    )


def _signal_if_hot(stats: Stats, name: str, label: str) -> list[Signal]:
    if stats.hours and stats.hot:
        return [Signal(name, f"{label} at its limit (p95 {stats.p95:.0f}%, max {stats.max:.0f}%)")]
    return []


def _hours_at_least(stats: Stats, limit: float) -> int:
    return sum(1 for v in stats.values if v >= limit)


def _sustained(hours: int, stats: Stats) -> bool:
    """Whether ``hours`` is a meaningful share of the *expected* hours (a few stray points never are)."""
    return hours / max(stats.expected, 1) >= HOT_SHARE


def _label(value: float) -> str:
    return f"{value:g}"


# ---------------------------------------------------------------------------------------------------- App Service plans

# family -> ((SKU, vCPU, GiB), ...) smallest first; steps only ever stay inside one family
PLAN_FAMILIES: dict[str, tuple[tuple[str, float, float], ...]] = {
    "Pv3": (("P0v3", 1, 4), ("P1v3", 2, 8), ("P2v3", 4, 16), ("P3v3", 8, 32)),
    "Pmv3": (("P1mv3", 2, 16), ("P2mv3", 4, 32), ("P3mv3", 8, 64), ("P4mv3", 16, 128), ("P5mv3", 32, 256)),
    "Pv2": (("P1v2", 1, 3.5), ("P2v2", 2, 7), ("P3v2", 4, 14)),
    "Standard": (("S1", 1, 1.75), ("S2", 2, 3.5), ("S3", 4, 7)),
    "Basic": (("B1", 1, 1.75), ("B2", 2, 3.5), ("B3", 4, 7)),
}
PLAN_NOT_ASSESSED = {
    "free": "Free plan",
    "shared": "Shared plan",
    "consumption": "Consumption plan: billed per execution, there is no size to change",
    "flex": "Flex Consumption plan: billed per execution, there is no size to change",
    "isolated": "Isolated (App Service Environment) plans are not covered",
}


def _plan_sku(res: Resource) -> str:
    return str(res.sku.get("name") or "")


def plan_skip(res: Resource) -> str | None:
    try:
        kind = parse_plan_sku(_plan_sku(res)).kind
    except UnresolvedError:
        return f"unrecognised App Service plan SKU {_plan_sku(res)!r}"
    return PLAN_NOT_ASSESSED.get(kind)


def _family_of(sku: str, families: dict[str, tuple[tuple[str, float, float], ...]]) -> tuple[str, int] | None:
    for family, rungs in families.items():
        for i, rung in enumerate(rungs):
            if rung[0].lower() == sku.lower():
                return family, i
    return None


def _plan_candidates(
    res: Resource, env: Env, instances: int, zone_redundant: bool
) -> tuple[list[Candidate], list[Candidate]]:
    sku = _plan_sku(res)
    found = _family_of(sku, PLAN_FAMILIES)
    group = f"-g {res.resource_group} -n {res.name}"
    down: list[Candidate] = []
    up: list[Candidate] = []

    def sku_candidate(new: tuple[str, float, float], cur: tuple[str, float, float], risk: str) -> Candidate:
        return Candidate(
            f"{cur[0]} -> {new[0]}",
            f"{new[0]} x{instances}",
            {"cpu": cur[1] / new[1], "memory": cur[2] / new[2]},
            lazy(lambda: list_price(with_value(res, "sku.name", new[0]), env)),
            risk,
            f"az appservice plan update {group} --sku {cur[0].upper()}",
        )

    if found is not None:
        family, i = found
        rungs = PLAN_FAMILIES[family]
        low = "low: the plan's workers restart while it scales (a short disruption); check memory-heavy apps afterwards"
        if i > 0:
            down.append(sku_candidate(rungs[i - 1], rungs[i], low))
        up += [
            sku_candidate(rungs[j], rungs[i], "low: the plan's workers restart while it scales")
            for j in range(i + 1, len(rungs))
        ]
    smallest = 2 if zone_redundant else 1
    if instances - 1 >= smallest and instances >= 2:
        ratio = instances / (instances - 1)
        down.append(
            Candidate(
                f"{instances} -> {instances - 1} instances",
                f"{sku} x{instances - 1}",
                {"cpu": ratio, "memory": ratio},
                lazy(lambda: list_price(with_value(res, "sku.capacity", instances - 1), env)),
                "low: no restart, but one instance less to absorb a peak or a failure",
                f"az appservice plan update {group} --number-of-workers {instances}",
            )
        )
    return down, up


def plan_case(res: Resource, env: Env, data: Metrics) -> Case:
    sku = _plan_sku(res)
    instances = int(as_number(res.sku.get("capacity"), 1))
    zone_redundant = res.get("properties.zoneRedundant") is True
    cpu = _stat(data, "CpuPercentage", env.expected, group="cpu", trigger=True)
    mem = _stat(data, "MemoryPercentage", env.expected, group="memory", trigger=True)
    queue = _stat(data, "HttpQueueLength", env.expected, unit="", required=True)
    signals: list[Signal] = []
    if _sustained(_hours_at_least(queue, QUEUE_LIMIT), queue):
        signals.append(
            Signal(
                "queue", f"HTTP queue of at least {QUEUE_LIMIT:g} requests in {_hours_at_least(queue, QUEUE_LIMIT)} h"
            )
        )
    kind = parse_plan_sku(sku).kind
    exempt: str | None = None
    if res.id.lower() in env.autoscaled:
        exempt = "an autoscale setting already manages the instance count"
    elif kind in ("functions-premium", "logic-standard") or res.get("properties.elasticScaleEnabled") is True:
        exempt = "already elastic (elastic scale / Premium Functions / Logic Apps Standard)"
    down, up = _plan_candidates(res, env, instances, zone_redundant)
    notes = [f"{instances} instance(s)" + (", zone redundant" if zone_redundant else "")]
    return Case(
        res.id,
        res.name,
        res.type,
        res.resource_group,
        f"{sku} x{instances}",
        env.days,
        [cpu, mem, queue],
        lazy(lambda: list_price(res, env)),
        down,
        up,
        signals,
        env.advisor.get(res.id.lower(), []),
        notes,
        exempt,
        "already the smallest size of its family and instance count",
        "already the largest size of its family",
    )


# ---------------------------------------------------------------------------------------------------- PostgreSQL

PG_BURSTABLE = (
    ("Standard_B1ms", 1, 2),
    ("Standard_B2s", 2, 4),
    ("Standard_B2ms", 2, 8),
    ("Standard_B4ms", 4, 16),
    ("Standard_B8ms", 8, 32),
    ("Standard_B12ms", 12, 48),
    ("Standard_B16ms", 16, 64),
    ("Standard_B20ms", 20, 80),
)
PG_VCORES = (2, 4, 8, 16, 32, 48, 64, 96)
_PG_SKU = re.compile(r"Standard_([DE])(\d+)([a-z]*)_(v\d+)", re.IGNORECASE)
PG_TIERS = {"generalpurpose": "GeneralPurpose", "memoryoptimized": "MemoryOptimized", "burstable": "Burstable"}


def pg_skip(res: Resource) -> str | None:
    role = str(res.get("properties.replicationRole") or "Primary")
    return (
        None if role.lower() in ("primary", "none", "") else f"read replica ({role}): sized by its primary's workload"
    )


def pg_names(res: Resource) -> list[str]:
    names = ["cpu_percent", "memory_percent", "storage_percent", "active_connections", "max_connections"]
    return [*names, "cpu_credits_remaining"] if _pg_burstable(res) else names


def _pg_burstable(res: Resource) -> bool:
    return str(res.sku.get("tier") or "").lower() == "burstable"


def _pg_ladder(sku: str) -> tuple[tuple[str, float, float], ...]:
    if m := _PG_SKU.fullmatch(sku):
        letter, feat, version = m.group(1).upper(), m.group(3).lower(), m.group(4).lower()
        gib = 4.0 if letter == "D" else 8.0
        return tuple((f"Standard_{letter}{n}{feat}_{version}", float(n), n * gib) for n in PG_VCORES)
    return PG_BURSTABLE if any(sku.lower() == b[0].lower() for b in PG_BURSTABLE) else ()


def _pg_candidates(res: Resource, env: Env) -> tuple[list[Candidate], list[Candidate]]:
    sku = str(res.sku.get("name") or "")
    ladder = _pg_ladder(sku)
    index = next((i for i, r in enumerate(ladder) if r[0].lower() == sku.lower()), None)
    if index is None:
        return [], []
    tier = PG_TIERS.get(str(res.sku.get("tier") or "").lower(), "")
    ha = str(res.get("properties.highAvailability.mode") or "Disabled").lower() != "disabled"
    risk = (
        "medium: changing the compute size restarts the server (minutes of downtime"
        + ("; with HA a failover" if ha else "")
        + ")"
    )

    def make(j: int) -> Candidate:
        cur, new = ladder[index], ladder[j]
        return Candidate(
            f"{cur[0].removeprefix('Standard_')} -> {new[0].removeprefix('Standard_')}",
            new[0].removeprefix("Standard_"),
            {"cpu": cur[1] / new[1], "memory": cur[2] / new[2]},
            lazy(lambda: list_price(with_value(res, "sku.name", new[0]), env)),
            risk,
            f"az postgres flexible-server update -g {res.resource_group} -n {res.name} "
            f"--sku-name {cur[0]} --tier {tier}",
        )

    down = [make(index - 1)] if index > 0 else []
    up = [make(j) for j in range(index + 1, len(ladder))]
    return down, up


def pg_case(res: Resource, env: Env, data: Metrics) -> Case:
    sku = str(res.sku.get("name") or "")
    cpu = _stat(data, "cpu_percent", env.expected, group="cpu", trigger=True)
    mem = _stat(data, "memory_percent", env.expected, group="memory", trigger=True)
    storage = _stat(data, "storage_percent", env.expected)
    active = data.get("active_connections", {}).get("maximum", [])
    limit = data.get("max_connections", {}).get("maximum", [])
    ratio = [100 * a / m if a is not None and m else None for a, m in zip(active, limit, strict=False)]
    conn = build_stats("connections of max_connections", ratio, None, env.expected, required=True)
    signals = []
    if _sustained(_hours_at_least(conn, CONNECTION_LIMIT), conn):
        signals.append(
            Signal(
                "connections",
                f"connections at {CONNECTION_LIMIT:.0f}% of max_connections in "
                f"{_hours_at_least(conn, CONNECTION_LIMIT)} h",
            )
        )
    notes: list[str] = []
    metrics = [cpu, mem, storage, conn]
    if _pg_burstable(res):
        remaining = _stat(data, "cpu_credits_remaining", env.expected, unit="")
        low = build_stats(remaining.name, data.get("cpu_credits_remaining", {}).get("minimum", []), None, env.expected)
        empty = sum(1 for v in low.values if v <= 1.0)
        if _sustained(empty, low):
            signals.append(Signal("credits", f"CPU credits exhausted in {empty} h"))
    if storage.hours and storage.max >= 90:
        notes.append(f"storage is {storage.max:.0f}% full at its peak; that is a disk size question, not a SKU one")
    if str(res.get("properties.highAvailability.mode") or "Disabled").lower() != "disabled":
        notes.append("high availability is on: the standby is resized together with the primary")
    exempt = "Burstable is already elastic (CPU credits)" if _pg_burstable(res) else None
    down, up = _pg_candidates(res, env)
    return Case(
        res.id,
        res.name,
        res.type,
        res.resource_group,
        sku.removeprefix("Standard_"),
        env.days,
        metrics,
        lazy(lambda: list_price(res, env)),
        down,
        up,
        signals,
        env.advisor.get(res.id.lower(), []),
        notes,
        exempt,
        "already the smallest size of its family (switching family is not proposed)",
        "already the largest size of its family",
    )


# ---------------------------------------------------------------------------------------------------- Azure SQL

# DTU level -> (DTUs, max database size in GB)
SQL_DTU_LEVELS: dict[str, tuple[tuple[str, int, int], ...]] = {
    "Standard": (
        ("S0", 10, 250),
        ("S1", 20, 250),
        ("S2", 50, 250),
        ("S3", 100, 1024),
        ("S4", 200, 1024),
        ("S6", 400, 1024),
        ("S7", 800, 1024),
        ("S9", 1600, 1024),
        ("S12", 3000, 1024),
    ),
    "Premium": (
        ("P1", 125, 1024),
        ("P2", 250, 1024),
        ("P4", 500, 1024),
        ("P6", 1000, 1024),
        ("P11", 1750, 4096),
        ("P15", 4000, 4096),
    ),
}
SQL_GEN5_VCORES = (2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24, 32, 40, 80)
# Every provisioned Gen5 size holds at least 1 TB; larger limits depend on the size and tier, so a smaller size is not
# proposed for a database configured above this.
SQL_VCORE_SAFE_GB = 1024
GIB = 2**30


def sql_skip(res: Resource) -> str | None:
    kind = str(res.kind or "").lower()
    if "system" in kind or str(res.sku.get("name") or "").upper().endswith("_SYSTEM"):
        return "system database"
    if res.get("properties.elasticPoolId") not in (None, ""):
        return "member of an elastic pool (the pool is not covered)"
    secondary = str(res.get("properties.secondaryType") or "")
    if secondary or str(res.get("properties.createMode") or "").lower() in ("secondary", "onlinesecondary"):
        return f"geo / named replica ({secondary or 'secondary'}): sized for its primary's replication workload"
    try:
        sku = parse_sql_sku(str(res.sku.get("name") or ""), int(as_number(res.sku.get("capacity"), 0)))
    except UnresolvedError as exc:
        return str(exc)
    if sku.model in ("free", "unsupported", "pool-member") or res.get("properties.useFreeLimit") is True:
        return "free offer, data warehouse or pool member"
    if sku.model == "dtu" and sku.tier == "Basic":
        return None
    if sku.model == "vcore" and sku.tier == "Hyperscale":
        return "Hyperscale is not covered"
    return None


def _sql_dtu(res: Resource) -> bool:
    return parse_sql_sku(str(res.sku.get("name") or ""), int(as_number(res.sku.get("capacity"), 0))).model == "dtu"


def sql_names(res: Resource) -> list[str]:
    names = ["workers_percent", "physical_data_read_percent", "log_write_percent", "storage_percent"]
    return ["dtu_consumption_percent", "cpu_percent", *names] if _sql_dtu(res) else ["cpu_percent", *names]


def _sql_server_name(res: Resource) -> tuple[str, str]:
    server, _, db = res.name.partition("/")
    return server, db


def _sql_candidates(
    res: Resource, env: Env, sku_name: str, capacity: int
) -> tuple[list[Candidate], list[Candidate], str | None]:
    """(smaller, larger, why there are none)."""
    parsed = parse_sql_sku(sku_name, capacity)
    server, db = _sql_server_name(res)
    max_gb = as_number(res.get("properties.maxSizeBytes"), 0.0) / GIB
    risk = "low: the database stays online; connections are dropped briefly when the change completes"
    if parsed.model == "dtu":
        levels = SQL_DTU_LEVELS.get(parsed.tier)
        index = next((i for i, lv in enumerate(levels or ()) if lv[0] == parsed.dtu_label), None)
        if levels is None or index is None:
            return [], [], "DTU level has no known ladder"
        current = levels[index]

        def dtu(j: int) -> Candidate | None:
            new = levels[j]
            if max_gb > new[2]:
                return None  # the configured maximum size would not fit the smaller level
            return Candidate(
                f"{current[0]} -> {new[0]}",
                f"{new[0]} ({new[1]} DTU)",
                {"dtu": current[1] / new[1]},
                lazy(lambda: list_price(with_value(res, "sku.capacity", new[1]), env)),
                risk,
                f"az sql db update -g {res.resource_group} -s {server} -n {db} --service-objective {current[0]}",
            )

        down = [c for c in [dtu(index - 1)] if c] if index > 0 else []
        up = [c for j in range(index + 1, len(levels)) if (c := dtu(j))]
        return down, up, None
    if parsed.serverless:
        return [], [], "serverless: billed by use, so the cost does not follow the configured maximum"
    if parsed.family.lower() != "gen5" or parsed.tier not in ("GeneralPurpose", "BusinessCritical"):
        return [], [], f"no known size ladder for {parsed.tier} {parsed.family}"
    if parsed.vcores not in SQL_GEN5_VCORES:
        return [], [], f"{parsed.vcores} vCores is not a known size"
    index = SQL_GEN5_VCORES.index(parsed.vcores)

    def vcore(j: int) -> Candidate | None:
        new = SQL_GEN5_VCORES[j]
        if new < parsed.vcores and max_gb > SQL_VCORE_SAFE_GB:
            return None
        ratio = parsed.vcores / new
        # CPU and worker threads scale with the vCores. The IO and log rate limits do not scale linearly (some sizes
        # share one limit): a smaller size is judged with the linear ratio, which overstates its load, a larger size
        # is assumed to bring no IO relief at all.
        return Candidate(
            f"{parsed.vcores} -> {new} vCores",
            f"{new} vCores",
            {"cpu": ratio, "io": max(ratio, 1.0)},
            lazy(lambda: list_price(with_value(res, "sku.capacity", new), env)),
            risk,
            f"az sql db update -g {res.resource_group} -s {server} -n {db} --capacity {parsed.vcores}",
        )

    down = [c for c in [vcore(index - 1)] if c] if index > 0 else []
    up = [c for j in range(index + 1, len(SQL_GEN5_VCORES)) if (c := vcore(j))]
    return down, up, None


def sql_case(res: Resource, env: Env, data: Metrics) -> Case:
    sku_name = str(res.sku.get("name") or "")
    capacity = int(as_number(res.sku.get("capacity"), 0))
    parsed = parse_sql_sku(sku_name, capacity)
    group = "dtu" if parsed.model == "dtu" else "cpu"
    io_group = "dtu" if parsed.model == "dtu" else "io"
    workers = _stat(data, "workers_percent", env.expected, group=group)
    storage = _stat(data, "storage_percent", env.expected)
    cpu = _stat(data, "cpu_percent", env.expected, group=group, trigger=parsed.model != "dtu")
    data_io = _stat(data, "physical_data_read_percent", env.expected, group=io_group)
    log_io = _stat(data, "log_write_percent", env.expected, group=io_group)
    signals = _signal_if_hot(workers, "workers", "worker threads")
    notes: list[str] = []
    if parsed.model == "dtu":
        # DTU is the highest of CPU, data IO and log IO: a component at its limit *is* the DTU at its limit, not a
        # second signal. It is shown, so the report says what saturates.
        dtu = _stat(data, "dtu_consumption_percent", env.expected, group="dtu", trigger=True)
        metrics = [dtu, cpu, data_io, log_io, workers, storage]
        label = f"{parsed.dtu_label} ({capacity} DTU)"
        for part, what in ((cpu, "CPU"), (data_io, "data IO"), (log_io, "log IO")):
            if part.hours and part.hot:
                notes.append(f"{what} is at or above {UNDER_MAX:.0f}% in {_hours_at_least(part, UNDER_MAX)} h")
        if notes:
            notes = ["the DTU is the highest of CPU, data IO and log IO; " + "; ".join(notes)]
    else:
        metrics = [cpu, workers, data_io, log_io, storage]
        label = f"{sku_name} {capacity} vCores" + (" (serverless)" if parsed.serverless else "")
        notes.append("memory is not judged: SQL Server fills its buffer pool whatever the load")
    down, up, why = _sql_candidates(res, env, sku_name, capacity)
    exempt = "serverless is already elastic (billed by use)" if parsed.serverless else None
    return Case(
        res.id,
        res.name,
        res.type,
        res.resource_group,
        label,
        env.days,
        metrics,
        lazy(lambda: list_price(res, env)),
        down,
        up,
        signals,
        env.advisor.get(res.id.lower(), []),
        notes,
        exempt,
        why or "already the smallest size of its family that holds the configured maximum size",
        why or "already the largest size of its family",
    )


# ---------------------------------------------------------------------------------------------------- Container Apps

# Consumption workload profile: vCPU in quarter steps up to 4, memory twice that in GiB
ACA_CPUS = tuple(n / 4 for n in range(1, 17))


def aca_names(res: Resource) -> list[str]:
    return ["Replicas", "CpuPercentage", "MemoryPercentage", "RestartCount"]


def _containers(res: Resource) -> list[dict[str, JsonValue]]:
    raw = res.get("properties.template.containers", [])
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


def _aca_zone_redundant(res: Resource, env: Env) -> bool:
    """True when the environment is zone redundant or not known (the conservative answer)."""
    environment = env.index.find(res.get("properties.environmentId") or res.get("properties.managedEnvironmentId"))
    return environment is None or environment.get("properties.zoneRedundant") is True


def _aca_max_cpu(res: Resource, env: Env) -> float:
    """Largest Consumption size: 4 vCPU on workload-profile environments, 2 on legacy (or unknown) ones."""
    environment = env.index.find(res.get("properties.environmentId") or res.get("properties.managedEnvironmentId"))
    profiles = environment.get("properties.workloadProfiles") if environment is not None else None
    return 4.0 if isinstance(profiles, list) and profiles else 2.0


def _restart_hours(series: list[float | None]) -> int:
    """Hours in which the cumulative RestartCount went up.

    A drop is a replaced replica (a new baseline), not a restart: replica-level counters are not available here.
    """
    return sum(1 for before, now in pairwise(series) if before is not None and now is not None and now > before)


def _aca_state(res: Resource) -> str:
    """Why an app without replicas has no telemetry, when the resource itself says so."""
    running = str(res.get("properties.runningStatus") or "")
    provisioning = str(res.get("properties.provisioningState") or "")
    why = [
        text
        for ok, text in (
            (running.lower() == "stopped", "the app is stopped (runningStatus Stopped), so no replica can run"),
            (provisioning.lower() == "failed", "its provisioning failed (provisioningState Failed)"),
        )
        if ok
    ]
    return f": {'; '.join(why)}" if why else ""


def _aca_candidates(
    res: Resource, env: Env, cpu: float, gib: float, replica_hours: float, min_r: int, observed_max: float
) -> tuple[list[Candidate], list[Candidate], str | None]:
    profile = res.get("properties.workloadProfileName")
    if isinstance(profile, str) and profile.lower() != "consumption":
        return [], [], f"runs on workload profile '{profile}', which is billed on the environment"
    containers = _containers(res)
    if len(containers) != 1:
        return [], [], "several containers: how to split a smaller size between them is not known"
    if cpu not in ACA_CPUS or abs(gib - 2 * cpu) > 1e-9:
        return [], [], f"{cpu:g} vCPU / {gib:g} GiB is not a Consumption size"
    name = res.name
    group = f"-g {res.resource_group} -n {name}"

    def usage(hours: float) -> dict[str, object]:
        return {"containerapps": {name: {"replica_hours": hours}}}

    def size(new: float) -> Candidate:
        def edit() -> Resource:
            return with_value(
                with_value(res, "properties.template.containers.0.resources.cpu", new),
                "properties.template.containers.0.resources.memory",
                f"{2 * new:g}Gi",
            )

        return Candidate(
            f"{cpu:g} vCPU / {gib:g} GiB -> {new:g} vCPU / {2 * new:g} GiB",
            f"{new:g} vCPU / {2 * new:g} GiB",
            {"cpu": cpu / new, "memory": gib / (2 * new)},
            lazy(lambda: list_price(edit(), env, usage(replica_hours))),
            "low: creates a new revision; a replica that needs more memory than the new size gets OOM-killed",
            f"az containerapp update {group} --cpu {cpu:g} --memory {gib:g}Gi",
        )

    i = ACA_CPUS.index(cpu)
    down = [size(ACA_CPUS[i - 1])] if i > 0 else []
    up = [size(c) for c in ACA_CPUS[i + 1 :] if c <= _aca_max_cpu(res, env)]
    smallest = 2 if _aca_zone_redundant(res, env) else 1
    if min_r - 1 >= smallest and min_r >= 2 and observed_max <= min_r:
        ratio = min_r / (min_r - 1)
        down.append(
            Candidate(
                f"min replicas {min_r} -> {min_r - 1}",
                f"min replicas {min_r - 1}",
                {"cpu": ratio, "memory": ratio},
                lazy(lambda: list_price(res, env, usage(replica_hours * (min_r - 1) / min_r))),
                "low: one replica less to absorb a peak; the autoscaler still adds replicas on demand",
                f"az containerapp update {group} --min-replicas {min_r}",
            )
        )
    return down, up, None


def _aligned(data: Metrics, name: str, at: list[int], *, group: str, span: int) -> Stats:
    """The metric's points at the given hourly positions; ``expected`` is the number of positions."""
    d = data.get(name.lower(), {})

    def pick(values: list[float | None] | None) -> list[float | None]:
        return [values[i] if values is not None and i < len(values) else None for i in at]

    return build_stats(
        name,
        pick(d.get("maximum")),
        pick(d.get("average")),
        len(at),
        group=group,
        trigger=True,
        span=span,
        positions=False,
    )


def _readings_while_replicas_unknown(data: Metrics) -> bool:
    unknown = [i for i, v in enumerate(data.get("replicas", {}).get("maximum", [])) if v is None]
    for name in ("cpupercentage", "memorypercentage"):
        series = data.get(name, {}).get("maximum", [])
        if any(i < len(series) and series[i] is not None for i in unknown):
            return True
    return False


def aca_case(res: Resource, env: Env, data: Metrics) -> Case:
    replicas = _stat(data, "Replicas", env.expected, unit="")
    # CPU and memory only report while a replica runs: judge them on those hours, aligned with the Replicas series
    running_at = [i for i, v in enumerate(data.get("replicas", {}).get("maximum", [])) if v is not None and v >= 1.0]
    running = len(running_at)
    cpu = _aligned(data, "CpuPercentage", running_at, group="cpu", span=env.expected)
    mem = _aligned(data, "MemoryPercentage", running_at, group="memory", span=env.expected)
    restarts = _stat(data, "RestartCount", env.expected, unit="", required=True)
    min_r = int(as_number(res.get("properties.template.scale.minReplicas"), 0))
    max_r = int(as_number(res.get("properties.template.scale.maxReplicas"), 10))
    signals: list[Signal] = []
    restart_hours = _restart_hours(data.get("restartcount", {}).get("maximum", []))
    if restart_hours:
        signals.append(Signal("restarts", f"container restarts in {restart_hours} h (RestartCount went up)"))
    pinned = _hours_at_least(replicas, max_r) / max(replicas.expected, 1)
    if (
        max_r >= 2 and max_r > min_r and pinned >= PINNED_SHARE
    ):  # with one replica at most, "at the maximum" is just "up"
        signals.append(Signal("pinned", f"replicas at the maximum of {max_r} in {pinned:.0%} of the hours"))
    cpus, gib = _size(res)
    replica_hours = replicas.mean * HOURS_PER_MONTH
    down, up, why = _aca_candidates(res, env, cpus, gib, replica_hours, min_r, replicas.max)
    notes = [
        f"configured {min_r}-{max_r} replicas, observed {min(replicas.values, default=0):g}-{replicas.max:g} "
        f"(mean {replicas.mean:.1f}); {cpus:g} vCPU / {gib:g} GiB per replica",
        f"ran in {running} of {replicas.hours} h",
    ]
    insufficient = None
    state = _aca_state(res)
    if replicas.hours == 0:
        insufficient = f"the Replicas metric returned no data{state}"
    elif replicas.coverage < MIN_COVERAGE:
        insufficient = (
            f"the Replicas metric has only {replicas.hours} of {replicas.expected} hourly points: "
            "scale-to-zero hours cannot be told from missing data"
        )
    elif _readings_while_replicas_unknown(data):
        insufficient = "CPU / memory readings exist for hours in which the replica count is unknown"
    elif (
        sum(1 for v in data.get("replicas", {}).get("average", []) if v is not None) < MIN_COVERAGE * replicas.expected
    ):
        insufficient = "the Replicas average has too few hourly points to price the replica hours"
    elif running == 0:
        insufficient = f"no replica ran in the whole window{state or ' (scaled to zero, stopped or not reporting)'}"
    elif running < MIN_RUNNING_HOURS:
        insufficient = f"ran in only {running} h of the window (needs {MIN_RUNNING_HOURS} h to judge)"
    return Case(
        res.id,
        res.name,
        res.type,
        res.resource_group,
        f"{cpus:g} vCPU / {gib:g} GiB, {min_r}-{max_r} replicas",
        env.days,
        [cpu, mem, replicas, restarts],
        lazy(lambda: list_price(res, env, {"containerapps": {res.name: {"replica_hours": replica_hours}}})),
        down,
        up,
        signals,
        env.advisor.get(res.id.lower(), []),
        notes,
        why,
        why or "already the smallest Consumption size and replica count",
        why or "already the largest Consumption size",
        insufficient,
    )


def _size(res: Resource) -> tuple[float, float]:
    cpu = gib = 0.0
    for c in _containers(res):
        r = c.get("resources")
        if isinstance(r, dict):
            cpu += as_number(r.get("cpu"), 0.0)
            try:
                gib += parse_memory_gib(r.get("memory", 0))
            except UnresolvedError:
                return 0.0, 0.0
    return cpu, gib


HANDLERS: dict[str, Handler] = {
    "microsoft.web/serverfarms": Handler(
        plan_skip, lambda r: ["CpuPercentage", "MemoryPercentage", "HttpQueueLength"], plan_case
    ),
    "microsoft.dbforpostgresql/flexibleservers": Handler(pg_skip, pg_names, pg_case),
    "microsoft.sql/servers/databases": Handler(sql_skip, sql_names, sql_case),
    "microsoft.app/containerapps": Handler(lambda r: None, aca_names, aca_case),
}
