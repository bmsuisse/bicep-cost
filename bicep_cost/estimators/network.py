"""Private endpoints and Private DNS zones."""

from bicep_cost.context import HOURS_PER_MONTH, EstimateContext
from bicep_cost.engine import Resource
from bicep_cost.estimators.common import line, priced
from bicep_cost.model import ResourceEstimate


def estimate_private_endpoint(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    region = ctx.region(res)
    hourly = ctx.meter(
        "Virtual Network", region, product="Virtual Network Private Link", meter="Standard Private Endpoint"
    )
    ingress = ctx.meter(
        "Virtual Network", region, product="Virtual Network Private Link", meter="Standard Data Processed - Ingress"
    )
    egress = ctx.meter(
        "Virtual Network", region, product="Virtual Network Private Link", meter="Standard Data Processed - Egress"
    )
    in_gb = ctx.usage.number("privateendpoints", res.name, "ingress_gb", 1.0)
    out_gb = ctx.usage.number("privateendpoints", res.name, "egress_gb", 1.0)
    scope = f"sub:{res.subscription_id}"
    return priced(
        res,
        [
            line("Private endpoint hours", hourly, HOURS_PER_MONTH),
            line(
                f"Data processed in ({in_gb:g} GB)", ingress, in_gb, "usage", scope=scope + ":in", note="assumed volume"
            ),
            line(
                f"Data processed out ({out_gb:g} GB)",
                egress,
                out_gb,
                "usage",
                scope=scope + ":out",
                note="assumed volume",
            ),
        ],
    )


def estimate_private_dns_zone(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    meter = ctx.meter(
        "Azure DNS", "Zone 1", product="Azure DNS", sku="Private", meter="Private Zone", fallback_regions=("", "Zone 2")
    )
    queries = ctx.meter(
        "Azure DNS",
        "Zone 1",
        product="Azure DNS",
        sku="Private",
        meter="Private Queries",
        fallback_regions=("", "Zone 2"),
    )
    scope = f"sub:{res.subscription_id}"
    items = [line("Private DNS zone (first 25 zones $0.50, then $0.10)", meter, 1.0, scope=scope)]
    millions = ctx.usage.number("privatednszones", res.name, "queries_millions", 0.0)
    if millions:
        items.append(line(f"{millions:g} M DNS queries", queries, millions, "usage", scope=scope + ":q"))
    return priced(res, items)
