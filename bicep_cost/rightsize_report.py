"""Render a right-sizing report as text, Markdown or JSON."""

import json
from dataclasses import dataclass

from bicep_cost.rightsize import (
    FULL_CYCLE_DAYS,
    HOT_SHARE,
    MIN_COVERAGE,
    MIN_DAYS,
    OVER_MAX,
    OVER_P95,
    PINNED_SHARE,
    TARGET_MAX,
    TARGET_P95,
    UNDER_MAX,
    UNDER_P95,
    VERDICTS,
    Assessment,
    Stats,
)
from bicep_cost.rightsize_run import Report

NOT_COVERED_SHOWN = 12
THRESHOLDS = {
    "over_p95": OVER_P95,
    "over_max": OVER_MAX,
    "target_p95": TARGET_P95,
    "target_max": TARGET_MAX,
    "under_p95": UNDER_P95,
    "under_max": UNDER_MAX,
    "hot_share": HOT_SHARE,
    "min_coverage": MIN_COVERAGE,
    "min_days": MIN_DAYS,
    "full_cycle_days": FULL_CYCLE_DAYS,
    "pinned_share": PINNED_SHARE,
}


@dataclass(frozen=True)
class Selection:
    shown: list[Assessment]
    below_min_saving: int


def select(report: Report, only: str | None = None, min_saving: float = 0.0) -> Selection:
    """The assessments to show: filtered by verdict and `over` proposals below the minimum saving, money first."""
    shown: list[Assessment] = []
    below = 0
    for a in report.assessments:
        if only and a.verdict != only:
            continue
        if a.verdict == "over" and a.proposal is not None and a.proposal.saving < min_saving:
            below += 1
            continue
        shown.append(a)

    def key(a: Assessment) -> tuple[int, float, str]:
        money = abs(a.proposal.delta) if a.proposal else 0.0
        return VERDICTS.index(a.verdict), -money, a.name.lower()

    proposals = sorted((a for a in shown if a.proposal), key=lambda a: -abs(a.proposal.delta if a.proposal else 0.0))
    rest = sorted((a for a in shown if not a.proposal), key=key)
    return Selection(proposals + rest, below)


def _count(report: Report) -> dict[str, int]:
    return {v: sum(1 for a in report.assessments if a.verdict == v) for v in VERDICTS}


def _money(value: float, currency: str) -> str:
    return f"{value:,.2f} {currency}"


def _num(stats: Stats, value: float) -> str:
    return f"{value:.1f}{stats.unit}" if stats.unit else f"{value:g}"


def _metric_line(s: Stats) -> str:
    cover = f"{s.hours}/{s.expected} h ({s.coverage:.0%})"
    if not s.hours:
        return f"{s.name:<32} no data   {cover}"
    return f"{s.name:<32} p95 {_num(s, s.p95):>7}  max {_num(s, s.max):>7}  mean {_num(s, s.mean):>7}  {cover}"


def _window(report: Report) -> str:
    return f"{report.window_days} d ending {report.end:%Y-%m-%d %H:%M} UTC ({report.window_days * 24} hourly points)"


def _headline(report: Report) -> list[str]:
    n = _count(report)
    over = sum(a.proposal.saving for a in report.assessments if a.proposal and a.verdict == "over")
    under = sum(a.proposal.delta for a in report.assessments if a.proposal and a.verdict == "under")
    scope = f"subscription {report.subscription_id}" + (
        f" (resource groups: {', '.join(report.resource_groups)})" if report.resource_groups else ""
    )
    return [
        f"bicep-cost rightsize: {scope}",
        f"window {_window(report)}; prices: {report.price_source} ({report.currency})",
        "verdicts: " + ", ".join(f"{n[v]} {v}" for v in VERDICTS),
        f"over proposals would save {_money(over, report.currency)}/month; "
        f"under proposals would cost {_money(under, report.currency)}/month more",
    ]


