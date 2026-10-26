from typing import Any

import pytest

from bicep_cost.armexpr import JsonValue, Unknown
from bicep_cost.calc import estimate_resources
from bicep_cost.engine import Resource
from bicep_cost.model import Estimate, ResourceEstimate
from bicep_cost.prices import PriceClient
from bicep_cost.usage import UsageConfig
from tests.conftest import make_resource

HOURS = 730


def run(prices: PriceClient, resources: list[Resource], usage: dict[str, Any] | None = None) -> Estimate:
    return estimate_resources(resources, prices, UsageConfig(usage or {}))


def one(est: Estimate, name: str) -> ResourceEstimate:
    return next(r for r in est.resources if r.resource.name == name)


def workflow(
    name: str = "wf",
    triggers: dict[str, Any] | None = None,
    actions: dict[str, Any] | None = None,
    state: str = "Enabled",
    connections: dict[str, Any] | None = None,
) -> Resource:
    props: dict[str, JsonValue] = {
        "state": state,
        "definition": {"triggers": triggers or {}, "actions": actions or {}},
    }
    if connections is not None:
        props["parameters"] = {"$connections": {"value": connections}}
    return make_resource("Microsoft.Logic/workflows", name, {"location": "westeurope", "properties": props})


def connector(api: str, key: str | None = None) -> dict[str, JsonValue]:
    return {
        "type": "ApiConnection",
        "inputs": {"host": {"connection": {"name": f"@parameters('$connections')['{key or api}']['connectionId']"}}},
    }


REQUEST: dict[str, Any] = {"manual": {"type": "Request", "kind": "Http"}}


def test_logic_app_recurrence_is_priced_from_the_schedule(prices: PriceClient) -> None:
    wf = workflow(
        triggers={"every_min": {"type": "Recurrence", "recurrence": {"frequency": "Minute", "interval": 1}}},
        actions={"a": {"type": "Compose"}, "b": {"type": "Http"}},
    )
    est = run(prices, [wf])
    runs = HOURS * 60
    total = runs + 2 * runs  # one trigger + two built-in actions per run
    assert est.monthly == pytest.approx((total - 4000) * 2.5e-5)
    assert est.fixed == est.monthly


def test_logic_app_event_trigger_uses_assumed_runs_and_free_grant(prices: PriceClient) -> None:
    wf = workflow(triggers=REQUEST, actions={"a": {"type": "Compose"}})
    assert run(prices, [wf]).monthly == 0  # 2,000 operations, inside the 4,000 free built-in operations
    heavy = run(prices, [wf], {"logicapps": {"wf": {"runs_per_month": 100_000}}})
    assert heavy.monthly == pytest.approx((200_000 - 4000) * 2.5e-5)
    assert heavy.usage == heavy.monthly


def test_logic_app_free_grant_is_shared_per_subscription(prices: PriceClient) -> None:
    usage: dict[str, Any] = {"logicapps": {"runs_per_month": 4000}}
    a, b = workflow("a", REQUEST), workflow("b", REQUEST)
    est = run(prices, [a, b], usage)
    assert est.monthly == pytest.approx((8000 - 4000) * 2.5e-5)  # trigger only, 4,000 runs each


def test_logic_app_connector_classes_and_loops(prices: PriceClient) -> None:
    wf = workflow(
        triggers=REQUEST,
        connections={"sap_1": {"id": "/subscriptions/x/providers/Microsoft.Web/locations/westeurope/managedApis/sap"}},
        actions={
            "mail": connector("office365"),
            "loop": {"type": "Foreach", "actions": {"in_loop": connector("sap_1"), "log": {"type": "Compose"}}},
            "branch": {
                "type": "If",
                "actions": {"c": {"type": "Compose"}},
                "else": {"actions": {"d": {"type": "Compose"}}},
            },
        },
    )
    est = run(prices, [wf], {"logicapps": {"wf": {"runs_per_month": 1000, "loop_iterations": 5}}})
    builtin = 1000 * (1 + 1 + 5 + 1 + 2)  # trigger, foreach, 5 x compose, the If, 2 branch composes
    standard = 1000 * 1
    enterprise = 1000 * 5
    expected = max(builtin - 4000, 0) * 2.5e-5 + standard * 0.000125 + enterprise * 0.001
    assert est.monthly == pytest.approx(expected)


