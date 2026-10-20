"""Log Analytics, Application Insights, scheduled query (log) alert rules."""

import re

from ..context import EstimateContext, Unresolved, as_number, need_str
from ..engine import Resource
from ..model import LineItem, ResourceEstimate
from .common import free, included, line, priced

WORKSPACE = "Microsoft.OperationalInsights/workspaces"


def _workspace_scope(workspace_id: str) -> str:
    return f"workspace:{workspace_id.lower()}"


def estimate_workspace(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    region = ctx.region(res)
    sku = str(res.get("properties.sku.name") or "PerGB2018")
    retention = as_number(res.get("properties.retentionInDays"), 30)
    ingest_gb = ctx.usage.number("loganalytics", res.name, "ingest_gb_per_month", 2.0)
    scope = _workspace_scope(res.id)
    items: list[LineItem] = []
    notes: list[str] = []
    if sku.lower() == "pergb2018":
        ingestion = ctx.meter("Log Analytics", region, meter="Analytics Logs Data Ingestion")
        items.append(
            line(
                f"Ingestion {ingest_gb:g} GB (pay-as-you-go, first 5 GB free)",
                ingestion,
                ingest_gb,
                "usage",
                scope=scope,
                note="assumed volume; Application Insights components using this workspace add to it",
            )
        )
        total_ingest = ingest_gb
    elif sku.lower() == "capacityreservation":
        level = int(as_number(res.get("properties.sku.capacityReservationLevel"), 100))
        meter = ctx.meter("Azure Monitor", region, meter_contains=f"{level} GB Commitment Tier Capacity Reservation")
        items.append(line(f"Commitment tier {level} GB/day", meter, 30.4, "fixed"))
        total_ingest = ingest_gb
    elif sku.lower() == "free":
        return free(res, "Free tier (500 MB/day cap, 7 days retention)")
    else:
        raise Unresolved(f"Log Analytics SKU {sku!r} is not priced")
    if retention > 31:
        retention_m = ctx.meter("Log Analytics", region, meter="Analytics Logs Data Retention")
        extra_gb_months = total_ingest * (retention - 31) / 30
        items.append(
            line(
                f"Retention beyond 31 days ({retention:g} d)",
                retention_m,
                extra_gb_months,
                "usage",
                note="assumed volume",
            )
        )
    cap = res.get("properties.workspaceCapping.dailyQuotaGb")
    if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0:
        notes.append(f"daily cap {cap:g} GB (~{cap * 30.4:,.0f} GB/month upper bound)")
    return priced(res, items, notes)


def estimate_app_insights(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    region = ctx.region(res)
    ws_id = res.get("properties.WorkspaceResourceId")
    ingest_gb = ctx.usage.number("appinsights", res.name, "ingest_gb_per_month", 1.0)
    meter = ctx.meter("Log Analytics", region, meter="Analytics Logs Data Ingestion")
    if isinstance(ws_id, str):
        scope = _workspace_scope(ws_id)
        target = ctx.index.find(ws_id)
        where = f"workspace {target.name}" if target else "workspace outside this template"
    else:
        scope = _workspace_scope(res.id)
        where = "classic (no workspace)"
    return priced(
        res,
        [
            line(
                f"Telemetry ingestion {ingest_gb:g} GB into {where}",
                meter,
                ingest_gb,
                "usage",
                scope=scope,
                note="assumed volume",
            )
        ],
    )


def _minutes(iso: str) -> int:
    m = re.fullmatch(r"P(?:T(?:(\d+)H)?(?:(\d+)M)?)?", iso.upper())
    if not m or iso.upper() == "PT":
        raise Unresolved(f"cannot read frequency {iso!r}")
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def estimate_log_alert(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    if res.get("properties.enabled") is False:
        return free(res, "disabled rules are not billed")
    region = ctx.region(res)
    freq = need_str(res, "properties.evaluationFrequency", "evaluationFrequency")
    minutes = _minutes(freq)
    bucket = 1 if minutes <= 1 else 5 if minutes <= 5 else 10 if minutes <= 10 else 15
    meter = ctx.meter("Azure Monitor", region, meter=f"Alerts System Log Monitored at {bucket} Minute Frequency")
    items = [line(f"Log search alert, every {minutes} min (billed as {bucket}-min)", meter, 1.0)]
    scopes = res.get("properties.scopes", [])
    extra = (len(scopes) - 1) if isinstance(scopes, list) else 0
    if extra > 0:
        per_resource = ctx.meter(
            "Azure Monitor", region, meter=f"Alerts Resource Monitored at {bucket} Minute Frequency"
        )
        items.append(line(f"{extra} additional monitored resource(s)", per_resource, float(extra)))
    return priced(res, items)


def estimate_diagnostic_setting(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    return included(
        res, "free itself; ingested logs are billed on the destination workspace (see loganalytics ingest_gb_per_month)"
    )
