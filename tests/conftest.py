import json
import re
import urllib.parse
from pathlib import Path

import pytest

from bicep_cost.armexpr import JsonValue
from bicep_cost.context import EstimateContext, ResourceIndex
from bicep_cost.engine import Resource
from bicep_cost.prices import PriceClient, RawPriceItem
from bicep_cost.usage import UsageConfig

REGION = "westeurope"
_counter = 0


def item(
    service: str,
    product: str,
    sku: str,
    meter: str,
    price: float,
    unit: str = "1 Hour",
    tier: float = 0.0,
    region: str = REGION,
    meter_id: str | None = None,
    arm_sku: str = "",
) -> RawPriceItem:
    global _counter
    _counter += 1
    return {
        "currencyCode": "USD",
        "tierMinimumUnits": tier,
        "retailPrice": price,
        "unitPrice": price,
        "armRegionName": region,
        "effectiveStartDate": "2026-01-01T00:00:00Z",
        "meterId": meter_id or f"{service}|{product}|{sku}|{meter}|{unit}|{region}",
        "skuId": f"{product}|{sku}|{meter}|{unit}",
        "armSkuName": arm_sku,
        "meterName": meter,
        "productName": product,
        "skuName": sku,
        "serviceName": service,
        "unitOfMeasure": unit,
        "type": "Consumption",
    }


SQLGP = "SQL Database Single/Elastic Pool General Purpose - Compute Gen5"
_PG = "Azure Database for PostgreSQL Flexible Server"
PGD = f"{_PG} General Purpose Ddsv5 Series Compute"
PGB = f"{_PG} Backup Storage"

