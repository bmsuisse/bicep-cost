"""Command line interface."""

import argparse
import json
import logging
import sys
from pathlib import Path

from .calc import estimate_resources
from .collect.arm import ArmClient, AzureError, az_current_subscription, http_transport
from .collect.collectors import Finding, collect_all
from .collect.toml_out import render_usage_toml
from .compile import CompileError, load_input
from .engine import DeploymentSettings, Engine
from .prices import PriceClient, PriceError, build_filter, default_cache_dir
from .report import EstimateJson, render_csv, render_diff, render_json, render_markdown, render_text
from .usage import UsageConfig


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of days")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bicep-cost",
        description="Estimate monthly Azure costs from Bicep / ARM templates using the public Azure Retail Prices API.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log price API requests")
    sub = parser.add_subparsers(dest="command", required=True)

    est = sub.add_parser("estimate", help="estimate the monthly cost of a template")
    est.add_argument(
        "template", type=Path, help=".bicep, .bicepparam (uses its `using` template) or compiled ARM .json"
    )
    est.add_argument(
        "-p",
        "--parameters",
        type=Path,
        help=".bicepparam or ARM parameter .json (azd ${VAR} placeholders are read from the environment)",
    )
    est.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="override a template parameter (value parsed as JSON if possible)",
    )
    est.add_argument(
        "--usage",
        type=Path,
        action="append",
        default=[],
        help="TOML usage file (storage GB, log volume, ...); repeatable, later files win. Default: <params>.usage.toml "
        "or bicep-cost.usage.toml next to the template, if present",
    )
    est.add_argument(
        "--currency", default=None, help="price currency (default: the usage file's cost currency, else USD)"
    )
    est.add_argument(
        "--location",
        default="westeurope",
        help="default Azure region for resourceGroup().location and for resources without a location",
    )
    est.add_argument(
        "--resource-group", default="rg-estimate", help="resource group name used for resourceGroup() / resourceId()"
    )
    est.add_argument("--subscription-id", default="00000000-0000-0000-0000-000000000000")
    est.add_argument("--format", choices=["text", "json", "markdown", "csv"], default="text")
    est.add_argument(
        "--group-by",
        choices=["resource", "type", "module"],
        default="resource",
        help="text format: how to group the table",
    )
    est.add_argument(
        "--detail",
        action="store_true",
        help="text format: line items, notes, assumptions and template evaluation notes",
    )
    est.add_argument("-o", "--output", type=Path, help="write the report to a file instead of stdout")
    est.add_argument(
        "--strict", action="store_true", help="exit with 2 if any resource could not be priced or has no estimator"
    )
    est.add_argument(
        "--fail-above", type=float, metavar="AMOUNT", help="exit with 3 if the monthly total exceeds AMOUNT"
    )
    _add_cache_args(est)

    diff = sub.add_parser("diff", help="compare two `estimate --format json` reports")
    diff.add_argument("base", type=Path)
    diff.add_argument("head", type=Path)

    collect = sub.add_parser(
        "collect",
        help="measure real usage (storage GB, log ingestion, replica hours, ...) into a usage file",
        description="Read-only: queries Azure Monitor metrics, Log Analytics and Cost Management for the resources the "
        "template declares and writes a usage TOML for `estimate --usage`. Needs `az login` with Reader on the "
        "resources (Cost Management Reader and Log Analytics Reader for cost / log data).",
    )
    collect.add_argument("template", type=Path, help="same input as `estimate`")
    collect.add_argument("-p", "--parameters", type=Path)
    collect.add_argument("--set", dest="overrides", action="append", default=[], metavar="NAME=VALUE")
    collect.add_argument("--subscription-id", help="subscription of the deployment (default: the az CLI's current one)")
    collect.add_argument(
        "--resource-group", required=True, help="resource group the template is deployed to (as for `estimate`)"
    )
    collect.add_argument("--location", default="westeurope")
    collect.add_argument("--days", type=_positive_int, default=30, help="measurement window in days (default 30)")
    collect.add_argument(
        "-o",
        "--output",
        type=Path,
        help="usage file to write ('-' for stdout; default: <params>.usage.toml or bicep-cost.usage.toml)",
    )
    collect.add_argument("--workers", type=int, default=8)

    init = sub.add_parser(
        "init-usage", help="write a usage file listing every tunable value of a template with its default"
    )
    init.add_argument("template", type=Path)
    init.add_argument("-p", "--parameters", type=Path)
    init.add_argument("--set", dest="overrides", action="append", default=[], metavar="NAME=VALUE")
    init.add_argument("--location", default="westeurope")
    init.add_argument("-o", "--output", type=Path, help="default: stdout")
    _add_cache_args(init)

    prices = sub.add_parser("prices", help="look up retail meters (for debugging estimators / writing usage files)")
    prices.add_argument("--service", required=True)
    prices.add_argument("--region", default="westeurope")
    prices.add_argument("--product")
    prices.add_argument("--sku")
    prices.add_argument("--meter")
    prices.add_argument("--meter-contains")
    prices.add_argument("--product-contains")
    prices.add_argument("--currency", default="USD")
    _add_cache_args(prices)
    return parser


