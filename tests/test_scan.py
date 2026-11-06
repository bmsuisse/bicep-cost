import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from bicep_cost import cli
from bicep_cost.armexpr import JsonValue
from bicep_cost.calc import estimate_resources
from bicep_cost.collect.arm import ArmClient, AzureError
from bicep_cost.engine import Resource
from bicep_cost.prices import PriceClient
from bicep_cost.scan import (
    ambiguous_ids,
    attach_known_disks,
    fetch_inventory,
    in_scope,
    reconcile,
    split_id,
    to_resources,
    without_aks_nodes,
)
from bicep_cost.usage import UsageConfig
from tests.conftest import fake_fetcher
from tests.test_collect import FakeAzure

SUB = "sub1"
PLAN = f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Web/serverFarms/plan1"
WORKSPACE = f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Databricks/workspaces/dbx"
GONE_VM = f"/subscriptions/{SUB}/resourceGroups/rg-managed/providers/Microsoft.Compute/virtualMachines/node-7"
OTHER_GROUP = f"/subscriptions/{SUB}/resourceGroups/rg-b/providers/Microsoft.Databricks/workspaces/other"
DAYS = 30
FACTOR = 730 / 24 / DAYS


def row(resource_id: str, type_: str, **fields: JsonValue) -> dict[str, JsonValue]:
    return {"id": resource_id, "name": resource_id.rsplit("/", 1)[1], "type": type_.lower(), **fields}


PLAN_ROW = row(
    PLAN,
    "Microsoft.Web/serverFarms",
    location="westeurope",
    kind="linux",
    sku={"name": "P1v3", "capacity": 1},
    properties={"reserved": True},
)
WORKSPACE_ROW = row(WORKSPACE, "Microsoft.Databricks/workspaces", location="westeurope", sku={"name": "premium"})


def costs_of(*rows: tuple[float, str]) -> dict[str, tuple[float, str]]:
    """Cost Management rows as ``collect`` keys them: lower-cased resource id -> (cost, currency)."""
    return {rid.lower(): (cost, "EUR") for cost, rid in rows}


def costs_payload(*rows: tuple[float, str]) -> dict[str, object]:
    return {
        "properties": {
            "columns": [{"name": "Cost"}, {"name": "ResourceId"}, {"name": "Currency"}],
            "rows": [[cost, rid, "EUR"] for cost, rid in rows],
        }
    }


