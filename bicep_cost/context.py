"""Shared context handed to every estimator."""

from collections.abc import Iterable
from dataclasses import dataclass, field

from bicep_cost.armexpr import Unknown
from bicep_cost.engine import Resource
from bicep_cost.prices import Meter, NoPriceFoundError, PriceClient, normalize_region
from bicep_cost.usage import UsageConfig

HOURS_PER_MONTH = 730.0
SECONDS_PER_MONTH = HOURS_PER_MONTH * 3600


class UnresolvedError(Exception):
    """A value the estimate depends on is not known (e.g. an unset parameter)."""


Unresolved = UnresolvedError


@dataclass
class ResourceIndex:
    resources: list[Resource]
    _by_id: dict[str, Resource] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_id = {r.id.lower(): r for r in self.resources}

    def find(self, resource_id: object) -> Resource | None:
        if not isinstance(resource_id, str):
            return None
        return self._by_id.get(resource_id.lower())

    def of_type(self, type_: str) -> Iterable[Resource]:
        return (r for r in self.resources if r.type.lower() == type_.lower())


@dataclass
class EstimateContext:
    prices: PriceClient
    usage: UsageConfig
    index: ResourceIndex
    default_region: str = "westeurope"
    assumed_region: set[str] = field(default_factory=set)

    def region(self, res: Resource) -> str:
        """The resource's price region; falls back to the default region when the template does not say."""
        loc = res.location
        if loc is None or loc.lower() == "global":
            if loc is None:
                self.assumed_region.add(res.id)
            return self.default_region
        return normalize_region(loc, self.default_region)

    def meter(
        self,
        service: str,
        region: str,
        *,
        product: str | None = None,
        sku: str | None = None,
        meter: str | None = None,
        product_contains: str | None = None,
        sku_contains: str | None = None,
        meter_contains: str | None = None,
        fallback_regions: tuple[str, ...] = ("Global",),
    ) -> Meter:
        found = self.prices.find(
            service,
            region,
            product=product,
            sku=sku,
            meter=meter,
            product_contains=product_contains,
            sku_contains=sku_contains,
            meter_contains=meter_contains,
            fallback_regions=fallback_regions,
        )
        if found is None:
            wanted = {k: v for k, v in (("product", product), ("sku", sku), ("meter", meter)) if v}
            raise NoPriceFoundError(f"no price for {service} in {region} {wanted}")
        return found

    def meters(
        self,
        service: str,
        region: str,
        *,
        product: str | None = None,
        sku: str | None = None,
        arm_sku: str | None = None,
    ) -> tuple[Meter, ...]:
        from bicep_cost.prices import build_filter

        return self.prices.query(build_filter(service, region, product=product, sku=sku, arm_sku=arm_sku))

    def meters_containing(self, service: str, region: str, *, product_contains: str) -> tuple[Meter, ...]:
        from bicep_cost.prices import build_filter

        return self.prices.query(build_filter(service, region, product_contains=product_contains))


def need_str(res: Resource, path: str, what: str | None = None) -> str:
    """A string value from the resource, raising :class:`UnresolvedError` if it is unknown or missing."""
    value = res.get(path)
    if isinstance(value, str):
        return value
    label = what or path
    if value is None and not res.is_unknown_at(path):
        raise UnresolvedError(f"{label} is not set on {res.name}")
    raise UnresolvedError(f"{label} of {res.name} is not known (depends on an unset parameter or a runtime value)")


def as_number(value: object, default: float) -> float:
    if isinstance(value, (bool, Unknown)):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default
