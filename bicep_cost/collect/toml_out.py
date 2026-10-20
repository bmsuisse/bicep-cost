"""Write usage assumptions as a commented TOML file."""

import json
from collections import defaultdict
from datetime import UTC, datetime

from bicep_cost.collect.collectors import Finding


def _quote(text: str) -> str:
    return json.dumps(text)  # JSON string escaping is valid TOML basic-string escaping


def _number(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.4g}" if abs(value) < 1 else f"{value:.2f}"


def render_usage_toml(
    findings: list[Finding],
    notes: dict[tuple[str, str], list[str]],
    header: list[str],
    cost_currency: str | None,
) -> str:
    lines = [f"# {h}" for h in header]
    lines.append(
        f"# generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `bicep-cost collect`; re-run to refresh, edit freely"
    )
    if cost_currency:
        lines += ["", "[meta]", f'cost_currency = "{cost_currency}"  # currency of every monthly_cost below']
    grouped: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for f in findings:
        grouped[(f.section, f.name)].append(f)
    for key in sorted(grouped.keys() | notes.keys()):
        if key not in grouped:
            continue
        section, name = key
        lines += ["", f"[{section}.{_quote(name)}]"]
        lines += [f"# {n}" for n in notes.get(key, [])]
        for f in sorted(grouped[key], key=lambda f: f.key):
            lines.append(f"{f.key} = {_number(f.value)}  # {f.source}")
    return "\n".join(lines) + "\n"