def _proposal_lines(a: Assessment, currency: str) -> list[str]:
    p = a.proposal
    if p is None:
        return []
    sign = "saves" if p.delta < 0 else "costs"
    return [
        f"    proposal: {p.change}  ({_money(p.current_monthly, currency)} -> "
        f"{_money(p.target_monthly, currency)}/month, {sign} {_money(abs(p.delta), currency)}/month, list price)",
        "    predicted after the change: "
        + "; ".join(f"{k} p95 {v[0]:.0f}% / max {v[1]:.0f}%" for k, v in p.predicted.items()),
        f"    risk: {p.risk}",
        f"    rollback: {p.rollback}",
    ]


def _evidence_lines(a: Assessment) -> list[str]:
    lines = [f"    {_metric_line(s)}" for s in a.metrics]
    lines += [f"    note: {n}" for n in a.notes]
    lines += [f"    signal: {s.text}" for s in a.signals if s.name != "Advisor"]
    lines += [f"    Advisor {r.category}/{r.impact}: {r.problem}" for r in a.advisor]
    return lines


def render_text(report: Report, sel: Selection, detail: bool = False) -> str:
    out = _headline(report)
    if sel.below_min_saving:
        out.append(f"{sel.below_min_saving} over proposal(s) saving less than --min-saving are hidden")
    proposals = [a for a in sel.shown if a.proposal]
    if proposals:
        out += ["", "PROPOSALS (ranked by monthly money)"]
    for i, a in enumerate(proposals, 1):
        out.append(f"{i:>2}. {a.verdict.upper():<5} {a.name}  [{a.type}, {a.resource_group}]  {a.sku}")
        out.append(f"    why: {a.reason}")
        out += _proposal_lines(a, report.currency)
        out += _evidence_lines(a)
    for verdict, title in (
        ("under", "UNDER without a proposal"),
        ("watch", "WATCH (not sure enough to propose)"),
        ("no-data", "NO DATA"),
        ("ok", "OK"),
    ):
        group = [a for a in sel.shown if a.verdict == verdict and not a.proposal]
        if not group:
            continue
        out += ["", f"{title}: {len(group)}"]
        for a in group:
            out.append(f"  {a.name}  [{a.type}, {a.resource_group}]  {a.sku}: {a.reason}")
            if detail or verdict in ("under", "watch"):
                out += _evidence_lines(a)
    if report.skipped:
        out += ["", f"NOT ASSESSED ({len(report.skipped)} resource(s) of a covered type)"]
        out += [f"  {s.name}  [{s.type}, {s.resource_group}]: {s.reason}" for s in report.skipped]
    if report.not_covered:
        shown = list(report.not_covered.items())[:NOT_COVERED_SHOWN]
        rest = len(report.not_covered) - len(shown)
        listing = ", ".join(f"{t} x{n}" for t, n in shown) + (f", and {rest} more types" if rest > 0 else "")
        out += ["", f"NOT COVERED (no right-sizing rules; never guessed): {listing}"]
    out += [f"warning: {w}" for w in report.warnings]
    return "\n".join(out) + "\n"


