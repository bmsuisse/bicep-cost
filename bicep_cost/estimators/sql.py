"""Azure SQL: single databases, elastic pools and Managed Instance (vCore and DTU purchasing models)."""

import re
from dataclasses import dataclass

from ..context import HOURS_PER_MONTH, EstimateContext, Unresolved, as_number, need_str
from ..engine import Resource
from ..model import LineItem, ResourceEstimate
from ..prices import Meter, NoPriceFound
from .common import free, included, line, node_price, priced

SERVICE = "SQL Database"
DAYS_PER_MONTH = HOURS_PER_MONTH / 24
GIB = 2**30

# Storage that is included in the DTU price (GB). Anything above is billed per GB.
DTU_INCLUDED_GB = {"Basic": 2.0, "Standard": 250.0, "Premium": 500.0}
DEFAULT_STORAGE_GB = {"GeneralPurpose": 32.0, "BusinessCritical": 32.0, "Hyperscale": 10.0}

FAMILY_LABELS = {
    "gen5": "Gen5",
    "dc": "DC-Series",
    "fsv2": "FSv2 Series",
    "m": "M Series",
}
TIER_NAMES = {"GP": "GeneralPurpose", "BC": "BusinessCritical", "HS": "Hyperscale"}
TIER_PRODUCT = {
    "GeneralPurpose": "General Purpose",
    "BusinessCritical": "Business Critical",
    "Hyperscale": "Hyperscale",
}


@dataclass(frozen=True)
class SqlSku:
    model: str  # vcore | dtu | pool-member | free | unsupported
    tier: str = ""  # GeneralPurpose | BusinessCritical | Hyperscale | Basic | Standard | Premium
    family: str = ""
    vcores: int = 0
    serverless: bool = False
    dtu_label: str = ""


def parse_sku(name: str) -> SqlSku:
    n = name.strip()
    low = n.lower()
    if low == "elasticpool":
        return SqlSku("pool-member")
    if low == "free":
        return SqlSku("free")
    if low == "basic":
        return SqlSku("dtu", "Basic", dtu_label="B")
    if m := re.fullmatch(r"s(\d{1,2})", low):
        return SqlSku("dtu", "Standard", dtu_label=f"S{m.group(1)}")
    if m := re.fullmatch(r"p(\d{1,2})", low):
        return SqlSku("dtu", "Premium", dtu_label=f"P{m.group(1)}")
    if m := re.fullmatch(r"(gp|bc|hs)(_s)?_([a-z0-9]+)_(\d+)", low):
        return SqlSku("vcore", TIER_NAMES[m.group(1).upper()], m.group(3), int(m.group(4)), serverless=bool(m.group(2)))
    if low.startswith("dw") or low.startswith("datawarehouse"):
        return SqlSku("unsupported")
    raise Unresolved(f"unrecognised SQL SKU {name!r}")


def _family_label(family: str) -> str:
    label = FAMILY_LABELS.get(family.lower())
    if label is None:
        raise Unresolved(f"SQL hardware family {family!r} is not priced")
    return label


def _compute_product(sku: SqlSku) -> str:
    fam = _family_label(sku.family)
    if sku.tier == "Hyperscale":
        if sku.serverless:
            return "SQL Database SingleDB Hyperscale - Serverless - Compute Gen5"
        return f"SQL Database SingleDB/Elastic Pool Hyperscale - Compute {fam}"
    if sku.serverless:
        return f"SQL Database General Purpose - Serverless - Compute {fam}"
    return f"SQL Database Single/Elastic Pool {TIER_PRODUCT[sku.tier]} - Compute {fam}"


def _storage_meter(
    ctx: EstimateContext, region: str, product: str, needle: str, exclude: tuple[str, ...] = ()
) -> Meter:
    for m in ctx.meters(SERVICE, region, product=product):
        name = m.name.lower()
        if needle.lower() in name and not any(x.lower() in name for x in exclude):
            return m
    raise NoPriceFound(f"no SQL storage price '{needle}' in {product} ({region})")


