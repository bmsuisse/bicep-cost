"""Azure Advisor recommendations (Cost and Performance), read-only, matched to resources by resource id."""

import urllib.parse
from dataclasses import dataclass

from bicep_cost.collect.arm import ARM, AzureError, Transport

API = "2025-01-01"
CATEGORIES = ("Cost", "Performance")
MAX_PAGES = 50


@dataclass(frozen=True)
class AdvisorRec:
    category: str
    impact: str
    problem: str


def _rec(item: object) -> tuple[str, AdvisorRec] | None:
    props = item.get("properties") if isinstance(item, dict) else None
    if not isinstance(props, dict):
        return None
    meta = props.get("resourceMetadata")
    rid = meta.get("resourceId") if isinstance(meta, dict) else None
    short = props.get("shortDescription")
    problem = short.get("problem") if isinstance(short, dict) else None
    category = props.get("category")
    if not isinstance(rid, str) or not isinstance(category, str) or category not in CATEGORIES:
        return None
    return rid.lower(), AdvisorRec(category, str(props.get("impact", "")), str(problem or "").strip())


def fetch_recommendations(transport: Transport, subscription_id: str) -> dict[str, list[AdvisorRec]]:
    """Cost and Performance recommendations of the subscription, keyed by lower-cased resource id."""
    flt = " or ".join(f"Category eq '{c}'" for c in CATEGORIES)
    url: str | None = (
        f"{ARM}/subscriptions/{subscription_id}/providers/Microsoft.Advisor/recommendations"
        f"?api-version={API}&$filter={urllib.parse.quote(flt)}"
    )
    out: dict[str, list[AdvisorRec]] = {}
    for _ in range(MAX_PAGES):
        if url is None:
            return out
        payload = transport("GET", url, None)
        if payload is None:
            raise AzureError("Advisor returned no result")
        items = payload.get("value")
        for item in items if isinstance(items, list) else []:
            found = _rec(item)
            if found is not None:
                rec = out.setdefault(found[0], [])
                if found[1] not in rec:
                    rec.append(found[1])
        next_link = payload.get("nextLink")
        url = next_link if isinstance(next_link, str) and next_link else None
    raise AzureError("too many Advisor result pages")
