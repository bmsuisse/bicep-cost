"""Find Azure cost spikes from the Cost Management Query API (read-only).

One Daily query, grouped by one dimension, over the last `days` days. Per group the mean daily cost of the most
recent complete days is compared with the median of the days before. Today is excluded: cost data lags by hours and
is always partial.
"""

import json
import statistics
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from bicep_cost.collect.arm import ARM, AzureError, Transport, az_current_subscription

API = "2023-11-01"
MAX_DAILY_DAYS = 31  # Daily granularity ceiling of the Query API
RETRY_WAITS = (15.0, 30.0, 60.0)  # the Query API allows 4 requests/minute per scope: space the retries, max 3
PAGE_PAUSE = 5.0
NO_GROUP = "(none)"
EPSILON = 1e-9  # float slack so that e.g. 10.2 - 5.2 still meets a minimum delta of 5
CHART_DAYS = 7  # days shown before the recent window in each spike's daily series


@dataclass(frozen=True)
class CostRow:
    group: str
    day: date
    cost: float


@dataclass(frozen=True)
class Spike:
    group: str
    baseline_per_day: float
    recent_per_day: float
    delta_per_day: float
    ratio: float | None  # None when the baseline median is zero
    new: bool  # no cost at all in the baseline days
    peak_day: date
    daily: dict[date, float]


@dataclass(frozen=True)
class SpikeReport:
    scope: str
    currency: str
    cost_type: str
    group_by: str
    start: date
    end: date
    recent_days: int
    spikes: list[Spike]
    total_per_day: dict[date, float]


def validate_window(days: int, recent: int) -> None:
    if not 2 <= days <= MAX_DAILY_DAYS or not 1 <= recent < days:
        raise ValueError(f"--days must be 2..{MAX_DAILY_DAYS} and --recent at least 1 and smaller than --days")


def resolve_scope(scope: str | None, subscription_id: str | None, resource_group: str | None) -> str:
    if scope:
        return scope.rstrip("/")
    base = f"/subscriptions/{subscription_id or az_current_subscription()}"
    return f"{base}/resourceGroups/{resource_group}" if resource_group else base


def build_query_body(start: date, end: date, group_by: str, service: str | None, cost_type: str) -> dict[str, object]:
    dataset: dict[str, object] = {
        "granularity": "Daily",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        "grouping": [{"type": "Dimension", "name": group_by}],
    }
    if service:
        dataset["filter"] = {"dimensions": {"name": "ServiceName", "operator": "In", "values": [service]}}
    return {
        "type": cost_type,
        "timeframe": "Custom",
        "timePeriod": {"from": start.isoformat(), "to": end.isoformat()},
        "dataset": dataset,
    }


def parse_usage_date(value: object) -> date:
    """UsageDate comes back as the integer 20261031 (or an ISO string, depending on the API version)."""
    text = str(value)
    digits = text[:8] if len(text) >= 8 and text[:8].isdigit() else ""
    return date.fromisoformat(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}") if digits else date.fromisoformat(text[:10])


def _parse_page(payload: dict[str, object] | None, group_by: str) -> tuple[list[CostRow], str, str | None]:
    props = payload.get("properties") if payload else None
    if not isinstance(props, dict):
        raise AzureError("Cost Management query returned no result (does the scope exist?)")
    names = [str(c.get("name")) for c in props.get("columns", []) if isinstance(c, dict)]
    missing = {"Cost", "UsageDate", group_by} - set(names)
    if missing:
        raise AzureError(f"Cost Management query result lacks column(s) {', '.join(sorted(missing))}")
    i_cost, i_day, i_group = names.index("Cost"), names.index("UsageDate"), names.index(group_by)
    i_cur = names.index("Currency") if "Currency" in names else None
    rows: list[CostRow] = []
    currency = ""
    for row in props.get("rows", []):
        if not isinstance(row, list):
            continue
        rows.append(CostRow(str(row[i_group] or NO_GROUP), parse_usage_date(row[i_day]), float(row[i_cost])))
        if i_cur is not None and row[i_cur]:
            currency = str(row[i_cur])
    next_link = props.get("nextLink")
    return rows, currency, next_link if isinstance(next_link, str) and next_link else None


def fetch_daily_costs(
    transport: Transport,
    scope: str,
    start: date,
    end: date,
    group_by: str,
    service: str | None,
    cost_type: str,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[CostRow], str]:
    """One Query API call (plus nextLink pages): daily cost per group and the billing currency."""
    body = build_query_body(start, end, group_by, service, cost_type)
    url: str | None = f"{ARM}{scope}/providers/Microsoft.CostManagement/query?api-version={API}"
    rows: list[CostRow] = []
    currency = ""
    while url:
        page_rows, page_currency, url = _parse_page(transport("POST", url, body), group_by)
        rows += page_rows
        currency = page_currency or currency
        if url:
            sleep(PAGE_PAUSE)
    return rows, currency