def test_logic_app_disabled_or_in_ise_is_not_priced(prices: PriceClient) -> None:
    disabled = workflow("off", REQUEST, {"a": {"type": "Compose"}}, state="Disabled")
    ise = make_resource(
        "Microsoft.Logic/workflows",
        "ise",
        {"location": "westeurope", "properties": {"integrationServiceEnvironment": {"id": "/x"}, "definition": {}}},
    )
    est = run(prices, [disabled, ise])
    assert one(est, "off").status == "free"
    assert one(est, "ise").status == "external"
    assert est.monthly == 0


def test_logic_app_without_known_definition_is_unresolved(prices: PriceClient) -> None:
    wf = make_resource("Microsoft.Logic/workflows", "w", {"location": "westeurope", "properties": {}})
    assert one(run(prices, [wf]), "w").status == "unresolved"


def test_integration_account_tiers(prices: PriceClient) -> None:
    def account(name: str, sku: str) -> Resource:
        return make_resource(
            "Microsoft.Logic/integrationAccounts", name, {"location": "westeurope", "sku": {"name": sku}}
        )

    est = run(prices, [account("f", "Free"), account("b", "Basic"), account("s", "Standard")])
    assert one(est, "f").status == "free"
    assert est.monthly == pytest.approx(1300)


def test_logic_apps_standard_workflow_app_is_included_unless_connector_calls_are_given(prices: PriceClient) -> None:
    plan = make_resource("Microsoft.Web/serverfarms", "ws", {"location": "westeurope", "sku": {"name": "WS1"}})
    app = make_resource(
        "Microsoft.Web/sites",
        "wfapp",
        {"location": "westeurope", "kind": "functionapp,workflowapp", "properties": {"serverFarmId": plan.id}},
    )
    base = run(prices, [plan, app])
    assert one(base, "wfapp").status == "included"
    busy = run(
        prices,
        [plan, app],
        {
            "logicapps": {
                "wfapp": {"connector_actions_per_month": 1_000_000, "enterprise_connector_actions_per_month": 1000}
            }
        },
    )
    assert busy.monthly - base.monthly == pytest.approx(1_000_000 * 0.000179 + 1000 * 0.00143)


def aks(
    tier: str | None = "Standard", pools: list[Any] | None = None, sku_name: str = "Base", **props: JsonValue
) -> Resource:
    sku: dict[str, JsonValue] = {"name": sku_name}
    if tier:
        sku["tier"] = tier
    return make_resource(
        "Microsoft.ContainerService/managedClusters",
        "aks",
        {"location": "westeurope", "sku": sku, "properties": {"agentPoolProfiles": pools or [], **props}},
    )


def pool(name: str = "sys", size: str = "Standard_D2s_v5", count: int = 3, **extra: JsonValue) -> dict[str, JsonValue]:
    return {"name": name, "vmSize": size, "count": count, "osDiskType": "Ephemeral", **extra}


def test_aks_free_tier_charges_only_the_nodes(prices: PriceClient) -> None:
    est = run(prices, [aks("Free", [pool()])])
    assert est.monthly == pytest.approx(3 * 0.115 * HOURS)
    assert est.fixed == est.monthly


def test_aks_standard_and_premium_management_fee(prices: PriceClient) -> None:
    assert run(prices, [aks("Standard")]).monthly == pytest.approx(0.1 * HOURS)
    assert run(prices, [aks("Premium")]).monthly == pytest.approx(0.6 * HOURS)
    assert run(prices, [aks(None)]).monthly == 0  # no tier means Free


