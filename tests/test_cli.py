import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bicep_cost import cli
from bicep_cost.compile import CompileError, load_input, parameters_from_arm, parse_set
from bicep_cost.prices import PriceClient

from .conftest import fake_fetcher

HOURS = 730


@pytest.fixture(autouse=True)
def fake_prices(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_client",
        lambda args, currency: PriceClient(currency=currency.upper(), cache_dir=None, fetcher=fake_fetcher),
    )


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_estimate_json_for_arm_template(capsys, fixtures_dir):
    code, out, _ = run_cli(capsys, "estimate", str(fixtures_dir / "app.json"), "--format", "json")
    assert code == 0
    report = json.loads(out)
    assert report["total_monthly"] == pytest.approx(0.178 * HOURS)
    plan = next(r for r in report["resources"] if r["type"] == "Microsoft.Web/serverfarms")
    assert plan["address"].startswith("web/")
    assert plan["items"][0]["quantity"] == pytest.approx(HOURS)


def test_set_overrides_parameters(capsys, fixtures_dir):
    _, out, _ = run_cli(
        capsys,
        "estimate",
        str(fixtures_dir / "app.json"),
        "--format",
        "json",
        "--set",
        "capacity=3",
        "--set",
        "skuName=B1",
    )
    assert json.loads(out)["total_monthly"] == pytest.approx(0.018 * HOURS * 3)


def test_text_output_lists_unsupported_resources(capsys, fixtures_dir):
    code, out, _ = run_cli(capsys, "estimate", str(fixtures_dir / "app.json"))
    assert code == 0
    assert "TOTAL per month" in out
    assert "Microsoft.Cdn/profiles" in out


def test_strict_and_fail_above_exit_codes(capsys, fixtures_dir):
    path = str(fixtures_dir / "app.json")
    assert run_cli(capsys, "estimate", path, "--strict")[0] == 2
    assert run_cli(capsys, "estimate", path, "--fail-above", "10")[0] == 3
    assert run_cli(capsys, "estimate", path, "--fail-above", "10000")[0] == 0


def test_markdown_and_csv(capsys, fixtures_dir):
    path = str(fixtures_dir / "app.json")
    _, md, _ = run_cli(capsys, "estimate", path, "--format", "markdown")
    assert "| **Total** |" in md
    _, csv_out, _ = run_cli(capsys, "estimate", path, "--format", "csv")
    assert csv_out.splitlines()[0].startswith("address,type,status")


def test_output_file(tmp_path, capsys, fixtures_dir):
    target = tmp_path / "r.json"
    code, out, _ = run_cli(capsys, "estimate", str(fixtures_dir / "app.json"), "--format", "json", "-o", str(target))
    assert code == 0
    assert out == ""
    assert json.loads(target.read_text())["currency"] == "USD"


def test_diff_between_two_reports(tmp_path, capsys, fixtures_dir):
    path = str(fixtures_dir / "app.json")
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    run_cli(capsys, "estimate", path, "--format", "json", "-o", str(a))
    run_cli(capsys, "estimate", path, "--format", "json", "--set", "capacity=2", "-o", str(b))
    code, out, _ = run_cli(capsys, "diff", str(a), str(b))
    assert code == 0
    assert f"{0.178 * HOURS:,.2f}" in out
    assert "+129.94/month" in out


def test_missing_bicep_input_reports_error(capsys, tmp_path):
    code, _, err = run_cli(capsys, "estimate", str(tmp_path / "nope.txt"))
    assert code == 1
    assert "unsupported input" in err


def test_prices_subcommand(capsys):
    code, out, _ = run_cli(capsys, "prices", "--service", "Key Vault", "--meter", "Operations")
    assert code == 0
    assert "Operations" in out


