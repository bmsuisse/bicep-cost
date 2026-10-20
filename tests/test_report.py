from bicep_cost.model import Estimate, LineItem, ResourceEstimate
from bicep_cost.report import render_chart, render_markdown


def test_cost_chart_orders_scales_and_groups() -> None:
    from tests.conftest import make_resource

    rows = []
    for name, amount in [("small", 25), ("large", 75), ("zero", 0)]:
        res = make_resource("Microsoft.Web/serverfarms", name, {})
        res.module_path = ("web",)
        rows.append(ResourceEstimate(res, "priced", [LineItem("cost", 1, "fixed", monthly=amount)]))
    est = Estimate("CHF", "westeurope", rows, [], [])
    chart = render_chart(est)
    assert chart.index("large") < chart.index("small")
    assert "75.00 CHF  (75.0%)" in chart
    assert "25.00 CHF  (25.0%)" in chart
    assert "#" * 32 in chart
    assert "zero" not in chart
    grouped = render_chart(est, "module")
    assert "web" in grouped
    assert "100.00 CHF  (100.0%)" in grouped
    assert "large" not in grouped
    assert "```text" in render_markdown(est, "template", [])


def test_empty_cost_chart() -> None:
    assert "no positive priced costs" in render_chart(Estimate("USD", "westeurope", [], [], []))
