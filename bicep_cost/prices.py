"""Azure Retail Prices API client (https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices).

The API is public and unauthenticated; it is what the Azure pricing calculator reads from. Responses are cached on
disk so repeated runs (and CI) stay fast and offline-capable.
"""

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from .pricesheet import PriceSheet

log = logging.getLogger(__name__)

API_URL = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
MAX_PAGES = 200


class PriceError(Exception):
    """The price API could not be reached or returned something unusable."""


class NoPriceFound(PriceError):
    """The API answered, but has no meter matching the query."""


class RawPriceItem(TypedDict, total=False):
    currencyCode: str
    tierMinimumUnits: float
    retailPrice: float
    unitPrice: float
    armRegionName: str
    location: str
    effectiveStartDate: str
    meterId: str
    meterName: str
    productId: str
    skuId: str
    productName: str
    skuName: str
    serviceName: str
    serviceId: str
    serviceFamily: str
    unitOfMeasure: str
    type: str
    isPrimaryMeterRegion: bool
    armSkuName: str


@dataclass(frozen=True)
class Tier:
    minimum_units: float
    price: float


@dataclass(frozen=True)
class Meter:
    """One billable meter with its (possibly tiered) price list."""

    meter_id: str
    service: str
    product: str
    sku: str
    name: str
    region: str
    unit: str
    currency: str
    tiers: tuple[Tier, ...]
    arm_sku: str = ""
    sku_id: str = ""
    account_price_missing: bool = False

    @property
    def key(self) -> str:
        """Identity of the priced thing: SKUs of one product may share a meter id."""
        return f"{self.meter_id}|{self.sku_id}"

    @property
    def unit_price(self) -> float:
        """Price of the first tier that is not free (the headline price), falling back to the first tier."""
        for t in self.tiers:
            if t.price > 0:
                return t.price
        return self.tiers[0].price if self.tiers else 0.0

    def cost(self, quantity: float, already_consumed: float = 0.0) -> float:
        """Cost of ``quantity`` units, walking the tiers starting after ``already_consumed`` units."""
        if quantity <= 0:
            return 0.0
        start, end = already_consumed, already_consumed + quantity
        total = 0.0
        for i, tier in enumerate(self.tiers):
            upper = self.tiers[i + 1].minimum_units if i + 1 < len(self.tiers) else float("inf")
            lo, hi = max(start, tier.minimum_units), min(end, upper)
            if hi > lo:
                total += (hi - lo) * tier.price
        return total

    def label(self) -> str:
        return f"{self.service} / {self.product} / {self.name} [{self.region}]"


def odata_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_filter(
    service: str,
    region: str,
    *,
    product: str | None = None,
    sku: str | None = None,
    meter: str | None = None,
    product_contains: str | None = None,
    sku_contains: str | None = None,
    meter_contains: str | None = None,
    arm_sku: str | None = None,
) -> str:
    parts = [
        f"serviceName eq {odata_quote(service)}",
        f"armRegionName eq {odata_quote(region)}",
        "priceType eq 'Consumption'",
    ]
    if arm_sku is not None:
        parts.append(f"armSkuName eq {odata_quote(arm_sku)}")
    for field_name, eq, contains in (
        ("productName", product, product_contains),
        ("skuName", sku, sku_contains),
        ("meterName", meter, meter_contains),
    ):
        if eq is not None:
            parts.append(f"{field_name} eq {odata_quote(eq)}")
        if contains is not None:
            parts.append(f"contains({field_name}, {odata_quote(contains)})")
    return " and ".join(parts)


Fetcher = Callable[[str], bytes]


def _http_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "bicep-cost/0.1"})
    last: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                break
            retry_after = exc.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt)
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(2**attempt)
    raise PriceError(f"Azure Retail Prices API request failed: {last}")


def normalize_currency(value: str) -> str:
    """Normalize an ISO currency code; Azure validates support for the requested code."""
    currency = value.strip().upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise PriceError("currency must be a three-letter code such as USD, CHF or EUR")
    return currency


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "bicep-cost"


