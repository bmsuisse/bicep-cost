"""Per-resource collectors: read metrics / queries from Azure and turn them into usage-file values."""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..context import HOURS_PER_MONTH, ResourceIndex
from ..engine import Resource
from ..estimators.appservice import parse_plan_sku
from .arm import ArmClient, AzureError, Series

log = logging.getLogger(__name__)

GIB = 2**30
GB = 1e9
IDLE_NANOCORES = 10_000_000  # Container Apps bills idle rates below 0.01 vCPU ...
IDLE_BYTES_PER_SECOND = 1000  # ... and below 1 kB/s of network traffic


@dataclass(frozen=True)
class Finding:
    section: str
    name: str
    key: str
    value: float
    source: str


@dataclass
class Collected:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    drift: list[str] = field(default_factory=list)


@dataclass
class Collection:
    arm: ArmClient
    index: ResourceIndex
    costs: dict[str, tuple[float, str]]

    @property
    def month_factor(self) -> float:
        """Scale a total over the window to a month."""
        return HOURS_PER_MONTH / 24 / self.arm.days

    def actual_monthly_cost(self, res: Resource) -> tuple[float, str] | None:
        found = self.costs.get(res.id.lower())
        if found is None:
            return None
        return found[0] * self.month_factor, found[1]


Collector = Callable[[Resource, Collection], Collected]


def _total(series: list[Series]) -> float | None:
    return sum(s.total() for s in series) if series else None


def classify_operation(api: str) -> str:
    """Bucket a storage ``ApiName`` into the read / write / other price categories."""
    low = api.lower()
    if low in ("getblob", "readfile", "read", "getfile"):
        return "read"
    if low.startswith(("put", "append", "copy", "create", "write", "upload", "flush", "snapshot", "list", "commit")):
        return "write"
    return "other"


def _operations_per_month(series: list[Series], factor: float) -> dict[str, float]:
    buckets = {"read": 0.0, "write": 0.0, "other": 0.0}
    for s in series:
        buckets[classify_operation(s.dimensions.get("apiname", ""))] += s.total() * factor
    return buckets


def collect_storage_account(res: Resource, c: Collection) -> Collected:
    out = Collected()
    blob = f"{res.id}/blobServices/default"
    capacity = c.arm.metrics(blob, "BlobCapacity", "Average", "P1D")
    latest = capacity[0].latest() if capacity else None
    if latest is not None:
        out.findings.append(
            Finding("storage", res.name, "stored_gb", latest / GIB, "BlobCapacity (latest daily average)")
        )
    tx = c.arm.metrics(blob, "Transactions", "Total", "P1D", "ApiName eq '*'")
    if tx:
        ops = _operations_per_month(tx, c.month_factor)
        for bucket in ("write", "read", "other"):
            out.findings.append(
                Finding(
                    "storage",
                    res.name,
                    f"{bucket}_operations_10k",
                    ops[bucket] / 10_000,
                    f"Transactions by ApiName, {c.arm.days} d",
                )
            )
    if not out.findings:
        out.notes.append("no blob service metrics found")
    return out


def collect_file_share(res: Resource, c: Collection) -> Collected:
    out = Collected()
    share = res.name.split("/")[-1]
    account_name = res.name.split("/")[0]
    service = f"{res.id.split('/fileServices/')[0]}/fileServices/default"
    siblings = [
        r
        for r in c.index.of_type("Microsoft.Storage/storageAccounts/fileServices/shares")
        if r.name.split("/")[0] == account_name and r.id.split("/fileServices/")[0] == res.id.split("/fileServices/")[0]
    ]
    flt = f"FileShare eq '{share}'"
    capacity = c.arm.metrics(service, "FileCapacity", "Average", "P1D", flt)
    tx = c.arm.metrics(service, "Transactions", "Total", "P1D", f"{flt} and ApiName eq '*'")
    divisor = 1
    if not capacity:
        # Standard accounts expose no per-share dimension: use the account total, split evenly over its shares.
        capacity = c.arm.metrics(service, "FileCapacity", "Average", "P1D")
        tx = c.arm.metrics(service, "Transactions", "Total", "P1D", "ApiName eq '*'")
        divisor = max(len(siblings), 1)
        if divisor > 1:
            out.notes.append(f"account-level metrics split evenly across its {divisor} shares")
    latest = capacity[0].latest() if capacity else None
    if latest is not None:
        out.findings.append(
            Finding("fileshares", res.name, "used_gb", latest / GIB / divisor, "FileCapacity (latest daily average)")
        )
    if tx:
        ops = _operations_per_month(tx, c.month_factor)
        for bucket in ("write", "read", "other"):
            out.findings.append(
                Finding(
                    "fileshares",
                    res.name,
                    f"{bucket}_operations_10k",
                    ops[bucket] / 10_000 / divisor,
                    f"Transactions by ApiName, {c.arm.days} d",
                )
            )
    if not out.findings:
        out.notes.append("no file service metrics found")
    return out


