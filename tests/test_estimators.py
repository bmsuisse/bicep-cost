import pytest

from bicep_cost.armexpr import JsonValue, Unknown
from bicep_cost.calc import estimate_resources
from bicep_cost.engine import Resource
from bicep_cost.model import Estimate, ResourceEstimate
from bicep_cost.prices import PriceClient
from bicep_cost.usage import UsageConfig
from tests.conftest import make_resource

HOURS = 730
SECONDS = HOURS * 3600


def run(prices: PriceClient, resources: list[Resource], usage: dict[str, object] | None = None) -> Estimate:
    return estimate_resources(resources, prices, UsageConfig(usage or {}))


def one(est: Estimate, name: str) -> ResourceEstimate:
    return next(r for r in est.resources if r.resource.name == name)


def plan(name: str = "plan", sku: str = "P1v3", capacity: int = 2, kind: str = "linux", **extra: JsonValue) -> Resource:
    return make_resource(
        "Microsoft.Web/serverfarms",
        name,
        {
            "location": "westeurope",
            "kind": kind,
            "sku": {"name": sku, "capacity": capacity},
            "properties": {"reserved": True},
            **extra,
        },
    )


def test_app_service_plan_linux_premium(prices: PriceClient) -> None:
    est = run(prices, [plan()])
    assert est.monthly == pytest.approx(0.178 * HOURS * 2)
    assert est.fixed == est.monthly


def test_app_service_plan_windows_uses_windows_product(prices: PriceClient) -> None:
    win = make_resource(
        "Microsoft.Web/serverfarms", "w", {"location": "westeurope", "kind": "app", "sku": {"name": "P1v3"}}
    )
    assert run(prices, [win]).monthly == pytest.approx(0.338 * HOURS)


def test_logic_apps_standard_plan(prices: PriceClient) -> None:
    p = make_resource("Microsoft.Web/serverfarms", "ws", {"location": "westeurope", "sku": {"name": "WS1"}})
    assert run(prices, [p]).monthly == pytest.approx(HOURS * (1 * 0.2 + 3.5 * 0.014))


def test_unknown_sku_is_unresolved_not_a_crash(prices: PriceClient) -> None:
    p = make_resource("Microsoft.Web/serverfarms", "p", {"location": "westeurope", "sku": {"name": Unknown("param")}})
    est = run(prices, [p])
    assert one(est, "p").status == "unresolved"
    assert est.monthly == 0


def test_site_on_plan_in_template_is_included_and_without_plan_is_external(prices: PriceClient) -> None:
    pl = plan()
    inside = make_resource("Microsoft.Web/sites", "a", {"kind": "app,linux", "properties": {"serverFarmId": pl.id}})
    outside = make_resource(
        "Microsoft.Web/sites", "b", {"kind": "app", "properties": {"serverFarmId": "/subscriptions/x/other"}}
    )
    est = run(prices, [pl, inside, outside])
    assert one(est, "a").status == "included"
    assert one(est, "b").status == "external"


def test_consumption_function_uses_shared_free_grant(prices: PriceClient) -> None:
    pl = make_resource("Microsoft.Web/serverfarms", "y1", {"location": "westeurope", "sku": {"name": "Y1"}})
    fa = make_resource(
        "Microsoft.Web/sites",
        "fa",
        {"location": "westeurope", "kind": "functionapp", "properties": {"serverFarmId": pl.id}},
    )
    free = run(prices, [pl, fa])
    assert free.monthly == 0  # default usage is inside the free grant
    heavy = run(
        prices,
        [pl, fa],
        {"functions": {"fa": {"executions_per_month": 11_000_000, "avg_duration_ms": 1000, "memory_mb": 1024}}},
    )
    # 11M executions: (1.1M tens - 100k tens) * 2e-6 = 2.0; 11M GB-s: (11M - 400k) * 1.6e-5
    assert heavy.monthly == pytest.approx(2.0 + (11_000_000 - 400_000) * 1.6e-5)
    assert heavy.usage == heavy.monthly


