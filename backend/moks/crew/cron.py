"""Five-field cron expressions for CrewAI console schedules, always in UTC.

minute hour day-of-month month day-of-week, each `*`, a number, a range `a-b`, a step `*/n` or `a-b/n`, or a list of
those; months and weekdays also by name (jan, mon). `@hourly`, `@daily`, `@weekly` and `@monthly` are shorthands.
As in cron, when both day fields are restricted a day matches either one. Seconds, `L`, `W` and `#` are not supported.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
DAYS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
FIELDS = (("minute", 0, 59, {}), ("hour", 0, 23, {}), ("day of month", 1, 31, {}), ("month", 1, 12, MONTHS), ("day of week", 0, 7, DAYS))
MIN_INTERVAL_MINUTES = 15  # each run is a model-backed investigation; no schedule may start one more often


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_any: bool
    dow_any: bool

    def day_matches(self, t: datetime) -> bool:
        dom, dow = t.day in self.doms, (t.isoweekday() % 7) in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow
        if self.dow_any:
            return dom
        return dom or dow


def _value(text: str, lo: int, hi: int, names: dict[str, int], what: str) -> int:
    v = names.get(text.lower())
    if v is None:
        if not text.isdigit():
            raise CronError(f"{what}: {text!r} is not a number")
        v = int(text)
    if not lo <= v <= hi:
        raise CronError(f"{what}: {v} is outside {lo}-{hi}")
    return v


def _field(text: str, lo: int, hi: int, names: dict[str, int], what: str) -> tuple[frozenset[int], bool]:
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{what}: empty list item")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"{what}: step {step_text!r} must be a positive number")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            start, end = _value(a, lo, hi, names, what), _value(b, lo, hi, names, what)
            if start > end:
                raise CronError(f"{what}: range {base} runs backwards")
        else:
            start = _value(base, lo, hi, names, what)
            end = hi if step_text else start
        out.update(range(start, end + 1, step))
    return frozenset(out), text == "*"


def parse(expr: str) -> Cron:
    text = " ".join((expr or "").split())
    text = MACROS.get(text.lower(), text)
    parts = text.split(" ")
    if len(parts) != 5:
        raise CronError(f"expected five fields (minute hour day-of-month month day-of-week), got {len(parts) if text else 0}")
    sets = [_field(p, lo, hi, names, what) for p, (what, lo, hi, names) in zip(parts, FIELDS)]
    dows = frozenset(d % 7 for d in sets[4][0])  # 7 is Sunday too
    return Cron(" ".join(parts), sets[0][0], sets[1][0], sets[2][0], sets[3][0], dows, sets[2][1], sets[4][1])


def next_after(cron: Cron, after: datetime) -> datetime | None:
    """The first time strictly after `after` (UTC, whole minutes) that the expression matches, within five years."""
    t = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = t + timedelta(days=366 * 5)
    while t < limit:
        if t.month not in cron.months:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            continue
        if not cron.day_matches(t):
            t = t.replace(hour=0, minute=0) + timedelta(days=1)
            continue
        if t.hour not in cron.hours:
            t = t.replace(minute=0) + timedelta(hours=1)
            continue
        if t.minute not in cron.minutes:
            t += timedelta(minutes=1)
            continue
        return t
    return None


def upcoming(cron: Cron, after: datetime, count: int = 5) -> list[datetime]:
    out: list[datetime] = []
    t = after
    while len(out) < count:
        n = next_after(cron, t)
        if n is None:
            break
        out.append(n)
        t = n
    return out


def validate(expr: str, now: datetime | None = None) -> tuple[Cron, list[datetime]]:
    """Parses and checks a schedule's expression. Raises CronError for one that never fires or fires too often."""
    cron = parse(expr)
    now = now or datetime.now(timezone.utc)
    times = upcoming(cron, now, 25)
    if not times:
        raise CronError("never fires (no matching date in the next five years)")
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    if gaps and min(gaps) < MIN_INTERVAL_MINUTES:
        raise CronError(f"fires every {int(min(gaps))} minutes; a schedule may start a run at most every {MIN_INTERVAL_MINUTES} minutes")
    return cron, times[:5]
