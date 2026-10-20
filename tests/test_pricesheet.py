import json
import stat
from pathlib import Path

import pytest

from bicep_cost import cli
from bicep_cost.prices import Meter, NoPriceFound, PriceClient, PriceError, Tier
from bicep_cost.pricesheet import PriceSheet
from tests.conftest import fake_fetcher


def row(**overrides: object) -> dict[str, object]:
    return {"meterId": "meter", "currencyCode": "CHF", "unitOfMeasure": "100 Hours", "unitPrice": 20, **overrides}


def meter(sku: str = "") -> Meter:
    return Meter("meter", "S", "P", "K", "M", "r", "1 Hour", "CHF", (Tier(0, 1),), sku_id=sku)


def test_live_download_pagination_and_private_snapshot(tmp_path):
    calls = []

    def transport(method, url, body):
        calls.append(url)
        if len(calls) == 1:
            return {"properties": {"pricesheets": [row()], "nextLink": "https://management.azure.com/page2"}}
        return {"properties": {"pricesheets": [row(meterId="other", unitPrice=0)]}}

    sheet = PriceSheet.download("sub", transport)
    assert len(calls) == 2
    assert "api-version=2024-08-01" in calls[0]
    path = tmp_path / "prices.json"
    sheet.save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = PriceSheet.load(path)
    assert loaded == sheet
    loaded_meter = loaded.apply(meter())
    assert loaded_meter is not None
    assert loaded_meter.cost(730) == pytest.approx(146)
    assert "token" not in path.read_text().lower()


@pytest.mark.parametrize(
    "link", ["http://management.azure.com/x", "https://evil.example/x", "https://management.azure.com@evil.example/x"]
)
def test_live_download_rejects_untrusted_links(link):
    calls = []

    def transport(method, url, body):
        calls.append(url)
        return {"properties": {"pricesheets": [row()], "nextLink": link}}

    with pytest.raises(PriceError, match="untrusted"):
        PriceSheet.download("sub", transport)
    assert len(calls) == 1


def test_sheet_tiers_units_skus_and_missing_meter():
    sheet = PriceSheet.from_rows(
        [row(unitPrice=0), row(tierMinimumUnits=1, unitPrice=20), row(skuId="special", unitPrice=40)], "test"
    )
    priced = sheet.apply(meter())
    assert priced is not None
    assert priced.tiers == (Tier(0, 0), Tier(100, 0.2))
    assert priced.cost(110) == pytest.approx(2)
    special = sheet.apply(meter("special"))
    assert special is not None
    assert special.unit_price == 0.4
    missing = Meter("absent", "s", "p", "k", "n", "r", "1 Hour", "CHF", ())
    assert sheet.apply(missing).account_price_missing
    assert sheet.apply(missing).tiers == missing.tiers
    with pytest.raises(NoPriceFound, match="incompatible"):
        sheet.apply(Meter("meter", "s", "p", "k", "n", "r", "1 GB", "CHF", ()))


@pytest.mark.parametrize(
    "bad",
    [
        row(unitPrice="nan"),
        row(unitPrice=-1),
        row(unitPrice=True),
        row(currencyCode="xx"),
        row(includedQuantity=100),
        row(tierMinimumUnits=1),
    ],
)
def test_sheet_rejects_invalid_rates(bad):
    with pytest.raises(PriceError):
        PriceSheet.from_rows([bad], "test")


def test_sheet_rejects_ambiguous_and_mixed_currency_rates():
    with pytest.raises(PriceError, match="ambiguous"):
        PriceSheet.from_rows([row(), row(unitPrice=40)], "test")
    with pytest.raises(PriceError, match="different currencies"):
        PriceSheet.from_rows([row(), row(meterId="other", currencyCode="USD")], "test")


def test_mca_csv_uses_price_currency_and_consumption_only(tmp_path):
    path = tmp_path / "mca.csv"
    path.write_text(
        "meterId,skuId,currency,billingCurrency,unitOfMeasure,unitPrice,tierMinimumUnits,priceType\n"
        "meter,,USD,CHF,1 Hour,0.1,0,Consumption\n"
        "meter,,USD,CHF,1 Hour,0.001,0,ReservedInstance\n",
        encoding="utf-8-sig",
    )
    sheet = PriceSheet.load(path)
    assert sheet.currency == "USD"
    priced = sheet.apply(meter())
    assert priced is not None
    assert priced.unit_price == 0.1