def container_app(
    name: str,
    cpu: float = 1.0,
    mem: str = "2Gi",
    min_r: int = 1,
    max_r: int = 1,
    rules: list[JsonValue] | None = None,
    profile: str | None = None,
) -> Resource:
    props: dict[str, JsonValue] = {
        "environmentId": "/subscriptions/x/env",
        "template": {
            "containers": [{"resources": {"cpu": cpu, "memory": mem}}],
            "scale": {"minReplicas": min_r, "maxReplicas": max_r, "rules": rules or []},
        },
    }
    if profile:
        props["workloadProfileName"] = profile
    return make_resource("Microsoft.App/containerApps", name, {"location": "westeurope", "properties": props})


def aca_cost(vcpu_seconds: float, gib_seconds: float) -> float:
    return max(vcpu_seconds - 180_000, 0) * 3.4e-5 + max(gib_seconds - 360_000, 0) * 4e-6


def test_container_app_always_on_with_free_grant(prices: PriceClient) -> None:
    est = run(prices, [container_app("a")])
    assert est.monthly == pytest.approx(aca_cost(SECONDS, 2 * SECONDS))


def test_free_grant_is_shared_between_apps_in_a_subscription(prices: PriceClient) -> None:
    est = run(prices, [container_app("a"), container_app("b")])
    assert est.monthly == pytest.approx(aca_cost(2 * SECONDS, 4 * SECONDS))
    a, b = one(est, "a"), one(est, "b")
    assert a.monthly == pytest.approx(b.monthly)
    assert a.monthly + b.monthly == pytest.approx(est.monthly)


def test_grant_is_per_subscription(prices: PriceClient) -> None:
    a = container_app("a")
    b = make_resource(
        "Microsoft.App/containerApps",
        "b",
        {"location": "westeurope", "properties": container_app("b").properties},
        sub="sub2",
    )
    est = run(prices, [a, b])
    assert est.monthly == pytest.approx(2 * aca_cost(SECONDS, 2 * SECONDS))


def test_scale_to_zero_costs_nothing_and_cron_adds_scheduled_hours(prices: PriceClient) -> None:
    zero = container_app("z", min_r=0)
    cron = container_app(
        "c",
        min_r=0,
        rules=[
            {
                "name": "bh",
                "custom": {
                    "type": "cron",
                    "metadata": {"start": "0 7 * * 1-5", "end": "0 19 * * 1-5", "desiredReplicas": "1"},
                },
            }
        ],
    )
    est = run(prices, [zero, cron])
    assert one(est, "z").status == "free"
    hours = 12 * 5 * 730 / 168
    assert one(est, "c").monthly == pytest.approx(aca_cost(hours * 3600, 2 * hours * 3600))
    assert one(est, "c").subtotal("fixed") > 0  # the schedule comes from the template, not from an assumption


def test_idle_percent_moves_baseline_to_idle_rates(prices: PriceClient) -> None:
    est = run(prices, [container_app("a")], {"containerapps": {"a": {"idle_percent": 100}}})
    assert est.monthly == pytest.approx(SECONDS * 4e-6 + 2 * SECONDS * 4e-6)


def test_dedicated_profile_app_is_billed_on_environment(prices: PriceClient) -> None:
    app = container_app("d", profile="D4")
    env = make_resource(
        "Microsoft.App/managedEnvironments",
        "env",
        {
            "location": "westeurope",
            "properties": {
                "workloadProfiles": [
                    {"name": "Consumption", "workloadProfileType": "Consumption"},
                    {"name": "D4", "workloadProfileType": "D4", "minimumCount": 1},
                ]
            },
        },
    )
    est = run(prices, [app, env])
    assert one(est, "d").status == "included"
    assert one(est, "env").monthly == pytest.approx(HOURS * (0.1 + 4 * 0.08 + 16 * 0.0066))


def test_session_pool_is_usage_only(prices: PriceClient) -> None:
    pool = make_resource(
        "Microsoft.App/sessionPools", "p", {"location": "westeurope", "properties": {"containerType": "PythonLTS"}}
    )
    assert run(prices, [pool]).monthly == 0
    assert run(prices, [pool], {"sessionpools": {"p": {"session_hours": 100}}}).monthly == pytest.approx(3.0)


