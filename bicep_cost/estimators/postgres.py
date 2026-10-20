"""Azure Database for PostgreSQL Flexible Server."""

import re

from ..context import HOURS_PER_MONTH, EstimateContext, Unresolved, as_number, need_str
from ..engine import Resource
from ..model import LineItem, ResourceEstimate
from .common import line, node_price, priced

TIER_LABELS = {
    "generalpurpose": "General Purpose",
    "memoryoptimized": "Memory Optimized",
    "burstable": "Burstable",
}

PRODUCT_PREFIX = "Azure Database for PostgreSQL Flexible Server"


def parse_sku(name: str) -> tuple[str, int, str]:
    """``Standard_D2ds_v5`` -> (series ``Ddsv5``, vcores 2, letters ``ds``)."""
    m = re.fullmatch(r"Standard_([A-Za-z])(\d+)([a-z]*)(?:_(v\d+))?", name, flags=re.IGNORECASE)
    if not m:
        raise Unresolved(f"unrecognised PostgreSQL SKU {name!r}")
    family, vcores, feat, version = m.group(1).upper(), int(m.group(2)), m.group(3).lower(), (m.group(4) or "").lower()
    return f"{family}{feat}{version}", vcores, feat


def estimate_server(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    sku_name = need_str(res, "sku.name", "PostgreSQL SKU")
    tier_raw = need_str(res, "sku.tier", "PostgreSQL tier")
    tier = TIER_LABELS.get(tier_raw.lower())
    if tier is None:
        raise Unresolved(f"unrecognised PostgreSQL tier {tier_raw!r}")
    region = ctx.region(res)
    series, vcores, _ = parse_sku(sku_name)
    ha_mode = str(res.get("properties.highAvailability.mode") or "Disabled")
    multiplier = 1 if ha_mode.lower() == "disabled" else 2

    items: list[LineItem] = []
    notes: list[str] = []
    candidates = [
        f"{PRODUCT_PREFIX} {tier} {series} Series Compute",
        f"{PRODUCT_PREFIX} {tier} {series}",
    ]
    if tier == "Burstable" and "_v" not in sku_name.lower():
        candidates.insert(0, f"{PRODUCT_PREFIX} Burstable BS Series Compute")
    compute = None
    qty_multiplier = 1.0
    wanted = sku_name.split("_", 1)[1].lower()
    for product in candidates:
        meters = ctx.meters("Azure Database for PostgreSQL", region, product=product)
        if not meters:
            continue
        node = next((m for m in meters if m.sku.lower() == wanted or m.name.lower() == wanted), None)
        if node is not None:  # burstable: one price per named size
            compute, qty_multiplier = node, 1.0
        else:
            compute, qty_multiplier = node_price(meters, vcores)
        break
    if compute is None:
        raise Unresolved(f"no PostgreSQL compute price for {sku_name} ({tier}) in {region}")
    qty = HOURS_PER_MONTH * multiplier * qty_multiplier
    label = f"{sku_name} ({tier})" + (" x2 (high availability)" if multiplier == 2 else "")
    items.append(line(f"Compute {label}", compute, qty))

    storage_gb = ctx.usage.number(
        "postgres", res.name, "storage_gb", as_number(res.get("properties.storage.storageSizeGB"), 32.0)
    )
    pg = "Azure Database for PostgreSQL"
    storage_product = "Azure Database for PostgreSQL Flex Server Storage"
    ha_label = " x2 (high availability)" if multiplier == 2 else ""
    v2 = str(res.get("properties.storage.type") or "").lower() == "premiumv2_lrs"
    stor_name = "Premium SSD v2 Storage Data Stored" if v2 else "Storage Data Stored"
    storage = ctx.meter(pg, region, product=storage_product, meter=stor_name)
    items.append(
        line(
            f"Storage {storage_gb:g} GB{' (Premium SSD v2)' if v2 else ''}{ha_label}", storage, storage_gb * multiplier
        )
    )
    if v2:
        iops = as_number(res.get("properties.storage.iops"), 3000.0)
        throughput = as_number(res.get("properties.storage.throughput"), 125.0)
        if iops > 3000:
            iops_m = ctx.meter(pg, region, product=storage_product, meter="Premium SSD v2 IOPS Provisioned IOPS")
            items.append(line(f"Provisioned IOPS above 3000: {iops - 3000:g}", iops_m, (iops - 3000) * multiplier))
        if throughput > 125:
            tp_m = ctx.meter(
                pg, region, product=storage_product, meter="Premium SSDv2 Throughput Prov Throughput (MBps)"
            )
            items.append(
                line(f"Throughput above 125 MB/s: {throughput - 125:g}", tp_m, (throughput - 125) * multiplier)
            )
    backup_gb = ctx.usage.number("postgres", res.name, "backup_storage_gb", 0.0)
    extra_backup = max(backup_gb - storage_gb, 0.0)
    if extra_backup:
        geo = str(res.get("properties.backup.geoRedundantBackup") or "").lower() == "enabled"
        backup_meters = ctx.meters(pg, region, product=f"{PRODUCT_PREFIX} Backup Storage")
        pick = next((m for m in backup_meters if ("GRS" in m.name) == geo and "Data Stored" in m.name), None)
        factor = 1.0
        if pick is None and geo:
            pick = next((m for m in backup_meters if "Data Stored" in m.name), None)
            factor = 2.0
            notes.append("no geo-redundant backup price found; priced at twice the locally redundant rate")
        if pick is not None:
            items.append(
                line(
                    f"Backup storage above the free allowance: {extra_backup:g} GB{' (geo-redundant)' if geo else ''}",
                    pick,
                    extra_backup * factor,
                    "usage",
                    note="measured/assumed backup size",
                )
            )
    if str(res.get("properties.storage.autoGrow") or "").lower() == "enabled":
        notes.append("storage auto-grow is enabled; priced at the provisioned size")
    notes.append("backup storage up to the provisioned size is free; read replicas are priced as separate servers")
    return priced(res, items, notes)
