"""Right-sizing verdicts: thresholds, hourly metric statistics and the decision rules.

Pure logic, no Azure access. A resource type's handler (`rightsize_types`) turns live metrics into a `Case`; `decide`
turns the case into one verdict and, only when the evidence is unambiguous, a priced proposal.

Verdicts:

* ``over``: low load on every sizing metric, a smaller step of the same family would stay comfortable, and it is
  cheaper. Carries a proposal.
* ``under``: saturation plus either an independent second signal or load that is sustained on its own (the hourly
  *average* saturated for a large share of the whole window). Carries a proposal when one larger step fixes it.
* ``watch``: something is off but not enough to propose (one signal only, thin data, contradictory data, unpriced).
* ``ok``: nothing to do, or nothing that can safely be done.
* ``no-data``: Azure Monitor returned nothing usable.
"""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from bicep_cost.advisor import AdvisorRec

# A resource is over-provisioned when every sizing metric stays at or below these (percent of capacity) ...
OVER_P95 = 40.0
OVER_MAX = 60.0
# ... and the smaller step it would move to must still keep the predicted load at or below these.
TARGET_P95 = 70.0
TARGET_MAX = 85.0
# Under-provisioned: p95 of the hourly maxima at or above UNDER_P95, or at least HOT_SHARE of the hours at or above
# UNDER_MAX (a single spike to 100% does not count).
UNDER_P95 = 85.0
UNDER_MAX = 95.0
HOT_SHARE = 0.02
# Sustained saturation corroborates itself. An hourly maximum is the busiest minute of the hour, so a p95 of 100% can
# be one minute of a batch job; the hourly *average* at or above SUSTAINED_AVG means most of the hour was saturated.
# A metric whose average was that high in at least SUSTAINED_SHARE of the hours of the full window (about 6 h a day,
# every hour returned) needs no second signal: a nightly batch window or a handful of spikes stay below it.
SUSTAINED_AVG = 90.0
SUSTAINED_SHARE = 0.25
MIN_COVERAGE = 0.9  # share of the expected hourly points that must have come back
MIN_DAYS = 14
FULL_CYCLE_DAYS = 30  # shorter windows can hide a monthly cycle: no `over` proposal
PINNED_SHARE = 0.25  # share of hours a scalable resource sits at its maximum replica count
BURSTY_MEAN = 25.0  # a saturated resource whose mean load is below this is flagged as bursty
MIN_SAVING = 0.01  # a proposal must save at least a cent a month

VERDICTS = ("over", "under", "watch", "ok", "no-data")


@dataclass(frozen=True)
class Stats:
    """Statistics of one metric's hourly points; ``values`` are the hourly maxima that came back."""

    name: str
    unit: str
    expected: int
    values: tuple[float, ...]
    mean: float
    group: str = ""  # capacity this metric scales with ("cpu", "memory", "dtu"); empty: shown, not used to size
    trigger: bool = False  # a saturated value of this metric can make the resource `under`
    required: bool = False  # shown, not sized on, but a verdict needs its coverage (it holds a contradicting signal)
    averages: tuple[float, ...] = ()  # the hourly averages that came back (empty when not read)
    gaps: tuple[int, int, int] | None = None  # (before the first point, between points, after the last) in hours

    @property
    def hours(self) -> int:
        return len(self.values)

    @property
    def coverage(self) -> float:
        return min(1.0, self.hours / self.expected) if self.expected else 0.0

    @property
    def p95(self) -> float:
        if not self.values:
            return 0.0
        ordered = sorted(self.values)
        return ordered[max(math.ceil(0.95 * len(ordered)) - 1, 0)]

    @property
    def max(self) -> float:
        return max(self.values, default=0.0)

    def share_at_least(self, limit: float) -> float:
        return sum(1 for v in self.values if v >= limit) / len(self.values) if self.values else 0.0

    @property
    def hot(self) -> bool:
        return self.p95 >= UNDER_P95 or self.share_at_least(UNDER_MAX) >= HOT_SHARE

    def avg_hours_at_least(self, limit: float) -> int:
        return sum(1 for v in self.averages if v >= limit)

    @property
    def peak_average(self) -> float:
        return max(self.averages, default=0.0)

    @property
    def sustained(self) -> bool:
        """The hourly average was saturated in a large share of the expected hours, and every hour came back."""
        return (
            bool(self.expected)
            and self.hours >= self.expected
            and self.avg_hours_at_least(SUSTAINED_AVG) / self.expected >= SUSTAINED_SHARE
        )

    def missing(self) -> str:
        """Where the hours that did not come back are (empty when nothing is missing or the positions are unknown)."""
        if self.gaps is None or self.hours >= self.expected:
            return ""
        before, between, after = self.gaps
        if before and not between and not after:
            return (
                f"no points in the first {before} h of the window, complete after that: the resource is younger than "
                "the window or reported nothing before"
            )
        if after and not before and not between:
            return f"no points in the last {after} h of the window: it stopped reporting"
        where = ((before, "before the first point"), (between, "between points"), (after, "after the last point"))
        return "missing: " + ", ".join(f"{n} h {w}" for n, w in where if n)


