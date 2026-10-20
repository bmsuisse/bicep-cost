"""Azure Container Apps: environments (dedicated workload profiles), apps and dynamic session pools."""

import re

from ..armexpr import JsonValue
from ..context import HOURS_PER_MONTH, SECONDS_PER_MONTH, EstimateContext, Unresolved, as_number, need_str
from ..engine import Resource
from ..model import LineItem, ResourceEstimate
from ..usage import UsageError
from .common import included, line, priced, usage_amount
from .cron import active_hours_per_month, extra_replica_hours_per_month

# Monthly free grants per subscription (not modelled by the retail price API).
FREE_VCPU_SECONDS = 180_000.0
FREE_GIB_SECONDS = 360_000.0
FREE_REQUESTS_MILLIONS = 2.0

# (vCPU, GiB) per node of a dedicated workload profile.
PROFILE_SIZES: dict[str, tuple[float, float]] = {
    "D4": (4, 16),
    "D8": (8, 32),
    "D16": (16, 64),
    "D32": (32, 128),
    "E4": (4, 32),
    "E8": (8, 64),
    "E16": (16, 128),
    "E32": (32, 256),
}


def parse_memory_gib(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        m = re.fullmatch(r"\s*([\d.]+)\s*(Gi|G|Mi|M|)\s*", value)
        if m:
            amount = float(m.group(1))
            return amount / 1024 if m.group(2) in ("Mi", "M") else amount
    raise Unresolved(f"cannot read container memory {value!r}")


def estimate_environment(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    profiles = res.get("properties.workloadProfiles", [])
    if not isinstance(profiles, list):
        profiles = []
    region = ctx.region(res)
    items: list[LineItem] = []
    notes: list[str] = []
    has_dedicated = False
    for p in profiles:
        if not isinstance(p, dict):
            continue
        ptype = str(p.get("workloadProfileType", "Consumption"))
        if ptype.lower().startswith("consumption"):
            continue
        has_dedicated = True
        nodes = ctx.usage.number(
            "containerapp_environments", res.name, f"{p.get('name')}_nodes", as_number(p.get("minimumCount"), 1)
        )
        if ptype.upper() not in PROFILE_SIZES:
            notes.append(f"workload profile {p.get('name')} ({ptype}) is not priced")
            continue
        vcpu, gib = PROFILE_SIZES[ptype.upper()]
        cpu_m = ctx.meter("Azure Container Apps", region, sku="Dedicated", meter="Dedicated vCPU Usage")
        mem_m = ctx.meter("Azure Container Apps", region, sku="Dedicated", meter="Dedicated Memory Usage")
        items.append(line(f"Profile {p.get('name')} ({ptype}) x{nodes:g}: vCPU", cpu_m, nodes * vcpu * HOURS_PER_MONTH))
        items.append(
            line(f"Profile {p.get('name')} ({ptype}) x{nodes:g}: memory", mem_m, nodes * gib * HOURS_PER_MONTH)
        )
        max_nodes = as_number(p.get("maximumCount"), nodes)
        if max_nodes > nodes:
            notes.append(f"profile {p.get('name')} can scale to {max_nodes:g} nodes; only {nodes:g} are priced")
    if has_dedicated:
        mgmt = ctx.meter("Azure Container Apps", region, sku="Dedicated", meter="Dedicated Plan Management")
        items.insert(0, line("Dedicated plan management", mgmt, HOURS_PER_MONTH))
    if not items:
        return included(res, "Consumption-only environment has no base fee")
    return priced(res, items, notes)


def _sum_resources(containers: JsonValue) -> tuple[float, float]:
    """Total (vCPU, GiB) requested by a list of container definitions."""
    cpu = gib = 0.0
    if isinstance(containers, list):
        for c in containers:
            r = c.get("resources") if isinstance(c, dict) else None
            if isinstance(r, dict):
                cpu += as_number(r.get("cpu"), 0.0)
                gib += parse_memory_gib(r.get("memory", 0))
    return cpu, gib


def _containers_size(res: Resource) -> tuple[float, float]:
    cpu, gib = _sum_resources(res.get("properties.template.containers", []))
    if cpu == 0:
        raise Unresolved(f"container resources of {res.name} are not known")
    return cpu, gib


def estimate_container_app(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    profile = res.get("properties.workloadProfileName")
    if isinstance(profile, str) and profile.lower() != "consumption":
        return included(res, f"runs on workload profile '{profile}' (billed on the environment)")
    region = ctx.region(res)
    cpu, gib = _containers_size(res)
    min_replicas = as_number(res.get("properties.template.scale.minReplicas"), 0)
    max_replicas = as_number(res.get("properties.template.scale.maxReplicas"), 10)
    rules = res.get("properties.template.scale.rules", [])
    notes: list[str] = []

    baseline_hours = min_replicas * HOURS_PER_MONTH
    cron_rules: list[tuple[str, str, int]] = []
    other_rules = False
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            custom = rule.get("custom")
            if isinstance(custom, dict) and str(custom.get("type", "")).lower() == "cron":
                meta = custom.get("metadata") if isinstance(custom.get("metadata"), dict) else {}
                assert isinstance(meta, dict)
                try:
                    hours = active_hours_per_month(str(meta["start"]), str(meta["end"]))
                except (KeyError, ValueError) as exc:
                    notes.append(f"cron rule '{rule.get('name')}' not understood ({exc}); ignored")
                    continue
                desired = int(as_number(meta.get("desiredReplicas"), 1))
                cron_rules.append((str(meta["start"]), str(meta["end"]), desired))
                notes.append(f"cron rule '{rule.get('name')}': {desired} replica(s) for {hours:.0f} h/month")
            else:
                other_rules = True

    scheduled_hours = extra_replica_hours_per_month(cron_rules, int(min_replicas))
    extra_hours = ctx.usage.number("containerapps", res.name, "extra_replica_hours", 0.0)
    measured = ctx.usage.optional_number("containerapps", res.name, "replica_hours")
    if measured is not None:
        # Measured replica-hours replace everything the template implies (min replicas, cron rules, autoscaling).
        baseline_hours, scheduled_hours, extra_hours = measured, 0.0, 0.0
        notes = [
            f"replica-hours taken from the usage file ({measured:,.0f} h/month), not from the template's scale rules"
        ]
        other_rules = False
    if other_rules and max_replicas > min_replicas:
        notes.append(
            "scales on request/event load (http/tcp/custom rule); not estimated unless "
            f'[containerapps."{res.name}"] extra_replica_hours is set'
        )
    idle_percent = ctx.usage.number("containerapps", res.name, "idle_percent", 0.0)
    if not 0 <= idle_percent <= 100:
        raise UsageError(
            f"usage containerapps[{res.name}].idle_percent must be between 0 and 100, got {idle_percent:g}"
        )
    active_hours = baseline_hours * (1 - idle_percent / 100) + scheduled_hours + extra_hours
    idle_hours = baseline_hours * idle_percent / 100
    scope = f"sub:{res.subscription_id}"
    items: list[LineItem] = []

    def meter(name: str):
        return ctx.meter("Azure Container Apps", region, sku="Standard", meter=name)

    basis = "usage" if (extra_hours or measured is not None) else "fixed"
    if active_hours:
        desc = f"{cpu:g} vCPU / {gib:g} GiB for {active_hours:,.0f} active h"
        items.append(
            line(
                f"{desc}: vCPU",
                meter("Standard vCPU Active Usage"),
                cpu * active_hours * 3600,
                basis,
                scope=scope,
                grant=FREE_VCPU_SECONDS,
                note="includes the free monthly grant (180k vCPU-s) shared per subscription",
            )
        )
        items.append(
            line(
                f"{desc}: memory",
                meter("Standard Memory Active Usage"),
                gib * active_hours * 3600,
                basis,
                scope=scope,
                grant=FREE_GIB_SECONDS,
                note="includes the free monthly grant (360k GiB-s) shared per subscription",
            )
        )
    if idle_hours:
        items.append(
            line(
                f"{cpu:g} vCPU idle {idle_hours:,.0f} h",
                meter("Standard vCPU Idle Usage"),
                cpu * idle_hours * 3600,
                basis,
            )
        )
        items.append(
            line(
                f"{gib:g} GiB idle {idle_hours:,.0f} h",
                meter("Standard Memory Idle Usage"),
                gib * idle_hours * 3600,
                basis,
            )
        )
    requests_m = ctx.usage.number("containerapps", res.name, "requests_millions", 0.0)
    if requests_m:
        items.append(
            line(
                f"{requests_m:g} M requests",
                meter("Standard Requests"),
                requests_m,
                "usage",
                scope=scope,
                grant=FREE_REQUESTS_MILLIONS,
            )
        )
    if not items:
        notes.insert(0, "scales to zero: no baseline cost")
        return ResourceEstimate(res, "free", [], notes)
    if measured is None and min_replicas > 0 and max_replicas > min_replicas:
        notes.append(f"autoscaling up to {max_replicas:g} replicas is not priced (baseline {min_replicas:g})")
    return priced(res, items, notes)


def estimate_session_pool(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    region = ctx.region(res)
    container_type = need_str(res, "properties.containerType", "containerType")
    if container_type.lower() in ("pythonlts", "nodelts"):
        hours = ctx.usage.number("sessionpools", res.name, "session_hours", 0.0)
        meter = ctx.meter("Azure Container Apps", region, sku="Dynamic Sessions", meter="Dynamic Sessions")
        notes = ["pay per allocated session-hour; an idle pool costs nothing"]
        if hours == 0:
            notes.append(f'set [sessionpools."{res.name}"] session_hours to estimate usage')
        items = (
            [
                line(
                    f"{container_type} sessions ({hours:,.0f} session-hours)",
                    meter,
                    hours,
                    "usage",
                    note="usage assumption",
                )
            ]
            if hours
            else []
        )
        items += usage_amount(res, ctx, "sessionpools")
        return ResourceEstimate(res, "priced" if items else "free", items, notes)
    # Custom container pools keep `readySessionInstances` running; they bill like consumption replicas.
    ready = as_number(res.get("properties.scaleConfiguration.readySessionInstances"), 0)
    cpu, gib = _sum_resources(res.get("properties.customContainerTemplate.containers", []))
    items: list[LineItem] = []
    scope = f"sub:{res.subscription_id}"
    if ready and cpu:
        vcpu_m = ctx.meter("Azure Container Apps", region, sku="Standard", meter="Standard vCPU Active Usage")
        mem_m = ctx.meter("Azure Container Apps", region, sku="Standard", meter="Standard Memory Active Usage")
        items.append(
            line(
                f"{ready:g} ready instance(s): vCPU",
                vcpu_m,
                ready * cpu * SECONDS_PER_MONTH,
                scope=scope,
                grant=FREE_VCPU_SECONDS,
            )
        )
        items.append(
            line(
                f"{ready:g} ready instance(s): memory",
                mem_m,
                ready * gib * SECONDS_PER_MONTH,
                scope=scope,
                grant=FREE_GIB_SECONDS,
            )
        )
    hours = ctx.usage.number("sessionpools", res.name, "session_hours", 0.0)
    if hours and cpu:
        vcpu_m = ctx.meter("Azure Container Apps", region, sku="Standard", meter="Standard vCPU Active Usage")
        mem_m = ctx.meter("Azure Container Apps", region, sku="Standard", meter="Standard Memory Active Usage")
        items.append(
            line(
                f"session usage {hours:,.0f} h: vCPU",
                vcpu_m,
                cpu * hours * 3600,
                "usage",
                scope=scope,
                grant=FREE_VCPU_SECONDS,
            )
        )
        items.append(
            line(
                f"session usage {hours:,.0f} h: memory",
                mem_m,
                gib * hours * 3600,
                "usage",
                scope=scope,
                grant=FREE_GIB_SECONDS,
            )
        )
    return ResourceEstimate(res, "priced" if items else "free", items, ["custom-container session pool"])
