"""App Service plans, Functions (Consumption / Flex / Premium) and Logic Apps Standard."""

import re
from dataclasses import dataclass

from ..context import HOURS_PER_MONTH, SECONDS_PER_MONTH, EstimateContext, Unresolved, as_number, need_str
from ..engine import Resource
from ..model import LineItem, ResourceEstimate
from .common import external, free, included, line, priced

DEDICATED_TIERS: dict[str, str] = {
    "B": "Basic",
    "S": "Standard",
}

# vCPU / GiB of the elastic plans (Functions Premium EPn, Logic Apps Standard WSn)
ELASTIC_SIZES: dict[str, tuple[int, float]] = {"1": (1, 3.5), "2": (2, 7.0), "3": (4, 14.0)}


@dataclass(frozen=True)
class PlanSku:
    kind: str  # dedicated | free | shared | consumption | functions-premium | logic-standard | flex | isolated
    tier: str = ""
    label: str = ""
    size: str = ""


def parse_plan_sku(name: str) -> PlanSku:
    n = name.strip()
    low = n.lower()
    if low == "f1":
        return PlanSku("free", "Free", "F1")
    if low == "d1":
        return PlanSku("shared", "Shared", "D1")
    if low in ("y1", "dynamic"):
        return PlanSku("consumption")
    if low == "fc1":
        return PlanSku("flex")
    if m := re.fullmatch(r"ep([123])", low):
        return PlanSku("functions-premium", size=m.group(1))
    if m := re.fullmatch(r"ws([123])", low):
        return PlanSku("logic-standard", size=m.group(1))
    if m := re.fullmatch(r"([bs])(\d)", low):
        return PlanSku("dedicated", DEDICATED_TIERS[m.group(1).upper()], n.upper())
    if m := re.fullmatch(r"p(\d)(m?)v([234])", low):
        return PlanSku("dedicated", f"Premium v{m.group(3)}", f"P{m.group(1)}{m.group(2)} v{m.group(3)}")
    if m := re.fullmatch(r"i(\d)(m?)v2", low):
        return PlanSku("isolated", "Isolated v2", f"I{m.group(1)}{m.group(2)} v2")
    raise Unresolved(f"unrecognised App Service plan SKU {name!r}")


def is_linux(plan: Resource) -> bool:
    kind = (plan.kind or "").lower()
    return "linux" in kind or plan.get("properties.reserved") is True


