"""Run the estimators over a resource list and turn quantities into money."""

import logging
from collections import defaultdict
from dataclasses import dataclass

from bicep_cost.context import EstimateContext, ResourceIndex, UnresolvedError
from bicep_cost.engine import Resource
from bicep_cost.estimators import estimator_for
from bicep_cost.model import Estimate, LineItem, ResourceEstimate
from bicep_cost.prices import NoPriceFoundError, PriceClient, PriceError
from bicep_cost.usage import UsageConfig, UsageError

log = logging.getLogger(__name__)


def estimate_resources(
    resources: list[Resource], prices: PriceClient, usage: UsageConfig, default_region: str = "westeurope"
) -> Estimate:
    ctx = EstimateContext(prices=prices, usage=usage, index=ResourceIndex(resources), default_region=default_region)
    results: list[ResourceEstimate] = []
    warnings: list[str] = []
    for res in resources:
        estimator = estimator_for(res.type)
        if estimator is None:
            results.append(ResourceEstimate(res, "unsupported", notes=["no cost estimator for this resource type"]))
            continue
        try:
            est = estimator(res, ctx)
        except UnresolvedError as exc:
            est = ResourceEstimate(res, "unresolved", notes=[str(exc)])
        except NoPriceFoundError as exc:
            est = ResourceEstimate(res, "unresolved", notes=[str(exc)])
        except (PriceError, UsageError):
            raise
        except Exception as exc:  # an estimator bug must not hide the other resources
            log.debug("estimator failed for %s", res.address, exc_info=True)
            est = ResourceEstimate(res, "unresolved", notes=[f"internal estimator error: {exc!r}"])
        # https://github.com/bmsuisse/bicep-cost/issues/12
        missing = [i.meter.label() for i in est.items if i.quantity > 0 and i.meter and i.meter.account_price_missing]
        for label in dict.fromkeys(missing):
            warning = f"{res.address}: no account price for {label}; using retail price"
            est.notes.append(warning)
            warnings.append(warning)
        if res.id in ctx.assumed_region:
            est.notes.append(f"location not known; priced in default region {default_region}")
        results.append(est)
    apply_pricing(results)
    for est in results:
        pct = 0.0 if prices.price_sheet else usage.discount_percent(est.resource.type)
        if pct and est.items:
            for item in est.items:
                if item.fixed_amount is None:  # user-supplied / measured actual costs are already net of discounts
                    item.monthly *= 1 - pct / 100
            est.notes.append(f"{pct:g}% discount from the usage file's [discounts] applied")
    return Estimate(
        price_source=(
            f"Account price sheet: {prices.price_sheet.source} (snapshot {prices.price_sheet.fetched_at}); "
            "retail fallback where needed; manual discounts ignored"
            if prices.price_sheet
            else "Azure Retail Prices API (list price, pay-as-you-go)"
        ),
        currency=prices.currency,
        region_default=default_region,
        resources=results,
        warnings=warnings,
        assumptions=[a.render() for a in usage.assumptions()],
        default_assumptions=sum(1 for a in usage.assumptions() if a.from_default),
    )


@dataclass
class _Group:
    items: list[LineItem]

    @property
    def quantity(self) -> float:
        return sum(i.quantity for i in self.items)


def apply_pricing(estimates: list[ResourceEstimate]) -> None:
    """Fill in ``monthly`` on every line item.

    Items sharing a meter and ``scope`` are priced as one pool, so tiered prices and free grants (per subscription,
    per workspace) apply once; the pool's cost is then split between the items by quantity.
    """
    groups: dict[tuple[str, str], _Group] = defaultdict(lambda: _Group([]))
    for est in estimates:
        for item in est.items:
            if item.fixed_amount is not None:
                item.monthly = item.fixed_amount
                continue
            if item.meter is None:
                continue
            key = (item.meter.key, item.scope if item.scope is not None else f"item:{id(item)}")
            groups[key].items.append(item)
    for group in groups.values():
        meter = group.items[0].meter
        assert meter is not None
        total = group.quantity
        grant = max(i.grant for i in group.items)
        cost = meter.cost(max(total - grant, 0.0)) if grant else meter.cost(total)
        for item in group.items:
            item.monthly = cost * (item.quantity / total) if total else 0.0
