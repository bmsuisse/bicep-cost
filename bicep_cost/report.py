"""Render an :class:`~bicep_cost.model.Estimate`."""

import csv
import io
import json
from collections import defaultdict
from typing import TypedDict

from .engine import Diagnostic
from .model import Estimate, LineItem, ResourceEstimate


class ItemJson(TypedDict):
    description: str
    basis: str
    quantity: float
    unit: str
    monthly: float
    meter: str | None
    note: str | None


class ResourceJson(TypedDict):
    address: str
    type: str
    name: str
    status: str
    monthly: float
    fixed: float
    usage: float
    items: list[ItemJson]
    notes: list[str]


class EstimateJson(TypedDict):
    source: str
    price_source: str
    currency: str
    total_monthly: float
    fixed_monthly: float
    usage_monthly: float
    resources: list[ResourceJson]
    warnings: list[str]
    assumptions: list[str]


def money(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def _item_json(i: LineItem) -> ItemJson:
    return {
        "description": i.description,
        "basis": i.basis,
        "quantity": round(i.quantity, 6),
        "unit": i.unit,
        "monthly": round(i.monthly, 6),
        "meter": i.meter.label() if i.meter else None,
        "note": i.note,
    }


def to_json(est: Estimate, source: str, diagnostics: list[Diagnostic]) -> EstimateJson:
    resources: list[ResourceJson] = []
    for r in est.resources:
        resources.append(
            {
                "address": r.resource.address,
                "type": r.resource.type,
                "name": r.resource.name,
                "status": r.status,
                "monthly": round(r.monthly, 6),
                "fixed": round(r.subtotal("fixed"), 6),
                "usage": round(r.subtotal("usage"), 6),
                "items": [_item_json(i) for i in r.items],
                "notes": r.notes,
            }
        )
    return {
        "source": source,
        "price_source": est.price_source,
        "currency": est.currency,
        "total_monthly": round(est.monthly, 6),
        "fixed_monthly": round(est.fixed, 6),
        "usage_monthly": round(est.usage, 6),
        "resources": resources,
        "warnings": [f"{d.where}: {d.message}" if d.where else d.message for d in diagnostics],
        "assumptions": est.assumptions,
    }


def render_json(est: Estimate, source: str, diagnostics: list[Diagnostic]) -> str:
    return json.dumps(to_json(est, source, diagnostics), indent=2)


def render_csv(est: Estimate) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["address", "type", "status", "description", "basis", "quantity", "unit", "monthly", "currency"])
    for r in est.resources:
        if not r.items:
            w.writerow([r.resource.address, r.resource.type, r.status, "", "", "", "", "0", est.currency])
        for i in r.items:
            w.writerow(
                [
                    r.resource.address,
                    r.resource.type,
                    r.status,
                    i.description,
                    i.basis,
                    f"{i.quantity:.6g}",
                    i.unit,
                    f"{i.monthly:.6f}",
                    est.currency,
                ]
            )
    return buf.getvalue()


def _short_type(type_: str) -> str:
    return type_.split("/", 1)[1] if "/" in type_ else type_


def _billed(est: Estimate) -> list[ResourceEstimate]:
    return sorted((r for r in est.resources if r.items or r.status == "priced"), key=lambda r: -r.monthly)


def _problems(est: Estimate) -> tuple[list[ResourceEstimate], list[ResourceEstimate]]:
    return (
        [r for r in est.resources if r.status == "unresolved"],
        [r for r in est.resources if r.status == "unsupported"],
    )


def _group_rows(est: Estimate, group_by: str) -> list[tuple[str, int, float, float]]:
    totals: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    for r in est.resources:
        if not (r.items or r.status == "priced"):
            continue
        key = (
            r.resource.type
            if group_by == "type"
            else (r.resource.module_path[0] if r.resource.module_path else "(root)")
        )
        totals[key][0] += 1
        totals[key][1] += r.subtotal("fixed")
        totals[key][2] += r.subtotal("usage")
    return sorted(((k, int(v[0]), v[1], v[2]) for k, v in totals.items()), key=lambda t: -(t[2] + t[3]))