def test_aks_node_pool_os_disk_windows_and_spot(prices: PriceClient) -> None:
    managed: dict[str, Any] = {
        "name": "user",
        "vmSize": "Standard_D2s_v5",
        "count": 2,
        "osDiskSizeGB": 128,
        "scaleSetPriority": "Spot",
    }
    windows = pool("win", count=1, osType="Windows")
    est = run(prices, [aks("Free", [managed, windows])])
    # P10 128 GB OS disk per node; spot node; Windows node priced at the Windows rate
    assert est.monthly == pytest.approx(2 * (0.0213 * HOURS + 21.68) + 0.207 * HOURS)


def test_aks_autoscaled_pool_uses_min_count_unless_overridden(prices: PriceClient) -> None:
    auto = pool(count=0, enableAutoScaling=True, minCount=2, maxCount=10)
    del auto["count"]
    assert run(prices, [aks("Free", [auto])]).monthly == pytest.approx(2 * 0.115 * HOURS)
    override = run(prices, [aks("Free", [auto])], {"aksnodepools": {"aks/sys": {"nodes": 6}}})
    assert override.monthly == pytest.approx(6 * 0.115 * HOURS)


def test_aks_standalone_agent_pool_and_unknown_size(prices: PriceClient) -> None:
    cluster = aks("Free")
    extra = make_resource(
        "Microsoft.ContainerService/managedClusters/agentPools", "aks/user", {"properties": pool("user", count=2)}
    )
    bad = make_resource(
        "Microsoft.ContainerService/managedClusters/agentPools", "aks/bad", {"properties": {"name": "bad"}}
    )
    est = run(prices, [cluster, extra, bad])
    assert one(est, "aks/user").monthly == pytest.approx(2 * 0.115 * HOURS)
    assert one(est, "aks/bad").status == "unresolved"


def test_aks_automatic_prices_the_hosted_control_plane(prices: PriceClient) -> None:
    est = run(prices, [aks("Standard", [pool()], sku_name="Automatic")], {"aks": {"aks": {"monthly_cost": 200}}})
    assert est.monthly == pytest.approx(0.16 * HOURS + 200)


def test_logic_app_sliding_window_counts_as_schedule(prices: PriceClient) -> None:
    wf = workflow(
        triggers={"w": {"type": "SlidingWindow", "recurrence": {"frequency": "Hour", "interval": 1}}},
        actions={"a": {"type": "Compose"}},
    )
    assert run(prices, [wf]).monthly == 0  # 1,460 operations, inside the free built-in grant
    wf = workflow(
        triggers={"w": {"type": "SlidingWindow", "recurrence": {"frequency": "Minute", "interval": 1}}},
        actions={"a": {"type": "Compose"}},
    )
    assert run(prices, [wf]).monthly == pytest.approx((2 * HOURS * 60 - 4000) * 2.5e-5)


def test_logic_app_polling_connector_trigger_fires_per_event(prices: PriceClient) -> None:
    trigger = connector("servicebus")
    trigger["recurrence"] = {"frequency": "Day", "interval": 1}
    wf = workflow(triggers={"sb": trigger})
    est = run(prices, [wf], {"logicapps": {"wf": {"runs_per_month": 100_000}}})
    assert est.monthly == pytest.approx(100_000 * 0.000125)  # not just 30 polls


def test_aks_child_pool_inherits_hybrid_benefit_and_default_disk_follows_vcpus(prices: PriceClient) -> None:
    cluster = aks("Free", windowsProfile={"licenseType": "Windows_Server"})
    win = make_resource(
        "Microsoft.ContainerService/managedClusters/agentPools",
        "aks/win",
        {"properties": {"vmSize": "Standard_D2s_v5", "count": 1, "osType": "Windows", "osDiskType": "Ephemeral"}},
    )
    assert one(run(prices, [cluster, win]), "aks/win").monthly == pytest.approx(0.115 * HOURS)  # Linux rate


def test_aks_default_os_disk_size_by_vcpu() -> None:
    from bicep_cost.estimators.aks import default_os_disk_gb

    assert [
        default_os_disk_gb(s) for s in ("Standard_D2s_v5", "Standard_D8s_v5", "Standard_E16ds_v5", "Standard_M64s")
    ] == [
        128.0,
        256.0,
        512.0,
        1024.0,
    ]


