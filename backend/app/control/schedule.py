"""Fox ESS mode schedule periods, and how tonight's force charge is written into them.

The inverter's schedule (Fox "Mode Scheduler", Open API v1) is a list of periods that repeat
every day. Each has a start and an end in local wall-clock time, both inclusive to the minute
(a period "to 05:30" is stored as ending 05:29), a work mode, and for a forced charge or
discharge the level to stop at (fdSoc, %) and the power (fdPwr, W). Periods can't cross midnight, so
a charge from 23:40 to 05:30 is written as two periods.

Only the periods that overlap tonight's charge are replaced; the user's other periods are kept
exactly as Fox returned them.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil
from typing import Any
from zoneinfo import ZoneInfo

FORCE_CHARGE = "ForceCharge"
FORCE_DISCHARGE = "ForceDischarge"
DEFAULT_MAX_GROUPS = 8
# Documented ranges for the battery SoC settings; the inverter's own ranges win when it
# reports them in the schedule's `properties`.
DEFAULT_RANGES = {
    "minsocongrid": (10.0, 100.0),
    "fdsoc": (10.0, 100.0),
    "fdpwr": (0.0, 30000.0),
    "maxsoc": (10.0, 100.0),
}


@dataclass(frozen=True)
class Period:
    """One schedule period. `start` and `end` are minutes after local midnight, inclusive."""

    start: int
    end: int
    work_mode: str
    fd_soc: float | None = None
    fd_pwr: float | None = None
    min_soc_on_grid: float | None = None
    # Every field as Fox returned it, so periods we don't change are written back untouched.
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_fox(cls, group: dict[str, Any]) -> "Period":
        return cls(
            start=int(group.get("startHour", 0)) * 60 + int(group.get("startMinute", 0)),
            end=int(group.get("endHour", 0)) * 60 + int(group.get("endMinute", 0)),
            work_mode=str(group.get("workMode", "")),
            fd_soc=_number(group.get("fdSoc")),
            fd_pwr=_number(group.get("fdPwr")),
            min_soc_on_grid=_number(group.get("minSocOnGrid")),
            raw=dict(group),
        )

    def to_fox(self) -> dict[str, Any]:
        group = dict(self.raw)
        group.update(
            enable=1,
            startHour=self.start // 60,
            startMinute=self.start % 60,
            endHour=self.end // 60,
            endMinute=self.end % 60,
            workMode=self.work_mode,
        )
        for key, value in (
            ("fdSoc", self.fd_soc),
            ("fdPwr", self.fd_pwr),
            ("minSocOnGrid", self.min_soc_on_grid),
        ):
            if value is not None:
                group[key] = int(round(value))
        return group

    def overlaps(self, other: "Period") -> bool:
        return self.start <= other.end and other.start <= self.end

    def same_slot(self, other: "Period") -> bool:
        return (self.start, self.end, self.work_mode) == (other.start, other.end, other.work_mode)

    def label(self) -> str:
        return f"{_clock(self.start)}–{_clock(self.end + 1)}"


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _clock(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Schedule:
    """The inverter's schedule as read: whether it is switched on, and its active periods."""

    enabled: bool
    periods: list[Period]
    max_groups: int
    ranges: dict[str, tuple[float, float]]
    # Settings the inverter lists in `properties` (lower case), e.g. "maxsoc" when it has one.
    reported: frozenset[str] = frozenset()

    @classmethod
    def from_fox(cls, result: dict[str, Any]) -> "Schedule":
        groups = [g for g in result.get("groups") or [] if g.get("enable", 1)]
        periods = sorted((Period.from_fox(g) for g in groups), key=lambda p: p.start)
        ranges = dict(DEFAULT_RANGES)
        properties = result.get("properties") or {}
        for key, spec in properties.items():
            bounds = (spec or {}).get("range") or {}
            low, high = _number(bounds.get("min")), _number(bounds.get("max"))
            if low is not None and high is not None and low <= high:
                ranges[key.lower()] = (low, high)
        max_groups = result.get("maxGroupCount")
        return cls(
            enabled=bool(result.get("enable", 0)),
            periods=periods,
            max_groups=max_groups
            if isinstance(max_groups, int) and max_groups > 0
            else DEFAULT_MAX_GROUPS,
            ranges=ranges,
            reported=frozenset(k.lower() for k in properties),
        )

    def to_snapshot(self) -> dict[str, Any]:
        return {"enable": int(self.enabled), "groups": [p.to_fox() for p in self.periods]}

    def clamp(self, key: str, value: float) -> float:
        low, high = self.ranges.get(key, DEFAULT_RANGES[key])
        return min(max(value, low), high)


def charge_periods(
    start: datetime,
    stop: datetime,
    tz: str,
    fd_soc: float,
    fd_pwr: float,
    min_soc: float,
    work_mode: str = FORCE_CHARGE,
) -> list[Period]:
    """Force charge (or discharge) periods covering [start, stop), split at local midnight."""
    zone = ZoneInfo(tz)
    local_start = start.astimezone(zone)
    # Fox ends are inclusive to the minute; round a stop with seconds up to the next minute.
    local_stop = stop.astimezone(zone)
    if local_stop.second or local_stop.microsecond:
        local_stop = local_stop.replace(second=0, microsecond=0) + timedelta(minutes=1)
    minutes = ceil((local_stop - local_start).total_seconds() / 60)
    if minutes <= 0:
        return []
    if minutes >= 24 * 60:
        raise ValueError("A forced charge can't be a whole day or longer")
    first = local_start.hour * 60 + local_start.minute
    last = first + minutes - 1
    spans = [(first, last)] if last < 24 * 60 else [(first, 24 * 60 - 1), (0, last - 24 * 60)]
    return [
        Period(s, e, work_mode, fd_soc=fd_soc, fd_pwr=fd_pwr, min_soc_on_grid=min_soc)
        for s, e in spans
    ]


def merge(existing: list[Period], added: list[Period]) -> tuple[list[Period], list[Period]]:
    """The schedule with `added` in place of whatever overlaps it, and what was removed."""
    removed = [p for p in existing if any(p.overlaps(a) for a in added)]
    kept = [p for p in existing if not any(p is r for r in removed)]
    return sorted(kept + added, key=lambda p: p.start), removed


def without(existing: list[Period], ours: list[Period]) -> tuple[list[Period], list[Period]]:
    """The schedule with the periods we wrote taken out, and what was taken out."""
    removed = [p for p in existing if any(p.same_slot(o) for o in ours)]
    return [p for p in existing if not any(p is r for r in removed)], removed


def fingerprint(periods: list[Period]) -> str:
    """Identifies an exact set of periods to write, so an apply writes what was previewed."""
    payload = json.dumps([p.to_fox() for p in periods], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def matches(written: list[Period], read_back: list[Period]) -> list[str]:
    """Differences between what was written and what the inverter reports, in words."""
    problems = []
    for w in written:
        found = next((r for r in read_back if r.same_slot(w)), None)
        if found is None:
            problems.append(f"The {w.work_mode} period {w.label()} is missing.")
            continue
        for name, a, b in (
            ("charge level", w.fd_soc, found.fd_soc),
            ("charge power", w.fd_pwr, found.fd_pwr),
            ("minimum charge", w.min_soc_on_grid, found.min_soc_on_grid),
        ):
            if a is not None and (b is None or abs(a - b) > 0.5):
                problems.append(f"The period {w.label()} reads back {name} {b}, not {a:g}.")
    extra = [r for r in read_back if not any(r.same_slot(w) for w in written)]
    for r in extra:
        problems.append(f"The inverter also has a {r.work_mode} period {r.label()}.")
    return problems