def build_stats(
    name: str,
    maxima: list[float | None],
    averages: list[float | None] | None,
    expected: int,
    *,
    unit: str = "%",
    group: str = "",
    trigger: bool = False,
    required: bool = False,
) -> Stats:
    values = tuple(v for v in maxima if v is not None)
    avgs = tuple(v for v in (averages or []) if v is not None)
    means = list(avgs) or list(values)
    return Stats(
        name,
        unit,
        expected,
        values,
        sum(means) / len(means) if means else 0.0,
        group,
        trigger,
        required,
        avgs,
        _gaps(maxima) if len(maxima) == expected else None,
    )


def _gaps(series: list[float | None]) -> tuple[int, int, int] | None:
    present = [i for i, v in enumerate(series) if v is not None]
    if not present:
        return None
    before, after = present[0], len(series) - 1 - present[-1]
    return before, (present[-1] - present[0] + 1) - len(present), after


@dataclass(frozen=True)
class Signal:
    """An independent sign of pressure (restarts, replicas pinned at their maximum, connections at the limit, ...)."""

    name: str
    text: str


@dataclass(frozen=True, eq=False)
class Candidate:
    """One concrete change: how it scales each capacity group, what it would cost, how risky it is."""

    change: str  # "P1v3 -> P0v3"
    target: str  # "P0v3"
    factors: Mapping[str, float]  # capacity group -> multiplier of the utilisation (old capacity / new capacity)
    price: Callable[[], float | None]  # monthly list price after the change; None when it cannot be priced
    risk: str
    rollback: str


@dataclass
class Case:
    resource_id: str
    name: str
    type: str
    resource_group: str
    sku: str
    window_days: int
    metrics: list[Stats]
    current_price: Callable[[], float | None]
    down: list[Candidate] = field(default_factory=list)  # smaller steps, nearest first
    up: list[Candidate] = field(default_factory=list)  # larger steps, nearest first
    signals: list[Signal] = field(default_factory=list)
    advisor: list[AdvisorRec] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    exempt_over: str | None = None  # why the resource must not be shrunk (elastic, replica, ...)
    no_down: str = "no smaller size in the same family"
    no_up: str = "no larger size in the same family"
    insufficient: str | None = None  # data exists but is too little to judge (e.g. a scale-to-zero app that rarely ran)


@dataclass(frozen=True)
class Proposal:
    kind: str  # over | under
    change: str
    target: str
    current_monthly: float
    target_monthly: float
    predicted: dict[str, tuple[float, float]]  # metric -> (p95, max) after the change
    risk: str
    rollback: str

    @property
    def delta(self) -> float:
        """Monthly change in cost: negative saves money."""
        return self.target_monthly - self.current_monthly

    @property
    def saving(self) -> float:
        return -self.delta


@dataclass
class Assessment:
    resource_id: str
    name: str
    type: str
    resource_group: str
    sku: str
    verdict: str
    reason: str
    window_days: int
    metrics: list[Stats]
    signals: list[Signal]
    advisor: list[AdvisorRec]
    proposal: Proposal | None = None
    current_monthly: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        sizing = [m.coverage for m in self.metrics if m.group]
        return min(sizing) if sizing else 0.0