def collect_workspace(res: Resource, c: Collection) -> Collected:
    out = Collected()
    rows = c.arm.query_workspace(res.id, "Usage | where IsBillable == true | summarize GB = sum(Quantity) / 1024")
    if rows and rows[0] and isinstance(rows[0][0], (int, float)):
        out.findings.append(
            Finding(
                "loganalytics",
                res.name,
                "ingest_gb_per_month",
                float(rows[0][0]) * c.month_factor,
                f"Usage table, billable GB over {c.arm.days} d",
            )
        )
        top = c.arm.query_workspace(
            res.id,
            "Usage | where IsBillable == true | summarize GB = sum(Quantity) / 1024 by DataType | top 5 by GB desc",
        )
        total = float(rows[0][0]) or 1.0
        shares = ", ".join(f"{r[0]} {float(r[1]) / total:.0%}" for r in top if isinstance(r[1], (int, float)))
        if shares:
            out.notes.append(f"top billable tables over {c.arm.days} d: {shares}")
    else:
        out.notes.append("Usage table returned no rows (no access to the workspace data, or nothing ingested)")
    return out


def collect_container_app(res: Resource, c: Collection) -> Collected:
    out = Collected()
    replicas = c.arm.metrics(res.id, "Replicas", "Average", "PT1H")
    if not replicas:
        if c.arm.get(res.id, str(res.body.get("apiVersion") or "2024-03-01")) is None:
            out.notes.append("not found in Azure")
            return out
        out.findings.append(
            Finding("containerapps", res.name, "replica_hours", 0.0, "no Replicas data: scaled to zero")
        )
        return out
    hourly = replicas[0].values
    expected = c.arm.days * 24
    present = sum(1 for v in hourly if v is not None)
    if present < expected:
        out.notes.append(
            f"Replicas metric has {present} of {expected} hourly points; missing hours are counted as 0 replicas"
        )
    replica_hours = sum(v for v in hourly if v is not None) / expected * HOURS_PER_MONTH
    out.findings.append(
        Finding("containerapps", res.name, "replica_hours", replica_hours, f"Replicas (hourly average), {c.arm.days} d")
    )
    idle = _idle_percent(res, c, hourly)
    if idle is not None:
        out.findings.append(
            Finding(
                "containerapps", res.name, "idle_percent", idle, "share of 5-min windows below 0.01 vCPU and 1 kB/s"
            )
        )
    requests = c.arm.metrics(res.id, "Requests", "Total", "P1D")
    total = _total(requests)
    if total:
        out.findings.append(
            Finding(
                "containerapps",
                res.name,
                "requests_millions",
                total * c.month_factor / 1e6,
                f"Requests, {c.arm.days} d",
            )
        )
    return out


