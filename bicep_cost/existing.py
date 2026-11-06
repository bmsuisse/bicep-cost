"""Resources that exist in Azure but are not deployed by the template (``existing`` in Bicep).

Templates that only *reference* platform-owned resources (App Service plan, database, Log Analytics, ...) would
otherwise be estimated at ~0. ``--existing FILE.json`` describes them with the same fields an ARM resource has, so
every estimator and usage table treats them like deployed resources::

    {"resources": [
      {"type": "Microsoft.Web/serverfarms", "name": "my-plan", "location": "westeurope",
       "sku": {"name": "P1v3", "capacity": 2}, "kind": "linux", "properties": {"reserved": true}},
      {"type": "Microsoft.DBforPostgreSQL/flexibleServers", "name": "my-pg", "resourceGroup": "rg-other", ...}
    ]}

A bare JSON list works too. ``resourceGroup`` / ``subscriptionId`` default to the deployment's, which is what makes a
template's ``plan.id`` match the entry.
"""

import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from bicep_cost.armexpr import JsonValue, build_resource_id
from bicep_cost.engine import Resource

MODULE_PATH = ("existing",)


class ExistingError(ValueError):
    """The --existing file is not valid."""


def load_existing(paths: Sequence[Path], subscription_id: str, resource_group: str) -> list[Resource]:
    """Read every file; later files win over earlier ones for the same resource id."""
    by_id: dict[str, Resource] = {}
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ExistingError(f"{path}: not valid JSON ({exc})") from exc
        for i, entry in enumerate(_entries(path, data)):
            res = _resource(path, i, entry, subscription_id, resource_group)
            by_id[res.id.lower()] = res
    return list(by_id.values())


def _entries(path: Path, data: JsonValue) -> list[JsonValue]:
    if isinstance(data, dict):
        data = data.get("resources")
    if not isinstance(data, list):
        raise ExistingError(f'{path}: expected a list of resources or {{"resources": [...]}}')
    return data


def _text(path: Path, where: str, entry: dict[str, JsonValue], key: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ExistingError(f"{path}: {where}: {key!r} must be a non-empty string")
    return value.strip()


def _resource(path: Path, index: int, entry: JsonValue, subscription_id: str, resource_group: str) -> Resource:
    where = f"resource #{index + 1}"
    if not isinstance(entry, dict):
        raise ExistingError(f"{path}: {where}: expected an object")
    type_ = _text(path, where, entry, "type")
    name = _text(path, where, entry, "name")
    if type_ is None or name is None or "/" not in type_:
        raise ExistingError(f"{path}: {where}: needs a 'type' like Microsoft.Web/serverfarms and a 'name'")
    where = f"{type_}/{name}"
    sub = _text(path, where, entry, "subscriptionId") or subscription_id
    rg = _text(path, where, entry, "resourceGroup") or resource_group
    rid = build_resource_id(f"/subscriptions/{sub}/resourceGroups/{rg}", type_, name.split("/"))
    body: dict[str, JsonValue] = {k: v for k, v in entry.items() if k not in ("subscriptionId", "resourceGroup")}
    body["id"] = rid
    return Resource(
        type=type_,
        name=name,
        id=rid,
        body=body,
        module_path=MODULE_PATH,
        subscription_id=sub,
        resource_group=rg,
    )


def merge_existing(deployed: list[Resource], existing: Iterable[Resource]) -> tuple[list[Resource], list[str]]:
    """Append ``existing`` after ``deployed``; what the template deploys itself wins (and is reported)."""
    known = {r.id.lower() for r in deployed}
    merged = list(deployed)
    skipped: list[str] = []
    for res in existing:
        if res.id.lower() in known:
            skipped.append(f"existing {res.type}/{res.name} is also deployed by the template; using the template's")
            continue
        merged.append(res)
    return merged, skipped