def test_parameter_file_parsing(monkeypatch):
    monkeypatch.setenv("AZURE_ENV_NAME", "prd")
    monkeypatch.delenv("MISSING_VAR", raising=False)
    params = parameters_from_arm(
        {
            "parameters": {
                "a": {"value": "${AZURE_ENV_NAME}-x"},
                "b": {"value": "${MISSING_VAR}"},
                "c": {"value": "${MISSING_VAR=dflt}"},
                "d": {"reference": {"keyVault": {}}},
                "e": {"value": 3},
            }
        }
    )
    assert params["a"] == "prd-x"
    assert params["c"] == "dflt"
    assert params["e"] == 3
    assert type(params["b"]).__name__ == "Unknown"
    assert type(params["d"]).__name__ == "Unknown"


def test_parse_set():
    assert parse_set(["a=1", "b=text", 'c={"x": true}']) == {"a": 1, "b": "text", "c": {"x": True}}
    with pytest.raises(CompileError):
        parse_set(["novalue"])


needs_bicep = pytest.mark.skipif(
    shutil.which("bicep") is None and not (Path.home() / ".azure" / "bin" / "bicep").exists(),
    reason="Bicep CLI not installed",
)


@needs_bicep
def test_bicep_and_bicepparam_compile(capsys, fixtures_dir):
    code, out, _ = run_cli(capsys, "estimate", str(fixtures_dir / "main.bicep"), "--format", "json")
    assert code == 0
    assert json.loads(out)["total_monthly"] == pytest.approx(0.178 * HOURS * 2)
    # a .bicepparam supplies parameters for its `using` template
    code, out, _ = run_cli(capsys, "estimate", str(fixtures_dir / "prod.bicepparam"), "--format", "json")
    assert code == 0
    assert json.loads(out)["total_monthly"] == pytest.approx(0.018 * HOURS * 2)
    # ... and can be combined with an explicit template
    code, out, _ = run_cli(
        capsys,
        "estimate",
        str(fixtures_dir / "main.bicep"),
        "-p",
        str(fixtures_dir / "prod.bicepparam"),
        "--format",
        "json",
    )
    assert json.loads(out)["total_monthly"] == pytest.approx(0.018 * HOURS * 2)


def test_load_input_rejects_param_and_bicepparam_together(fixtures_dir):
    with pytest.raises(CompileError):
        load_input(fixtures_dir / "prod.bicepparam", fixtures_dir / "prod.bicepparam", [])


