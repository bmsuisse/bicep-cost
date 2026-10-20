import pytest

from bicep_cost.armexpr import JsonValue
from bicep_cost.calc import estimate_resources
from bicep_cost.engine import Resource
from bicep_cost.model import Estimate
from bicep_cost.prices import PriceClient
from bicep_cost.usage import UsageConfig
from tests.conftest import make_resource

HOURS = 730


def namespace(sku: str, capacity: int | None = None) -> Resource:
    sku_body: dict[str, JsonValue] = {"name": sku, **({"capacity": capacity} if capacity else {})}
    return make_resource("Microsoft.ServiceBus/namespaces", "sb", {"location": "westeurope", "sku": sku_body})


def run(prices: PriceClient, res: Resource, usage: dict[str, object] | None = None) -> Estimate:
    return estimate_resources([res], prices, UsageConfig(usage or {}))


def test_standard_has_hourly_base_and_free_first_13m_operations(prices: PriceClient) -> None:
    inside = run(prices, namespace("Standard"), {"servicebus": {"sb": {"operations_millions": 10}}})
    assert inside.monthly == pytest.approx(0.0135 * HOURS)
    over = run(prices, namespace("Standard"), {"servicebus": {"sb": {"operations_millions": 113}}})
    assert over.monthly == pytest.approx(0.0135 * HOURS + 87 * 0.8 + 13 * 0.5)
    assert over.usage == pytest.approx(87 * 0.8 + 13 * 0.5)


def test_basic_is_operations_only(prices: PriceClient) -> None:
    est = run(prices, namespace("Basic"), {"servicebus": {"sb": {"operations_millions": 100}}})
    assert est.monthly == pytest.approx(5.0)


def test_premium_is_messaging_units_and_rejects_bad_capacity(prices: PriceClient) -> None:
    assert run(prices, namespace("Premium", 2)).monthly == pytest.approx(2 * 0.9275 * HOURS)
    bad = run(prices, namespace("Premium", 3))
    assert bad.resources[0].status == "unresolved"


def test_children_are_included(prices: PriceClient) -> None:
    queue = make_resource("Microsoft.ServiceBus/namespaces/queues", "sb/q", {})
    assert run(prices, queue).resources[0].status == "free"