def _vcore_items(
    res: Resource,
    ctx: EstimateContext,
    sku: SqlSku,
    vcores: int,
    storage_gb: float,
    zone_redundant: bool,
    replicas: int,
    section: str,
) -> tuple[list[LineItem], list[str]]:
    region = ctx.region(res)
    items: list[LineItem] = []
    notes: list[str] = []
    meters = ctx.meters(SERVICE, region, product=_compute_product(sku))
    if sku.serverless:
        meter, mult = node_price(meters, 1)
        min_cap = as_number(res.get("properties.minCapacity"), 0.5)
        measured = ctx.usage.optional_number(section, res.name, "vcore_hours")
        if measured is not None:
            vcore_hours = measured
            notes.append("billed vCore-hours taken from the usage file")
        else:
            avg = ctx.usage.number(section, res.name, "avg_vcores", max(min_cap, 0.5))
            active = ctx.usage.number(section, res.name, "active_percent", 100.0)
            vcore_hours = avg * HOURS_PER_MONTH * active / 100
            pause = res.get("properties.autoPauseDelay")
            if isinstance(pause, (int, float)) and pause != -1:
                notes.append("auto-pause is on: assuming it never pauses; set active_percent or vcore_hours")
        items.append(
            line(
                f"Serverless {sku.family} {vcore_hours:,.0f} vCore-hours",
                meter,
                vcore_hours * mult,
                "usage",
                note="assumed utilisation",
            )
        )
        if zone_redundant:
            zr, zmult = node_price(meters, 1, zone_redundant=True)
            items.append(line("Zone redundancy", zr, vcore_hours * zmult, "usage"))
    else:
        meter, mult = node_price(meters, vcores)
        nodes = 1 + replicas
        label = f"{TIER_PRODUCT[sku.tier]} {sku.family} {vcores} vCore" + (
            f" x{nodes} (with {replicas} HA replica(s))" if replicas else ""
        )
        items.append(line(label, meter, HOURS_PER_MONTH * nodes * mult))
        if zone_redundant and sku.tier != "BusinessCritical":
            zr, zmult = node_price(meters, vcores, zone_redundant=True)
            items.append(line("Zone redundancy", zr, HOURS_PER_MONTH * nodes * zmult))
    # storage
    if sku.tier == "Hyperscale":
        stor = _storage_meter(ctx, region, "SQL Database Hyperscale - Storage", "Hyperscale Data Stored")
        items.append(
            line(f"Hyperscale storage {storage_gb:g} GB used", stor, storage_gb, "usage", note="billed on used size")
        )
    elif sku.tier == "BusinessCritical":
        stor = _storage_meter(
            ctx, region, "SQL Database Single/Elastic Pool Business Critical - Storage", "Data Stored"
        )
        items.append(line(f"Storage {storage_gb:g} GB (provisioned max size)", stor, storage_gb))
    else:
        product = "SQL Database Single/Elastic Pool General Purpose - Storage"
        needle, excl = (
            ("Zone Redundancy Data Stored", ()) if zone_redundant else ("Data Stored", ("Free", "Zone Redundancy"))
        )
        stor = _storage_meter(ctx, region, product, needle, excl)
        items.append(line(f"Storage {storage_gb:g} GB (provisioned max size)", stor, storage_gb))
    return items, notes


def _backup_item(res: Resource, ctx: EstimateContext, section: str) -> list[LineItem]:
    extra = ctx.usage.number(section, res.name, "backup_storage_gb", 0.0)
    if extra <= 0:
        return []
    redundancy = str(res.get("properties.requestedBackupStorageRedundancy") or "Geo").lower()
    label = {"local": "Backup LRS", "zone": "Backup ZRS", "geozone": "Backup RA-GZRS"}.get(redundancy, "Backup RA-GRS")
    region = ctx.region(res)
    for m in ctx.meters(SERVICE, region, product="SQL Database Single/Elastic Pool PITR Backup Storage", sku=label):
        if "free" not in m.name.lower():
            return [
                line(
                    f"PITR backup storage above the free allowance ({label})",
                    m,
                    extra,
                    "usage",
                    note="measured/assumed",
                )
            ]
    return []