def test_cli_live_save_and_load_account_prices(tmp_path, monkeypatch, capsys):
    meter_id = "Azure App Service|Azure App Service Premium v3 Plan - Linux|P1 v3|P1 v3 App|1 Hour|westeurope"
    sheet = PriceSheet.from_rows([row(meterId=meter_id, currencyCode="USD", unitPrice=10)], "test subscription")
    monkeypatch.setattr(PriceSheet, "download", lambda *args: sheet)
    monkeypatch.setattr(cli, "http_transport", lambda: None)
    monkeypatch.setattr(cli, "az_current_subscription", lambda: "test-subscription")
    monkeypatch.setattr(
        cli, "_client", lambda args, currency: PriceClient(currency=currency, cache_dir=None, fetcher=fake_fetcher)
    )
    template = Path(__file__).parent / "fixtures/app.json"
    saved = tmp_path / "sheet.json"
    usage = tmp_path / "usage.toml"
    usage.write_text("[discounts]\ndefault = 50\n")
    args = ["estimate", str(template), "--format", "json", "--usage", str(usage)]
    assert cli.main([*args, "--azure-prices", "--save-price-sheet", str(saved)]) == 0
    live = json.loads(capsys.readouterr().out)
    assert live["total_monthly"] == pytest.approx(73)
    assert "Account price sheet" in live["price_source"]
    assert cli.main([*args, "--price-sheet", str(saved)]) == 0
    assert json.loads(capsys.readouterr().out) == live
    assert cli.main([*args, "--price-sheet", str(saved), "--currency", "CHF"]) == 1
    assert "no conversion" in capsys.readouterr().err
    empty = PriceSheet.from_rows([row()], "missing all app meters")
    empty.save(saved)
    assert cli.main([*args, "--price-sheet", str(saved), "--strict"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["total_monthly"] == pytest.approx(129.94)
    assert any(r["status"] == "priced" for r in report["resources"])
    assert any("using retail price" in warning for warning in report["warnings"])


def test_partial_sheet_does_not_hide_storage_operations():
    from bicep_cost.calc import estimate_resources
    from bicep_cost.usage import UsageConfig
    from tests.conftest import make_resource

    storage = make_resource("Microsoft.Storage/storageAccounts", "store", {"sku": {"name": "Standard_LRS"}})
    meter_id = "Storage|General Block Blob v2|Hot LRS|Hot LRS Data Stored|1 GB/Month|westeurope"
    sheet = PriceSheet.from_rows([row(meterId=meter_id, currencyCode="USD", unitOfMeasure="1 GB/Month")], "partial")
    prices = PriceClient(cache_dir=None, fetcher=fake_fetcher, price_sheet=sheet)
    result = estimate_resources([storage], prices, UsageConfig())
    assert result.resources[0].status == "priced"
    assert any("Write Operations" in n for n in result.resources[0].notes)
    assert result.monthly == pytest.approx(200 + 10 * 0.054 + 100 * 0.0043 + 10 * 0.0043)
    assert len(result.warnings) == 3
    from bicep_cost.report import render_markdown, render_text

    assert "using retail price" in render_text(result, "test", [])
    assert "using retail price" in render_markdown(result, "test", [])
    zero_ops = UsageConfig(
        {"storage": {"write_operations_10k": 0, "read_operations_10k": 0, "other_operations_10k": 0}}
    )
    result = estimate_resources([storage], prices, zero_ops)
    assert result.resources[0].status == "priced"
    assert result.monthly == 200
    assert not result.warnings


def test_retail_fallback_does_not_fail_strict(tmp_path, monkeypatch, capsys):
    template = tmp_path / "storage.json"
    template.write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "type": "Microsoft.Storage/storageAccounts",
                        "name": "store",
                        "location": "westeurope",
                        "sku": {"name": "Standard_LRS"},
                    }
                ]
            }
        )
    )
    sheet = PriceSheet.from_rows([row(meterId="unrelated")], "partial")
    path = tmp_path / "prices.json"
    sheet.save(path)
    monkeypatch.setattr(
        cli, "_client", lambda args, currency: PriceClient(currency=currency, cache_dir=None, fetcher=fake_fetcher)
    )
    assert cli.main(["estimate", str(template), "--price-sheet", str(path), "--strict", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total_monthly"] > 0
    assert len(report["warnings"]) == 4
    assert all("using retail price" in warning for warning in report["warnings"])