def render_markdown(report: Report, sel: Selection) -> str:
    out = ["## bicep-cost rightsize", ""]
    out += [f"- {line}" for line in _headline(report)[1:]]
    proposals = [a for a in sel.shown if a.proposal]
    if proposals:
        out += [
            "",
            "### Proposals",
            "",
            f"| # | Verdict | Resource | Change | Monthly ({report.currency}) | Evidence |",
            "|---:|---|---|---|---:|---|",
        ]
        for i, a in enumerate(proposals, 1):
            p = a.proposal
            assert p is not None
            evidence = "<br>".join(
                f"{s.name} p95 {_num(s, s.p95)} / max {_num(s, s.max)}" for s in a.metrics if s.group
            )
            cover = min(s.coverage for s in a.metrics if s.group)
            out.append(
                f"| {i} | {a.verdict} | `{a.name}` ({a.type}, {a.resource_group}) | {p.change} | "
                f"{p.delta:+,.2f} | {evidence} ({a.window_days} d, {cover:.0%} of hours) |"
            )
        for a in proposals:
            p = a.proposal
            assert p is not None
            out += ["", f"**{a.name}**: {a.reason}", "", f"- risk: {p.risk}", f"- rollback: `{p.rollback}`"]
            out += [f"- Advisor {r.category}/{r.impact}: {r.problem}" for r in a.advisor]
    others = [a for a in sel.shown if not a.proposal and a.verdict != "ok"]
    if others:
        out += ["", "### Not sure enough to propose", "", "| Verdict | Resource | SKU | Why |", "|---|---|---|---|"]
        out += [f"| {a.verdict} | `{a.name}` ({a.resource_group}) | {a.sku} | {a.reason} |" for a in others]
    oks = [a for a in sel.shown if a.verdict == "ok"]
    if oks:
        out += ["", f"### OK ({len(oks)})", ""]
        out += [f"- `{a.name}` {a.sku}: {a.reason}" for a in oks]
    if report.skipped:
        out += ["", f"### Not assessed ({len(report.skipped)})", ""]
        out += [f"- `{s.name}` ({s.type}, {s.resource_group}): {s.reason}" for s in report.skipped]
    if report.warnings:
        out += ["", "### Warnings", ""]
        out += [f"- {w}" for w in report.warnings]
    if report.not_covered:
        out += [
            "",
            "### Not covered",
            "",
            ", ".join(f"`{t}` x{n}" for t, n in list(report.not_covered.items())[:NOT_COVERED_SHOWN]),
        ]
    return "\n".join(out) + "\n"


def _stats_json(s: Stats) -> dict[str, object]:
    return {
        "name": s.name,
        "unit": s.unit,
        "sizing": bool(s.group),
        "hours": s.hours,
        "expected_hours": s.expected,
        "coverage": round(s.coverage, 4),
        "p95": round(s.p95, 2),
        "max": round(s.max, 2),
        "mean": round(s.mean, 2),
    }


def _assessment_json(a: Assessment, currency: str) -> dict[str, object]:
    p = a.proposal
    return {
        "id": a.resource_id,
        "name": a.name,
        "type": a.type,
        "resource_group": a.resource_group,
        "sku": a.sku,
        "verdict": a.verdict,
        "reason": a.reason,
        "window_days": a.window_days,
        "coverage": round(a.coverage, 4),
        "current_monthly": None if a.current_monthly is None else round(a.current_monthly, 2),
        "metrics": [_stats_json(s) for s in a.metrics],
        "signals": [s.text for s in a.signals],
        "advisor": [{"category": r.category, "impact": r.impact, "problem": r.problem} for r in a.advisor],
        "notes": a.notes,
        "proposal": None
        if p is None
        else {
            "kind": p.kind,
            "change": p.change,
            "target": p.target,
            "currency": currency,
            "current_monthly": round(p.current_monthly, 2),
            "target_monthly": round(p.target_monthly, 2),
            "monthly_delta": round(p.delta, 2),
            "predicted": {k: {"p95": round(v[0], 2), "max": round(v[1], 2)} for k, v in p.predicted.items()},
            "risk": p.risk,
            "rollback": p.rollback,
        },
    }


def render_json(report: Report, sel: Selection) -> str:
    n = _count(report)
    payload = {
        "subscription_id": report.subscription_id,
        "resource_groups": list(report.resource_groups),
        "window": {
            "days": report.window_days,
            "end": report.end.isoformat(),
            "expected_hours": report.window_days * 24,
            "granularity": "PT1H",
        },
        "currency": report.currency,
        "price_source": report.price_source,
        "thresholds": THRESHOLDS,
        "summary": {
            **n,
            "over_monthly_saving": round(
                sum(a.proposal.saving for a in report.assessments if a.proposal and a.verdict == "over"), 2
            ),
            "under_monthly_increase": round(
                sum(a.proposal.delta for a in report.assessments if a.proposal and a.verdict == "under"), 2
            ),
            "hidden_below_min_saving": sel.below_min_saving,
        },
        "resources": [_assessment_json(a, report.currency) for a in sel.shown],
        "not_assessed": [
            {"name": s.name, "type": s.type, "resource_group": s.resource_group, "reason": s.reason}
            for s in report.skipped
        ],
        "not_covered": report.not_covered,
        "warnings": report.warnings,
    }
    return json.dumps(payload, indent=2) + "\n"