CATALOG: list[RawPriceItem] = [
    item("Azure App Service", "Azure App Service Premium v3 Plan - Linux", "P1 v3", "P1 v3 App", 0.178),
    item("Azure App Service", "Azure App Service Premium v3 Plan", "P1 v3", "P1 v3 App", 0.338),
    item("Azure App Service", "Azure App Service Basic Plan - Linux", "B1", "B1", 0.018),
    item("Logic Apps", "Logic Apps", "Standard", "Standard vCPU Duration", 0.2),
    item("Logic Apps", "Logic Apps", "Standard", "Standard Memory Duration", 0.014, "1 GiB Hour"),
    item("Functions", "Functions", "Standard", "Standard Total Executions", 0.0, "10"),
    item("Functions", "Functions", "Standard", "Standard Total Executions", 2e-6, "10", tier=100000),
    item("Functions", "Functions", "Standard", "Standard Execution Time", 0.0, "1 GB Second"),
    item("Functions", "Functions", "Standard", "Standard Execution Time", 1.6e-5, "1 GB Second", tier=400000),
    item("Azure Container Apps", "Azure Container Apps", "Standard", "Standard vCPU Active Usage", 3.4e-5, "1 Second"),
    item(
        "Azure Container Apps", "Azure Container Apps", "Standard", "Standard Memory Active Usage", 4e-6, "1 GiB Second"
    ),
    item("Azure Container Apps", "Azure Container Apps", "Standard", "Standard vCPU Idle Usage", 4e-6, "1 Second"),
    item(
        "Azure Container Apps", "Azure Container Apps", "Standard", "Standard Memory Idle Usage", 4e-6, "1 GiB Second"
    ),
    item("Azure Container Apps", "Azure Container Apps", "Standard", "Standard Requests", 0.56, "1M"),
    item("Azure Container Apps", "Azure Container Apps", "Dynamic Sessions", "Dynamic Sessions", 0.03),
    item("Azure Container Apps", "Azure Container Apps", "Dedicated", "Dedicated Plan Management", 0.1),
    item("Azure Container Apps", "Azure Container Apps", "Dedicated", "Dedicated vCPU Usage", 0.08),
    item("Azure Container Apps", "Azure Container Apps", "Dedicated", "Dedicated Memory Usage", 0.0066, "1 GiB Hour"),
    item("Storage", "General Block Blob v2", "Hot LRS", "Hot LRS Data Stored", 0.0196, "1 GB/Month"),
    item("Storage", "General Block Blob v2", "Hot LRS", "Hot LRS Write Operations", 0.054, "10K"),
    item("Storage", "General Block Blob v2", "Hot LRS", "Hot Read Operations", 0.0043, "10K"),
    item("Storage", "General Block Blob v2", "Hot LRS", "All Other Operations", 0.0043, "10K"),
    item("Storage", "General Block Blob v2", "Hot GRS", "Hot GRS Data Stored", 0.0392, "1 GB/Month"),
    item("Storage", "Files v2", "Standard LRS", "LRS Data Stored", 0.06, "1 GB/Month"),
    item("Storage", "Files v2", "Standard LRS", "LRS Write Operations", 0.015, "10K"),
    item("Storage", "Files v2", "Standard LRS", "Read Operations", 0.0015, "10K"),
    item("Storage", "Files v2", "Standard LRS", "Protocol Operations", 0.0015, "10K"),
    item(
        "Azure Database for PostgreSQL",
        "Azure Database for PostgreSQL Flexible Server General Purpose Dsv3 Series Compute",
        "vCore",
        "vCore",
        0.1015,
    ),
    item(
        "Azure Database for PostgreSQL",
        "Azure Database for PostgreSQL Flexible Server Burstable BS Series Compute",
        "B1MS",
        "B1MS",
        0.0199,
    ),
    item(
        "Azure Database for PostgreSQL",
        "Azure Database for PostgreSQL Flex Server Storage",
        "Storage",
        "Storage Data Stored",
        0.1369,
        "1 GB/Month",
    ),
    item("Log Analytics", "Log Analytics", "Analytics Logs", "Analytics Logs Data Ingestion", 0.0, "1 GB"),
    item("Log Analytics", "Log Analytics", "Analytics Logs", "Analytics Logs Data Ingestion", 2.99, "1 GB", tier=5),
    item("Log Analytics", "Log Analytics", "Analytics Logs", "Analytics Logs Data Retention", 0.13, "1 GB/Month"),
    item(
        "Azure Monitor", "Azure Monitor", "Alerts", "Alerts System Log Monitored at 5 Minute Frequency", 1.5, "1/Month"
    ),
    item(
        "Azure Monitor", "Azure Monitor", "Alerts", "Alerts System Log Monitored at 15 Minute Frequency", 0.5, "1/Month"
    ),
    item("Key Vault", "Key Vault", "Standard", "Operations", 0.03, "10K"),
    item(
        "Virtual Network",
        "Virtual Network Private Link",
        "Standard",
        "Standard Private Endpoint",
        0.01,
        region="Global",
    ),
    item(
        "Virtual Network",
        "Virtual Network Private Link",
        "Standard",
        "Standard Data Processed - Ingress",
        0.01,
        "1 GB",
        region="Global",
    ),
    item(
        "Virtual Network",
        "Virtual Network Private Link",
        "Standard",
        "Standard Data Processed - Egress",
        0.01,
        "1 GB",
        region="Global",
    ),
    item("Azure DNS", "Azure DNS", "Private", "Private Zone", 0.5, "1", region="Zone 1"),
    item("Azure DNS", "Azure DNS", "Private", "Private Zone", 0.1, "1", tier=25, region="Zone 1"),
    item("Azure DNS", "Azure DNS", "Private", "Private Queries", 0.4, "1M", region="Zone 1"),
    item("Service Bus", "Service Bus", "Standard", "Standard Base Unit", 0.0135, "1/Hour"),
    item("Service Bus", "Service Bus", "Standard", "Standard Base Unit", 10.0, "1/Month"),
    item("Service Bus", "Service Bus", "Standard", "Standard Messaging Operations", 0.0, "1M"),
    item("Service Bus", "Service Bus", "Standard", "Standard Messaging Operations", 0.8, "1M", tier=13),
    item("Service Bus", "Service Bus", "Standard", "Standard Messaging Operations", 0.5, "1M", tier=100),
    item("Service Bus", "Service Bus", "Basic", "Basic Messaging Operations", 0.05, "1M"),
    item("Service Bus", "Service Bus", "Premium", "Premium Messaging Unit", 0.9275, "1/Hour"),
    # Azure SQL: all "N vCore" rows of a product share ONE meterId in the real API
    *[
        item("SQL Database", SQLGP, sku, name, price, meter_id=mid)
        for sku, name, price, mid in [
            ("2 vCore", "vCore", 0.3349, "sql-gp"),
            ("4 vCore", "vCore", 0.6697, "sql-gp"),
            ("1 vCore", "vCore", 0.1674, "sql-gp"),
            ("2 vCore Zone Redundancy", "Zone Redundancy vCore", 0.2009, "sql-gp-zr"),
        ]
    ],
    item(
        "SQL Database",
        "SQL Database Single/Elastic Pool General Purpose - Storage",
        "General Purpose",
        "General Purpose Data Stored",
        0.13685,
        "1 GB/Month",
    ),
    item(
        "SQL Database",
        "SQL Database Single/Elastic Pool General Purpose - Storage",
        "General Purpose",
        "General Purpose Data Stored - Free",
        0.0,
        "1 GB/Month",
    ),
    item("SQL Database", "SQL Database Single Standard", "S0", "S0 DTUs", 0.4839, "1/Day"),
    item("SQL Database", "SQL Database Standard - Storage", "Standard", "Data Stored", 0.221, "1 GB/Month"),
    item("SQL Database", "SQL Database General Purpose - Serverless - Compute Gen5", "1 vCore", "vCore", 0.5739),
    item("SQL Database", "SQL Database Elastic Pool - Standard", "1 DTU", "eDTUs", 0.0725, "1/Day"),
    # Virtual machines: Spot and Windows rows share the plain armSkuName
    item("Virtual Machines", "Virtual Machines Dsv5 Series", "D2s v5", "D2s v5", 0.115, arm_sku="Standard_D2s_v5"),
    item(
        "Virtual Machines", "Virtual Machines Dsv5 Series Windows", "D2s v5", "D2s v5", 0.207, arm_sku="Standard_D2s_v5"
    ),
    item(
        "Virtual Machines",
        "Virtual Machines Dsv5 Series",
        "D2s v5 Spot",
        "D2s v5 Spot",
        0.0213,
        arm_sku="Standard_D2s_v5",
    ),
    item(
        "Virtual Machines",
        "Virtual Machines Dsv5 Series",
        "D2s v5 Low Priority",
        "D2s v5 Low Priority",
        0.023,
        arm_sku="Standard_D2s_v5",
    ),
    item("Storage", "Premium SSD Managed Disks", "P4 LRS", "P4 LRS Disk", 5.8, "1/Month"),
    item("Storage", "Premium SSD Managed Disks", "P4 LRS", "P4 LRS Disk Mount", 0.5, "1/Month"),
    item("Storage", "Premium SSD Managed Disks", "P10 LRS", "P10 LRS Disk", 21.68, "1/Month"),
    item("Storage", "Standard SSD Managed Disks", "E10 LRS", "E10 LRS Disk", 9.6, "1/Month"),
    item("Storage", "Standard HDD Managed Disks", "S10 LRS", "S10 LRS Disk", 5.888, "1/Month"),
    item("Virtual Network", "IP Addresses", "Standard", "Standard IPv4 Static Public IP", 0.005),
    item("Virtual Network", "IP Addresses", "Basic", "Basic IPv4 Dynamic Public IP", 0.004),
    # PostgreSQL: whole-node rows next to a per-vCore row
    *[
        item("Azure Database for PostgreSQL", PGD, sku, "vCore", price, meter_id="pg-d")
        for sku, price in [("4 vCore", 0.424), ("64 vCore", 6.784), ("vCore", 0.106)]
    ],
    item(
        "Azure Database for PostgreSQL",
        PGB,
        "Backup Storage LRS",
        "Backup Storage LRS Data Stored",
        0.103,
        "1 GB/Month",
    ),
]

