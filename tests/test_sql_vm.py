import pytest

from bicep_cost.armexpr import JsonValue
from bicep_cost.calc import estimate_resources
from bicep_cost.estimators.vm import disk_label
from bicep_cost.usage import UsageConfig

from .conftest import make_resource

HOURS = 730
GB = 2**30


def run(prices, resources, usage=None):
    return estimate_resources(resources, prices, UsageConfig(usage or {}))


def db(sku: str, **props: JsonValue):
    return make_resource(
        "Microsoft.Sql/servers/databases",
        "srv/db",
        {"location": "westeurope", "sku": {"name": sku}, "properties": props},
    )


def test_vcore_database_prices_whole_size_row_even_when_skus_share_a_meter_id(prices):
    est = run(prices, [db("GP_Gen5_2", maxSizeBytes=32 * GB)])
    assert est.monthly == pytest.approx(0.3349 * HOURS + 32 * 0.13685)
    est4 = run(prices, [db("GP_Gen5_4", maxSizeBytes=32 * GB)])
    assert est4.monthly == pytest.approx(0.6697 * HOURS + 32 * 0.13685)


def test_vcore_without_whole_size_row_falls_back_to_per_vcore(prices):
    est = run(prices, [db("GP_Gen5_6", maxSizeBytes=32 * GB)])
    assert est.monthly == pytest.approx(6 * 0.1674 * HOURS + 32 * 0.13685)


def test_zone_redundant_adds_compute_and_uses_zr_storage_or_reports_missing_price(prices):
    est = run(prices, [db("GP_Gen5_2", maxSizeBytes=32 * GB, zoneRedundant=True)])
    status = est.resources[0].status
    assert status in ("priced", "unresolved")  # catalog has no zone-redundant storage row -> clean "unresolved"


def test_serverless_uses_measured_vcore_hours_and_dtu_models(prices):
    sl = db("GP_S_Gen5_2", maxSizeBytes=32 * GB)
    est = run(prices, [sl], {"sqldatabases": {"srv/db": {"vcore_hours": 200}}})
    assert est.monthly == pytest.approx(200 * 0.5739 + 32 * 0.13685)
    assert est.usage >= 200 * 0.5739
    s0 = run(prices, [db("S0", maxSizeBytes=250 * GB)])
    assert s0.monthly == pytest.approx(0.4839 * 730 / 24)
    big = run(prices, [db("S0", maxSizeBytes=500 * GB)])
    assert big.monthly == pytest.approx(0.4839 * 730 / 24 + 250 * 0.221)


def test_database_in_pool_is_included_and_pool_is_priced(prices):
    member = db("ElasticPool", elasticPoolId="/x/pool")
    pool = make_resource(
        "Microsoft.Sql/servers/elasticPools",
        "srv/pool",
        {"location": "westeurope", "sku": {"name": "StandardPool", "capacity": 100}},
    )
    est = run(prices, [member, pool])
    assert est.resources[0].status == "included"
    assert est.monthly == pytest.approx(100 * 0.0725 * 730 / 24)


def test_sql_server_and_children_are_free(prices):
    server = make_resource("Microsoft.Sql/servers", "srv", {"location": "westeurope"})
    rule = make_resource("Microsoft.Sql/servers/firewallRules", "srv/r", {})
    assert run(prices, [server, rule]).monthly == 0


def vm(size="Standard_D2s_v5", windows=False, os_disk=None, data=None, **profile):
    props: dict[str, JsonValue] = {
        "hardwareProfile": {"vmSize": size},
        "storageProfile": {"osDisk": {"createOption": "FromImage", **(os_disk or {})}, "dataDisks": data or []},
        **profile,
    }
    if windows:
        props["osProfile"] = {"windowsConfiguration": {}}
    return make_resource("Microsoft.Compute/virtualMachines", "vm", {"location": "westeurope", "properties": props})


def test_linux_vm_with_default_os_disk(prices):
    est = run(prices, [vm()])
    assert est.monthly == pytest.approx(0.115 * HOURS + 5.8)  # 30 GB -> P4
    assert any("OS disk size not set" in n for n in est.resources[0].notes)


def test_windows_license_and_hybrid_benefit(prices):
    disk = {"diskSizeGB": 128, "managedDisk": {"storageAccountType": "Premium_LRS"}}
    assert run(prices, [vm(windows=True, os_disk=disk)]).monthly == pytest.approx(0.207 * HOURS + 21.68)
    ahb = run(prices, [vm(windows=True, os_disk=disk, licenseType="Windows_Server")])
    assert ahb.monthly == pytest.approx(0.115 * HOURS + 21.68)


def test_spot_vm_ignores_low_priority_and_regular_rows(prices):
    disk = {"diskSizeGB": 32, "managedDisk": {"storageAccountType": "Premium_LRS"}}
    assert run(prices, [vm(os_disk=disk, priority="Spot")]).monthly == pytest.approx(0.0213 * HOURS + 5.8)