def test_postgres_general_purpose_and_ha(prices: PriceClient) -> None:
    pg = make_resource(
        "Microsoft.DBforPostgreSQL/flexibleServers",
        "pg",
        {
            "location": "westeurope",
            "sku": {"name": "Standard_D2s_v3", "tier": "GeneralPurpose"},
            "properties": {"storage": {"storageSizeGB": 32}, "highAvailability": {"mode": "Disabled"}},
        },
    )
    assert run(prices, [pg]).monthly == pytest.approx(2 * 0.1015 * HOURS + 32 * 0.1369)
    ha = make_resource(
        "Microsoft.DBforPostgreSQL/flexibleServers",
        "ha",
        {
            "location": "westeurope",
            "sku": {"name": "Standard_D2s_v3", "tier": "GeneralPurpose"},
            "properties": {"storage": {"storageSizeGB": 32}, "highAvailability": {"mode": "ZoneRedundant"}},
        },
    )
    assert run(prices, [ha]).monthly == pytest.approx(2 * (2 * 0.1015 * HOURS + 32 * 0.1369))


def test_postgres_burstable_uses_whole_node_price(prices: PriceClient) -> None:
    pg = make_resource(
        "Microsoft.DBforPostgreSQL/flexibleServers",
        "b",
        {
            "location": "westeurope",
            "sku": {"name": "Standard_B1ms", "tier": "Burstable"},
            "properties": {"storage": {"storageSizeGB": 32}},
        },
    )
    assert run(prices, [pg]).monthly == pytest.approx(0.0199 * HOURS + 32 * 0.1369)


def storage(name: str = "sa", sku: str = "Standard_LRS", **props: JsonValue) -> Resource:
    return make_resource(
        "Microsoft.Storage/storageAccounts",
        name,
        {"location": "westeurope", "kind": "StorageV2", "sku": {"name": sku}, "properties": props},
    )


def test_storage_usage_from_file_and_glob(prices: PriceClient) -> None:
    est = run(
        prices,
        [storage("stapp")],
        {
            "storage": {
                "stap*": {
                    "stored_gb": 1000,
                    "write_operations_10k": 0,
                    "read_operations_10k": 0,
                    "other_operations_10k": 0,
                }
            }
        },
    )
    assert est.monthly == pytest.approx(1000 * 0.0196)
    assert est.fixed == 0


def test_storage_grs_uses_grs_meter(prices: PriceClient) -> None:
    est = run(
        prices,
        [storage(sku="Standard_GRS")],
        {"storage": {"stored_gb": 100, "write_operations_10k": 0, "read_operations_10k": 0, "other_operations_10k": 0}},
    )
    assert est.monthly == pytest.approx(100 * 0.0392)


def test_storage_children_are_free_and_share_priced(prices: PriceClient) -> None:
    sa = storage("acct")
    share = make_resource(
        "Microsoft.Storage/storageAccounts/fileServices/shares", "acct/default/s", {"properties": {"shareQuota": 100}}
    )
    container = make_resource("Microsoft.Storage/storageAccounts/blobServices/containers", "acct/default/c", {})
    est = run(
        prices,
        [sa, share, container],
        {
            "fileshares": {
                "acct/default/s": {
                    "used_gb": 10,
                    "write_operations_10k": 0,
                    "read_operations_10k": 0,
                    "other_operations_10k": 0,
                }
            }
        },
    )
    assert one(est, "acct/default/s").monthly == pytest.approx(10 * 0.06)
    assert one(est, "acct/default/c").status == "free"


def workspace(name: str = "ws", **props: JsonValue) -> Resource:
    return make_resource(
        "Microsoft.OperationalInsights/workspaces",
        name,
        {"location": "westeurope", "properties": {"sku": {"name": "PerGB2018"}, "retentionInDays": 30, **props}},
    )