def estimate_plan(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    sku = parse_plan_sku(need_str(res, "sku.name", "plan SKU"))
    region = ctx.region(res)
    capacity = max(
        int(ctx.usage.number("appserviceplans", res.name, "instances", as_number(res.get("sku.capacity"), 1))), 1
    )
    if sku.kind == "free":
        return free(res, "Free tier")
    if sku.kind == "shared":
        return ResourceEstimate(res, "unsupported", notes=["Shared (D1) plans are not priced"])
    if sku.kind == "consumption":
        return free(res, "Consumption plan: billed per execution on the function apps")
    if sku.kind == "flex":
        return free(res, "Flex Consumption plan: billed per execution / always-ready instance on the function apps")
    if sku.kind in ("dedicated", "isolated"):
        linux = is_linux(res)
        product = f"Azure App Service {sku.tier} Plan" + (" - Linux" if linux else "")
        meter = ctx.meter("Azure App Service", region, product=product, sku=sku.label)
        notes = []
        if sku.kind == "isolated":
            notes.append("App Service Environment v3 stamp fee is not included")
        return priced(
            res,
            [
                line(
                    f"{sku.label} x{capacity} instance(s), {'Linux' if linux else 'Windows'}",
                    meter,
                    capacity * HOURS_PER_MONTH,
                )
            ],
            notes,
        )
    vcpu, gib = ELASTIC_SIZES[sku.size]
    if sku.kind == "functions-premium":
        cpu_m = ctx.meter("Functions", region, product="Premium Functions", meter="Premium vCPU Duration")
        mem_m = ctx.meter("Functions", region, product="Premium Functions", meter="Premium Memory Duration")
        label = f"EP{sku.size}"
    else:
        cpu_m = ctx.meter("Logic Apps", region, sku="Standard", meter="Standard vCPU Duration")
        mem_m = ctx.meter("Logic Apps", region, sku="Standard", meter="Standard Memory Duration")
        label = f"WS{sku.size}"
    hours = capacity * HOURS_PER_MONTH
    return priced(
        res,
        [
            line(f"{label} x{capacity}: {vcpu} vCPU", cpu_m, vcpu * hours),
            line(f"{label} x{capacity}: {gib:g} GiB memory", mem_m, gib * hours),
        ],
        [f"Billed for the {capacity} always-on instance(s); scale-out beyond that is extra"],
    )


def _plan_of(site: Resource, ctx: EstimateContext) -> Resource | None:
    return ctx.index.find(site.get("properties.serverFarmId"))


def estimate_site(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    kind = (res.kind or "").lower()
    plan = _plan_of(res, ctx)
    if plan is None:
        note = "its App Service plan is not part of this template, so the hosting cost is not included"
        if "functionapp" in kind:
            return external(res, note + " (a Consumption plan would add usage costs)")
        return external(res, note)
    if "functionapp" not in kind:
        return included(res, f"runs on plan {plan.name}")
    try:
        sku = parse_plan_sku(need_str(plan, "sku.name"))
    except Unresolved:
        return included(res, f"runs on plan {plan.name}")
    if sku.kind == "consumption":
        return _consumption_function(res, ctx)
    if sku.kind == "flex":
        return _flex_function(res, ctx)
    return included(res, f"runs on plan {plan.name}")


def _consumption_function(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    region = ctx.region(res)
    executions = ctx.usage.number("functions", res.name, "executions_per_month", 500_000)
    duration_ms = ctx.usage.number("functions", res.name, "avg_duration_ms", 500)
    memory_mb = ctx.usage.number("functions", res.name, "memory_mb", 256)
    gb_seconds = ctx.usage.number(
        "functions", res.name, "gb_seconds_per_month", executions * duration_ms / 1000 * memory_mb / 1024
    )
    exec_m = ctx.meter("Functions", region, product="Functions", meter="Standard Total Executions")
    time_m = ctx.meter("Functions", region, product="Functions", meter="Standard Execution Time")
    scope = f"sub:{res.subscription_id}"
    note = "usage assumption; the monthly free grant (1M executions, 400k GB-s) is shared per subscription"
    return priced(
        res,
        [
            line(
                f"Consumption executions ({executions:,.0f}/month)",
                exec_m,
                executions / 10,
                "usage",
                scope=scope,
                note=note,
            ),
            line(
                f"Consumption execution time ({gb_seconds:,.0f} GB-s)",
                time_m,
                gb_seconds,
                "usage",
                scope=scope,
                note=note,
            ),
        ],
    )


def _flex_function(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    region = ctx.region(res)
    items: list[LineItem] = []
    instance_mb = as_number(res.get("properties.functionAppConfig.scaleAndConcurrency.instanceMemoryMB"), 2048)
    always_ready = res.get("properties.functionAppConfig.scaleAndConcurrency.alwaysReady", [])
    if isinstance(always_ready, list):
        baseline = ctx.meter("Functions", region, product="Flex Consumption", meter="Always Ready Baseline")
        for entry in always_ready:
            if isinstance(entry, dict):
                count = as_number(entry.get("instanceCount"), 0)
                if count > 0:
                    gb = count * instance_mb / 1024
                    items.append(
                        line(
                            f"Flex always-ready '{entry.get('name')}' x{count:g} ({instance_mb:g} MB)",
                            baseline,
                            gb * SECONDS_PER_MONTH,
                        )
                    )
    executions = ctx.usage.number("functions", res.name, "executions_per_month", 500_000)
    duration_ms = ctx.usage.number("functions", res.name, "avg_duration_ms", 500)
    gb_seconds = ctx.usage.number(
        "functions", res.name, "gb_seconds_per_month", executions * duration_ms / 1000 * instance_mb / 1024
    )
    scope = f"sub:{res.subscription_id}"
    items.append(
        line(
            f"Flex on-demand executions ({executions:,.0f}/month)",
            ctx.meter("Functions", region, product="Flex Consumption", meter="On Demand Total Executions"),
            executions / 10,
            "usage",
            scope=scope,
            note="usage assumption",
        )
    )
    items.append(
        line(
            f"Flex on-demand execution time ({gb_seconds:,.0f} GB-s)",
            ctx.meter("Functions", region, product="Flex Consumption", meter="On Demand Execution Time"),
            gb_seconds,
            "usage",
            scope=scope,
            note="usage assumption",
        )
    )
    return priced(res, items)