def test_subprocess_entrypoint_runs():
    proc = subprocess.run(["python", "-m", "bicep_cost.cli", "--help"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "estimate" in proc.stdout


def test_diff_rejects_mixed_currencies(tmp_path, capsys, fixtures_dir):
    path = str(fixtures_dir / "app.json")
    usd, chf = tmp_path / "usd.json", tmp_path / "chf.json"
    run_cli(capsys, "estimate", path, "--format", "json", "-o", str(usd))
    run_cli(capsys, "estimate", path, "--format", "json", "--currency", "CHF", "-o", str(chf))
    code, _, err = run_cli(capsys, "diff", str(usd), str(chf))
    assert code == 1
    assert "different currencies" in err


def test_markdown_lists_external_resources(capsys, tmp_path):
    template = {
        "resources": [
            {
                "type": "Microsoft.Web/sites",
                "apiVersion": "2023-12-01",
                "name": "site",
                "location": "westeurope",
                "kind": "app",
                "properties": {"serverFarmId": "/subscriptions/x/outside"},
            }
        ]
    }
    path = tmp_path / "t.json"
    path.write_text(json.dumps(template))
    _, out, _ = run_cli(capsys, "estimate", str(path), "--format", "markdown")
    assert "Hosted outside this template" in out
    assert "site" in out


def test_invalid_usage_value_is_a_clean_error(capsys, tmp_path):
    template = {
        "resources": [
            {
                "type": "Microsoft.App/containerApps",
                "apiVersion": "2024-03-01",
                "name": "a",
                "location": "westeurope",
                "properties": {
                    "template": {
                        "containers": [{"resources": {"cpu": 1, "memory": "1Gi"}}],
                        "scale": {"minReplicas": 1},
                    }
                },
            }
        ]
    }
    (tmp_path / "t.json").write_text(json.dumps(template))
    (tmp_path / "u.toml").write_text("[containerapps.a]\nidle_percent = 150\n")
    code, _, err = run_cli(capsys, "estimate", str(tmp_path / "t.json"), "--usage", str(tmp_path / "u.toml"))
    assert code == 1
    assert "idle_percent" in err


@pytest.mark.parametrize("currency", [None, "chf"])
def test_usage_currency_default_and_conflict(currency, tmp_path, capsys):
    template = tmp_path / "empty.json"
    template.write_text('{"resources": []}')
    usage = tmp_path / "empty.usage.toml"
    usage.write_text('[meta]\ncost_currency = "CHF"\n')
    args = ["--currency", currency] if currency else []
    code, out, _ = run_cli(capsys, "estimate", str(template), "--format", "json", *args)
    assert code == 0
    assert json.loads(out)["currency"] == "CHF"
    code, out, err = run_cli(capsys, "estimate", str(template), "--currency", "USD")
    assert code == 1
    assert not out
    assert "conversion is not supported" in err


@pytest.mark.parametrize(
    ("old", "new", "percent", "cap", "expected"),
    [
        (100, 110, 10, None, 0),
        (100, 111, 10, None, 3),
        (100, 90, 0, None, 0),
        (0, 0, 10, None, 0),
        (0, 1, 10000, None, 3),
        (100, 105, 10, 104, 3),
        (100, 111, 10, 200, 3),
        (100, 100, 0, 100, 0),
    ],
)
def test_diff_budget_caps(old, new, percent, cap, expected, tmp_path, capsys):
    base, head = tmp_path / "base.json", tmp_path / "head.json"
    for path, amount in ((base, old), (head, new)):
        path.write_text(json.dumps({"resources": [], "currency": "USD", "source": "test", "total_monthly": amount}))
    args = ["--fail-above", str(cap)] if cap is not None else []
    code, out, _ = run_cli(capsys, "diff", str(base), str(head), "--fail-increase-percent", str(percent), *args)
    assert code == expected
    assert "Cost change" in out


def test_estimate_relative_cap_writes_report(tmp_path, capsys, fixtures_dir):
    template = str(fixtures_dir / "app.json")
    base, head = tmp_path / "base.json", tmp_path / "head.json"
    run_cli(capsys, "estimate", template, "--format", "json", "-o", str(base))
    code, out, err = run_cli(
        capsys,
        "estimate",
        template,
        "--set",
        "capacity=2",
        "--baseline",
        str(base),
        "--fail-increase-percent",
        "10",
        "--format",
        "json",
        "-o",
        str(head),
    )
    assert code == 3
    assert not out
    assert json.loads(head.read_text())["total_monthly"] == pytest.approx(259.88)
    assert "100.00%" in err
    code, _, err = run_cli(capsys, "estimate", template, "--fail-increase-percent", "10")
    assert code == 1
    assert "requires --baseline" in err


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_budget_rejects_invalid_threshold(value):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["estimate", "t.json", f"--fail-increase-percent={value}"])
    assert exc.value.code == 2


@pytest.mark.parametrize("total", [-1, float("nan"), float("inf"), True, "100"])
def test_baseline_rejects_invalid_totals(total, tmp_path, capsys):
    path = tmp_path / "base.json"
    path.write_text(json.dumps({"resources": [], "total_monthly": total, "source": "x", "currency": "USD"}))
    code, out, err = run_cli(
        capsys, "estimate", "missing.json", "--baseline", str(path), "--fail-increase-percent", "10"
    )
    assert code == 1
    assert not out
    assert "finite nonnegative" in err


def test_chart_survives_budget_failure(capsys, fixtures_dir):
    for format_ in ("text", "markdown"):
        code, out, _ = run_cli(
            capsys, "estimate", str(fixtures_dir / "app.json"), "--format", format_, "--fail-above", "1"
        )
        assert code == 3
        assert "Monthly cost breakdown (USD/month" in out
        assert "################################" in out
        assert "unsupported" in out or "no estimator" in out
