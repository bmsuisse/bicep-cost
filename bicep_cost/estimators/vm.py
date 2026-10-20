"""Virtual machines, scale sets, managed disks and public IP addresses."""

import re

from bicep_cost.armexpr import JsonValue
from bicep_cost.context import HOURS_PER_MONTH, EstimateContext, UnresolvedError, as_number, need_str
from bicep_cost.engine import Resource
from bicep_cost.estimators.common import line, priced
from bicep_cost.model import LineItem, ResourceEstimate
from bicep_cost.prices import Meter, NoPriceFoundError

# (max size in GiB, tier number) for managed disk SKUs.
SSD_TIERS = [
    (4, 1),
    (8, 2),
    (16, 3),
    (32, 4),
    (64, 6),
    (128, 10),
    (256, 15),
    (512, 20),
    (1024, 30),
    (2048, 40),
    (4096, 50),
    (8192, 60),
    (16384, 70),
    (32767, 80),
]
HDD_TIERS = [(4, 1), (8, 2), (16, 3), *SSD_TIERS[3:]]

# storageAccountType -> (price product, tier letter, redundancy, tier table)
DISK_TYPES: dict[str, tuple[str, str, str, list[tuple[int, int]]]] = {
    "premium_lrs": ("Premium SSD Managed Disks", "P", "LRS", SSD_TIERS),
    "premium_zrs": ("Premium SSD Managed Disks", "P", "ZRS", SSD_TIERS),
    "standardssd_lrs": ("Standard SSD Managed Disks", "E", "LRS", SSD_TIERS),
    "standardssd_zrs": ("Standard SSD Managed Disks", "E", "ZRS", SSD_TIERS),
    "standard_lrs": ("Standard HDD Managed Disks", "S", "LRS", HDD_TIERS),
}
DEFAULT_OS_DISK_GB = {"linux": 30.0, "windows": 127.0}


def disk_label(account_type: str, size_gb: float) -> tuple[str, str]:
    """(product, SKU like ``P10 LRS``) for a managed disk of at least ``size_gb``."""
    spec = DISK_TYPES.get(account_type.lower())
    if spec is None:
        raise UnresolvedError(
            f"managed disk type {account_type!r} is not priced (Ultra / Premium SSD v2 are not supported)"
        )
    product, letter, redundancy, tiers = spec
    for max_gb, number in tiers:
        if size_gb <= max_gb:
            return product, f"{letter}{number} {redundancy}"
    raise UnresolvedError(f"disk size {size_gb:g} GB exceeds the largest managed disk")


def disk_item(
    ctx: EstimateContext, region: str, what: str, account_type: str, size_gb: float, count: float = 1.0
) -> LineItem:
    product, sku = disk_label(account_type, size_gb)
    meters = ctx.meters("Storage", region, product=product, sku=sku)
    meter = next((m for m in meters if m.name.endswith(" Disk")), None)
    if meter is None:
        raise NoPriceFoundError(f"no managed disk price for {product} {sku} in {region}")
    return line(f"{what}: {sku} ({size_gb:g} GB)" + (f" x{count:g}" if count != 1 else ""), meter, count)


def _is_windows(profile: dict[str, JsonValue]) -> bool:
    os_profile = profile.get("osProfile")
    if isinstance(os_profile, dict) and "windowsConfiguration" in set(os_profile):
        return True
    storage = profile.get("storageProfile")
    if isinstance(storage, dict):
        os_disk = storage.get("osDisk")
        if isinstance(os_disk, dict) and str(os_disk.get("osType", "")).lower() == "windows":
            return True
        image = storage.get("imageReference")
        if isinstance(image, dict):
            offer = f"{image.get('publisher', '')} {image.get('offer', '')} {image.get('sku', '')}".lower()
            return "windows" in offer
    return False


def _vm_meter(ctx: EstimateContext, region: str, size: str, windows: bool, spot: bool) -> Meter:
    meters = ctx.meters("Virtual Machines", region, arm_sku=size)

    def kind(m: Meter) -> tuple[bool, bool, bool]:
        label = f"{m.sku} {m.name}".lower()
        return "windows" in m.product.lower(), "spot" in label, "low priority" in label

    pick = [m for m in meters if kind(m) == (windows, spot, False)]
    if not pick:
        raise NoPriceFoundError(
            f"no VM price for {size}{' Spot' if spot else ''} ({'Windows' if windows else 'Linux'}) in {region}"
        )
    return min(pick, key=lambda m: (m.product, m.sku))


def _license_notes(profile: dict[str, JsonValue], windows: bool, license_type: str) -> list[str]:
    notes: list[str] = []
    storage = profile.get("storageProfile")
    image = storage.get("imageReference") if isinstance(storage, dict) else None
    if isinstance(image, dict):
        text = f"{image.get('publisher', '')} {image.get('offer', '')}".lower()
        if "sql" in text:
            notes.append("SQL Server image: the SQL license surcharge is not included")
        if any(k in text for k in ("redhat", "rhel", "suse", "sles")):
            notes.append("RHEL/SUSE image: the OS license surcharge is not included")
    if windows and license_type.lower() in ("windows_server", "windows_client"):
        notes.append("Azure Hybrid Benefit: priced at the Linux (no Windows license) rate")
    return notes


