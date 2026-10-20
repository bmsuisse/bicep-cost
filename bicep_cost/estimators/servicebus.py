"""Service Bus namespaces (Basic / Standard / Premium)."""

from bicep_cost.context import HOURS_PER_MONTH, EstimateContext, UnresolvedError, as_number, need_str
from bicep_cost.engine import Resource
from bicep_cost.estimators.common import line, priced
from bicep_cost.model import LineItem, ResourceEstimate
from bicep_cost.prices import NoPriceFoundError

PREMIUM_UNITS = (1, 2, 4, 8, 16)


def estimate_namespace(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    tier = need_str(res, "sku.name", "Service Bus SKU").capitalize()
    region = ctx.region(res)
    scope = f"sub:{res.subscription_id}"
    items: list[LineItem] = []
    notes: list[str] = []
    if tier == "Premium":
        units = int(as_number(res.get("sku.capacity"), 1))
        if units not in PREMIUM_UNITS:
            raise UnresolvedError(f"Premium messaging units must be one of {PREMIUM_UNITS}, got {units}")
        unit = ctx.meter("Service Bus", region, sku="Premium", meter="Premium Messaging Unit")
        items.append(line(f"Premium x{units} messaging unit(s)", unit, units * HOURS_PER_MONTH))
        notes.append("operations are included; geo-replication data transfer is not priced")
        return priced(res, items, notes)
    ops_m = ctx.usage.number("servicebus", res.name, "operations_millions", 5.0)
    if tier == "Basic":
        meter = ctx.meter("Service Bus", region, sku="Basic", meter="Basic Messaging Operations")
        items.append(line(f"Basic operations ({ops_m:g} M)", meter, ops_m, "usage", scope=scope, note="assumed volume"))
        return priced(res, items, notes)
    if tier != "Standard":
        raise UnresolvedError(f"Service Bus SKU {tier!r} is not priced")
    hourly = next(
        (
            m
            for m in ctx.meters("Service Bus", region, product="Service Bus", sku="Standard")
            if m.name == "Standard Base Unit" and m.unit.lower() == "1/hour"
        ),
        None,
    )
    if hourly is None:
        raise NoPriceFoundError(f"no hourly Standard base price for Service Bus in {region}")
    items.append(line("Standard base charge", hourly, HOURS_PER_MONTH))
    ops = ctx.meter("Service Bus", region, sku="Standard", meter="Standard Messaging Operations")
    items.append(
        line(
            f"Standard operations ({ops_m:g} M, first 13 M included)",
            ops,
            ops_m,
            "usage",
            scope=scope,
            note="assumed volume; brokered connections beyond 1,000 are not priced",
        )
    )
    return priced(res, items, notes)
