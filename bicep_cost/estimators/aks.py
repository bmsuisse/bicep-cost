"""Azure Kubernetes Service: cluster management fee (tier) and the VMs behind each node pool."""

from bicep_cost.armexpr import JsonValue
from bicep_cost.context import HOURS_PER_MONTH, EstimateContext, UnresolvedError, as_number
from bicep_cost.engine import Resource
from bicep_cost.estimators.common import line, priced, usage_amount
from bicep_cost.estimators.vm import default_os_disk_type, disk_item, vm_meter
from bicep_cost.model import LineItem, ResourceEstimate

DEFAULT_OS_DISK_GB = 128.0


def _control_plane(res: Resource, ctx: EstimateContext, region: str) -> tuple[list[LineItem], list[str]]:
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
            notes.append(f"node pool {label}: OS disk type not set; assumed a managed disk")
        size_gb = as_number(pool.get("osDiskSizeGB"), 0.0)
        if size_gb <= 0:
            size_gb = DEFAULT_OS_DISK_GB
            notes.append(f"node pool {label}: OS disk size not set; assumed {size_gb:g} GB")
        items.append(disk_item(ctx, region, f"{label} OS disk", default_os_disk_type(size), size_gb, nodes))
    return items, notes


def estimate_cluster(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    region = ctx.region(res)
    items, notes = _control_plane(res, ctx, region)
    if str(res.get("sku.name") or "").lower() == "automatic":
        items += usage_amount(res, ctx, "aks")
    else:
        hybrid = str(res.get("properties.windowsProfile.licenseType") or "").lower() == "windows_server"
        pools = res.get("properties.agentPoolProfiles", [])
        for pool in pools if isinstance(pools, list) else []:
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
    items, notes = _pool_items(ctx, region, res.name, res.properties, hybrid_benefit=False)
    return priced(res, items, notes)