def test_running_hours_override_and_data_disks(prices):
    data = [
        {
            "lun": 0,
            "diskSizeGB": 100,
            "createOption": "Empty",
            "managedDisk": {"storageAccountType": "StandardSSD_LRS"},
        },
        {"lun": 1, "diskSizeGB": 100, "createOption": "Empty", "managedDisk": {"storageAccountType": "Standard_LRS"}},
    ]
    disk = {"diskSizeGB": 32, "managedDisk": {"storageAccountType": "Premium_LRS"}}
    est = run(prices, [vm(os_disk=disk, data=data)], {"virtualmachines": {"vm": {"hours_per_month": 100}}})
    assert est.monthly == pytest.approx(0.115 * 100 + 5.8 + 9.6 + 5.888)
    assert est.usage == pytest.approx(0.115 * 100)  # running hours are an assumption, disks are not
    assert any("HDD transaction" in n for n in est.resources[0].notes)


def test_attached_data_disk_that_is_its_own_resource_is_not_counted_twice(prices):
    disk = make_resource(
        "Microsoft.Compute/disks",
        "d1",
        {"location": "westeurope", "sku": {"name": "Premium_LRS"}, "properties": {"diskSizeGB": 128}},
    )
    data = [{"lun": 0, "createOption": "Attach", "managedDisk": {"id": disk.id}}]
    os_disk = {"diskSizeGB": 32, "managedDisk": {"storageAccountType": "Premium_LRS"}}
    est = run(prices, [vm(os_disk=os_disk, data=data), disk])
    assert est.monthly == pytest.approx(0.115 * HOURS + 5.8 + 21.68)


def test_ephemeral_os_disk_has_no_disk_charge(prices):
    est = run(prices, [vm(os_disk={"diffDiskSettings": {"option": "Local"}})])
    assert est.monthly == pytest.approx(0.115 * HOURS)


def test_scale_set_multiplies_instances_and_accepts_override(prices):
    vmss = make_resource(
        "Microsoft.Compute/virtualMachineScaleSets",
        "ss",
        {
            "location": "westeurope",
            "sku": {"name": "Standard_D2s_v5", "capacity": 3},
            "properties": {
                "virtualMachineProfile": {
                    "storageProfile": {
                        "osDisk": {
                            "createOption": "FromImage",
                            "diskSizeGB": 32,
                            "managedDisk": {"storageAccountType": "Premium_LRS"},
                        }
                    }
                }
            },
        },
    )
    assert run(prices, [vmss]).monthly == pytest.approx(3 * (0.115 * HOURS + 5.8))
    assert run(prices, [vmss], {"virtualmachinescalesets": {"ss": {"instances": 5}}}).monthly == pytest.approx(
        5 * (0.115 * HOURS + 5.8)
    )


@pytest.mark.parametrize(
    ("kind", "size", "expected"),
    [
        ("Premium_LRS", 30, "P4 LRS"),
        ("Premium_LRS", 128, "P10 LRS"),
        ("Premium_LRS", 129, "P15 LRS"),
        ("Premium_ZRS", 512, "P20 ZRS"),
        ("StandardSSD_LRS", 100, "E10 LRS"),
        ("Standard_LRS", 100, "S10 LRS"),
        ("Premium_LRS", 1024, "P30 LRS"),
    ],
)
def test_disk_tier_is_the_smallest_that_fits(kind, size, expected):
    assert disk_label(kind, size)[1] == expected


def test_unsupported_disk_types_are_unresolved_not_wrong(prices):
    ultra = make_resource(
        "Microsoft.Compute/disks",
        "u",
        {"location": "westeurope", "sku": {"name": "UltraSSD_LRS"}, "properties": {"diskSizeGB": 256}},
    )
    assert run(prices, [ultra]).resources[0].status == "unresolved"


def test_public_ips_and_free_vm_children(prices):
    std = make_resource(
        "Microsoft.Network/publicIPAddresses",
        "ip",
        {"location": "westeurope", "sku": {"name": "Standard"}, "properties": {"publicIPAllocationMethod": "Static"}},
    )
    basic = make_resource(
        "Microsoft.Network/publicIPAddresses",
        "ip2",
        {"location": "westeurope", "sku": {"name": "Basic"}, "properties": {"publicIPAllocationMethod": "Dynamic"}},
    )
    ext = make_resource("Microsoft.Compute/virtualMachines/extensions", "vm/ext", {})
    est = run(prices, [std, basic, ext])
    assert est.monthly == pytest.approx((0.005 + 0.004) * HOURS)


def test_postgres_vcore_product_uses_whole_node_row_not_an_arbitrary_one(prices):
    pg = make_resource(
        "Microsoft.DBforPostgreSQL/flexibleServers",
        "pg",
        {
            "location": "westeurope",
            "sku": {"name": "Standard_D4ds_v5", "tier": "GeneralPurpose"},
            "properties": {"storage": {"storageSizeGB": 32}},
        },
    )
    assert run(prices, [pg]).monthly == pytest.approx(0.424 * HOURS + 32 * 0.1369)


def test_postgres_backup_above_free_allowance(prices):
    pg = make_resource(
        "Microsoft.DBforPostgreSQL/flexibleServers",
        "pg",
        {
            "location": "westeurope",
            "sku": {"name": "Standard_D4ds_v5", "tier": "GeneralPurpose"},
            "properties": {"storage": {"storageSizeGB": 32}},
        },
    )
    est = run(prices, [pg], {"postgres": {"pg": {"backup_storage_gb": 52}}})
    assert est.monthly == pytest.approx(0.424 * HOURS + 32 * 0.1369 + 20 * 0.103)