def find_spikes(
    rows: list[CostRow], start: date, last_day: date, recent: int, min_delta: float, ratio: float
) -> list[Spike]:
    """Groups whose last `recent` days cost clearly more per day than their median before, biggest increase first."""
    per_group: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        if start <= row.day <= last_day:
            per_group[row.group][row.day] += row.cost

    days = [start + timedelta(n) for n in range((last_day - start).days + 1)]
    recent_days, base_days = days[-recent:], days[:-recent]
    spikes: list[Spike] = []
    for group, costs in per_group.items():
        base = [costs.get(d, 0.0) for d in base_days]
        baseline = statistics.median(base) if base else 0.0
        now = statistics.fmean(costs.get(d, 0.0) for d in recent_days)
        delta = now - baseline
        is_new = all(c == 0 for c in base)
        if delta > 0 and delta >= min_delta - EPSILON and (is_new or now >= ratio * baseline):
            spikes.append(
                Spike(
                    group=group,
                    baseline_per_day=round(baseline, 2),
                    recent_per_day=round(now, 2),
                    delta_per_day=round(delta, 2),
                    ratio=round(now / baseline, 1) if baseline else None,
                    new=is_new,
                    peak_day=max(recent_days, key=lambda d: costs.get(d, 0.0)),
                    daily={d: round(costs.get(d, 0.0), 2) for d in days[-(len(recent_days) + CHART_DAYS) :]},
                )
            )
    return sorted(spikes, key=lambda s: s.delta_per_day, reverse=True)


def detect_spikes(
    transport: Transport,
    *,
    scope: str,
    today: date,
    group_by: str = "ServiceName",
    service: str | None = None,
    days: int = MAX_DAILY_DAYS,
    recent: int = 3,
    min_delta: float = 5.0,
    ratio: float = 1.5,
    cost_type: str = "ActualCost",
    sleep: Callable[[float], None] = time.sleep,
) -> SpikeReport:
    validate_window(days, recent)
    last_day = today - timedelta(days=1)
    start = last_day - timedelta(days=days - 1)
    rows, currency = fetch_daily_costs(transport, scope, start, today, group_by, service, cost_type, sleep)
    total: dict[date, float] = defaultdict(float)
    for row in rows:
        if start <= row.day <= last_day:
            total[row.day] += row.cost
    return SpikeReport(
        scope=scope,
        currency=currency,
        cost_type=cost_type,
        group_by=group_by,
        start=start,
        end=last_day,
        recent_days=recent,
        spikes=find_spikes(rows, start, last_day, recent, min_delta, ratio),
        total_per_day={d: round(total[d], 2) for d in sorted(total)},
    )


def render_text(report: SpikeReport) -> str:
    lines = [
        f"scope {report.scope}  {report.start} .. {report.end} ({report.cost_type}, {report.currency}), "
        f"last {report.recent_days} days vs median of the rest"
    ]
    if not report.spikes:
        lines.append("no spikes above thresholds")
        return "\n".join(lines) + "\n"
    width = max(len("group"), *(len(s.group) for s in report.spikes))
    lines.append(f"{'group':{width}} {'baseline/d':>11} {'recent/d':>10} {'delta/d':>9} {'x':>6}  peak")
    for s in report.spikes:
        mult = "new" if s.new else "n/a" if s.ratio is None else f"{s.ratio}x"
        lines.append(
            f"{s.group:{width}} {s.baseline_per_day:>11} {s.recent_per_day:>10} {s.delta_per_day:>9} {mult:>6}  "
            f"{s.peak_day.isoformat()}"
        )
    return "\n".join(lines) + "\n"


def render_json(report: SpikeReport) -> str:
    payload = {
        "scope": report.scope,
        "currency": report.currency,
        "from": report.start.isoformat(),
        "to": report.end.isoformat(),
        "recent_days": report.recent_days,
        "spikes": [
            {
                report.group_by: s.group,
                "baseline_per_day": s.baseline_per_day,
                "recent_per_day": s.recent_per_day,
                "delta_per_day": s.delta_per_day,
                "ratio": s.ratio,
                "new": s.new,
                "peak_day": s.peak_day.isoformat(),
                "daily": {d.isoformat(): v for d, v in s.daily.items()},
            }
            for s in report.spikes
        ],
        "total_per_day": {d.isoformat(): v for d, v in report.total_per_day.items()},
    }
    return json.dumps(payload, indent=2) + "\n"