def estimate_database(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    sku = parse_sku(need_str(res, "sku.name", "SQL SKU"))
    if sku.model == "free" or res.get("properties.useFreeLimit") is True:
        return free(res, "free offer")
    if sku.model == "unsupported":
        return ResourceEstimate(
            res, "unsupported", notes=["data warehouse (Synapse dedicated SQL pool) SKUs are not priced"]
        )
    if sku.model == "pool-member" or res.get("properties.elasticPoolId") not in (None, ""):
        return included(res, "billed through its elastic pool")
    region = ctx.region(res)
    notes: list[str] = []
    if str(res.get("properties.licenseType") or "").lower() == "baseprice":
        notes.append("Azure Hybrid Benefit (BasePrice) is set; the price includes the SQL license, so it is overstated")
    max_gb = as_number(res.get("properties.maxSizeBytes"), 0.0) / GIB
    if sku.model == "dtu":
        label_meter = ctx.meter(SERVICE, region, product=f"SQL Database Single {sku.tier}", sku=sku.dtu_label)
        items = [line(f"{sku.tier} {sku.dtu_label} (DTU)", label_meter, DAYS_PER_MONTH)]
        included_gb = DTU_INCLUDED_GB[sku.tier]
        size_gb = ctx.usage.number("sqldatabases", res.name, "storage_gb", max_gb or included_gb)
        if size_gb > included_gb:
            stor = _storage_meter(ctx, region, f"SQL Database {sku.tier} - Storage", "Data Stored")
            items.append(
                line(
                    f"Storage above the included {included_gb:g} GB: {size_gb - included_gb:g} GB",
                    stor,
                    size_gb - included_gb,
                )
            )
        items += _backup_item(res, ctx, "sqldatabases")
        return priced(res, items, notes)
    vcores = sku.vcores
    if vcores <= 0:
        raise Unresolved("vCore count of the SQL SKU is not known")
    default_gb = max_gb or DEFAULT_STORAGE_GB[sku.tier]
    storage_gb = ctx.usage.number("sqldatabases", res.name, "storage_gb", default_gb)
    replicas = int(as_number(res.get("properties.highAvailabilityReplicaCount"), 0)) if sku.tier == "Hyperscale" else 0
    zone = res.get("properties.zoneRedundant") is True
    items, more = _vcore_items(res, ctx, sku, vcores, storage_gb, zone, replicas, "sqldatabases")
    items += _backup_item(res, ctx, "sqldatabases")
    return priced(res, items, notes + more)


def estimate_elastic_pool(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    name = need_str(res, "sku.name", "elastic pool SKU")
    capacity = int(as_number(res.get("sku.capacity"), 0))
    region = ctx.region(res)
    low = name.lower()
    if low in ("basicpool", "standardpool", "premiumpool"):
        tier = low.removesuffix("pool").capitalize()
        meter = ctx.meter(SERVICE, region, product=f"SQL Database Elastic Pool - {tier}", sku="1 DTU")
        if capacity <= 0:
            raise Unresolved("eDTU capacity of the pool is not known")
        return priced(
            res,
            [line(f"{tier} pool {capacity} eDTUs", meter, capacity * DAYS_PER_MONTH)],
            ["storage above the pool's included amount is not priced"],
        )
    sku = parse_sku(f"{name}_{capacity}" if capacity else name)
    if sku.model != "vcore" or sku.vcores <= 0:
        raise Unresolved(f"unrecognised elastic pool SKU {name!r} (capacity {capacity})")
    max_gb = as_number(res.get("properties.maxSizeBytes"), 0.0) / GIB
    storage_gb = ctx.usage.number("sqlelasticpools", res.name, "storage_gb", max_gb or DEFAULT_STORAGE_GB[sku.tier])
    items, notes = _vcore_items(
        res, ctx, sku, sku.vcores, storage_gb, res.get("properties.zoneRedundant") is True, 0, "sqlelasticpools"
    )
    return priced(res, items, notes)


MI_PRODUCTS = {
    "gen5": "Compute Gen5",
    "gen8ih": "Premium Series Compute",
    "gen8im": "Premium Series Memory Optimized Compute",
}


def estimate_managed_instance(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    name = need_str(res, "sku.name", "managed instance SKU").lower()
    m = re.fullmatch(r"(gp|bc)_([a-z0-9]+)", name)
    if not m or m.group(2) not in MI_PRODUCTS:
        raise Unresolved(f"unrecognised managed instance SKU {name!r}")
    tier = "General Purpose" if m.group(1) == "gp" else "Business Critical"
    vcores = int(as_number(res.get("properties.vCores"), as_number(res.get("sku.capacity"), 0)))
    if vcores <= 0:
        raise Unresolved("vCore count of the managed instance is not known")
    region = ctx.region(res)
    product = f"SQL Managed Instance {tier} - {MI_PRODUCTS[m.group(2)]}"
    meters = ctx.meters("SQL Managed Instance", region, product=product)
    meter, mult = node_price(meters, vcores)
    items = [line(f"{tier} {m.group(2)} {vcores} vCore", meter, HOURS_PER_MONTH * mult)]
    if res.get("properties.zoneRedundant") is True and tier == "General Purpose":
        zr, zmult = node_price(meters, vcores, zone_redundant=True)
        items.append(line("Zone redundancy", zr, HOURS_PER_MONTH * zmult))
    storage_gb = ctx.usage.number(
        "sqlmanagedinstances", res.name, "storage_gb", as_number(res.get("properties.storageSizeInGB"), 32)
    )
    extra = max(storage_gb - 32, 0.0)
    if extra:
        storage_meters = ctx.meters("SQL Managed Instance", region, product=f"SQL Managed Instance {tier} - Storage")
        stor = next((s for s in storage_meters if s.name.endswith("Data Stored") and "Zone" not in s.name), None)
        if stor is None:
            raise NoPriceFound(f"no managed instance storage price for {tier} in {region}")
        items.append(line(f"Storage above the included 32 GB: {extra:g} GB", stor, extra))
    notes = ["backup storage above the free allowance and IOPS above the included amount are not priced"]
    if str(res.get("properties.licenseType") or "").lower() == "baseprice":
        notes.append("Azure Hybrid Benefit (BasePrice) is set; the price includes the SQL license, so it is overstated")
    return priced(res, items, notes)
