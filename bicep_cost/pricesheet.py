"""Negotiated consumption rates from Azure subscription price sheets or saved exports."""

import csv
import json
import math
import os
import re
import tempfile
import urllib.parse
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from bicep_cost.collect.arm import ARM, Transport
from bicep_cost.prices import MAX_PAGES, Meter, NoPriceFoundError, PriceError, Tier, normalize_currency


@dataclass(frozen=True)
class Rate:
    meter_id: str
    sku_id: str
    unit: str
    minimum: float
    price: float


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PriceError(f"price sheet {name} must be a finite nonnegative number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise PriceError(f"price sheet {name} must be a finite nonnegative number") from exc
    if not math.isfinite(number) or number < 0:
        raise PriceError(f"price sheet {name} must be a finite nonnegative number")
    return number


def _unit(unit: str) -> tuple[float, str]:
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)([kKmM])?)?\s*(.*)", unit.strip())
    if match is None:
        raise PriceError(f"unsupported price sheet unit: {unit}")
    scale = float(match[1] or 1) * {"k": 1000, "m": 1_000_000, "": 1}[(match[2] or "").lower()]
    suffix = match[3].lower().replace(" ", "").replace("hours", "hour").replace("seconds", "second")
    if not math.isfinite(scale) or scale <= 0:
        raise PriceError(f"invalid price sheet unit: {unit}")
    return scale, suffix


