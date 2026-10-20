"""Minimal read-only Azure Resource Manager / Azure Monitor client (bearer token from the Azure CLI)."""

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypedDict

ARM = "https://management.azure.com"
METRICS_API = "2023-10-01"


class AzureError(Exception):
    pass


class Transport(Protocol):
    def __call__(self, method: str, url: str, body: dict[str, object] | None) -> dict[str, object] | None:
        """Return the JSON response, or None for 404."""
        ...


def az_access_token() -> str:
    try:
        proc = subprocess.run(
            ["az", "account", "get-access-token", "--resource", ARM, "--query", "accessToken", "-o", "tsv"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AzureError("Azure CLI (`az`) not found; install it and run `az login`") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AzureError("could not get an Azure token; run `az login` first:\n" + proc.stderr.strip()[:300])
    return proc.stdout.strip()


def az_current_subscription() -> str:
    proc = subprocess.run(
        ["az", "account", "show", "--query", "id", "-o", "tsv"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise AzureError("no current Azure subscription; run `az login` or pass --subscription-id")
    return proc.stdout.strip()


def http_transport(token_provider: Callable[[], str] = az_access_token) -> Transport:
    token = token_provider()

    def call(method: str, url: str, body: dict[str, object] | None) -> dict[str, object] | None:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "bicep-cost/0.1",
        }
        last: Exception | None = None
        for attempt in range(6):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                last = exc
                if exc.code not in (429, 500, 502, 503, 504):
                    detail = exc.read().decode(errors="replace")[:300]
                    raise AzureError(f"{method} {url.split('?')[0]} -> HTTP {exc.code}: {detail}") from exc
                retry_after = exc.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt)
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                time.sleep(2**attempt)
        raise AzureError(f"{method} {url.split('?')[0]} failed: {last}")

    return call


@dataclass(frozen=True)
class Series:
    dimensions: dict[str, str]
    values: list[float | None]

    def total(self) -> float:
        return sum(v for v in self.values if v is not None)

    def latest(self) -> float | None:
        for v in reversed(self.values):
            if v is not None:
                return v
        return None


class _MetricData(TypedDict, total=False):
    average: float
    total: float
    maximum: float
    minimum: float
    count: float


@dataclass
class ArmClient:
    transport: Transport
    days: int = 30
    now: datetime | None = None

    @property
    def end(self) -> datetime:
        return (self.now or datetime.now(UTC)).replace(second=0, microsecond=0)

    @property
    def start(self) -> datetime:
        return self.end - timedelta(days=self.days)

    def get(self, resource_id: str, api_version: str) -> dict[str, object] | None:
        return self.transport("GET", f"{ARM}{resource_id}?api-version={api_version}", None)

    def post(self, path: str, api_version: str, body: dict[str, object]) -> dict[str, object] | None:
        return self.transport("POST", f"{ARM}{path}?api-version={api_version}", body)

    def metrics(
        self,
        resource_id: str,
        name: str,
        aggregation: str,
        interval: str,
        odata_filter: str | None = None,
        namespace: str | None = None,
    ) -> list[Series]:
        """Time series of one metric over the window; empty if the resource or metric does not exist."""
        timespan = f"{self.start:%Y-%m-%dT%H:%MZ}/{self.end:%Y-%m-%dT%H:%MZ}"
        params = {
            "api-version": METRICS_API,
            "metricnames": name,
            "aggregation": aggregation,
            "timespan": timespan,
            "interval": interval,
        }
        if odata_filter:
            params["$filter"] = odata_filter
        if namespace:
            params["metricnamespace"] = namespace
        url = f"{ARM}{resource_id}/providers/microsoft.insights/metrics?" + urllib.parse.urlencode(
            params, quote_via=urllib.parse.quote
        )
        payload = self.transport("GET", url, None)
        return parse_series(payload, aggregation)

    def query_workspace(self, workspace_id: str, query: str) -> list[list[object]]:
        """Rows of the first result table of a Log Analytics query over the window."""
        body: dict[str, object] = {"query": query, "timespan": f"P{self.days}D"}
        payload = self.post(f"{workspace_id}/api/query", "2017-01-01-preview", body)
        tables = payload.get("Tables") if payload else None
        if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
            return []
        rows = tables[0].get("Rows")
        return [r for r in rows if isinstance(r, list)] if isinstance(rows, list) else []

    def costs_by_resource(self, subscription_id: str, resource_group: str) -> dict[str, tuple[float, str]]:
        """Actual cost of the window per lower-cased resource id, with the billing currency."""
        body: dict[str, object] = {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": {"from": f"{self.start:%Y-%m-%dT00:00:00Z}", "to": f"{self.end:%Y-%m-%dT00:00:00Z}"},
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [{"type": "Dimension", "name": "ResourceId"}],
            },
        }
        path = (
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.CostManagement/query"
        )
        payload = self.post(path, "2023-11-01", body)
        props = payload.get("properties") if payload else None
        if not isinstance(props, dict):
            return {}
        columns = [c.get("name") for c in props.get("columns", []) if isinstance(c, dict)]
        out: dict[str, tuple[float, str]] = {}
        try:
            i_cost, i_id, i_cur = columns.index("Cost"), columns.index("ResourceId"), columns.index("Currency")
        except ValueError:
            return {}
        for row in props.get("rows", []):
            out[str(row[i_id]).lower()] = (float(row[i_cost]), str(row[i_cur]))
        return out


def parse_series(payload: dict[str, object] | None, aggregation: str) -> list[Series]:
    if not payload:
        return []
    values = payload.get("value")
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        return []
    result: list[Series] = []
    key = aggregation.lower()
    for ts in values[0].get("timeseries", []):
        if not isinstance(ts, dict):
            continue
        dims = {
            str(m["name"]["value"]).lower(): str(m["value"])
            for m in ts.get("metadatavalues", [])
            if isinstance(m, dict) and isinstance(m.get("name"), dict)
        }
        data: list[_MetricData] = [d for d in ts.get("data", []) if isinstance(d, dict)]
        result.append(Series(dims, [d.get(key) for d in data]))  # type: ignore[misc]
    return result
