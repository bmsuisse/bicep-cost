"""Price a deployed subscription (or some of its resource groups) instead of a template.

Read-only. One Azure Resource Graph query lists the resources with their live properties; those have the same shape as
ARM template resources, so every estimator runs unchanged. Cost Management actuals (``--actuals``) reconcile the
estimate with what was billed and price resource types that have no estimator; Azure Monitor (``--measure``) fills in
the usage-based quantities with the same collectors as ``collect``.

Resource Graph does not return child resources such as file shares, and lags a few minutes behind ARM.
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

from bicep_cost.armexpr import JsonValue
from bicep_cost.collect.arm import ArmClient, AzureError
from bicep_cost.context import HOURS_PER_MONTH
from bicep_cost.engine import Resource
from bicep_cost.model import Estimate, LineItem

ARG_API = "2022-10-01"
PAGE_SIZE = 1000
FIELDS = ("location", "kind", "sku", "properties", "plan", "zones", "managedBy")
_RESOURCE_GROUP = re.compile(r"^[\w.()-]{1,90}$")
_ID = re.compile(r"^/subscriptions/([^/]+)/resourcegroups/([^/]+)/providers/([^/]+)/(.+)$", re.IGNORECASE)
UNPRICED = ("unsupported", "unresolved")


def fetch_inventory(
    arm: ArmClient, subscription_id: str, resource_groups: tuple[str, ...] = ()
) -> list[dict[str, JsonValue]]:
    """All resources of the subscription (optionally only some resource groups), with their live properties."""
    for group in resource_groups:
        if not _RESOURCE_GROUP.match(group):
            raise ValueError(f"invalid resource group name {group!r}")
    query = "resources"
    if resource_groups:  # filter first: `project` drops the resourceGroup column
        query += " | where resourceGroup in~ (" + ", ".join(f"'{g}'" for g in resource_groups) + ")"
    query += " | project id, name, type, location, kind, sku, properties, plan, zones, managedBy"
    return query_graph(arm, subscription_id, query)


def query_graph(arm: ArmClient, subscription_id: str, query: str) -> list[dict[str, JsonValue]]:
    """All rows of a Resource Graph query (follows the skip token)."""
    rows: list[dict[str, JsonValue]] = []
    skip: str | None = None
    while True:
        options: dict[str, object] = {"$top": PAGE_SIZE}
        if skip:
            options["$skipToken"] = skip
        body: dict[str, object] = {"subscriptions": [subscription_id], "query": query, "options": options}
        payload = arm.post("/providers/Microsoft.ResourceGraph/resources", ARG_API, body)
        data = payload.get("data") if payload else None
        if not isinstance(data, list):
            raise AzureError("Resource Graph query returned no result")
        rows += [r for r in data if isinstance(r, dict)]
        token = payload.get("$skipToken") if payload else None
        skip = token if isinstance(token, str) and token else None
        if skip is None:
            return rows


def split_id(resource_id: str) -> tuple[str, str, str, str] | None:
    """(subscription, resource group, type, name) of a resource id; child names are joined with ``/``."""
    match = _ID.match(resource_id)
    if match is None:
        return None
    parts = match.group(4).split("/")
    if len(parts) % 2:
        return None
    return match.group(1), match.group(2), f"{match.group(3)}/{'/'.join(parts[0::2])}", "/".join(parts[1::2])


def to_resources(rows: list[dict[str, JsonValue]]) -> list[Resource]:
    """Resource Graph returns only the last name segment (``db``); estimators expect ``server/db`` like a template."""
    out: list[Resource] = []
    for row in rows:
        rid = row.get("id")
        parsed = split_id(rid) if isinstance(rid, str) else None
        if parsed is None or not isinstance(rid, str):
            continue
        sub, group, type_, name = parsed
        body = {k: row[k] for k in FIELDS if row.get(k) is not None}
        out.append(Resource(type_, name, rid, body, (), sub, group))
    return out


def in_scope(
    costs: dict[str, tuple[float, str]], subscription_id: str, groups: tuple[str, ...]
) -> dict[str, tuple[float, str]]:
    """Keep only the cost rows of the scanned resource groups (all of them when none are given)."""
    prefixes = tuple(f"/subscriptions/{subscription_id}/resourcegroups/{g}/".lower() for g in groups)
    return {k: v for k, v in costs.items() if not prefixes or k.startswith(prefixes)}


@dataclass
class TypeRow:
    type: str
    resources: int = 0
    modelled: float = 0.0
    actual: float = 0.0
    measured: int = 0  # priced from the actual cost because there is no estimator (or it could not price)
    unpriced: int = 0


@dataclass
class Coverage:
    currency: str
    window_days: int
    rows: list[TypeRow] = field(default_factory=list)
    outside: dict[str, float] = field(default_factory=dict)  # actual cost of ids that are not in the inventory

    @property
    def actual(self) -> float:
        return sum(r.actual for r in self.rows) + sum(self.outside.values())

    @property
    def modelled(self) -> float:
        return sum(r.modelled for r in self.rows)


def reconcile(estimate: Estimate, costs: dict[str, tuple[float, str]], window_days: int) -> Coverage:
    """Compare the estimate with actual cost per type and price unpriced resources from their actual cost."""
    factor = HOURS_PER_MONTH / 24 / window_days
    rows: dict[str, TypeRow] = {}
    seen: set[str] = set()
    for est in estimate.resources:
        res = est.resource
        key = res.id.lower()
        seen.add(key)
        row = rows.setdefault(res.type.lower(), TypeRow(res.type.lower()))
        actual = costs.get(key, (0.0, ""))[0] * factor
        row.resources += 1
        row.modelled += est.monthly
        row.actual += actual
        # resources of a managed service (Databricks / AKS nodes, ...) come and go: the bill is the truth
        # nothing modelled: no estimator, or one that returns no line items (usage-only types such as AI accounts)
        unmodelled = est.status in (*UNPRICED, "free") or (est.status == "priced" and not est.items)
        managed = bool(res.body.get("managedBy")) and est.status == "priced" and key in costs
        if (actual > 0 and unmodelled) or managed:
            est.notes.append(f"{est.status}: priced from its actual cost instead of the model")
            est.items = [LineItem("actual cost (Cost Management, scaled to a month)", 1, "usage", fixed_amount=actual)]
            est.items[0].monthly = actual
            est.status = "priced"
            row.measured += 1
        elif est.status in UNPRICED:
            row.unpriced += 1
    outside: dict[str, float] = defaultdict(float)
    for key, (cost, _) in costs.items():
        if key not in seen and cost:
            parsed = split_id(key)
            outside[parsed[2] if parsed else "(unknown)"] += cost * factor
    return Coverage(
        estimate.currency, window_days, sorted(rows.values(), key=lambda r: -max(r.actual, r.modelled)), dict(outside)
    )


def _ratio(row: TypeRow) -> str:
    return f"{row.modelled / row.actual * 100:>5.0f}%" if row.actual > 0 else "     -"


def _note(row: TypeRow) -> str:
    notes = []
    if row.measured:
        notes.append(f"{row.measured} priced from actuals")
    if row.unpriced:
        notes.append(f"{row.unpriced} not priced")
    return ", ".join(notes)


def render_coverage_text(cov: Coverage, top: int = 30) -> str:
    out = [
        "",
        f"Coverage: estimate vs actual cost (Cost Management, last {cov.window_days} d, per month, {cov.currency})",
        "  MODELLED = what the estimators priced before using actuals; 100% means the model matches the bill.",
        f"{'TYPE':<58} {'RES':>4} {'MODELLED':>11} {'ACTUAL':>11} {'M/A':>6}  NOTE",
    ]
    out.extend(
        f"{r.type[:58]:<58} {r.resources:>4} {r.modelled:>11,.0f} {r.actual:>11,.0f} {_ratio(r)}  {_note(r)}"
        for r in cov.rows[:top]
    )
    if len(cov.rows) > top:
        rest = cov.rows[top:]
        out.append(
            f"{f'({len(rest)} more types)':<58} {sum(r.resources for r in rest):>4} "
            f"{sum(r.modelled for r in rest):>11,.0f} {sum(r.actual for r in rest):>11,.0f}"
        )
    if cov.outside:
        total = sum(cov.outside.values())
        top_types = sorted(cov.outside.items(), key=lambda kv: -kv[1])[:3]
        detail = ", ".join(f"{t} {v:,.0f}" for t, v in top_types)
        out.append(f"{'not in the inventory (deleted / managed)':<58} {'':>4} {'':>11} {total:>11,.0f}  {detail}")
    out.append(f"{'TOTAL':<58} {sum(r.resources for r in cov.rows):>4} {cov.modelled:>11,.0f} {cov.actual:>11,.0f}")
    return "\n".join(out) + "\n"


def render_coverage_markdown(cov: Coverage, top: int = 30) -> str:
    lines = [
        "",
        f"### Coverage: estimate vs actual ({cov.currency}/month, last {cov.window_days} d)",
        "",
        "| Type | Resources | Modelled | Actual | Modelled / actual | Note |",
        "|---|---:|---:|---:|---:|---|",
    ]
    lines.extend(
        f"| `{r.type}` | {r.resources} | {r.modelled:,.0f} | {r.actual:,.0f} | {_ratio(r).strip()} | {_note(r)} |"
        for r in cov.rows[:top]
    )
    if cov.outside:
        lines.append(f"| not in the inventory | | | {sum(cov.outside.values()):,.0f} | | deleted / managed resources |")
    lines.append(f"| **Total** | | {cov.modelled:,.0f} | {cov.actual:,.0f} | | |")
    return "\n".join(lines) + "\n"


def coverage_json(cov: Coverage) -> dict[str, object]:
    return {
        "currency": cov.currency,
        "window_days": cov.window_days,
        "types": [
            {
                "type": r.type,
                "resources": r.resources,
                "modelled": round(r.modelled, 2),
                "actual": round(r.actual, 2),
                "priced_from_actuals": r.measured,
                "not_priced": r.unpriced,
            }
            for r in cov.rows
        ],
        "outside_inventory": {k: round(v, 2) for k, v in cov.outside.items()},
    }


def add_coverage_json(text: str, cov: Coverage) -> str:
    data = json.loads(text)
    data["coverage"] = coverage_json(cov)
    return json.dumps(data, indent=2) + "\n"


AKS_NODE_TYPES = ("microsoft.compute/virtualmachinescalesets", "microsoft.compute/virtualmachines")


def without_aks_nodes(resources: list[Resource]) -> tuple[list[Resource], int]:
    """Drop node VMs / scale sets in AKS node resource groups: the cluster's estimate already prices its node pools.

    Load balancers, public IPs and volume disks of the node group stay: the cluster estimate does not cover them.
    """
    node_groups = {
        (r.subscription_id.lower(), str(r.get("properties.nodeResourceGroup")).lower())
        for r in resources
        if r.type.lower() == "microsoft.containerservice/managedclusters" and r.get("properties.nodeResourceGroup")
    }
    kept = [
        r
        for r in resources
        if r.type.lower() not in AKS_NODE_TYPES
        or (r.subscription_id.lower(), r.resource_group.lower()) not in node_groups
    ]
    return kept, len(resources) - len(kept)


def attach_known_disks(resources: list[Resource]) -> None:
    """Live VMs say ``FromImage`` for disks that are also listed as ``disks`` resources: price those only once."""
    ids = {r.id.lower() for r in resources if r.type.lower() == "microsoft.compute/disks"}
    for vm in resources:
        if vm.type.lower() != "microsoft.compute/virtualmachines":
            continue
        storage = vm.get("properties.storageProfile")
        if not isinstance(storage, dict):
            continue
        os_disk = storage.get("osDisk")
        data_disks = storage.get("dataDisks")
        disks = [os_disk] if isinstance(os_disk, dict) else []
        disks += [d for d in data_disks if isinstance(d, dict)] if isinstance(data_disks, list) else []
        for disk in disks:
            managed = disk.get("managedDisk")
            if isinstance(managed, dict) and str(managed.get("id", "")).lower() in ids:
                disk["createOption"] = "Attach"


def ambiguous_ids(resources: list[Resource]) -> set[str]:
    """Ids of resources whose type and name also exist in another group: usage tables are keyed by name only."""
    seen: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in resources:
        seen[(r.type.lower(), r.name.lower())].append(r.id.lower())
    return {rid for ids in seen.values() if len(ids) > 1 for rid in ids}