def render_chart(est: Estimate, group_by: str = "resource") -> str:
    """Plain-text bars of known monthly costs, largest first, suitable for CI logs."""
    if group_by in ("type", "module"):
        rows = [(key, fixed + usage) for key, _, fixed, usage in _group_rows(est, group_by)]
    else:
        rows = [(r.resource.address, r.monthly) for r in _billed(est)]
    rows = [(name, cost) for name, cost in rows if cost > 0]
    if not rows:
        return "Monthly cost breakdown: no positive priced costs."
    peak = rows[0][1]
    lines = [f"Monthly cost breakdown ({est.currency}/month; share of known total)", ""]
    for name, cost in rows:
        bar = "#" * max(1, round(cost / peak * 32))
        lines.append(f"{name}")
        lines.append(f"  {bar:<32}  {money(cost, est.currency)}  ({cost / est.monthly * 100:.1f}%)")
    return "\n".join(lines)


def render_text(
    est: Estimate, source: str, diagnostics: list[Diagnostic], detail: bool = False, group_by: str = "resource"
) -> str:
    cur = est.currency
    out: list[str] = [
        f"Monthly cost estimate for {source}",
        f"Prices: {est.price_source} ({cur}), default region {est.region_default}",
        "",
    ]
    out.extend([render_chart(est, group_by), ""])
    billed = _billed(est)
    if group_by in ("type", "module"):
        rows = _group_rows(est, group_by)
        width = max([len(r[0]) for r in rows] + [len(group_by)])
        out.append(f"{group_by.upper():<{width}}  {'COUNT':>5}  {'FIXED':>12}  {'USAGE*':>12}  {'TOTAL':>12}")
        for key, count, fixed, usage in rows:
            out.append(f"{key:<{width}}  {count:>5}  {fixed:>12,.2f}  {usage:>12,.2f}  {fixed + usage:>12,.2f}")
    else:
        names = [f"{_short_type(r.resource.type)}: {r.resource.name}" for r in billed]
        width = min(max([len(n) for n in names] + [8]), 70)
        out.append(f"{'RESOURCE':<{width}}  {'FIXED':>12}  {'USAGE*':>12}  {'TOTAL':>12}")
        for r, name in zip(billed, names, strict=True):
            mod = f"  [{'/'.join(r.resource.module_path)}]" if detail and r.resource.module_path else ""
            out.append(
                f"{name[:width]:<{width}}  {r.subtotal('fixed'):>12,.2f}  {r.subtotal('usage'):>12,.2f}"
                f"  {r.monthly:>12,.2f}{mod}"
            )
            if detail:
                for i in r.items:
                    flag = "*" if i.basis == "usage" else " "
                    out.append(f"    {flag} {i.description:<64} {i.monthly:>12,.2f}")
                for n in r.notes:
                    out.append(f"      - {n}")
    out.append("")
    out.append(f"Provisioned (fixed):        {money(est.fixed, cur):>18}")
    out.append(f"Usage-based* (assumed):     {money(est.usage, cur):>18}")
    out.append(f"TOTAL per month:            {money(est.monthly, cur):>18}")
    out.append(f"TOTAL per year:             {money(est.monthly * 12, cur):>18}")
    zero_cost = sum(1 for r in est.resources if r.status in ("free", "included"))
    unresolved, unsupported = _problems(est)
    external_res = [r for r in est.resources if r.status == "external"]
    out.append("")
    out.append(
        f"{len(billed)} billed, {zero_cost} free/included, {len(external_res)} external, "
        f"{len(unresolved)} unresolved, {len(unsupported)} unsupported resource(s)."
        "  * usage-based amounts depend on assumed consumption (--usage FILE to change)."
    )
    if unresolved:
        out += ["", "Could not be priced (value unknown or no matching price):"]
        for r in unresolved:
            out.append(f"  ! {r.resource.type} {r.resource.name}: {'; '.join(r.notes)}")
    if unsupported:
        seen: dict[str, int] = defaultdict(int)
        for r in unsupported:
            seen[r.resource.type] += 1
        out += ["", "Not covered by this tool (cost NOT included):"]
        for t, n in sorted(seen.items()):
            out.append(f"  ? {t} x{n}")
    if external_res:
        out += ["", "Hosted outside this template (cost NOT included):"]
        out += [f"  > {r.resource.type} {r.resource.name}: {'; '.join(r.notes)}" for r in external_res]
    if est.default_assumptions:
        out += [
            "",
            f"{est.default_assumptions} usage figure(s) are built-in defaults, not measurements. Measure them from the "
            "deployed resources with `bicep-cost collect` (see --help).",
        ]
    if detail and est.assumptions:
        out += ["", "Usage assumptions used:"]
        out += [f"  {a}" for a in est.assumptions]
    if detail and diagnostics:
        out += ["", "Template evaluation notes:"]
        out += [f"  - {d.where + ': ' if d.where else ''}{d.message}" for d in diagnostics]
    elif diagnostics:
        out += ["", f"{len(diagnostics)} template evaluation note(s); run with --detail to see them."]
    return "\n".join(out) + "\n"