def test_aks_pool_declared_inline_and_as_child_is_priced_once(prices: PriceClient) -> None:
    cluster = aks("Free", [pool("user", count=3)])
    child = make_resource(
        "Microsoft.ContainerService/managedClusters/agentPools", "aks/user", {"properties": pool("user", count=3)}
    )
    est = run(prices, [cluster, child])
    assert one(est, "aks/user").status == "included"
    assert est.monthly == pytest.approx(3 * 0.115 * HOURS)


def test_unknown_pools_or_workflow_actions_are_unresolved_not_free(prices: PriceClient) -> None:
    cluster = make_resource(
        "Microsoft.ContainerService/managedClusters",
        "aks",
        {"location": "westeurope", "sku": {"tier": "Free"}, "properties": {"agentPoolProfiles": Unknown("param")}},
    )
    wf = make_resource(
        "Microsoft.Logic/workflows",
        "wf",
        {"location": "westeurope", "properties": {"definition": {"triggers": {}, "actions": Unknown("param")}}},
    )
    est = run(prices, [cluster, wf])
    assert one(est, "aks").status == "unresolved"
    assert one(est, "wf").status == "unresolved"


def test_unknown_nested_actions_pool_entries_and_tier_are_unresolved(prices: PriceClient) -> None:
    wf = workflow(actions={"scope": {"type": "Scope", "actions": Unknown("param")}})
    pools = make_resource(
        "Microsoft.ContainerService/managedClusters",
        "pools",
        {"location": "westeurope", "sku": {"tier": "Free"}, "properties": {"agentPoolProfiles": [Unknown("p")]}},
    )
    tier = make_resource(
        "Microsoft.ContainerService/managedClusters",
        "tier",
        {"location": "westeurope", "sku": {"tier": Unknown("p")}, "properties": {"agentPoolProfiles": []}},
    )
    est = run(prices, [wf, pools, tier])
    assert [one(est, n).status for n in ("wf", "pools", "tier")] == ["unresolved"] * 3


def test_unknown_values_that_change_the_price_are_unresolved(prices: PriceClient) -> None:
    def wf_with(**props: Any) -> Resource:
        return make_resource("Microsoft.Logic/workflows", "w", {"location": "westeurope", "properties": props})

    definition: dict[str, Any] = {"triggers": {}, "actions": {}}
    cases = [
        wf_with(state=Unknown("p"), definition=definition),
        wf_with(definition={"triggers": {}, "actions": {"a": Unknown("p")}}),
        wf_with(
            definition={
                "triggers": {
                    "r": {"type": "Recurrence", "recurrence": {"frequency": "Minute", "interval": Unknown("p")}}
                },
                "actions": {},
            }
        ),
        make_resource(
            "Microsoft.ContainerService/managedClusters",
            "c",
            {
                "location": "westeurope",
                "sku": {"tier": "Free"},
                "properties": {
                    "agentPoolProfiles": [{"name": "p", "vmSize": "Standard_D2s_v5", "count": Unknown("p")}]
                },
            },
        ),
        make_resource(
            "Microsoft.ContainerService/managedClusters",
            "c",
            {"location": "westeurope", "sku": {"name": Unknown("p")}, "properties": {}},
        ),
    ]
    assert [run(prices, [c]).resources[0].status for c in cases] == ["unresolved"] * len(cases)


def test_recurrence_schedule_multiplies_the_rate(prices: PriceClient) -> None:
    def runs(recurrence: dict[str, Any]) -> float:
        wf = workflow(triggers={"r": {"type": "Recurrence", "recurrence": recurrence}})
        return run(prices, [wf]).resources[0].items[0].quantity

    assert runs({"frequency": "Hour", "interval": 1, "schedule": {"minutes": [0, 15, 30, 45]}}) == pytest.approx(
        4 * HOURS
    )
    assert runs({"frequency": "Month", "interval": 1, "schedule": {"monthDays": [1, 15]}}) == pytest.approx(2)