def _add_cache_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--offline", action="store_true", help="only use cached prices")
    p.add_argument("--refresh", action="store_true", help="ignore cached prices")
    p.add_argument("--no-cache", action="store_true", help="do not read or write the price cache")
    p.add_argument(
        "--cache-dir", type=Path, default=None, help=f"price cache directory (default: {default_cache_dir()})"
    )


def _client(args: argparse.Namespace, currency: str) -> PriceClient:
    cache = None if args.no_cache else (args.cache_dir or default_cache_dir())
    return PriceClient(currency=currency.upper(), cache_dir=cache, offline=args.offline, refresh=args.refresh)


def usage_file_stem(template: Path, parameters: Path | None) -> Path:
    """Where this deployment's usage file lives: next to the parameter file if given, else the template."""
    anchor = parameters or template
    return anchor.parent / f"{anchor.stem}.usage.toml"


def discover_usage_files(template: Path, parameters: Path | None) -> list[Path]:
    candidates = [template.parent / "bicep-cost.usage.toml", usage_file_stem(template, parameters)]
    seen: list[Path] = []
    for c in candidates:
        if c.exists() and c not in seen:
            seen.append(c)
    return seen


def _cmd_estimate(args: argparse.Namespace) -> int:
    compiled = load_input(args.template, args.parameters, args.overrides)
    engine = Engine(
        DeploymentSettings(
            subscription_id=args.subscription_id,
            resource_group=args.resource_group,
            location=args.location,
        )
    )
    engine.expand(compiled.template, compiled.parameters)
    usage_files = args.usage or discover_usage_files(args.template, args.parameters)
    if usage_files:
        print("bicep-cost: usage from " + ", ".join(str(f) for f in usage_files), file=sys.stderr)
    usage = UsageConfig.load(usage_files)
    currency = (args.currency or usage.cost_currency or "USD").upper()
    estimate = estimate_resources(
        engine.resources,
        _client(args, currency),
        usage,
        default_region=args.location.replace(" ", "").lower(),
    )
    if usage.cost_currency and usage.cost_currency.upper() != currency:
        print(
            f"bicep-cost: warning: monthly_cost values in the usage file are {usage.cost_currency}, "
            f"the estimate is in {currency}; they are not converted",
            file=sys.stderr,
        )
    source = compiled.source
    if args.format == "json":
        text = render_json(estimate, source, engine.diagnostics)
    elif args.format == "csv":
        text = render_csv(estimate)
    elif args.format == "markdown":
        text = render_markdown(estimate, source, engine.diagnostics)
    else:
        text = render_text(estimate, source, engine.diagnostics, detail=args.detail, group_by=args.group_by)
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    if args.strict and (estimate.count("unresolved") or estimate.count("unsupported")):
        print("bicep-cost: some resources were not priced (--strict)", file=sys.stderr)
        return 2
    if args.fail_above is not None and estimate.monthly > args.fail_above:
        print(
            f"bicep-cost: {estimate.monthly:,.2f} {estimate.currency}/month "
            f"exceeds --fail-above {args.fail_above:,.2f}",
            file=sys.stderr,
        )
        return 3
    return 0


