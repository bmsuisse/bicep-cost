"""Logic Apps: Consumption workflows (billed per executed operation), integration accounts, Standard connectors.

A Consumption workflow is priced from its definition: every trigger execution and every action execution is one
metered operation, either *built-in* (Request, HTTP, Compose, control flow, ...), a *standard* managed connector
or an *enterprise* managed connector (SAP, IBM MQ, IBM 3270). The first 4,000 built-in operations per subscription and
month are free. Logic Apps Standard (workflow apps) is billed through its plan, see ``appservice``.
"""

import re
from dataclasses import dataclass

from bicep_cost.armexpr import JsonValue
from bicep_cost.context import HOURS_PER_MONTH, EstimateContext, UnresolvedError, as_number
from bicep_cost.engine import Resource
from bicep_cost.estimators.common import external, free, included, line, priced
from bicep_cost.model import CostBasis, LineItem, ResourceEstimate

ENTERPRISE_CONNECTORS = frozenset({"sap", "mq", "ibmmq", "ibm3270", "3270"})
SCHEDULE_TRIGGERS = frozenset({"recurrence", "slidingwindow"})
CONNECTOR_TYPES = frozenset({"apiconnection", "apiconnectionwebhook", "apiconnectionnotification"})
LOOP_TYPES = frozenset({"foreach", "until"})
HOURS_PER_UNIT = {
    "second": 1 / 3600,
    "minute": 1 / 60,
    "hour": 1.0,
    "day": 24.0,
    "week": 168.0,
    "month": HOURS_PER_MONTH,
}
DEFAULT_TRIGGERS_PER_MONTH = 1000.0
DEFAULT_LOOP_ITERATIONS = 10.0
OPERATION_METERS = (
    ("builtin", "Consumption Built-in Actions", "built-in"),
    ("standard", "Consumption Standard Connector Actions", "standard connector"),
    ("enterprise", "Consumption Enterprise Connector Actions", "enterprise connector"),
)
_CONNECTION_KEY = re.compile(r"\['([^']+)'\]\['connectionId'\]")


@dataclass
class OperationCounts:
    """Executions of one workflow run, by pricing class."""

    builtin: float = 0.0
    standard: float = 0.0
    enterprise: float = 0.0
    has_loops: bool = False

    def add(self, kind: str, count: float) -> None:
        setattr(self, kind, getattr(self, kind) + count)


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _connector_name(node: dict[str, JsonValue], connections: dict[str, JsonValue]) -> str:
    """Managed API name behind an ``ApiConnection`` node (``sap``, ``office365``, ...)."""
    host = _dict(_dict(node.get("inputs")).get("host"))
    ref = _dict(host.get("connection")).get("name")
    match = _CONNECTION_KEY.search(ref) if isinstance(ref, str) else None
    if match is None:
        return ""
    key = match.group(1)
    api_id = _dict(connections.get(key)).get("id")
    return (api_id.rsplit("/", 1)[-1] if isinstance(api_id, str) else key).lower()


def _classify(node: dict[str, JsonValue], connections: dict[str, JsonValue]) -> str:
    if str(node.get("type", "")).lower() not in CONNECTOR_TYPES:
        return "builtin"
    name = _connector_name(node, connections)
    # connection keys of repeated connectors are suffixed (``sap_1``, ``mq-2``)
    if name in ENTERPRISE_CONNECTORS or re.sub(r"[-_]\d+$", "", name) in ENTERPRISE_CONNECTORS:
        return "enterprise"
    return "standard"


