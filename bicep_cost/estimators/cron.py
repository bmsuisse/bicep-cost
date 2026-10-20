"""Cron schedules (KEDA cron scale rules) to active hours per month."""

MINUTES_PER_WEEK = 7 * 24 * 60
HOURS_PER_MONTH = 730.0


def _expand(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for raw_part in field.split(","):
        part = raw_part
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = int(part)
            end = hi if step != 1 else start
        values.update(range(start, end + 1, step))
    return values


def _events(expr: str) -> set[int]:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 cron fields, got {expr!r}")
    minute, hour, dom, month, dow = fields
    if dom != "*" or month != "*":
        raise ValueError("day-of-month / month cron fields are not supported")
    days = {d % 7 for d in _expand(dow, 0, 7)}
    return {d * 1440 + h * 60 + m for d in days for h in _expand(hour, 0, 23) for m in _expand(minute, 0, 59)}


def active_minutes(start: str, end: str) -> bytearray:
    """For every minute of a week: 1 if the rule is between a ``start`` trigger and the next ``end`` trigger."""
    starts, ends = _events(start), _events(end)
    active = False
    result = bytearray(MINUTES_PER_WEEK)
    for t in range(2 * MINUTES_PER_WEEK):
        m = t % MINUTES_PER_WEEK
        if m in ends:
            active = False
        if m in starts:
            active = True
        if t >= MINUTES_PER_WEEK and active:
            result[m] = 1
    return result


def _to_month_hours(minutes: float) -> float:
    return minutes / 60 * HOURS_PER_MONTH / 168


def active_hours_per_month(start: str, end: str) -> float:
    """Hours per month between each ``start`` trigger and the next ``end`` trigger."""
    return _to_month_hours(sum(active_minutes(start, end)))


def extra_replica_hours_per_month(rules: list[tuple[str, str, int]], baseline: int) -> float:
    """Replica-hours above ``baseline`` per month for ``(start, end, desired)`` cron rules.

    Overlapping rules do not add up: the autoscaler runs the highest replica count any active rule asks for.
    """
    schedules = [(active_minutes(start, end), desired) for start, end, desired in rules]
    total = 0
    for m in range(MINUTES_PER_WEEK):
        target = max([desired for sched, desired in schedules if sched[m]], default=0)
        total += max(target - baseline, 0)
    return _to_month_hours(total)
