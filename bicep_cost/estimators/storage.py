"""Storage accounts (blob / ADLS Gen2 / file shares). Capacity and operations are usage assumptions."""

import re

from ..context import EstimateContext, Unresolved, need_str
from ..engine import Resource
from ..model import LineItem, ResourceEstimate
from ..prices import Meter
from .common import included, line, priced

REDUNDANCY = {
    "lrs": "LRS",
    "grs": "GRS",
    "ragrs": "RA-GRS",
    "zrs": "ZRS",
    "gzrs": "GZRS",
    "ragzrs": "RA-GZRS",
}


def _pick(meters: tuple[Meter, ...], *, contains: str, excludes: tuple[str, ...] = ()) -> Meter | None:
    matches = [
        m
        for m in meters
        if contains.lower() in m.name.lower() and not any(x.lower() in m.name.lower() for x in excludes)
    ]
    return sorted(matches, key=lambda m: (len(m.name), m.name))[0] if matches else None


def estimate_account(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    sku_name = need_str(res, "sku.name", "storage SKU")
    kind = res.kind or "StorageV2"
    region = ctx.region(res)
    tier_part, _, redundancy_part = sku_name.partition("_")
    if tier_part.lower() == "premium":
        return ResourceEstimate(res, "unsupported", notes=[f"Premium storage ({sku_name}) is not priced"])
    redundancy = REDUNDANCY.get(redundancy_part.lower().replace("-", ""))
    if redundancy is None:
        raise Unresolved(f"unrecognised storage redundancy in {sku_name!r}")
    access_tier = res.get("properties.accessTier") or "Hot"
    if not isinstance(access_tier, str) or access_tier.capitalize() not in ("Hot", "Cool", "Cold"):
        access_tier = "Hot"
    access_tier = access_tier.capitalize()
    hns = res.get("properties.isHnsEnabled") is True
    if kind.lower() == "blobstorage":
        product = "Blob Storage"
    elif hns:
        product = "Azure Data Lake Storage Gen2 Hierarchical Namespace"
    else:
        product = "General Block Blob v2"
    sku = f"{access_tier} {redundancy}"
    meters = ctx.meters("Storage", region, product=product, sku=sku)
    if not meters:
        raise Unresolved(f"no storage price for {product} / {sku} in {region}")

    stored_gb = ctx.usage.number("storage", res.name, "stored_gb", 10.0)
    write_ops = ctx.usage.number("storage", res.name, "write_operations_10k", 10.0)
    read_ops = ctx.usage.number("storage", res.name, "read_operations_10k", 100.0)
    other_ops = ctx.usage.number("storage", res.name, "other_operations_10k", 10.0)
    items: list[LineItem] = []
    stored = _pick(meters, contains="Data Stored")
    if stored:
        items.append(
            line(f"{product}, {sku}: {stored_gb:g} GB stored", stored, stored_gb, "usage", note="assumed capacity")
        )
    for label, qty, needle, excludes in (
        ("write operations", write_ops, "Write Operations", ("Iterative",)),
        ("read operations", read_ops, "Read Operations", ("Iterative",)),
        ("other operations", other_ops, "Other Operations", ("Iterative",)),
    ):
        m = _pick(meters, contains=needle, excludes=excludes)
        if m and qty:
            items.append(
                line(f"{product}, {sku}: {qty * 10_000:,.0f} {label}", m, qty, "usage", note="assumed transactions")
            )
    notes = [f"{access_tier}/{redundancy}" + (" with hierarchical namespace" if hns else "")]
    if not hns and redundancy_part.lower().startswith(("ra", "g")):
        notes.append("geo-replication data transfer is not priced")
    return priced(res, items, notes)


def estimate_share(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    account_name = res.name.split("/")[0]
    account = next((a for a in ctx.index.of_type("Microsoft.Storage/storageAccounts") if a.name == account_name), None)
    if account is None:
        return included(res, "storage account is not part of this template")
    sku_name = need_str(account, "sku.name", "storage SKU")
    tier_part, _, redundancy_part = sku_name.partition("_")
    if tier_part.lower() == "premium":
        return ResourceEstimate(res, "unsupported", notes=["Premium file shares are not priced"])
    redundancy = REDUNDANCY.get(redundancy_part.lower().replace("-", ""))
    if redundancy is None:
        raise Unresolved(f"unrecognised storage redundancy in {sku_name!r}")
    share_tier = str(res.get("properties.accessTier") or "TransactionOptimized")
    tier_label = {"hot": "Hot", "cool": "Cool"}.get(share_tier.lower(), "Standard")
    region = ctx.region(account)
    meters = ctx.meters("Storage", region, product="Files v2", sku=f"{tier_label} {redundancy}")
    quota = res.get("properties.shareQuota")
    used_default = min(float(quota), 5.0) if isinstance(quota, (int, float)) and not isinstance(quota, bool) else 5.0
    used_gb = ctx.usage.number("fileshares", res.name, "used_gb", used_default)
    stored = _pick(meters, contains="Data Stored")
    items: list[LineItem] = []
    if stored:
        items.append(
            line(
                f"File share ({tier_label} {redundancy}): {used_gb:g} GB used",
                stored,
                used_gb,
                "usage",
                note="assumed capacity (pay-as-you-go, not the quota)",
            )
        )
    for key, needle, label in (
        ("write_operations_10k", "Write Operations", "write operations"),
        ("read_operations_10k", "Read Operations", "read operations"),
        ("other_operations_10k", "Protocol Operations", "other operations"),
    ):
        ops_10k = ctx.usage.number("fileshares", res.name, key, 10.0)
        m = _pick(meters, contains=needle)
        if m and ops_10k:
            items.append(
                line(f"File share {label}: {ops_10k * 10_000:,.0f}", m, ops_10k, "usage", note="assumed transactions")
            )
    return priced(res, items, ["standard file share, billed on used capacity"])


def share_quota_label(value: object) -> str:
    return re.sub(r"\.0$", "", str(value))