def _child_action_maps(node: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    """Every nested ``actions`` map of a control action (scope, loops, condition branches, switch cases)."""
    nested = [_dict(node.get("actions")), _dict(_dict(node.get("else")).get("actions"))]
    nested.append(_dict(_dict(node.get("default")).get("actions")))
    nested.extend(_dict(_dict(case).get("actions")) for case in _dict(node.get("cases")).values())
    return [m for m in nested if m]


def _count_actions(
    actions: dict[str, JsonValue],
    multiplier: float,
    connections: dict[str, JsonValue],
    iterations: float,
    counts: OperationCounts,
) -> None:
    for raw in actions.values():
        node = _dict(raw)
        counts.add(_classify(node, connections), multiplier)
        is_loop = str(node.get("type", "")).lower() in LOOP_TYPES
        counts.has_loops = counts.has_loops or is_loop
        for children in _child_action_maps(node):
            _count_actions(children, multiplier * (iterations if is_loop else 1.0), connections, iterations, counts)


def _polls_per_month(recurrence: dict[str, JsonValue]) -> float | None:
    unit = HOURS_PER_UNIT.get(str(recurrence.get("frequency", "")).lower())
    interval = as_number(recurrence.get("interval"), 0.0)
    if unit is None or interval <= 0:
        return None
    polls = HOURS_PER_MONTH / (unit * interval)
    schedule = _dict(recurrence.get("schedule"))
    frequency = str(recurrence.get("frequency", "")).lower()
    if frequency in ("day", "week"):
        hours, minutes = schedule.get("hours"), schedule.get("minutes")
        polls *= (len(hours) if isinstance(hours, list) and hours else 1) * (
            len(minutes) if isinstance(minutes, list) and minutes else 1
        )
    if frequency == "week":
        week_days = schedule.get("weekDays")
        polls *= len(week_days) if isinstance(week_days, list) and week_days else 1
    return polls


def _recount(definition: dict[str, JsonValue], connections: dict[str, JsonValue], iterations: float) -> OperationCounts:
    counts = OperationCounts()
    _count_actions(_dict(definition.get("actions")), 1.0, connections, iterations, counts)
    return counts


def estimate_workflow(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    if str(res.get("properties.state") or "Enabled").lower() == "disabled":
        return free(res, "disabled workflows are not billed")
    if res.properties.get("integrationServiceEnvironment") is not None:
        return external(res, "runs in an Integration Service Environment (retired), which is billed separately")
    definition = res.get("properties.definition")
    if not isinstance(definition, dict):
        raise UnresolvedError(f"workflow definition of {res.name} is not known")
    if res.is_unknown_at("properties.definition.actions") or res.is_unknown_at("properties.definition.triggers"):
        raise UnresolvedError(f"actions or triggers of {res.name} are not known (depend on an unset parameter)")
    region = ctx.region(res)
    scope = f"sub:{res.subscription_id}"
    connections = _dict(_dict(_dict(res.get("properties.parameters")).get("$connections")).get("value"))

    actions = _recount(definition, connections, 1.0)
    iterations = 1.0
    if actions.has_loops:
        iterations = ctx.usage.number("logicapps", res.name, "loop_iterations", DEFAULT_LOOP_ITERATIONS)
        actions = _recount(definition, connections, iterations)

    polls = OperationCounts()  # trigger executions that follow from the template's recurrence
    events = OperationCounts()  # event-driven trigger executions, set from the number of runs below
    scheduled_runs: float | None = None
    event_triggers: list[tuple[str, float]] = []  # (class, polls per month); None polls = pure push trigger
    for trigger in (_dict(v) for v in _dict(definition.get("triggers")).values()):
        kind = _classify(trigger, connections)
        recurrence = _dict(trigger.get("recurrence"))
        per_month = _polls_per_month(recurrence) if recurrence else None
        polls.add(kind, per_month or 0.0)
        if str(trigger.get("type", "")).lower() in SCHEDULE_TRIGGERS and per_month is not None:
            scheduled_runs = (scheduled_runs or 0.0) + per_month
        else:
            event_triggers.append((kind, per_month or 0.0))
    runs = ctx.usage.number(
        "logicapps",
        res.name,
        "runs_per_month",
        scheduled_runs if scheduled_runs is not None else DEFAULT_TRIGGERS_PER_MONTH,
    )
    for kind, per_month in event_triggers:
        # a polling connector trigger fires once per waiting event, on top of the polls that find nothing
        events.add(kind, max(runs - per_month, 0.0))
    run_basis: CostBasis = (
        "fixed"
        if scheduled_runs is not None and not ctx.usage.has("logicapps", res.name, "runs_per_month")
        else "usage"
    )

    items: list[LineItem] = []
    for kind, meter_name, label in OPERATION_METERS:
        meter = None
        per_run = ctx.usage.number("logicapps", res.name, f"{kind}_actions_per_run", getattr(actions, kind))
        for quantity, basis, what in (
            (getattr(polls, kind), "fixed", "trigger polls"),
            (getattr(events, kind), "usage", "trigger events"),
            (per_run * runs, run_basis, "actions"),
        ):
            if quantity <= 0:
                continue
            meter = meter or ctx.meter("Logic Apps", region, sku="Consumption", meter=meter_name)
            items.append(
                line(
                    f"{label} {what} ({quantity:,.0f}/month)",
                    meter,
                    quantity,
                    basis,
                    scope=scope,
                    note="assumed volume" if basis == "usage" else None,
                )
            )
    notes = ["every branch of a condition or switch is assumed to run; retries are not counted"]
    if actions.has_loops:
        notes.append(f"loops assumed {iterations:g} iterations; set [logicapps.<name>] loop_iterations")
    if any(i.meter and i.meter.name == "Consumption Built-in Actions" for i in items):
        notes.append("the first 4,000 built-in operations per subscription and month are free")
    retention = ctx.usage.number("logicapps", res.name, "data_retention_gb", 0.0)
    if retention > 0:
        meter = ctx.meter("Logic Apps", region, sku="Consumption", meter="Consumption Data Retention")
        items.append(line(f"run history retention ({retention:g} GB)", meter, retention, "usage", scope=scope))
    return priced(res, items, notes)


def estimate_integration_account(res: Resource, ctx: EstimateContext) -> ResourceEstimate:
    sku = str(res.get("sku.name") or "").capitalize()
    if sku == "Free":
        return free(res, "Free integration account")
    if sku not in ("Basic", "Standard", "Premium"):
        raise UnresolvedError(f"integration account SKU {sku or '(not set)'!r} is not priced")
    meter = ctx.meter(
        "Logic Apps", ctx.region(res), product="Logic Apps Integration Account", sku=sku, meter=f"{sku} Unit"
    )
    return priced(res, [line(f"{sku} integration account", meter, 1.0)])


def estimate_standard_app(res: Resource, ctx: EstimateContext, plan: Resource) -> ResourceEstimate:
    """A Logic Apps Standard workflow app: hosting is on the plan, managed connector calls are billed per call."""
    region = ctx.region(plan)
    scope = f"sub:{res.subscription_id}"
    items: list[LineItem] = []
    for key, meter_name, label in (
        ("connector_actions_per_month", "Connectors Standard Connector Actions", "standard connector"),
        ("enterprise_connector_actions_per_month", "Connectors Enterprise Connector Actions", "enterprise connector"),
    ):
        calls = ctx.usage.number("logicapps", res.name, key, 0.0)
        if calls > 0:
            meter = ctx.meter("Logic Apps", region, sku="Connectors", meter=meter_name)
            items.append(line(f"{label} calls ({calls:,.0f}/month)", meter, calls, "usage", scope=scope))
    if not items:
        return included(
            res,
            f"runs on plan {plan.name}; built-in operations are free "
            "(set [logicapps.<name>] connector_actions_per_month for managed connector calls)",
        )
    return priced(res, items, [f"runs on plan {plan.name}; built-in operations are free"])
