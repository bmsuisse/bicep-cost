"""Helpers shared by the estimators."""

from ..context import EstimateContext
from ..engine import Resource
from ..model import CostBasis, LineItem, ResourceEstimate
from ..prices import Meter, NoPriceFound


def priced(res: Resource, items: list[LineItem], notes: list[str] | None = None) -> ResourceEstimate:
    return ResourceEstimate(res, "priced", items, notes or [])


def free(res: Resource, why: str) -> ResourceEstimate:
    return ResourceEstimate(res, "free", [], [why] if why else [])


def included(res: Resource, why: str) -> ResourceEstimate:
    """Costs nothing on its own because another resource is billed for it."""
    return ResourceEstimate(res, "included", [], [why])


def external(res: Resource, why: str) -> ResourceEstimate:
    """Runs on something that is not part of the template, so its cost is not in the total."""
    return ResourceEstimate(res, "external", [], [why])


def line(
    description: str,
    meter: Meter,
    quantity: float,
    basis: CostBasis = "fixed",
    *,
    scope: str | None = None,
    grant: float = 0.0,
    note: str | None = None,
) -> LineItem:
    return LineItem(description, quantity, basis, meter, scope=scope, grant=grant, note=note)


def usage_amount(res: Resource, ctx: EstimateContext, section: str) -> list[LineItem]:
    """An optional hand-entered monthly amount (``monthly_cost``) for costs the estimator cannot model."""
    amount = ctx.usage.number(section, res.name, "monthly_cost", 0.0)
    if amount > 0:
        return [
            LineItem(
                f"{res.name}: user-supplied monthly cost", 1.0, "usage", fixed_amount=amount, note="from usage file"
            )
        ]
    return []


def node_price(meters: tuple[Meter, ...], vcores: int, *, zone_redundant: bool = False) -> tuple[Meter, float]:
    """Meter and quantity multiplier for ``vcores`` in a vCore-priced product.

    These products list a whole-size row per SKU (``8 vCore``, price for all 8) next to a per-vCore row (``vCore``).
    Prefer the whole-size row; otherwise price per vCore and multiply.
    """
    suffix = " Zone Redundancy" if zone_redundant else ""
    usable = [m for m in meters if "free" not in m.name.lower() and "free" not in m.sku.lower()]
    whole = next((m for m in usable if m.sku == f"{vcores} vCore{suffix}"), None)
    if whole is not None and vcores != 1:
        return whole, 1.0
    per = next((m for m in usable if m.sku in (f"1 vCore{suffix}", f"vCore{suffix}")), None)
    if per is None:
        if whole is not None:
            return whole, 1.0
        raise NoPriceFound(f"no compute price for {vcores} vCores{suffix}")
    return per, float(vcores)