def _vm_items(
    res: Resource, ctx: EstimateContext, base: str, size: str, count: float, section: str
) -> tuple[list[LineItem], list[str]]:
    region = ctx.region(res)
    raw = res.get(base)
    profile: dict[str, JsonValue] = raw if isinstance(raw, dict) else {}
    windows = _is_windows(profile)
    license_type = str(profile.get("licenseType") or "")
    priced_windows = windows and license_type.lower() not in ("windows_server", "windows_client")
    spot = str(profile.get("priority") or "").lower() == "spot"
    hours = ctx.usage.number(section, res.name, "hours_per_month", HOURS_PER_MONTH)
    meter = _vm_meter(ctx, region, size, priced_windows, spot)
    os_label = "Windows" if priced_windows else "Linux" if not windows else "Windows (Hybrid Benefit)"
    items = [
        line(
            f"{size} {os_label}{' Spot' if spot else ''}"
            + (f" x{count:g}" if count != 1 else "")
            + f", {hours:,.0f} h",
            meter,
            hours * count,
            "fixed" if hours == HOURS_PER_MONTH else "usage",
            note=None if hours == HOURS_PER_MONTH else "assumed / measured running hours",
        )
    ]
    notes = _license_notes(profile, windows, license_type)
    if spot:
        notes.append("Spot price; the VM may be evicted, hours are as configured")
    storage = profile.get("storageProfile")
    storage = storage if isinstance(storage, dict) else {}
    os_disk = storage.get("osDisk")
    if isinstance(os_disk, dict):
        diff = os_disk.get("diffDiskSettings")
        if isinstance(diff, dict) and str(diff.get("option", "")).lower() == "local":
            notes.append("ephemeral OS disk: no disk charge")
        else:
            managed = os_disk.get("managedDisk")
            managed = managed if isinstance(managed, dict) else {}
            if str(os_disk.get("createOption", "FromImage")).lower() == "attach":
                notes.append("OS disk is attached from an existing disk (priced separately if it is in the template)")
            else:
                account = managed.get("storageAccountType")
                if not isinstance(account, str):
                    account = "Premium_LRS" if re.search(r"\d[a-z]*s(?:_|$)", size.lower()) else "StandardSSD_LRS"
                    notes.append(f"OS disk type not set; assumed {account}")
                default = DEFAULT_OS_DISK_GB["windows" if windows else "linux"]
                size_gb = as_number(os_disk.get("diskSizeGB"), default)
                if not isinstance(os_disk.get("diskSizeGB"), (int, float)):
                    notes.append(f"OS disk size not set; assumed {size_gb:g} GB")
                items.append(disk_item(ctx, region, "OS disk", account, size_gb, count))
    data = storage.get("dataDisks")
    if isinstance(data, list):
        for d in data:
            if not isinstance(d, dict):
                continue
            managed = d.get("managedDisk")
            managed = managed if isinstance(managed, dict) else {}
            if str(d.get("createOption", "")).lower() == "attach" and ctx.index.find(managed.get("id")) is not None:
                continue  # that disk resource is priced on its own
            account = managed.get("storageAccountType") or "Premium_LRS"
            if not isinstance(account, str):
                account = "Premium_LRS"
            size_gb = as_number(d.get("diskSizeGB"), 0.0)
            if size_gb <= 0:
                notes.append(f"data disk lun {d.get('lun')} has no size in the template and is not priced")
                continue
            items.append(disk_item(ctx, region, f"data disk {d.get('lun', '?')}", account, size_gb, count))
    if any(i.meter and i.meter.product.startswith("Standard HDD") for i in items):
        notes.append("Standard HDD transaction charges are not included")
    return items, notes


def estimate_vm(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    size = need_str(res, "properties.hardwareProfile.vmSize", "VM size")
    items, notes = _vm_items(res, ctx, "properties", size, 1.0, "virtualmachines")
    return priced(res, items, notes)


def estimate_vmss(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    size = need_str(res, "sku.name", "scale set VM size")
    override = ctx.usage.optional_number("virtualmachinescalesets", res.name, "instances")
    count = override if override is not None else max(as_number(res.get("sku.capacity"), 1.0), 0.0)
    items, notes = _vm_items(res, ctx, "properties.virtualMachineProfile", size, count, "virtualmachinescalesets")
    notes.append("autoscale is not priced; set [virtualmachinescalesets.<name>] instances to the typical count")
    return priced(res, items, notes)


def estimate_disk(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    account = need_str(res, "sku.name", "disk SKU")
    size = as_number(res.get("properties.diskSizeGB"), 0.0)
    if size <= 0:
        raise UnresolvedError(f"disk size of {res.name} is not known")
    region = ctx.region(res)
    return priced(res, [disk_item(ctx, region, "managed disk", account, size)])


def estimate_public_ip(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    sku = str(res.get("sku.name") or "Basic").capitalize()
    tier = str(res.get("sku.tier") or "Regional")
    static = str(res.get("properties.publicIPAllocationMethod") or "Dynamic").lower() == "static" or sku == "Standard"
    region = ctx.region(res)
    if tier.lower() == "global":
        meter = ctx.meter(
            "Virtual Network", region, product="IP Addresses", sku="Global", meter="Global IPv4 Static Public IP"
        )
    elif sku == "Standard":
        meter = ctx.meter(
            "Virtual Network", region, product="IP Addresses", sku="Standard", meter="Standard IPv4 Static Public IP"
        )
    else:
        name = "Basic IPv4 Static Public IP" if static else "Basic IPv4 Dynamic Public IP"
        meter = ctx.meter("Virtual Network", region, product="IP Addresses", sku="Basic", meter=name)
    return priced(
        res,
        [line(f"{sku} {'static' if static else 'dynamic'} public IP", meter, HOURS_PER_MONTH)],
        ["priced as always allocated; data transfer is not included"],
    )
