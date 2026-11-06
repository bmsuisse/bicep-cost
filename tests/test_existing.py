import json
from dataclasses import replace
from pathlib import Path

import pytest

from bicep_cost import cli
from bicep_cost.existing import ExistingError, load_existing, merge_existing
from bicep_cost.prices import PriceClient
from tests.conftest import fake_fetcher, make_resource

HOURS = 730
SUB = "sub1"
RG = "rg"
PLAN = {"type": "Microsoft.Web/serverfarms", "name": "shared-plan", "location": "westeurope", "kind": "linux"}


@pytest.fixture(autouse=True)
def fake_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "_client",
        lambda args, currency: PriceClient(currency=currency.upper(), cache_dir=None, fetcher=fake_fetcher),
    )


def write(tmp_path: Path, data: object, name: str = "existing.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return path


def run_estimate(
    capsys: pytest.CaptureFixture[str], fixtures_dir: Path, *extra: str
) -> tuple[int, dict[str, object], str]:
    code = cli.main(
        [
            "estimate",
            str(fixtures_dir / "site-on-existing-plan.json"),
            "--resource-group",
            RG,
            "--subscription-id",
            SUB,
            "--format",
            "json",
            *extra,
        ]
    )
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out else {}, out.err


def by_type(report: dict[str, object], type_: str) -> dict[str, object]:
    resources = report["resources"]
    assert isinstance(resources, list)
    return next(r for r in resources if r["type"] == type_)


def test_without_existing_the_hosting_plan_is_not_priced(
    capsys: pytest.CaptureFixture[str], fixtures_dir: Path
) -> None:
    code, report, _ = run_estimate(capsys, fixtures_dir)
    assert code == 0
    assert report["total_monthly"] == 0
    assert by_type(report, "Microsoft.Web/sites")["status"] == "external"


def test_existing_plan_is_priced_and_the_site_runs_on_it(
    capsys: pytest.CaptureFixture[str], fixtures_dir: Path, tmp_path: Path
) -> None:
    plan = {**PLAN, "sku": {"name": "P1v3", "capacity": 2}, "properties": {"reserved": True}}
    code, report, _ = run_estimate(capsys, fixtures_dir, "--existing", str(write(tmp_path, {"resources": [plan]})))
    assert code == 0
    assert report["total_monthly"] == pytest.approx(0.178 * HOURS * 2)
    assert (
        by_type(report, "Microsoft.Web/serverfarms")["address"] == "existing/rg/Microsoft.Web/serverfarms/shared-plan"
    )
    assert by_type(report, "Microsoft.Web/sites")["status"] == "included"


def test_existing_accepts_a_bare_list_and_honours_usage_tables(
    capsys: pytest.CaptureFixture[str], fixtures_dir: Path, tmp_path: Path
) -> None:
    plan = {**PLAN, "sku": {"name": "P1v3", "capacity": 1}}
    usage = tmp_path / "u.toml"
    usage.write_text('[appserviceplans."shared-plan"]\ninstances = 3\n')
    code, report, _ = run_estimate(
        capsys, fixtures_dir, "--existing", str(write(tmp_path, [plan])), "--usage", str(usage)
    )
    assert code == 0
    assert report["total_monthly"] == pytest.approx(0.178 * HOURS * 3)


def test_resource_group_and_subscription_default_to_the_deployment_and_can_be_overridden(tmp_path: Path) -> None:
    other = {"type": "Microsoft.DBforPostgreSQL/flexibleServers", "name": "pg", "resourceGroup": "rg-other"}
    moved = {**PLAN, "subscriptionId": "sub2"}
    res_pg, res_plan, res_moved = load_existing([write(tmp_path, [other, PLAN, moved])], SUB, RG)
    assert (
        res_pg.id
        == "/subscriptions/sub1/resourceGroups/rg-other/providers/Microsoft.DBforPostgreSQL/flexibleServers/pg"
    )
    assert res_plan.id == "/subscriptions/sub1/resourceGroups/rg/providers/Microsoft.Web/serverfarms/shared-plan"
    assert res_moved.subscription_id == "sub2"
    assert res_plan.body["id"] == res_plan.id
    assert "resourceGroup" not in res_pg.body


def test_later_files_win_for_the_same_resource(tmp_path: Path) -> None:
    first = write(tmp_path, [{**PLAN, "sku": {"name": "B1"}}], "a.json")
    second = write(tmp_path, [{**PLAN, "sku": {"name": "P1v3"}}], "b.json")
    (res,) = load_existing([first, second], SUB, RG)
    assert res.get("sku.name") == "P1v3"


def test_template_resources_win_over_existing_entries() -> None:
    deployed = make_resource("Microsoft.Web/serverfarms", "shared-plan", {"sku": {"name": "B1"}})
    clash = make_resource("Microsoft.Web/serverfarms", "SHARED-PLAN", {"sku": {"name": "P3v3"}})
    merged, skipped = merge_existing([deployed], [clash])
    assert merged == [deployed]
    assert len(skipped) == 1 and "also deployed by the template" in skipped[0]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not json", "not valid JSON"),
        ('{"resources": 3}', "expected a list of resources"),
        ('["plan"]', "expected an object"),
        ('[{"name": "x"}]', "needs a 'type'"),
        ('[{"type": "Microsoft.Web/serverfarms"}]', "needs a 'type'"),
        ('[{"type": "serverfarms", "name": "x"}]', "needs a 'type'"),
        ('[{"type": "Microsoft.Web/serverfarms", "name": "x", "resourceGroup": ""}]', "'resourceGroup' must be"),
    ],
)
def test_invalid_files_are_rejected_with_the_file_name(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content)
    with pytest.raises(ExistingError, match=message) as exc:
        load_existing([path], SUB, RG)
    assert "bad.json" in str(exc.value)