def test_split_id_derives_child_names_and_rejects_other_ids() -> None:
    db = f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Sql/servers/srv/databases/app"
    assert split_id(db) == (SUB, "rg-a", "Microsoft.Sql/servers/databases", "srv/app")
    assert split_id(f"/subscriptions/{SUB}/resourceGroups/rg-a") is None
    assert split_id(f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Sql/servers") is None


def test_to_resources_keeps_live_fields_and_skips_unparsable_rows() -> None:
    resources = to_resources([PLAN_ROW, {"id": "nonsense"}, {"name": "no id"}])
    assert [r.name for r in resources] == ["plan1"]
    plan = resources[0]
    assert plan.resource_group == "rg-a"
    assert plan.sku == {"name": "P1v3", "capacity": 1}
    assert plan.get("properties.reserved") is True


def test_fetch_inventory_follows_skip_tokens_and_filters_groups() -> None:
    az = FakeAzure()
    pages: Iterator[dict[str, object]] = iter(
        [
            {"data": [PLAN_ROW], "$skipToken": "next"},
            {"data": [WORKSPACE_ROW]},
        ]
    )
    bodies: list[dict[str, object]] = []

    def respond(url: str, body: dict[str, object] | None) -> dict[str, object]:
        assert body is not None
        bodies.append(body)
        return next(pages)

    az.on("Microsoft.ResourceGraph/resources", respond)
    rows = fetch_inventory(ArmClient(az), SUB, ("rg-a",))
    assert len(rows) == 2
    query = str(bodies[0]["query"])
    assert "resourceGroup in~ ('rg-a')" in query
    assert query.index("where") < query.index("project")  # project drops the resourceGroup column
    assert bodies[0]["subscriptions"] == [SUB]
    assert bodies[1]["options"] == {"$top": 1000, "$skipToken": "next"}


def test_fetch_inventory_rejects_suspicious_group_names_and_empty_results() -> None:
    az = FakeAzure()
    with pytest.raises(ValueError, match="invalid resource group"):
        fetch_inventory(ArmClient(az), SUB, ("rg') | take 1 //",))
    with pytest.raises(AzureError):
        fetch_inventory(ArmClient(az), SUB)


def test_in_scope_keeps_only_the_selected_groups() -> None:
    costs = costs_of((1.0, PLAN), (2.0, OTHER_GROUP))
    assert set(in_scope(costs, SUB, ("rg-a",))) == {PLAN.lower()}
    assert set(in_scope(costs, SUB, ())) == set(costs)


def estimate_and_reconcile(
    prices: PriceClient, rows: list[dict[str, JsonValue]], costs: dict[str, tuple[float, str]]
) -> tuple[list[str], float, float, dict[str, float]]:
    estimate = estimate_resources(to_resources(rows), prices, UsageConfig())
    coverage = reconcile(estimate, costs, DAYS)
    return [r.status for r in estimate.resources], estimate.monthly, coverage.actual, coverage.outside


def test_unsupported_types_are_priced_from_their_actual_cost(prices: PriceClient) -> None:
    costs = costs_of((100.0, WORKSPACE))
    statuses, monthly, actual, outside = estimate_and_reconcile(prices, [WORKSPACE_ROW], costs)
    assert statuses == ["priced"]
    assert monthly == pytest.approx(100 * FACTOR)
    assert actual == pytest.approx(100 * FACTOR)
    assert outside == {}


def test_unsupported_type_without_cost_stays_unpriced(prices: PriceClient) -> None:
    statuses, monthly, _, _ = estimate_and_reconcile(prices, [WORKSPACE_ROW], costs_of((5.0, PLAN)))
    assert statuses == ["unsupported"]
    assert monthly == 0


def test_modelled_resources_keep_their_model_and_actual_is_compared(prices: PriceClient) -> None:
    estimate = estimate_resources(to_resources([PLAN_ROW]), prices, UsageConfig())
    modelled = estimate.monthly
    coverage = reconcile(estimate, costs_of((50.0, PLAN)), DAYS)
    assert estimate.monthly == modelled > 0
    assert coverage.rows[0].modelled == pytest.approx(modelled)
    assert coverage.rows[0].actual == pytest.approx(50 * FACTOR)
    assert coverage.rows[0].measured == 0


def test_resources_of_a_managed_service_are_priced_from_the_bill(prices: PriceClient) -> None:
    managed = {**PLAN_ROW, "managedBy": WORKSPACE}
    estimate = estimate_resources(to_resources([managed]), prices, UsageConfig())
    assert estimate.monthly > 0
    coverage = reconcile(estimate, costs_of((10.0, PLAN)), DAYS)
    assert estimate.monthly == pytest.approx(10 * FACTOR)
    assert coverage.rows[0].measured == 1


def test_cost_of_resources_missing_from_the_inventory_is_reported_by_type(prices: PriceClient) -> None:
    costs = costs_of((1.0, PLAN), (30.0, GONE_VM))
    _, _, actual, outside = estimate_and_reconcile(prices, [PLAN_ROW], costs)
    assert outside == {"microsoft.compute/virtualmachines": pytest.approx(30 * FACTOR)}
    assert actual == pytest.approx(31 * FACTOR)


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> FakeAzure:
    fake = FakeAzure()
    monkeypatch.setattr(cli, "http_transport", lambda *a, **k: fake)
    monkeypatch.setattr(cli, "az_current_subscription", lambda: SUB)
    monkeypatch.setattr(
        cli,
        "_client",
        lambda args, currency: PriceClient(currency=currency.upper(), cache_dir=None, fetcher=fake_fetcher),
    )
    fake.on("Microsoft.ResourceGraph/resources", {"data": [PLAN_ROW, WORKSPACE_ROW]})
    return fake


def test_scan_without_actuals_prices_the_inventory_and_calls_no_cost_api(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["scan", "--currency", "USD"])
    out = capsys.readouterr()
    assert code == 0
    assert "TYPE" in out.out
    assert "Microsoft.Web/serverFarms" in out.out
    assert "Microsoft.Databricks/workspaces x1" in out.out
    assert "Coverage" not in out.out
    assert not any("CostManagement" in c for c in azure.calls)


def test_scan_actuals_adds_coverage_uses_billing_currency_and_prices_unsupported(
    azure: FakeAzure,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    azure.on("Microsoft.CostManagement/query", costs_payload((50.0, PLAN), (200.0, WORKSPACE), (9.0, GONE_VM)))
    target = tmp_path / "scan.json"
    code = cli.main(["scan", "--actuals", "--format", "json", "-o", str(target)])
    capsys.readouterr()
    assert code == 0
    report = json.loads(target.read_text())
    assert report["currency"] == "EUR"
    coverage = report["coverage"]
    assert {t["type"] for t in coverage["types"]} == {"microsoft.web/serverfarms", "microsoft.databricks/workspaces"}
    assert coverage["outside_inventory"] == {"microsoft.compute/virtualmachines": pytest.approx(9 * FACTOR, abs=0.01)}
    workspace = next(t for t in coverage["types"] if t["type"] == "microsoft.databricks/workspaces")
    assert workspace["priced_from_actuals"] == 1


def test_scan_rejects_a_currency_that_differs_from_the_bill(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str]
) -> None:
    azure.on("Microsoft.CostManagement/query", costs_payload((50.0, PLAN)))
    code = cli.main(["scan", "--actuals", "--currency", "CHF"])
    assert code == 1
    assert "no conversion" in capsys.readouterr().err


def test_scan_strict_fails_on_unpriced_resources(azure: FakeAzure, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["scan", "--strict", "--currency", "USD"])
    capsys.readouterr()
    assert code == 2


def test_managed_resource_without_a_billing_row_keeps_its_model(prices: PriceClient) -> None:
    managed = {**PLAN_ROW, "managedBy": WORKSPACE}
    estimate = estimate_resources(to_resources([managed]), prices, UsageConfig())
    modelled = estimate.monthly
    reconcile(estimate, costs_of((10.0, GONE_VM)), DAYS)
    assert estimate.monthly == modelled > 0


def test_priced_but_empty_estimates_are_rescued_by_actuals(prices: PriceClient) -> None:
    account = row(
        f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.CognitiveServices/accounts/ai",
        "Microsoft.CognitiveServices/accounts",
        location="westeurope",
        kind="OpenAI",
        sku={"name": "S0"},
    )
    estimate = estimate_resources(to_resources([account]), prices, UsageConfig())
    assert estimate.monthly == 0
    reconcile(estimate, costs_of((40.0, str(account["id"]))), DAYS)
    assert estimate.monthly == pytest.approx(40 * FACTOR)


def test_same_name_in_two_groups_is_ambiguous() -> None:
    twin = row(PLAN.replace("rg-a", "rg-b"), "Microsoft.Web/serverFarms")
    resources = to_resources([PLAN_ROW, twin, WORKSPACE_ROW])
    assert ambiguous_ids(resources) == {PLAN.lower(), PLAN.replace("rg-a", "rg-b").lower()}


def test_resources_of_aks_node_groups_are_left_to_the_cluster_estimate() -> None:
    cluster = row(
        f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.ContainerService/managedClusters/aks",
        "Microsoft.ContainerService/managedClusters",
        properties={"nodeResourceGroup": "MC_rg-a_aks_westeurope"},
    )
    nodes = row(
        f"/subscriptions/{SUB}/resourceGroups/mc_rg-a_aks_westeurope/providers/Microsoft.Compute/virtualMachineScaleSets/pool1",
        "Microsoft.Compute/virtualMachineScaleSets",
    )
    balancer = row(
        f"/subscriptions/{SUB}/resourceGroups/mc_rg-a_aks_westeurope/providers/Microsoft.Network/loadBalancers/kubernetes",
        "Microsoft.Network/loadBalancers",
    )
    kept, skipped = without_aks_nodes(to_resources([cluster, nodes, balancer, PLAN_ROW]))
    assert skipped == 1  # node compute only: the load balancer is not part of the cluster estimate
    assert [r.name for r in kept] == ["aks", "kubernetes", "plan1"]


def test_scan_rejects_a_usage_file_in_another_currency(
    azure: FakeAzure, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    usage = tmp_path / "u.toml"
    usage.write_text('[meta]\ncost_currency = "EUR"\n')
    code = cli.main(["scan", "--currency", "USD", "--usage", str(usage)])
    assert code == 1
    assert "currency conversion is not supported" in capsys.readouterr().err


def create_options(vm: Resource) -> list[object]:
    storage = vm.get("properties.storageProfile")
    assert isinstance(storage, dict)
    os_disk, data = storage["osDisk"], storage["dataDisks"]
    assert isinstance(os_disk, dict)
    assert isinstance(data, list)
    return [os_disk["createOption"], *(d["createOption"] for d in data if isinstance(d, dict))]


def test_disks_listed_in_the_inventory_are_not_priced_again_by_their_vm() -> None:
    disk_id = f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Compute/disks/vm1_OsDisk"
    vm = row(
        f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Compute/virtualMachines/vm1",
        "Microsoft.Compute/virtualMachines",
        properties={
            "storageProfile": {
                "osDisk": {"createOption": "FromImage", "managedDisk": {"id": disk_id}},
                "dataDisks": [
                    {"createOption": "Empty", "managedDisk": {"id": disk_id.replace("OsDisk", "unknown")}},
                    {"createOption": "Empty", "managedDisk": {"id": disk_id}},
                ],
            }
        },
    )
    resources = to_resources([vm, row(disk_id, "Microsoft.Compute/disks")])
    attach_known_disks(resources)
    assert create_options(resources[0]) == ["Attach", "Empty", "Attach"]


def test_free_estimates_with_a_billing_row_are_priced_from_the_bill(prices: PriceClient) -> None:
    nic_id = f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Network/networkInterfaces/nic"
    estimate = estimate_resources(
        to_resources([row(nic_id, "Microsoft.Network/networkInterfaces")]), prices, UsageConfig()
    )
    assert estimate.resources[0].status == "free"
    reconcile(estimate, costs_of((12.0, nic_id)), DAYS)
    assert estimate.monthly == pytest.approx(12 * FACTOR)


def test_an_explicit_zero_estimate_is_kept(prices: PriceClient) -> None:
    account_id = f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Storage/storageAccounts/st1"
    account = row(
        account_id,
        "Microsoft.Storage/storageAccounts",
        location="westeurope",
        kind="StorageV2",
        sku={"name": "Standard_LRS"},
    )
    zero = {"stored_gb": 0, "write_operations_10k": 0, "read_operations_10k": 0, "other_operations_10k": 0}
    estimate = estimate_resources(to_resources([account]), prices, UsageConfig({"storage": {"st1": zero}}))
    assert estimate.resources[0].items  # modelled, and the model says zero
    reconcile(estimate, costs_of((500.0, account_id)), DAYS)
    assert estimate.monthly < 500


def test_databricks_tagged_nodes_are_priced_from_the_bill(prices: PriceClient) -> None:
    tagged: dict[str, JsonValue] = {**PLAN_ROW, "tags": {"Vendor": "Databricks", "ClusterId": "0101-abc"}}
    estimate = estimate_resources(to_resources([tagged]), prices, UsageConfig())
    assert estimate.monthly > 0
    reconcile(estimate, costs_of((10.0, PLAN)), DAYS)
    assert estimate.monthly == pytest.approx(10 * FACTOR)


def test_sub_cent_billing_rows_do_not_turn_free_resources_into_billed_ones(prices: PriceClient) -> None:
    nic_id = f"/subscriptions/{SUB}/resourceGroups/rg-a/providers/Microsoft.Network/networkInterfaces/nic"
    estimate = estimate_resources(
        to_resources([row(nic_id, "Microsoft.Network/networkInterfaces")]), prices, UsageConfig()
    )
    reconcile(estimate, costs_of((0.004, nic_id)), DAYS)
    assert estimate.resources[0].status == "free"