def _load_report(path: Path) -> EstimateJson:
    data = json.loads(path.read_text())
    if "resources" not in data or "total_monthly" not in data:
        raise CompileError(f"{path} is not a `bicep-cost estimate --format json` report")
    return data


def _cmd_diff(args: argparse.Namespace) -> int:
    sys.stdout.write(render_diff(_load_report(args.base), _load_report(args.head)))
    return 0


def _expand(args: argparse.Namespace, subscription_id: str, resource_group: str) -> tuple[Engine, str]:
    compiled = load_input(args.template, args.parameters, args.overrides)
    engine = Engine(
        DeploymentSettings(subscription_id=subscription_id, resource_group=resource_group, location=args.location)
    )
    engine.expand(compiled.template, compiled.parameters)
    return engine, compiled.source


def _cmd_collect(args: argparse.Namespace) -> int:
    subscription = args.subscription_id or az_current_subscription()
    engine, source = _expand(args, subscription, args.resource_group)
    arm = ArmClient(http_transport(), days=args.days)
    print(
        f"bicep-cost: measuring {len(engine.resources)} resources of {source} over the last {args.days} days ...",
        file=sys.stderr,
    )
    result = collect_all(engine.resources, arm, workers=args.workers)
    header = [
        f"Measured usage for {source}",
        f"subscription {subscription}, resource group {args.resource_group}, window {args.days} d "
        f"ending {arm.end:%Y-%m-%d}",
    ]
    text = render_usage_toml(result.findings, result.notes, header, result.cost_currency)
    for w in result.warnings:
        print(f"bicep-cost: warning: {w}", file=sys.stderr)
    target = args.output or usage_file_stem(args.template, args.parameters)
    if str(target) == "-":
        sys.stdout.write(text)
    else:
        target.write_text(text)
        print(
            f"bicep-cost: wrote {len({(f.section, f.name) for f in result.findings})} resource(s), "
            f"{len(result.findings)} value(s) to {target}",
            file=sys.stderr,
        )
    return 0


def _cmd_init_usage(args: argparse.Namespace) -> int:
    engine, source = _expand(args, "00000000-0000-0000-0000-000000000000", "rg-estimate")
    usage = UsageConfig()
    estimate_resources(
        engine.resources, _client(args, "USD"), usage, default_region=args.location.replace(" ", "").lower()
    )
    findings = [
        Finding(a.section, a.name, a.key, float(a.value), "default")
        for a in sorted(usage.used.values(), key=lambda a: (a.section, a.name, a.key))
        if isinstance(a.value, (int, float)) and not isinstance(a.value, bool)
    ]
    text = render_usage_toml(findings, {}, [f"Usage assumptions for {source} (values shown are the defaults)"], None)
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


def _cmd_prices(args: argparse.Namespace) -> int:
    client = _client(args, args.currency)
    flt = build_filter(
        args.service,
        args.region,
        product=args.product,
        sku=args.sku,
        meter=args.meter,
        product_contains=args.product_contains,
        meter_contains=args.meter_contains,
    )
    meters = client.query(flt)
    if not meters:
        print("no meters found", file=sys.stderr)
        return 1
    for m in sorted(meters, key=lambda m: (m.product, m.sku, m.name)):
        tiers = ", ".join(f"{t.price:g}" + (f" from {t.minimum_units:g}" if t.minimum_units else "") for t in m.tiers)
        print(f"{m.product} | {m.sku} | {m.name} | {m.unit} | {tiers} {m.currency}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(message)s")
    try:
        if args.command == "estimate":
            return _cmd_estimate(args)
        if args.command == "diff":
            return _cmd_diff(args)
        if args.command == "collect":
            return _cmd_collect(args)
        if args.command == "init-usage":
            return _cmd_init_usage(args)
        return _cmd_prices(args)
    except (CompileError, PriceError, AzureError, ValueError, OSError) as exc:
        print(f"bicep-cost: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