_CLAUSE = re.compile(r"(contains\((\w+), '((?:[^']|'')*)'\))|((\w+) eq '((?:[^']|'')*)')")


def fake_fetcher(url: str) -> bytes:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    flt = qs["$filter"][0]
    clauses = []
    for m in _CLAUSE.finditer(flt):
        if m.group(1):
            clauses.append(("contains", m.group(2), m.group(3).replace("''", "'")))
        else:
            clauses.append(("eq", m.group(5), m.group(6).replace("''", "'")))
    out = []
    for it in CATALOG:
        ok = True
        for op, fld, val in clauses:
            actual = str(it.get("type" if fld == "priceType" else fld, ""))  # type: ignore[call-overload]
            ok &= (actual == val) if op == "eq" else (val in actual)
        if ok:
            out.append(it)
    return json.dumps(
        {"Items": [{**row, "currencyCode": qs.get("currencyCode", ["USD"])[0]} for row in out], "NextPageLink": None}
    ).encode()


@pytest.fixture
def prices() -> PriceClient:
    return PriceClient(cache_dir=None, fetcher=fake_fetcher)


def make_resource(type_: str, name: str, body: dict[str, JsonValue], sub: str = "sub1") -> Resource:
    from bicep_cost.armexpr import build_resource_id

    rid = build_resource_id(f"/subscriptions/{sub}/resourceGroups/rg", type_, name.split("/"))
    full: dict[str, JsonValue] = {"type": type_, "name": name, "id": rid, **body}
    return Resource(type=type_, name=name, id=rid, body=full, module_path=(), subscription_id=sub, resource_group="rg")


@pytest.fixture
def make_ctx(prices: PriceClient):
    def build(resources: list[Resource] | None = None, usage: dict[str, object] | None = None) -> EstimateContext:
        return EstimateContext(
            prices=prices,
            usage=UsageConfig(usage or {}),
            index=ResourceIndex(resources or []),
        )

    return build


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