def _idle_percent(res: Resource, c: Collection, hourly_replicas: list[float | None]) -> float | None:
    """Share of the time replicas ran below the idle thresholds, judged on 5-minute windows."""
    cpu = c.arm.metrics(res.id, "UsageNanoCores", "Average", "PT5M")
    if not cpu:
        return None
    rx = c.arm.metrics(res.id, "RxBytes", "Total", "PT5M")
    tx = c.arm.metrics(res.id, "TxBytes", "Total", "PT5M")
    points = len(cpu[0].values)
    per_hour = max(points // len(hourly_replicas), 1) if hourly_replicas else 12
    running = idle = 0
    for i in range(points):
        replicas_now = hourly_replicas[min(i // per_hour, len(hourly_replicas) - 1)] if hourly_replicas else None
        if not replicas_now:
            continue
        nano = cpu[0].values[i]
        if nano is None:  # no CPU reading: cannot tell idle from busy, leave the window out
            continue
        running += 1
        net = ((rx[0].values[i] if rx and i < len(rx[0].values) else 0.0) or 0.0) + (
            (tx[0].values[i] if tx and i < len(tx[0].values) else 0.0) or 0.0
        )
        if nano < IDLE_NANOCORES and net / 300 < IDLE_BYTES_PER_SECOND:
            idle += 1
    return 100 * idle / running if running else None


def _cost_finding(res: Resource, c: Collection, section: str) -> Finding | None:
    cost = c.actual_monthly_cost(res)
    if cost is None:
        return None
    return Finding(
        section, res.name, "monthly_cost", cost[0], f"Cost Management actual cost ({cost[1]}), {c.arm.days} d"
    )


def collect_session_pool(res: Resource, c: Collection) -> Collected:
    out = Collected()
    finding = _cost_finding(res, c, "sessionpools")
    if finding is None:
        out.notes.append(
            "no cost data (needs Cost Management Reader); the pool metrics do not reflect billed session-hours"
        )
        return out
    out.findings.append(finding)
    return out


def collect_cognitive(res: Resource, c: Collection) -> Collected:
    out = Collected()
    finding = _cost_finding(res, c, "cognitiveservices")
    if finding is not None:
        out.findings.append(finding)
        return out
    estimated = c.arm.metrics(res.id, "FoundryModelEstimatedCost", "Total", "P1D")
    total = _total(estimated)
    if total:
        out.findings.append(
            Finding(
                "cognitiveservices",
                res.name,
                "monthly_cost",
                total * c.month_factor,
                "FoundryModelEstimatedCost metric (USD, estimated)",
            )
        )
    else:
        out.notes.append("no cost data and no estimated-cost metric")
    return out


def collect_key_vault(res: Resource, c: Collection) -> Collected:
    out = Collected()
    hits = c.arm.metrics(res.id, "ServiceApiHit", "Total", "P1D")
    total = _total(hits)
    if total is not None:
        out.findings.append(
            Finding(
                "keyvault",
                res.name,
                "operations_10k",
                total * c.month_factor / 10_000,
                f"ServiceApiHit, {c.arm.days} d",
            )
        )
    return out


def collect_private_endpoint(res: Resource, c: Collection) -> Collected:
    out = Collected()
    for metric, key in (("PEBytesIn", "ingress_gb"), ("PEBytesOut", "egress_gb")):
        total = _total(c.arm.metrics(res.id, metric, "Total", "P1D"))
        if total is not None:
            out.findings.append(
                Finding("privateendpoints", res.name, key, total * c.month_factor / GB, f"{metric}, {c.arm.days} d")
            )
    return out


def collect_private_dns_zone(res: Resource, c: Collection) -> Collected:
    out = Collected()
    total = _total(c.arm.metrics(res.id, "QueryVolume", "Total", "P1D"))
    if total is not None:
        out.findings.append(
            Finding(
                "privatednszones",
                res.name,
                "queries_millions",
                total * c.month_factor / 1e6,
                f"QueryVolume, {c.arm.days} d",
            )
        )
    return out


def collect_service_bus(res: Resource, c: Collection) -> Collected:
    out = Collected()
    parts = [
        _total(c.arm.metrics(res.id, m, "Total", "P1D"))
        for m in ("IncomingMessages", "OutgoingMessages", "CompleteMessage")
    ]
    if any(p is not None for p in parts):
        ops = sum(p for p in parts if p is not None) * c.month_factor
        out.findings.append(
            Finding(
                "servicebus",
                res.name,
                "operations_millions",
                ops / 1e6,
                f"IncomingMessages + OutgoingMessages + CompleteMessage, {c.arm.days} d",
            )
        )
        out.notes.append("operations are approximated from message counts")
    return out


def collect_function_app(res: Resource, c: Collection) -> Collected:
    out = Collected()
    plan = c.index.find(res.get("properties.serverFarmId"))
    if "functionapp" not in (res.kind or "").lower() or plan is None:
        return out
    try:
        if parse_plan_sku(str(plan.get("sku.name"))).kind not in ("consumption", "flex"):
            return out
    except Exception:  # an unrecognised plan SKU simply means: not a consumption function
        return out
    executions = _total(c.arm.metrics(res.id, "FunctionExecutionCount", "Total", "P1D"))
    units = _total(c.arm.metrics(res.id, "FunctionExecutionUnits", "Total", "P1D"))
    if executions is not None:
        out.findings.append(
            Finding(
                "functions",
                res.name,
                "executions_per_month",
                executions * c.month_factor,
                f"FunctionExecutionCount, {c.arm.days} d",
            )
        )
    if units is not None:
        gb_seconds = units / 1024 / 1000 * c.month_factor
        out.findings.append(
            Finding(
                "functions",
                res.name,
                "gb_seconds_per_month",
                gb_seconds,
                f"FunctionExecutionUnits (MB-ms), {c.arm.days} d",
            )
        )
    return out


def collect_plan(res: Resource, c: Collection) -> Collected:
    out = Collected()
    live = c.arm.get(res.id, str(res.body.get("apiVersion") or "2023-12-01"))
    if live is None:
        out.notes.append("not found in Azure")
        return out
    sku = live.get("sku")
    if isinstance(sku, dict):
        capacity = sku.get("capacity")
        if isinstance(capacity, (int, float)):
            out.findings.append(Finding("appserviceplans", res.name, "instances", float(capacity), "live sku.capacity"))
            declared = res.get("sku.capacity")
            if declared is not None and declared != capacity:
                out.drift.append(f"instances: template {declared}, live {capacity}")
        if str(sku.get("name", "")).lower() != str(res.get("sku.name", "")).lower():
            out.drift.append(f"sku: template {res.get('sku.name')}, live {sku.get('name')}")
    return out


def collect_postgres(res: Resource, c: Collection) -> Collected:
    out = Collected()
    live = c.arm.get(res.id, str(res.body.get("apiVersion") or "2024-08-01"))
    if live is None:
        out.notes.append("not found in Azure")
        return out
    props = live.get("properties")
    storage = props.get("storage") if isinstance(props, dict) else None
    size = storage.get("storageSizeGB") if isinstance(storage, dict) else None
    if isinstance(size, (int, float)):
        out.findings.append(
            Finding(
                "postgres",
                res.name,
                "storage_gb",
                float(size),
                "live storageSizeGB (auto-grow can exceed the template)",
            )
        )
        declared = res.get("properties.storage.storageSizeGB")
        if declared is not None and declared != size:
            out.drift.append(f"storage: template {declared} GB, live {size} GB")
    live_sku = live.get("sku")
    if isinstance(live_sku, dict) and str(live_sku.get("name", "")).lower() != str(res.get("sku.name", "")).lower():
        out.drift.append(f"sku: template {res.get('sku.name')}, live {live_sku.get('name')}")
    backup = c.arm.metrics(res.id, "backup_storage_used", "Average", "P1D")
    latest = backup[0].latest() if backup else None
    if latest is not None:
        out.findings.append(
            Finding(
                "postgres", res.name, "backup_storage_gb", latest / GIB, "backup_storage_used (latest daily average)"
            )
        )
    return out


COLLECTORS: dict[str, Collector] = {
    "microsoft.storage/storageaccounts": collect_storage_account,
    "microsoft.storage/storageaccounts/fileservices/shares": collect_file_share,
    "microsoft.operationalinsights/workspaces": collect_workspace,
    "microsoft.app/containerapps": collect_container_app,
    "microsoft.app/sessionpools": collect_session_pool,
    "microsoft.cognitiveservices/accounts": collect_cognitive,
    "microsoft.keyvault/vaults": collect_key_vault,
    "microsoft.network/privateendpoints": collect_private_endpoint,
    "microsoft.network/privatednszones": collect_private_dns_zone,
    "microsoft.servicebus/namespaces": collect_service_bus,
    "microsoft.web/sites": collect_function_app,
    "microsoft.web/serverfarms": collect_plan,
    "microsoft.dbforpostgresql/flexibleservers": collect_postgres,
}


@dataclass
class CollectionResult:
    findings: list[Finding]
    notes: dict[tuple[str, str], list[str]]
    warnings: list[str]
    cost_currency: str | None


SECTION_OF_TYPE = {
    "microsoft.storage/storageaccounts": "storage",
    "microsoft.storage/storageaccounts/fileservices/shares": "fileshares",
    "microsoft.operationalinsights/workspaces": "loganalytics",
    "microsoft.app/containerapps": "containerapps",
    "microsoft.app/sessionpools": "sessionpools",
    "microsoft.cognitiveservices/accounts": "cognitiveservices",
    "microsoft.keyvault/vaults": "keyvault",
    "microsoft.network/privateendpoints": "privateendpoints",
    "microsoft.network/privatednszones": "privatednszones",
    "microsoft.servicebus/namespaces": "servicebus",
    "microsoft.web/sites": "functions",
    "microsoft.web/serverfarms": "appserviceplans",
    "microsoft.dbforpostgresql/flexibleservers": "postgres",
}


def collect_all(resources: list[Resource], arm: ArmClient, workers: int = 8) -> CollectionResult:
    index = ResourceIndex(resources)
    costs: dict[str, tuple[float, str]] = {}
    warnings: list[str] = []
    for sub, rg in sorted({(r.subscription_id, r.resource_group) for r in resources}):
        try:
            costs.update(arm.costs_by_resource(sub, rg))
        except AzureError as exc:
            warnings.append(f"cost data for {rg} unavailable: {exc}")
    collection = Collection(arm, index, costs)
    targets = [r for r in resources if r.type.lower() in COLLECTORS]

    def run(res: Resource) -> tuple[Resource, Collected | None, str | None]:
        try:
            return res, COLLECTORS[res.type.lower()](res, collection), None
        except AzureError as exc:
            return res, None, str(exc)

    findings: list[Finding] = []
    notes: dict[tuple[str, str], list[str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for res, result, error in pool.map(run, targets):
            section = SECTION_OF_TYPE[res.type.lower()]
            if error:
                warnings.append(f"{res.type} {res.name}: {error}")
                continue
            assert result is not None
            findings.extend(result.findings)
            extra = list(result.notes) + [
                f"DRIFT {d} (the estimate follows the template unless overridden)" for d in result.drift
            ]
            cost = collection.actual_monthly_cost(res)
            if cost is not None and result.findings:
                extra.append(f"actual cost, scaled to a month: {cost[0]:,.2f} {cost[1]}")
            if extra and (result.findings or result.drift):
                notes[(section, res.name)] = extra
            elif extra:
                warnings.append(f"{res.type} {res.name}: " + "; ".join(extra))
    findings.extend(_zero_app_insights(resources, findings))
    currency = next(iter({cur for _, cur in costs.values()}), None) if costs else None
    return CollectionResult(findings, notes, warnings, currency)


def _zero_app_insights(resources: list[Resource], findings: list[Finding]) -> list[Finding]:
    """Workspace-based App Insights data is already inside the workspace's measured ingestion."""
    measured = {f.name for f in findings if f.section == "loganalytics"}
    index = ResourceIndex(resources)
    out: list[Finding] = []
    for res in resources:
        if res.type.lower() != "microsoft.insights/components":
            continue
        ws = index.find(res.get("properties.WorkspaceResourceId"))
        if ws is not None and ws.name in measured:
            out.append(
                Finding("appinsights", res.name, "ingest_gb_per_month", 0.0, f"already counted in workspace {ws.name}")
            )
    return out