def predict(metrics: list[Stats], factors: Mapping[str, float]) -> dict[str, tuple[float, float]]:
    return {m.name: (m.p95 * factors.get(m.group, 1.0), m.max * factors.get(m.group, 1.0)) for m in metrics if m.group}


def comfortable(predicted: dict[str, tuple[float, float]]) -> bool:
    return all(p95 <= TARGET_P95 and peak <= TARGET_MAX for p95, peak in predicted.values())


def _fmt(stats: Stats) -> str:
    return f"{stats.name} p95 {stats.p95:.0f}{stats.unit} / max {stats.max:.0f}{stats.unit}"


def _worst(predicted: dict[str, tuple[float, float]]) -> str:
    name, (p95, peak) = max(predicted.items(), key=lambda kv: kv[1][0])
    return f"{name} p95 {p95:.0f}% / max {peak:.0f}%"


@dataclass
class _Decision:
    case: Case
    signals: list[Signal]
    extra_notes: list[str] = field(default_factory=list)

    def done(
        self, verdict: str, reason: str, proposal: Proposal | None = None, current: float | None = None
    ) -> Assessment:
        c = self.case
        return Assessment(
            c.resource_id,
            c.name,
            c.type,
            c.resource_group,
            c.sku,
            verdict,
            reason,
            c.window_days,
            c.metrics,
            self.signals,
            c.advisor,
            proposal,
            current,
            [*c.notes, *self.extra_notes],
        )


def decide(case: Case) -> Assessment:
    sizing = [m for m in case.metrics if m.group]
    advisor_perf = [r for r in case.advisor if r.category == "Performance"]
    d = _Decision(
        case, [*case.signals, *(Signal("Advisor", f"Advisor Performance: {r.problem}") for r in advisor_perf)]
    )
    if not sizing or all(m.hours == 0 for m in sizing):
        return d.done("no-data", case.insufficient or "Azure Monitor returned no data for the sizing metrics")
    thin = [m for m in case.metrics if (m.group or m.required) and m.coverage < MIN_COVERAGE]
    if thin:
        worst = min(thin, key=lambda m: m.coverage)
        why = f"; {worst.missing()}" if worst.missing() else ""
        return d.done(
            "watch",
            f"{worst.name}: only {worst.hours} of {worst.expected} hourly points ({worst.coverage:.0%}){why}; "
            f"{MIN_COVERAGE:.0%} needed",
        )
    if case.insufficient:
        return d.done("watch", case.insufficient)
    hot = [m for m in sizing if m.trigger and m.hot]
    if hot:
        sustained = [m for m in hot if case.window_days >= FULL_CYCLE_DAYS and m.sustained]
        if not d.signals and not sustained:
            return _watch_hot(d, hot)
        return _under(d, hot, sustained)
    if all(m.p95 <= OVER_P95 and m.max <= OVER_MAX for m in sizing):
        return _over(d, sizing)
    worst = max(sizing, key=lambda m: m.p95)
    limit = next((m for m in sizing if m.hot), None)
    if limit is not None:  # saturated, but not a metric this rule sizes on: never "nothing to do"
        return d.done(
            "watch",
            f"{_fmt(limit)}: at its limit, but {limit.name} is not a metric a size is proposed from "
            "(its limit does not scale with the size); look at it by hand",
        )
    return d.done(
        "ok", f"{_fmt(worst)}: between the over ({OVER_P95:.0f}/{OVER_MAX:.0f}%) and under thresholds, nothing to do"
    )


def _character(m: Stats) -> str:
    """How the saturation looks: the hourly *average* shows whether it was sustained or only peaks inside the hours."""
    hours = m.avg_hours_at_least(SUSTAINED_AVG)
    return (
        f"hourly average at or above {SUSTAINED_AVG:.0f}% in {hours} of {m.expected} h "
        f"({hours / max(m.expected, 1):.0%}), highest {m.peak_average:.0f}%, mean {m.mean:.0f}%"
    )


def _bursty_note(m: Stats) -> str:
    peak_hours = sum(1 for v in m.values if v >= UNDER_MAX)
    return (
        f"bursty load: {m.name} is at or above {UNDER_MAX:.0f}% in {peak_hours} of {m.hours} h but averages "
        f"{m.mean:.0f}%; a larger size is paid for every hour, so look at the peaks first (scale up only around the "
        "busy hours, tune what runs then, serverless or autoscale where the type has it)"
    )