@dataclass
class PriceSheet:
    currency: str
    source: str
    fetched_at: str
    rates: dict[tuple[str, str], tuple[Rate, ...]]

    @classmethod
    def from_rows(cls, rows: list[dict[str, object]], source: str, fetched_at: str = "") -> "PriceSheet":
        """Read Consumption API rows or MCA CSV rows; reject conflicting tier rates."""
        currency: str | None = None
        grouped: dict[tuple[str, str], dict[float, Rate]] = {}
        for row in rows:
            if any(not isinstance(key, str) for key in row):
                raise PriceError("price sheet contains invalid column names")
            fields = {key.lower(): value for key, value in row.items()}
            if fields.get("pricetype", "Consumption") != "Consumption":
                continue
            cur = fields.get("currencycode", fields.get("currency"))
            if not isinstance(cur, str):
                raise PriceError("price sheet row is missing currencyCode/currency")
            cur = normalize_currency(cur)
            if currency and currency != cur:
                raise PriceError("price sheet contains different currencies")
            currency = cur
            meter_id, sku_id = fields.get("meterid"), fields.get("skuid", "")
            unit = fields.get("unitofmeasure")
            if not isinstance(meter_id, str) or not meter_id or not isinstance(sku_id, str):
                raise PriceError("price sheet row requires meterId and optional skuId")
            if not isinstance(unit, str) or not unit:
                raise PriceError("price sheet row requires unitOfMeasure")
            if _number(fields.get("includedquantity", 0) or 0, "includedQuantity"):
                raise PriceError("price sheet includedQuantity grants require billing-scope modelling; not supported")
            minimum = _number(fields.get("tierminimumunits", 0) or 0, "tierMinimumUnits")
            price = _number(fields.get("unitprice"), "unitPrice")
            rate = Rate(meter_id.lower(), sku_id.lower(), unit, minimum, price)
            tiers = grouped.setdefault((rate.meter_id, rate.sku_id), {})
            current = tiers.get(minimum)
            if current and current != rate:
                raise PriceError(f"ambiguous price sheet rates for meter {meter_id}, SKU {sku_id}, tier {minimum}")
            tiers[minimum] = rate
        if not currency or not grouped:
            raise PriceError("price sheet has no consumption prices")
        rates = {key: tuple(tiers[n] for n in sorted(tiers)) for key, tiers in grouped.items()}
        for tiers in rates.values():
            if tiers[0].minimum != 0 or len({r.unit for r in tiers}) != 1:
                raise PriceError("price sheet tiers must start at zero and use one unit")
        return cls(currency, source, fetched_at or datetime.now(UTC).isoformat(), rates)

    @classmethod
    def download(cls, subscription_id: str, transport: Transport) -> "PriceSheet":
        """Download current subscription prices using an authenticated ARM transport."""
        sub = urllib.parse.quote(subscription_id, safe="")
        url = f"{ARM}/subscriptions/{sub}/providers/Microsoft.Consumption/pricesheets/default"
        url += "?api-version=2024-08-01"
        rows: list[dict[str, object]] = []
        seen: set[str] = set()
        while url:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or parsed.netloc != "management.azure.com":
                raise PriceError("untrusted price sheet pagination URL")
            if url in seen or len(seen) >= MAX_PAGES:
                raise PriceError("too many or repeated price sheet pages")
            seen.add(url)
            payload = transport("GET", url, None)
            props = payload.get("properties") if payload else None
            if not isinstance(props, dict) or not isinstance(props.get("pricesheets"), list):
                raise PriceError("Azure returned no price sheet; check billing agreement and price-sheet permissions")
            for row in props["pricesheets"]:
                if not isinstance(row, dict):
                    raise PriceError("invalid Azure price sheet row")
                rows.append(row)
            next_link = props.get("nextLink")
            if next_link is not None and not isinstance(next_link, str):
                raise PriceError("invalid price sheet nextLink")
            url = next_link or ""
        return cls.from_rows(rows, f"Azure subscription {subscription_id}")

    @classmethod
    def load(cls, path: Path) -> "PriceSheet":
        """Load a normalized snapshot or current Azure MCA/EA CSV export."""
        if path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as stream:
                return cls.from_rows([dict(row) for row in csv.DictReader(stream)], f"CSV {path.name}")
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("rates"), list):
            raise PriceError("not a bicep-cost price sheet snapshot (schema 1)")
        rows: list[dict[str, object]] = []
        for rate in data["rates"]:
            if not isinstance(rate, dict):
                raise PriceError("invalid saved price sheet rate")
            rows.append(
                {
                    "meterId": rate.get("meter_id"),
                    "skuId": rate.get("sku_id", ""),
                    "unitOfMeasure": rate.get("unit"),
                    "tierMinimumUnits": rate.get("minimum"),
                    "unitPrice": rate.get("price"),
                    "currencyCode": data.get("currency"),
                }
            )
        source, fetched = data.get("source"), data.get("fetched_at")
        if not isinstance(source, str) or not isinstance(fetched, str):
            raise PriceError("invalid saved price sheet provenance")
        return cls.from_rows(rows, source, fetched)

    def save(self, path: Path) -> None:
        """Atomically save rates and provenance with owner-only access, without tokens."""
        data = {
            "schema": 1,
            "currency": self.currency,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "rates": [asdict(rate) for tiers in self.rates.values() for rate in tiers],
        }
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, indent=2, allow_nan=False)
                stream.write("\n")
            Path(temporary).replace(path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()

    def apply(self, meter: Meter) -> Meter:
        """Replace matched account rates; retain retail rates for missing account meters."""
        tiers = self.rates.get((meter.meter_id.lower(), meter.sku_id.lower()))
        if tiers is None:
            tiers = self.rates.get((meter.meter_id.lower(), ""))
        if tiers is None:
            return replace(meter, account_price_missing=True)
        retail_scale, retail_unit = _unit(meter.unit)
        sheet_scale, sheet_unit = _unit(tiers[0].unit)
        if retail_unit != sheet_unit:
            raise NoPriceFoundError(
                f"incompatible units for account meter {meter.meter_id}: {tiers[0].unit} vs {meter.unit}"
            )
        factor = retail_scale / sheet_scale
        converted = tuple(Tier(rate.minimum / factor, rate.price * factor) for rate in tiers)
        if any(not math.isfinite(t.price) or not math.isfinite(t.minimum_units) for t in converted):
            raise PriceError(f"account meter {meter.meter_id} overflows after unit conversion")
        return replace(meter, currency=self.currency, tiers=converted)