def render_markdown(est: Estimate, source: str, diagnostics: list[Diagnostic]) -> str:
    cur = est.currency
    lines = [
        f"### Azure cost estimate — `{source}`",
        "",
        "```text",
        render_chart(est).replace("`", "'"),
        "```",
        "",
        f"| | {cur}/month |",
        "|---|---:|",
        f"| Provisioned (fixed) | {est.fixed:,.2f} |",
        f"| Usage-based (assumed) | {est.usage:,.2f} |",
        f"| **Total** | **{est.monthly:,.2f}** |",
        "",
        "| Resource | Fixed | Usage | Total |",
        "|---|---:|---:|---:|",
    ]
    for r in _billed(est):
        lines.append(
            f"| `{_short_type(r.resource.type)}` {r.resource.name} | {r.subtotal('fixed'):,.2f} "
            f"| {r.subtotal('usage'):,.2f} | {r.monthly:,.2f} |"
        )
    unresolved, unsupported = _problems(est)
    external_res = [r for r in est.resources if r.status == "external"]
    if external_res:
        lines += ["", "**Hosted outside this template (cost not included):**"]
        lines += [f"- `{r.resource.type}` {r.resource.name}: {'; '.join(r.notes)}" for r in external_res]
    if unresolved or unsupported:
        lines += ["", "**Not included:**"]
        lines += [f"- `{r.resource.type}` {r.resource.name}: {'; '.join(r.notes)}" for r in unresolved]
        lines += [f"- `{t}` (no estimator)" for t in sorted({r.resource.type for r in unsupported})]
    lines += ["", f"_{est.price_source}; usage-based amounts use assumed consumption._", ""]
    return "\n".join(lines)


def render_diff(base: EstimateJson, head: EstimateJson) -> str:
    cur = head["currency"]
    if base["currency"] != cur:
        raise ValueError(f"cannot compare reports in different currencies ({base['currency']} vs {cur})")
    b = {r["address"]: r for r in base["resources"]}
    h = {r["address"]: r for r in head["resources"]}
    rows: list[tuple[str, float, float]] = []
    for key in sorted(set(b) | set(h)):
        old = b[key]["monthly"] if key in b else 0.0
        new = h[key]["monthly"] if key in h else 0.0
        if abs(new - old) > 0.005 or ((key in b) != (key in h) and (old or new)):
            rows.append((key, old, new))
    rows.sort(key=lambda r: -abs(r[2] - r[1]))
    delta = head["total_monthly"] - base["total_monthly"]
    out = [f"Cost change ({cur}/month): {base['source']}  ->  {head['source']}", ""]
    width = min(max([len(r[0]) for r in rows] + [8]), 90)
    out.append(f"{'RESOURCE':<{width}}  {'BEFORE':>12}  {'AFTER':>12}  {'CHANGE':>12}")
    for key, old, new in rows:
        tag = " (new)" if key not in b else " (removed)" if key not in h else ""
        out.append(f"{key[:width]:<{width}}  {old:>12,.2f}  {new:>12,.2f}  {new - old:>+12,.2f}{tag}")
    out += [
        "",
        f"Total: {base['total_monthly']:,.2f} -> {head['total_monthly']:,.2f} {cur}"
        f"  ({delta:+,.2f}/month, {delta * 12:+,.2f}/year)",
    ]
    return "\n".join(out) + "\n"