def _watch_hot(d: _Decision, hot: list[Stats]) -> Assessment:
    case = d.case
    what = "; ".join(f"{_fmt(m)} [{_character(m)}]" if m.averages else _fmt(m) for m in hot)
    complete = case.window_days >= FULL_CYCLE_DAYS and all(m.hours >= m.expected for m in hot)
    proof = (
        f"not sustained (an hourly average of {SUSTAINED_AVG:.0f}% in {SUSTAINED_SHARE:.0%} of the hours would be)"
        if complete
        else f"a complete {FULL_CYCLE_DAYS} d window of every hour is needed to show it is sustained"
    )
    d.extra_notes += [_bursty_note(m) for m in hot if m.averages and m.mean < BURSTY_MEAN]
    return d.done("watch", f"{what}: {proof}; no second signal (restarts, pinned replicas, limits, Advisor)")


def _under(d: _Decision, hot: list[Stats], sustained: list[Stats]) -> Assessment:
    case = d.case
    evidence = [
        *(s.text for s in d.signals),
        *(f"sustained: {m.name} {_character(m)}" for m in sustained),
    ]
    why = f"{', '.join(_fmt(m) for m in hot)} and {'; '.join(evidence)}"
    d.extra_notes += [_bursty_note(m) for m in hot if m.mean < BURSTY_MEAN]
    sizing = [m for m in case.metrics if m.group]
    for cand in case.up:
        predicted = predict(sizing, cand.factors)
        if not comfortable(predicted):
            continue
        current, target = case.current_price(), cand.price()
        if current is None or target is None:
            return d.done("under", f"{why}; {cand.target} could not be priced, so no proposal", current=current)
        proposal = Proposal("under", cand.change, cand.target, current, target, predicted, cand.risk, cand.rollback)
        return d.done("under", why, proposal, current)
    limit = f"no single larger step brings it below {TARGET_P95:.0f}%/{TARGET_MAX:.0f}%" if case.up else case.no_up
    return d.done("under", f"{why}; {limit}")


def _over(d: _Decision, sizing: list[Stats]) -> Assessment:
    case = d.case
    low = "; ".join(_fmt(m) for m in sizing)
    if case.exempt_over:
        return d.done("ok", f"{low}: {case.exempt_over}")
    if d.signals:
        return d.done("watch", f"{low}, yet {'; '.join(s.text for s in d.signals)}: contradictory, no proposal")
    if case.window_days < FULL_CYCLE_DAYS:
        return d.done(
            "watch", f"{low}: a {case.window_days} d window cannot rule out a monthly cycle (re-run with --days 30)"
        )
    if not case.down:
        return d.done("ok", f"{low}: {case.no_down}")
    fitting = [(c, predict(sizing, c.factors)) for c in case.down]
    fitting = [(c, p) for c, p in fitting if comfortable(p)]
    if not fitting:
        nearest = predict(sizing, case.down[0].factors)
        return d.done(
            "ok",
            f"{low}: {case.down[0].target} would reach {_worst(nearest)}, above the "
            f"{TARGET_P95:.0f}/{TARGET_MAX:.0f}% limits",
        )
    current = case.current_price()
    if current is None:
        return d.done("watch", f"{low}: the current configuration could not be priced, so no saving can be shown")
    priced: list[tuple[Candidate, dict[str, tuple[float, float]], float]] = []
    for cand, predicted in fitting:
        target = cand.price()
        if target is not None:
            priced.append((cand, predicted, target))
    if not priced:
        return d.done(
            "watch", f"{low}: {fitting[0][0].target} could not be priced, so no saving can be shown", current=current
        )
    cand, predicted, target = max(priced, key=lambda t: current - t[2])
    if current - target < MIN_SAVING:
        return d.done("ok", f"{low}: {cand.target} would not be cheaper", current=current)
    proposal = Proposal("over", cand.change, cand.target, current, target, predicted, cand.risk, cand.rollback)
    return d.done("over", f"{low}; {cand.target} would reach {_worst(predicted)}", proposal, current)