def test_invalid_file_is_a_cli_error_not_a_traceback(
    capsys: pytest.CaptureFixture[str], fixtures_dir: Path, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[1]")
    code, _, err = run_estimate(capsys, fixtures_dir, "--existing", str(bad))
    assert code == 1
    assert "bicep-cost:" in err and "bad.json" in err


def test_overlap_with_the_template_is_reported_in_the_diagnostics(
    capsys: pytest.CaptureFixture[str], fixtures_dir: Path, tmp_path: Path
) -> None:
    site = {"type": "Microsoft.Web/sites", "name": "shop", "kind": "app,linux"}
    code, report, _ = run_estimate(capsys, fixtures_dir, "--existing", str(write(tmp_path, [site])))
    assert code == 0
    assert "also deployed by the template" in json.dumps(report)


def test_report_address_includes_the_scope_when_it_is_not_the_deployments(tmp_path: Path) -> None:
    other_rg = {**PLAN, "name": "p2", "resourceGroup": "rg-other"}
    other_sub = {**PLAN, "name": "p3", "subscriptionId": "sub2", "resourceGroup": "rg-x"}
    res_default, res_rg, res_sub = load_existing([write(tmp_path, [PLAN, other_rg, other_sub])], SUB, RG)
    assert res_default.address == "existing/rg/Microsoft.Web/serverfarms/shared-plan"
    assert res_rg.address == "existing/rg-other/Microsoft.Web/serverfarms/p2"
    assert res_sub.address == "existing/sub2/rg-x/Microsoft.Web/serverfarms/p3"


def test_same_named_resources_in_different_groups_are_rejected() -> None:
    deployed = make_resource("Microsoft.Web/serverfarms", "shared-plan", {})
    elsewhere = make_resource("Microsoft.Web/serverfarms", "Shared-Plan", {})
    elsewhere = replace(elsewhere, id=elsewhere.id.replace("/rg/", "/rg-other/"), resource_group="rg-other")
    with pytest.raises(ExistingError, match="cannot be told apart"):
        merge_existing([deployed], [elsewhere])
    with pytest.raises(ExistingError, match="cannot be told apart"):
        merge_existing([], [deployed, elsewhere])
