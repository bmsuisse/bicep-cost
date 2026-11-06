"""Collect the live data for `bicep-cost rightsize` and run the verdicts (read-only)."""

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from bicep_cost.advisor import fetch_recommendations
from bicep_cost.collect.arm import ArmClient, AzureError
from bicep_cost.context import ResourceIndex
from bicep_cost.engine import Resource
from bicep_cost.estimators import FREE_PREFIXES, FREE_TYPES
from bicep_cost.prices import PriceClient
from bicep_cost.rightsize import Assessment, decide
from bicep_cost.rightsize_types import HANDLERS, Env, Handler, Metrics
from bicep_cost.scan import fetch_inventory, query_graph, to_resources

log = logging.getLogger(__name__)

AGGREGATIONS = ("Average", "Maximum", "Minimum")
AUTOSCALE_QUERY = (
    "resources | where type =~ 'microsoft.insights/autoscalesettings' "
    "| project target = tolower(tostring(properties.targetResourceUri)), enabled = tobool(properties.enabled)"
)


@dataclass(frozen=True)
class Skipped:
    name: str
    type: str
    resource_group: str
    reason: str


@dataclass
class Report:
    subscription_id: str
    resource_groups: tuple[str, ...]
    window_days: int
    end: datetime
    currency: str
    price_source: str
    assessments: list[Assessment] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    not_covered: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def fetch_metrics(arm: ArmClient, res: Resource, names: list[str]) -> Metrics:
    """All metrics in one call; when Azure rejects the set (a name the SKU lacks), one call per metric."""
    try:
        return arm.metrics_many(res.id, names, AGGREGATIONS, "PT1H")
    except AzureError as exc:
        if "HTTP 400" not in str(exc):
            raise
    out: Metrics = {}
    for name in names:
        try:
            out.update(arm.metrics_many(res.id, [name], AGGREGATIONS, "PT1H"))
        except AzureError as exc:
            if "HTTP 400" not in str(exc):
                raise
    return out


def _unavailable(res: Resource, reason: str, days: int) -> Assessment:
    return Assessment(res.id, res.name, res.type, res.resource_group, "", "no-data", reason, days, [], [], [])


def _is_free(type_: str) -> bool:
    low = type_.lower()
    return low in FREE_TYPES or low.startswith(tuple(FREE_PREFIXES))


def analyse(
    arm: ArmClient, prices: PriceClient, subscription_id: str, resource_groups: tuple[str, ...], workers: int = 4
) -> Report:
    resources = to_resources(fetch_inventory(arm, subscription_id, resource_groups))
    report = Report(subscription_id, resource_groups, arm.days, arm.end, prices.currency, "", [], [], {}, [])
    autoscaled = frozenset(
        str(r["target"]) for r in query_graph(arm, subscription_id, AUTOSCALE_QUERY) if r.get("enabled") is True
    )
    try:
        advisor = fetch_recommendations(arm.transport, subscription_id)
    except AzureError as exc:
        advisor = {}
        report.warnings.append(f"Azure Advisor is not available, so there is no second opinion: {exc}")
    env = Env(arm.days, prices, "westeurope", advisor, autoscaled, ResourceIndex(resources))

    targets: list[tuple[Resource, Handler]] = []
    uncovered: Counter[str] = Counter()
    for res in resources:
        handler = HANDLERS.get(res.type.lower())
        if handler is None:
            if not _is_free(res.type):
                uncovered[res.type.lower()] += 1
            continue
        reason = handler.skip(res)
        if reason:
            report.skipped.append(Skipped(res.name, res.type, res.resource_group, reason))
        else:
            targets.append((res, handler))
    report.not_covered = dict(uncovered.most_common())

    def load(item: tuple[Resource, Handler]) -> tuple[Resource, Handler, Metrics | str]:
        res, handler = item
        try:
            return res, handler, fetch_metrics(arm, res, handler.names(res))
        except AzureError as exc:
            return res, handler, str(exc)

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        loaded = list(pool.map(load, targets))
    for res, handler, data in loaded:
        if isinstance(data, str):
            report.assessments.append(_unavailable(res, f"metrics could not be read: {data}", arm.days))
            continue
        report.assessments.append(decide(handler.case(res, env, data)))
    return report