@dataclass
class PriceClient:
    currency: str = "USD"
    cache_dir: Path | None = field(default_factory=default_cache_dir)
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    offline: bool = False
    refresh: bool = False
    fetcher: Fetcher = _http_fetch
    price_sheet: "PriceSheet | None" = None
    queries_made: int = 0
    _memo: dict[str, tuple[Meter, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.currency = normalize_currency(self.currency)

    def _cache_path(self, odata_filter: str) -> Path | None:
        if self.cache_dir is None:
            return None
        key = hashlib.sha256(f"{API_VERSION}|{self.currency}|{odata_filter}".encode()).hexdigest()
        return self.cache_dir / "prices" / f"{key}.json"

    def _raw_items(self, odata_filter: str) -> list[RawPriceItem]:
        path = self._cache_path(odata_filter)
        if path is not None and path.exists() and not self.refresh:
            age = time.time() - path.stat().st_mtime
            if self.offline or age < self.ttl_seconds:
                return json.loads(path.read_text())["items"]
        if self.offline:
            raise PriceError(f"offline mode and no cached prices for: {odata_filter}")
        params = urllib.parse.urlencode(
            {"currencyCode": self.currency, "api-version": API_VERSION, "$filter": odata_filter},
            quote_via=urllib.parse.quote,
        )
        url: str | None = f"{API_URL}?{params}"
        items: list[RawPriceItem] = []
        pages = 0
        while url:
            pages += 1
            if pages > MAX_PAGES:
                raise PriceError("too many result pages from the Retail Prices API")
            self.queries_made += 1
            started = time.monotonic()
            payload = json.loads(self.fetcher(url))
            log.debug("price query page %d took %.1fs: %s", pages, time.monotonic() - started, odata_filter)
            items.extend(payload.get("Items", []))
            url = payload.get("NextPageLink")
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"fetched": datetime.now(UTC).isoformat(), "filter": odata_filter, "items": items})
            )
            tmp.replace(path)
        return items

    def query(self, odata_filter: str, now: datetime | None = None) -> tuple[Meter, ...]:
        cached = self._memo.get(odata_filter)
        if cached is not None:
            return cached
        meters = group_meters(self._raw_items(odata_filter), self.currency, now or datetime.now(UTC))
        if self.price_sheet is not None:
            if self.price_sheet.currency != self.currency:
                raise PriceError("account price sheet currency differs from requested currency")
            meters = tuple(self.price_sheet.apply(m) for m in meters)
        self._memo[odata_filter] = meters
        return meters

    def find(
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
        arm_sku: str | None = None,
        fallback_regions: Sequence[str] = ("Global",),
    ) -> Meter | None:
        """Return the single best matching meter, trying the region first and then the fallbacks."""
        for r in (region, *fallback_regions):
            found = self.query(
                build_filter(
                    service,
                    r,
                    product=product,
                    sku=sku,
                    meter=meter,
                    product_contains=product_contains,
                    sku_contains=sku_contains,
                    meter_contains=meter_contains,
                    arm_sku=arm_sku,
                )
            )
            if found:
                return sorted(found, key=lambda m: (m.product, m.sku, m.name, m.meter_id))[0]
        return None


def group_meters(items: Iterable[RawPriceItem], currency: str, now: datetime) -> tuple[Meter, ...]:
    """Group raw price rows into meters; for each (meter, tier) keep the row effective at ``now``."""
    best: dict[tuple[str, str, float], RawPriceItem] = {}
    for item in items:
        if item.get("type", "Consumption") != "Consumption":
            continue
        if item.get("currencyCode", currency).upper() != currency:
            raise PriceError(f"requested {currency} prices but Azure returned {item['currencyCode']}")
        start = _parse_date(item.get("effectiveStartDate"))
        if start is not None and start > now:
            continue
        # Several SKUs can share one meterId (e.g. the "N vCore" rows of an Azure SQL product); skuId tells them apart.
        key = (item["meterId"], item.get("skuId", ""), float(item.get("tierMinimumUnits", 0.0)))
        current = best.get(key)
        if current is None or (_parse_date(current.get("effectiveStartDate")) or datetime.min.replace(tzinfo=UTC)) <= (
            start or datetime.min.replace(tzinfo=UTC)
        ):
            best[key] = item
    by_meter: dict[tuple[str, str], list[RawPriceItem]] = {}
    for (meter_id, sku_id, _), item in best.items():
        by_meter.setdefault((meter_id, sku_id), []).append(item)
    meters: list[Meter] = []
    for (meter_id, sku_id), rows in by_meter.items():
        rows.sort(key=lambda r: float(r.get("tierMinimumUnits", 0.0)))
        first = rows[0]
        meters.append(
            Meter(
                meter_id=meter_id,
                service=first.get("serviceName", ""),
                product=first.get("productName", ""),
                sku=first.get("skuName", ""),
                name=first.get("meterName", ""),
                region=first.get("armRegionName", ""),
                unit=first.get("unitOfMeasure", ""),
                arm_sku=first.get("armSkuName", ""),
                sku_id=sku_id,
                currency=first.get("currencyCode", currency),
                tiers=tuple(
                    Tier(float(r.get("tierMinimumUnits", 0.0)), float(r.get("retailPrice", 0.0))) for r in rows
                ),
            )
        )
    return tuple(meters)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_region(location: str | None, default: str) -> str:
    """Map ARM locations ("West Europe", "westeurope") to the API's ``armRegionName``."""
    if not location:
        return default
    return location.replace(" ", "").lower()