def test_log_analytics_free_tier_is_shared_with_app_insights(prices: PriceClient) -> None:
    ws = workspace()
    ai = make_resource(
        "Microsoft.Insights/components", "ai", {"location": "westeurope", "properties": {"WorkspaceResourceId": ws.id}}
    )
    assert run(prices, [ws, ai]).monthly == 0  # 2 GB + 1 GB < 5 GB free
    est = run(
        prices,
        [ws, ai],
        {"loganalytics": {"ws": {"ingest_gb_per_month": 6}}, "appinsights": {"ai": {"ingest_gb_per_month": 2}}},
    )
    assert est.monthly == pytest.approx((8 - 5) * 2.99)
    assert one(est, "ws").monthly + one(est, "ai").monthly == pytest.approx(est.monthly)


def test_log_analytics_retention_beyond_31_days(prices: PriceClient) -> None:
    ws = workspace(retentionInDays=91)
    est = run(prices, [ws], {"loganalytics": {"ws": {"ingest_gb_per_month": 5}}})
    assert est.monthly == pytest.approx(5 * (91 - 31) / 30 * 0.13)


@pytest.mark.parametrize(("freq", "expected"), [("PT5M", 1.5), ("PT15M", 0.5), ("PT30M", 0.5), ("PT1H", 0.5)])
def test_log_alert_price_by_frequency(prices: PriceClient, freq: int, expected: float) -> None:
    rule = make_resource(
        "Microsoft.Insights/scheduledQueryRules",
        "r",
        {"location": "westeurope", "properties": {"evaluationFrequency": freq, "enabled": True, "scopes": ["/x"]}},
    )
    assert run(prices, [rule]).monthly == pytest.approx(expected)


def test_disabled_alert_is_free(prices: PriceClient) -> None:
    rule = make_resource(
        "Microsoft.Insights/scheduledQueryRules",
        "r",
        {"location": "westeurope", "properties": {"evaluationFrequency": "PT5M", "enabled": False}},
    )
    assert run(prices, [rule]).monthly == 0


def test_key_vault_operations(prices: PriceClient) -> None:
    kv = make_resource(
        "Microsoft.KeyVault/vaults", "kv", {"location": "westeurope", "properties": {"sku": {"name": "standard"}}}
    )
    assert run(prices, [kv], {"keyvault": {"operations_10k": 100}}).monthly == pytest.approx(100 * 0.03)


def test_private_endpoint_hours_and_dns_zone_tiers(prices: PriceClient) -> None:
    pe = make_resource("Microsoft.Network/privateEndpoints", "pe", {"location": "westeurope", "properties": {}})
    est = run(prices, [pe], {"privateendpoints": {"pe": {"ingress_gb": 0, "egress_gb": 0}}})
    assert est.monthly == pytest.approx(0.01 * HOURS)
    zones = [
        make_resource("Microsoft.Network/privateDnsZones", f"z{i}.example", {"location": "global"}) for i in range(26)
    ]
    assert run(prices, zones).monthly == pytest.approx(25 * 0.5 + 0.1)


def test_cognitive_account_has_no_base_fee_but_accepts_manual_cost(prices: PriceClient) -> None:
    acct = make_resource(
        "Microsoft.CognitiveServices/accounts",
        "ai",
        {"location": "westeurope", "kind": "AIServices", "sku": {"name": "S0"}},
    )
    assert run(prices, [acct]).monthly == 0
    assert run(prices, [acct], {"cognitiveservices": {"ai": {"monthly_cost": 120}}}).monthly == 120


def test_unsupported_and_free_types(prices: PriceClient) -> None:
    other = make_resource("Microsoft.Cdn/profiles", "cdn", {})
    role = make_resource("Microsoft.Authorization/roleAssignments", "ra", {})
    est = run(prices, [other, role])
    assert one(est, "cdn").status == "unsupported"
    assert one(est, "ra").status == "free"


def test_missing_location_falls_back_to_default_region_with_note(prices: PriceClient) -> None:
    sa = make_resource(
        "Microsoft.Storage/storageAccounts",
        "nl",
        {"kind": "StorageV2", "sku": {"name": "Standard_LRS"}, "properties": {}},
    )
    est = run(prices, [sa])
    assert est.monthly > 0
    assert any("default region" in n for n in one(est, "nl").notes)


