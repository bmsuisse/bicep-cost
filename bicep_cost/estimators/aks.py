"""Azure Kubernetes Service: cluster management fee (tier) and the VMs behind each node pool."""

import re

from bicep_cost.armexpr import JsonValue, Unknown
from bicep_cost.context import HOURS_PER_MONTH, EstimateContext, UnresolvedError, as_number
from bicep_cost.engine import Resource
from bicep_cost.estimators.common import included, line, priced, usage_amount
from bicep_cost.estimators.vm import default_os_disk_type, disk_item, vm_meter
from bicep_cost.model import LineItem, ResourceEstimate

# AKS sizes a managed OS disk it is not told the size of by the node's vCPU count (P10 / P15 / P20 / P30).
DEFAULT_OS_DISK_GB = ((8, 128.0), (16, 256.0), (64, 512.0), (float("inf"), 1024.0))


def default_os_disk_gb(vm_size: str) -> float:
    match = re.match(r"standard_[a-z]+(\d+)", vm_size.lower())
    vcpus = int(match.group(1)) if match else 1
    return next(gb for limit, gb in DEFAULT_OS_DISK_GB if vcpus < limit)


def _control_plane(res: Resource, ctx: EstimateContext, region: str) -> tuple[list[LineItem], list[str]]:
    if res.is_unknown_at("sku.name"):
        raise UnresolvedError(f"SKU of {res.name} is not known (depends on an unset parameter)")
    if str(res.get("sku.name") or "Base").lower() == "automatic":
        meter = ctx.meter(
            "Azure Kubernetes Service",
            region,
            product="Azure Kubernetes Service - Automatic",
            sku="Automatic",
            meter="Automatic Hosted Control Plane",
        )
        return [line("AKS Automatic hosted control plane", meter, HOURS_PER_MONTH)], [
            "AKS Automatic provisions and bills its nodes itself; set [aks.<name>] monthly_cost for that compute"
        ]
    if res.is_unknown_at("sku.tier"):
        raise UnresolvedError(f"tier of {res.name} is not known (depends on an unset parameter)")
    tier = str(res.get("sku.tier") or "Free").lower()
    if tier == "free":
        return [], ["Free tier: no cluster management fee (no financially backed uptime SLA)"]
    if tier == "standard":
        meter = ctx.meter("Azure Kubernetes Service", region, sku="Standard", meter="Standard Uptime SLA")
        return [line("Standard tier cluster management (uptime SLA)", meter, HOURS_PER_MONTH)], []
    if tier == "premium":
        meter = ctx.meter("Azure Kubernetes Service", region, sku="Standard", meter="Standard Long Term Support")
        return [line("Premium tier cluster management (uptime SLA + long term support)", meter, HOURS_PER_MONTH)], []
    raise UnresolvedError(f"AKS tier {tier!r} is not priced")


def _pool_items(
    ctx: EstimateContext, region: str, label: str, pool: dict[str, JsonValue], *, hybrid_benefit: bool
) -> tuple[list[LineItem], list[str]]:
    size = pool.get("vmSize")
    if not isinstance(size, str):
        raise UnresolvedError(f"VM size of node pool {label} is not known")
    for key in ("count", "minCount", "osType", "scaleSetPriority", "osDiskType", "osDiskSizeGB", "enableAutoScaling"):
        if isinstance(pool.get(key), Unknown):
            raise UnresolvedError(f"{key} of node pool {label} is not known (depends on an unset parameter)")
    notes: list[str] = []
    autoscale = pool.get("enableAutoScaling") is True
    explicit = ctx.usage.optional_number("aksnodepools", label, "nodes")
    if explicit is not None:
        nodes = explicit
    elif isinstance(pool.get("count"), (int, float)):
        nodes = as_number(pool.get("count"), 1.0)
    elif autoscale and isinstance(pool.get("minCount"), (int, float)):
        nodes = as_number(pool.get("minCount"), 1.0)
    else:
        nodes = 1.0
        notes.append(f"node pool {label} has no node count; assumed 1")
    if autoscale:
        notes.append(
            f"node pool {label} autoscales ({pool.get('minCount')}-{pool.get('maxCount')}); "
            f'priced at {nodes:g} node(s), set [aksnodepools."{label}"] nodes to the typical count'
        )
    windows = str(pool.get("osType") or "Linux").lower() == "windows"
    spot = str(pool.get("scaleSetPriority") or "").lower() == "spot"
    priced_windows = windows and not hybrid_benefit
    meter = vm_meter(ctx, region, size, priced_windows, spot)
    os_label = "Windows" if priced_windows else "Windows (Hybrid Benefit)" if windows else "Linux"
    items = [line(f"{label}: {size} {os_label}{' Spot' if spot else ''} x{nodes:g}", meter, nodes * HOURS_PER_MONTH)]
    if str(pool.get("osDiskType") or "").lower() == "ephemeral":
        notes.append(f"node pool {label}: ephemeral OS disk, no disk charge")
    else:
        if not pool.get("osDiskType"):
            notes.append(
                f"node pool {label}: OS disk type not set; priced as a managed disk (AKS uses a free ephemeral disk "
                "when the VM's temp storage is large enough: set osDiskType to Ephemeral)"
            )
        size_gb = as_number(pool.get("osDiskSizeGB"), 0.0)
        if size_gb <= 0:
            size_gb = default_os_disk_gb(size)
            notes.append(f"node pool {label}: OS disk size not set; assumed {size_gb:g} GB")
        items.append(disk_item(ctx, region, f"{label} OS disk", default_os_disk_type(size), size_gb, nodes))
    return items, notes


def _hybrid_benefit(cluster: Resource) -> bool:
    return str(cluster.get("properties.windowsProfile.licenseType") or "").lower() == "windows_server"


def estimate_cluster(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    region = ctx.region(res)
    items, notes = _control_plane(res, ctx, region)
    if str(res.get("sku.name") or "").lower() == "automatic":
        items += usage_amount(res, ctx, "aks")
    else:
        if res.is_unknown_at("properties.agentPoolProfiles"):
            raise UnresolvedError(f"node pools of {res.name} are not known (depend on an unset parameter)")
        hybrid = _hybrid_benefit(res)
        pools = res.get("properties.agentPoolProfiles", [])
        for pool in pools if isinstance(pools, list) else []:
            if isinstance(pool, Unknown):
                raise UnresolvedError(f"a node pool of {res.name} is not known (depends on an unset parameter)")
            if isinstance(pool, dict):
                pool_items, pool_notes = _pool_items(
                    ctx, region, f"{res.name}/{pool.get('name')}", pool, hybrid_benefit=hybrid
                )
                items += pool_items
                notes += pool_notes
    notes.append(
        "load balancers, public IPs, egress, persistent volumes and add-ons (Defender, monitoring) are not included "
        "unless they are separate resources in the template"
    )
    return priced(res, items, notes)


def estimate_agent_pool(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    cluster = ctx.index.find(res.id.rsplit("/", 2)[0])
    region = ctx.region(cluster) if cluster is not None else ctx.region(res)
    if cluster is not None:
        inline = cluster.get("properties.agentPoolProfiles", [])
        names = (
            {str(p.get("name", "")).lower() for p in inline if isinstance(p, dict)}
            if isinstance(inline, list)
            else set()
        )
        if res.name.rsplit("/", 1)[-1].lower() in names:
            return included(res, f"node pool is also defined inline on cluster {cluster.name} and priced there")
    hybrid = cluster is not None and _hybrid_benefit(cluster)
    items, notes = _pool_items(ctx, region, res.name, res.properties, hybrid_benefit=hybrid)
    return priced(res, items, notes)
