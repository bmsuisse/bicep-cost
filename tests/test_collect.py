import json
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bicep_cost import cli
from bicep_cost.collect import collectors
from bicep_cost.collect.arm import ArmClient, AzureError, parse_series
from bicep_cost.collect.collectors import Finding, classify_operation, collect_all
from bicep_cost.collect.toml_out import render_usage_toml
from bicep_cost.engine import Resource
from bicep_cost.prices import PriceClient
from tests.conftest import fake_fetcher, make_resource

Response = dict[str, object] | Callable[[str, dict[str, object] | None], dict[str, object]] | Exception

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
DAYS = 30
FACTOR = 730 / 24 / DAYS


def metric(values_by_dims: list[tuple[dict[str, str], list[float | None]]], agg: str = "total") -> dict[str, object]:
    return {
        "value": [
            {
                "timeseries": [
                    {
                        "metadatavalues": [{"name": {"value": k}, "value": v} for k, v in dims.items()],
                        "data": [{agg: v} if v is not None else {} for v in values],
                    }
                    for dims, values in values_by_dims
                ]
            }
        ]
    }


class FakeAzure:
    """Routes ARM calls by (substring of url) -> response; records every call."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, Response]] = []
        self.calls: list[str] = []

    def on(self, fragment: str, response: Response) -> None:
        self.routes.append((fragment, response))

    def __call__(self, method: str, url: str, body: dict[str, object] | None) -> dict[str, object] | None:
        self.calls.append(f"{method} {url}")
        for fragment, response in self.routes:
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                if isinstance(response, dict):
                    return response
                return response(url, body)
        return None


@pytest.fixture
def az() -> FakeAzure:
    return FakeAzure()


def arm(az: FakeAzure) -> ArmClient:
    return ArmClient(az, days=DAYS, now=NOW)


def test_classify_operation() -> None:
    assert classify_operation("GetBlob") == "read"
    assert classify_operation("PutBlock") == "write"
    assert classify_operation("ListBlobs") == "write"
    assert classify_operation("GetBlobProperties") == "other"
    assert classify_operation("DeleteBlob") == "other"


def test_parse_series_reads_dimensions_and_missing_points() -> None:
    (s,) = parse_series(metric([({"ApiName": "GetBlob"}, [1, None, 3])]), "Total")
    assert s.dimensions == {"apiname": "GetBlob"}
    assert s.values == [1, None, 3]
    assert s.total() == 4
    assert s.latest() == 3
    assert parse_series(None, "Total") == []


def storage(name: str = "sa", sub: str = "sub1") -> Resource:
    return make_resource(
        "Microsoft.Storage/storageAccounts",
        name,
        {"location": "westeurope", "kind": "StorageV2", "sku": {"name": "Standard_LRS"}},
        sub,
    )


def test_storage_capacity_and_operations(az: FakeAzure) -> None:
    az.on("BlobCapacity", metric([({}, [1 * 2**30, 5 * 2**30, None])], "average"))
    az.on(
        "Transactions",
        metric(
            [
                ({"ApiName": "GetBlob"}, [100_000.0, 200_000.0]),
                ({"ApiName": "PutBlob"}, [50_000.0]),
                ({"ApiName": "DeleteBlob"}, [10_000.0]),
            ]
        ),
    )
    res = storage()
    out = collectors.collect_storage_account(res, collectors.Collection(arm(az), collectors.ResourceIndex([res]), {}))
    values = {f.key: f.value for f in out.findings}
    assert values["stored_gb"] == pytest.approx(5.0)
    assert values["read_operations_10k"] == pytest.approx(300_000 * FACTOR / 10_000)
    assert values["write_operations_10k"] == pytest.approx(50_000 * FACTOR / 10_000)
    assert values["other_operations_10k"] == pytest.approx(10_000 * FACTOR / 10_000)
    # blob-service level metrics, not the whole account
    assert any("/blobServices/default/providers/microsoft.insights/metrics" in c for c in az.calls)


def test_workspace_ingestion_and_top_tables(az: FakeAzure) -> None:
    def query(url: str, body: dict[str, object] | None) -> dict[str, object]:
        assert body is not None
        assert isinstance(body["query"], str)
        if "top 5" in body["query"]:
            return {"Tables": [{"Rows": [["StorageBlobLogs", 90.0], ["AppTraces", 10.0]]}]}
        return {"Tables": [{"Rows": [[100.0]]}]}

    az.on("/api/query", query)
    ws = make_resource("Microsoft.OperationalInsights/workspaces", "ws", {"location": "westeurope", "properties": {}})
    ai = make_resource(
        "Microsoft.Insights/components", "ai", {"location": "westeurope", "properties": {"WorkspaceResourceId": ws.id}}
    )
    result = collect_all([ws, ai], arm(az))
    by = {(f.section, f.name, f.key): f.value for f in result.findings}
    assert by[("loganalytics", "ws", "ingest_gb_per_month")] == pytest.approx(100 * FACTOR)
    assert by[("appinsights", "ai", "ingest_gb_per_month")] == 0
    assert "StorageBlobLogs 90%" in result.notes[("loganalytics", "ws")][0]


def container_app(name: str = "app") -> Resource:
    return make_resource("Microsoft.App/containerApps", name, {"location": "westeurope", "apiVersion": "2024-03-01"})


def test_container_app_replica_hours_idle_and_requests(az: FakeAzure) -> None:
    hours = DAYS * 24
    az.on("metricnames=Replicas", metric([({}, [1.0] * hours)], "average"))
    # 5-minute windows: first half busy, second half idle
    windows = hours * 12
    az.on(
        "metricnames=UsageNanoCores",
        metric([({}, [50_000_000.0] * (windows // 2) + [0.0] * (windows // 2))], "average"),
    )
    az.on("metricnames=RxBytes", metric([({}, [0.0] * windows)]))
    az.on("metricnames=TxBytes", metric([({}, [0.0] * windows)]))
    az.on("metricnames=Requests", metric([({}, [1_000_000.0, 1_000_000.0])]))
    res = container_app()
    out = collectors.collect_container_app(res, collectors.Collection(arm(az), collectors.ResourceIndex([res]), {}))
    values = {f.key: f.value for f in out.findings}
    assert values["replica_hours"] == pytest.approx(730)
    assert values["idle_percent"] == pytest.approx(50)
    assert values["requests_millions"] == pytest.approx(2 * FACTOR)


def test_container_app_without_replica_data_is_zero_if_it_exists_else_not_found(az: FakeAzure) -> None:
    res = container_app()
    coll = collectors.Collection(arm(az), collectors.ResourceIndex([res]), {})
    assert collectors.collect_container_app(res, coll).notes == ["not found in Azure"]
    az.on(res.id, {"id": res.id})
    out = collectors.collect_container_app(res, coll)
    assert [(f.key, f.value) for f in out.findings] == [("replica_hours", 0.0)]


def test_file_share_falls_back_to_account_total_split_over_shares(az: FakeAzure) -> None:
    acct = storage("acct")
    shares = [
        make_resource("Microsoft.Storage/storageAccounts/fileServices/shares", f"acct/default/{n}", {})
        for n in ("a", "b")
    ]
    az.on("FileShare%20eq", metric([], "average"))  # per-share filter: nothing
    az.on("metricnames=FileCapacity", metric([({}, [4 * 2**30])], "average"))
    az.on("metricnames=Transactions", metric([({"ApiName": "Read"}, [20_000.0])]))
    out = collectors.collect_file_share(
        shares[0], collectors.Collection(arm(az), collectors.ResourceIndex([acct, *shares]), {})
    )
    values = {f.key: f.value for f in out.findings}
    assert values["used_gb"] == pytest.approx(2.0)
    assert values["read_operations_10k"] == pytest.approx(20_000 * FACTOR / 10_000 / 2)
    assert "split evenly across its 2 shares" in out.notes[0]


def test_costs_drive_session_pools_and_cognitive_accounts(az: FakeAzure) -> None:
    pool = make_resource("Microsoft.App/sessionPools", "pool", {"location": "westeurope"})
    ai = make_resource("Microsoft.CognitiveServices/accounts", "ai", {"location": "westeurope"})
    az.on(
        "CostManagement/query",
        {
            "properties": {
                "columns": [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "Currency"}],
                "rows": [[3.0, pool.id.upper(), "EUR"], [60.0, ai.id, "EUR"]],
            }
        },
    )
    result = collect_all([pool, ai], arm(az))
    by = {(f.section, f.key): f.value for f in result.findings}
    assert by[("sessionpools", "monthly_cost")] == pytest.approx(3.0 * FACTOR)
    assert by[("cognitiveservices", "monthly_cost")] == pytest.approx(60.0 * FACTOR)
    assert result.cost_currency == "EUR"


def test_one_failing_resource_becomes_a_warning_not_a_crash(az: FakeAzure) -> None:
    az.on("resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/bad", AzureError("HTTP 403"))
    result = collect_all([storage("bad"), storage("ok")], arm(az))
    assert any("bad" in w and "403" in w for w in result.warnings)


def test_postgres_and_plan_report_live_values_and_drift(az: FakeAzure) -> None:
    pg = make_resource(
        "Microsoft.DBforPostgreSQL/flexibleServers",
        "pg",
        {
            "location": "westeurope",
            "sku": {"name": "Standard_D2s_v3"},
            "properties": {"storage": {"storageSizeGB": 32}},
        },
    )
    plan = make_resource(
        "Microsoft.Web/serverfarms", "plan", {"location": "westeurope", "sku": {"name": "P1v3", "capacity": 2}}
    )
    az.on(pg.id, {"sku": {"name": "Standard_D2s_v3"}, "properties": {"storage": {"storageSizeGB": 64}}})
    az.on(plan.id, {"sku": {"name": "P1v3", "capacity": 3}})
    result = collect_all([pg, plan], arm(az))
    by = {(f.section, f.key): f.value for f in result.findings}
    assert by[("postgres", "storage_gb")] == 64
    assert by[("appserviceplans", "instances")] == 3
    assert "DRIFT storage: template 32 GB, live 64 GB" in result.notes[("postgres", "pg")][0]
    assert "DRIFT instances: template 2, live 3" in result.notes[("appserviceplans", "plan")][0]


def test_toml_roundtrip_with_awkward_names() -> None:
    findings = [
        Finding("fileshares", 'acct/default/we"ird', "used_gb", 1.5, "src"),
        Finding("storage", "sa", "stored_gb", 12345.678, "src"),
        Finding("containerapps", "app", "requests_millions", 0.00001234, "src"),
        Finding("containerapps", "app", "replica_hours", 730.0, "src"),
    ]
    text = render_usage_toml(findings, {("storage", "sa"): ["a note"]}, ["header"], "EUR")
    data = tomllib.loads(text)
    assert data["meta"]["cost_currency"] == "EUR"
    assert data["fileshares"]['acct/default/we"ird']["used_gb"] == 1.5
    assert data["storage"]["sa"]["stored_gb"] == pytest.approx(12345.68)
    assert data["containerapps"]["app"]["replica_hours"] == 730
    assert data["containerapps"]["app"]["requests_millions"] == pytest.approx(1.234e-5, rel=1e-3)
    assert "# a note" in text


# --- CLI -----------------------------------------------------------------------------------------------------------


TEMPLATE = {
    "resources": [
        {
            "type": "Microsoft.Storage/storageAccounts",
            "apiVersion": "2023-01-01",
            "name": "sa",
            "location": "westeurope",
            "kind": "StorageV2",
            "sku": {"name": "Standard_LRS"},
        },
        {
            "type": "Microsoft.App/containerApps",
            "apiVersion": "2024-03-01",
            "name": "app",
            "location": "westeurope",
            "properties": {
                "template": {
                    "containers": [{"resources": {"cpu": 1, "memory": "2Gi"}}],
                    "scale": {"minReplicas": 1, "maxReplicas": 1},
                }
            },
        },
    ]
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FakeAzure]:
    (tmp_path / "main.json").write_text(json.dumps(TEMPLATE))
    azure = FakeAzure()
    azure.on("BlobCapacity", metric([({}, [100 * 2**30])], "average"))
    azure.on("metricnames=Transactions", metric([({"ApiName": "GetBlob"}, [0.0])]))
    azure.on("metricnames=Replicas", metric([({}, [0.5] * (DAYS * 24))], "average"))
    monkeypatch.setattr(cli, "http_transport", lambda: azure)
    monkeypatch.setattr(cli, "az_current_subscription", lambda: "sub-from-az")
    monkeypatch.setattr(
        cli, "_client", lambda args, currency: PriceClient(currency=currency, cache_dir=None, fetcher=fake_fetcher)
    )
    return tmp_path, azure


def test_collect_writes_usage_file_next_to_template_and_estimate_picks_it_up(
    project: tuple[Path, FakeAzure], capsys: pytest.CaptureFixture[str]
) -> None:
    path, azure = project
    assert cli.main(["collect", str(path / "main.json"), "--resource-group", "my-rg"]) == 0
    usage_file = path / "main.usage.toml"
    data = tomllib.loads(usage_file.read_text())
    assert data["storage"]["sa"]["stored_gb"] == pytest.approx(100)
    assert data["containerapps"]["app"]["replica_hours"] == pytest.approx(0.5 * 730, rel=0.02)
    # the deployment's real subscription / resource group were used for the resource ids
    assert any("/subscriptions/sub-from-az/resourceGroups/my-rg/providers/Microsoft.Storage" in c for c in azure.calls)
    capsys.readouterr()

    assert cli.main(["estimate", str(path / "main.json"), "--format", "json", "--usage", str(usage_file)]) == 0
    report = json.loads(capsys.readouterr().out)
    storage_item = next(i for r in report["resources"] if r["type"].endswith("storageAccounts") for i in r["items"])
    assert "100 GB stored" in storage_item["description"]
    app = next(r for r in report["resources"] if r["type"].endswith("containerApps"))
    assert any("365 active h" in i["description"] for i in app["items"])


def test_estimate_discovers_usage_files_and_reports_them(
    project: tuple[Path, FakeAzure], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = project
    (path / "main.usage.toml").write_text("[storage.sa]\nstored_gb = 7\n")
    assert cli.main(["estimate", str(path / "main.json"), "--detail"]) == 0
    out = capsys.readouterr()
    assert "usage from" in out.err and "main.usage.toml" in out.err
    assert "7 GB stored" in out.out
    assert "built-in defaults" in out.out  # other figures still come from defaults


def test_currency_follows_usage_file_and_discounts_apply(
    project: tuple[Path, FakeAzure], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = project
    (path / "main.usage.toml").write_text(
        '[meta]\ncost_currency = "EUR"\n[discounts]\n"microsoft.app/containerapps" = 50\n'
    )
    assert cli.main(["estimate", str(path / "main.json"), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["currency"] == "EUR"
    app = next(r for r in report["resources"] if r["type"].endswith("containerApps"))
    full = 730 * 3600
    list_price = (full - 180_000) * 3.4e-5 + (2 * full - 360_000) * 4e-6
    assert app["monthly"] == pytest.approx(list_price * 0.5)
    assert any("50% discount" in n for n in app["notes"])


def test_init_usage_lists_every_tunable_with_default(
    project: tuple[Path, FakeAzure], capsys: pytest.CaptureFixture[str]
) -> None:
    path, _ = project
    assert cli.main(["init-usage", str(path / "main.json")]) == 0
    data = tomllib.loads(capsys.readouterr().out)
    assert data["storage"]["sa"]["stored_gb"] == 10
    assert data["containerapps"]["app"]["idle_percent"] == 0


def test_collect_requires_resource_group(project: tuple[Path, FakeAzure]) -> None:
    path, _ = project
    with pytest.raises(SystemExit):
        cli.main(["collect", str(path / "main.json")])