def test_user_set_values_are_listed_as_assumptions(prices: PriceClient) -> None:
    est = run(prices, [storage("x")], {"storage": {"x": {"stored_gb": 50}}})
    assert any("storage[x].stored_gb = 50" in a and "usage file" in a for a in est.assumptions)
    assert not any("monthly_cost" in a for a in est.assumptions)


def test_idle_percent_out_of_range_is_a_usage_error(prices: PriceClient) -> None:
    from bicep_cost.usage import UsageError

    with pytest.raises(UsageError, match="between 0 and 100"):
        run(prices, [container_app("a")], {"containerapps": {"a": {"idle_percent": 150}}})


def test_measured_replica_hours_replace_the_templates_scale_rules(prices: PriceClient) -> None:
    est = run(
        prices, [container_app("a", min_r=1)], {"containerapps": {"a": {"replica_hours": 100, "idle_percent": 50}}}
    )
    # 50 active h + 50 idle h; the free grants still apply
    active = 50 * 3600
    expected = (
        max(active * 1 - 180_000, 0) * 3.4e-5 + max(active * 2 - 360_000, 0) * 4e-6 + 50 * 3600 * (4e-6 + 2 * 4e-6)
    )
    assert est.monthly == pytest.approx(expected)
    assert est.usage == pytest.approx(est.monthly)
    zero = run(prices, [container_app("z", min_r=1)], {"containerapps": {"z": {"replica_hours": 0}}})
    assert zero.monthly == 0


def test_live_instance_count_and_postgres_storage_override_the_template(prices: PriceClient) -> None:
    est = run(prices, [plan(capacity=2)], {"appserviceplans": {"plan": {"instances": 3}}})
    assert est.monthly == pytest.approx(0.178 * HOURS * 3)
    pg = make_resource(
        "Microsoft.DBforPostgreSQL/flexibleServers",
        "pg",
        {
            "location": "westeurope",
            "sku": {"name": "Standard_D2s_v3", "tier": "GeneralPurpose"},
            "properties": {"storage": {"storageSizeGB": 32}},
        },
    )
    assert run(prices, [pg], {"postgres": {"pg": {"storage_gb": 64}}}).monthly == pytest.approx(
        2 * 0.1015 * HOURS + 64 * 0.1369
    )


def test_measured_function_gb_seconds_override(prices: PriceClient) -> None:
    pl = make_resource("Microsoft.Web/serverfarms", "y1", {"location": "westeurope", "sku": {"name": "Y1"}})
    fa = make_resource(
        "Microsoft.Web/sites",
        "fa",
        {"location": "westeurope", "kind": "functionapp", "properties": {"serverFarmId": pl.id}},
    )
    est = run(prices, [pl, fa], {"functions": {"fa": {"executions_per_month": 0, "gb_seconds_per_month": 1_400_000}}})
    assert est.monthly == pytest.approx(1_000_000 * 1.6e-5)


def test_session_pool_monthly_cost_and_discount_table(prices: PriceClient) -> None:
    pool = make_resource(
        "Microsoft.App/sessionPools", "p", {"location": "westeurope", "properties": {"containerType": "PythonLTS"}}
    )
    assert run(prices, [pool], {"sessionpools": {"p": {"monthly_cost": 4.15}}}).monthly == pytest.approx(4.15)
    discounted = run(prices, [plan(capacity=1)], {"discounts": {"Microsoft.Web/serverfarms": 30}})
    assert discounted.monthly == pytest.approx(0.178 * HOURS * 0.7)
    default_discount = run(prices, [plan(capacity=1)], {"discounts": {"default": 10}})
    assert default_discount.monthly == pytest.approx(0.178 * HOURS * 0.9)
    from bicep_cost.usage import UsageError

    with pytest.raises(UsageError):
        run(prices, [plan()], {"discounts": {"default": 150}})
