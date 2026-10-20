from datetime import UTC, datetime
from pathlib import Path

import pytest

from bicep_cost.prices import Meter, PriceClient, Tier, build_filter, group_meters, normalize_region
from tests.conftest import item


def meter(*tiers: tuple[float, float]) -> Meter:
    return Meter("m", "s", "p", "k", "n", "r", "u", "USD", tuple(Tier(a, b) for a, b in tiers))


def test_tiered_cost() -> None:
    m = meter((0, 0.0), (5, 3.0))
    assert m.cost(3) == 0
    assert m.cost(8) == 9.0
    assert m.cost(2, already_consumed=5) == 6.0
    flat = meter((0, 0.5))
    assert flat.cost(10) == 5.0
    assert flat.cost(0) == 0


def test_unit_price_skips_free_tier() -> None:
    assert meter((0, 0.0), (5, 3.0)).unit_price == 3.0


def test_filter_escapes_quotes_and_builds_clauses() -> None:
    flt = build_filter("Azure O'Neil", "westeurope", product="P", meter_contains="X")
    assert "serviceName eq 'Azure O''Neil'" in flt
    assert "priceType eq 'Consumption'" in flt
    assert "productName eq 'P'" in flt
    assert "contains(meterName, 'X')" in flt


def test_group_meters_picks_latest_effective_row_and_ignores_future() -> None:
    old = item("S", "P", "K", "M", 1.0)
    old["effectiveStartDate"] = "2026-01-01T00:00:00Z"
    new = item("S", "P", "K", "M", 2.0)
    new["effectiveStartDate"] = "2026-06-01T00:00:00Z"
    future = item("S", "P", "K", "M", 9.0)
    future["effectiveStartDate"] = "2099-01-01T00:00:00Z"
    (m,) = group_meters([old, new, future], "USD", datetime(2026, 10, 1, tzinfo=UTC))
    assert m.tiers == (Tier(0.0, 2.0),)


def test_region_fallback_and_memoisation(prices: PriceClient) -> None:
    pe = prices.find("Virtual Network", "westeurope", meter="Standard Private Endpoint")
    assert pe is not None
    assert pe.region == "Global"
    before = prices.queries_made
    prices.find("Virtual Network", "westeurope", meter="Standard Private Endpoint")
    assert prices.queries_made == before


def test_disk_cache_roundtrip_and_offline(tmp_path: Path) -> None:
    from tests.conftest import fake_fetcher

    calls = []

    def counting(url: str) -> bytes:
        calls.append(url)
        return fake_fetcher(url)

    first = PriceClient(cache_dir=tmp_path, fetcher=counting)
    assert first.find("Key Vault", "westeurope", meter="Operations") is not None
    second = PriceClient(cache_dir=tmp_path, fetcher=counting, offline=True)
    assert second.find("Key Vault", "westeurope", meter="Operations") is not None
    assert len(calls) == 1


def test_normalize_region() -> None:
    assert normalize_region("West Europe", "x") == "westeurope"
    assert normalize_region(None, "westeurope") == "westeurope"


def test_group_meters_keeps_skus_that_share_a_meter_id_apart() -> None:
    a = item("S", "P", "2 vCore", "vCore", 1.0, meter_id="shared")
    b = item("S", "P", "4 vCore", "vCore", 2.0, meter_id="shared")
    meters = group_meters([a, b], "USD", datetime(2026, 10, 1, tzinfo=UTC))
    assert {m.sku: m.unit_price for m in meters} == {"2 vCore": 1.0, "4 vCore": 2.0}


def test_currency_request_and_cache_isolation(tmp_path: Path) -> None:
    import json
    import urllib.parse

    def fetch(url: str) -> bytes:
        currency = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["currencyCode"][0]
        row = item("S", "P", "K", "M", 2.0 if currency == "CHF" else 3.0)
        row["currencyCode"] = currency
        return json.dumps({"Items": [row]}).encode()

    for code, amount in ((" chf ", 2.0), ("USD", 3.0)):
        client = PriceClient(currency=code, cache_dir=tmp_path, fetcher=fetch)
        assert client.query("x")[0].unit_price == amount
        cached = PriceClient(currency=code, cache_dir=tmp_path, offline=True)
        assert cached.query("x")[0].currency == code.strip().upper()


def test_currency_rejects_invalid_codes_and_mismatched_responses() -> None:

    from bicep_cost.prices import PriceError

    with pytest.raises(PriceError, match="three-letter"):
        PriceClient(currency="dollars")
    with pytest.raises(PriceError, match="requested CHF"):
        group_meters([item("S", "P", "K", "M", 1)], "CHF", datetime.now(UTC))
