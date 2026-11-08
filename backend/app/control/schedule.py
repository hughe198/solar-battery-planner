"""Fox ESS mode schedule periods, and how tonight's force charge is written into them.

The inverter's schedule (Fox "Mode Scheduler", Open API Scheduler V3) is a list of periods that
repeat every day. Each has a start and an end in local wall-clock time, both inclusive to the
minute (a period "to 05:30" is stored as ending 05:29), a work mode, and its settings in
`extraParam`: for a forced charge or discharge the level to stop at (fdSoc, %) and the power
(fdPwr, W). Periods can't cross midnight, so a charge from 23:40 to 05:30 is written as two
periods.

Only the periods that overlap tonight's charge are changed, and only where they overlap; the
user's other periods are written back with every setting as Fox returned it. The exception is
the user's own forced charge or discharge: if tonight's charge overlaps one, all of it is
replaced, so no stray piece of it forces the battery outside the plan.

An all-day period (00:00 to 23:59) is Fox's default for the times no other period covers. The
Open API can't set it, and community reports say sending it can stop the other periods working,
so it is never written. Fox drops it on any write, so the times only it covered are written as
periods with its settings instead (see `fill_gaps`).

Stored copies of the schedule from before the move to V3 have V1's flat periods, with the
settings on the period itself and an `enable` field. Both shapes are read, and V3's is written.
"""

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

FORCE_CHARGE = "ForceCharge"
FORCE_DISCHARGE = "ForceDischarge"
DAY_MINUTES = 24 * 60
DEFAULT_MAX_GROUPS = 8
# The settings a period carries in V3's extraParam (on the period itself in V1).
SETTINGS = (
    "fdSoc",
    "fdPwr",
    "minSocOnGrid",
    "maxSoc",
    "importLimit",
    "exportLimit",
    "pvLimit",
    "reactivePower",
)
# Documented ranges for the battery SoC settings; the inverter's own ranges win when it
# reports them in the schedule's `properties`.
DEFAULT_RANGES = {
    "minsocongrid": (10.0, 100.0),
    "fdsoc": (10.0, 100.0),
    "fdpwr": (0.0, 30000.0),
    "maxsoc": (10.0, 100.0),
}


def flag(value: Any) -> bool:
    """A Fox on/off value. Fox documents the schedule's `enable` as the string "0" or "1" and
    has been seen returning the integer, so both are read; bool("0") would be true."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true")
    return bool(value)


def _number(value: Any) -> float | None:
    """A number Fox sent, as a number or as a numeric string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_sent(value: Any) -> Any:
    """A setting as it goes back to Fox: a number stays as it is, a numeric string becomes a
    number (Fox documents settings as numbers when written), anything else is left alone."""
    if isinstance(value, str):
        number = _number(value)
        if number is not None:
            return int(number) if number.is_integer() else number
    return value


def v3_group(raw: dict[str, Any]) -> dict[str, Any]:
    """A period as Fox returned it (V3, or V1 from an older stored copy), in V3's shape:
    settings in extraParam, numbers as numbers, and no per-period `enable`."""
    group = {k: v for k, v in raw.items() if k not in ("enable", "extraParam", *SETTINGS)}
    extra = {k: raw[k] for k in SETTINGS if k in raw}
    extra.update(raw.get("extraParam") or {})
    if extra:
        group["extraParam"] = {k: _as_sent(v) for k, v in extra.items()}
    return group


@dataclass(frozen=True)
class Period:
    """One schedule period. `start` and `end` are minutes after local midnight, inclusive."""

    start: int
    end: int
    work_mode: str
    fd_soc: float | None = None
    fd_pwr: float | None = None
    min_soc_on_grid: float | None = None
    max_soc: float | None = None
    # Every field as Fox returned it, so the settings we don't set are written back untouched.
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_fox(cls, group: dict[str, Any]) -> "Period":
        settings = {k: group[k] for k in SETTINGS if k in group}
        settings.update(group.get("extraParam") or {})
        return cls(
            start=int(group.get("startHour", 0)) * 60 + int(group.get("startMinute", 0)),
            end=int(group.get("endHour", 0)) * 60 + int(group.get("endMinute", 0)),
            work_mode=str(group.get("workMode", "")),
            fd_soc=_number(settings.get("fdSoc")),
            fd_pwr=_number(settings.get("fdPwr")),
            min_soc_on_grid=_number(settings.get("minSocOnGrid")),
            max_soc=_number(settings.get("maxSoc")),
            raw=dict(group),
        )

    def to_fox(self) -> dict[str, Any]:
        """The period as a V3 write sends it."""
        group = v3_group(self.raw)
        group.update(
            startHour=self.start // 60,
            startMinute=self.start % 60,
            endHour=self.end // 60,
            endMinute=self.end % 60,
            workMode=self.work_mode,
        )
        extra = dict(group.pop("extraParam", {}))
        for key, value in (
            ("fdSoc", self.fd_soc),
            ("fdPwr", self.fd_pwr),
            ("minSocOnGrid", self.min_soc_on_grid),
            ("maxSoc", self.max_soc),
        ):
            if value is not None:
                extra[key] = int(round(value))
        if extra:
            group["extraParam"] = extra
        return group

    @property
    def forced(self) -> bool:
        return self.work_mode in (FORCE_CHARGE, FORCE_DISCHARGE)

    @property
    def all_day(self) -> bool:
        return self.start == 0 and self.end == DAY_MINUTES - 1

    def overlaps(self, other: "Period") -> bool:
        return self.start <= other.end and other.start <= self.end

    def same_slot(self, other: "Period") -> bool:
        return (self.start, self.end, self.work_mode) == (other.start, other.end, other.work_mode)

    def label(self) -> str:
        return f"{_clock(self.start)}–{_clock(self.end + 1)}"


def _clock(minutes: int) -> str:
    minutes %= DAY_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Schedule:
    """The inverter's schedule as read: whether it is switched on, and its periods."""

    enabled: bool
    # The periods that can be written: switched on, and not the all-day default.
    periods: list[Period]
    max_groups: int
    ranges: dict[str, tuple[float, float]]
    # Settings the inverter lists in `properties` (lower case), e.g. "maxsoc" when it has one.
    reported: frozenset[str] = frozenset()
    # Fox's all-day default period, if it reports one. Never written.
    default: Period | None = None
    # Switched-off periods. V1 reports them; V3 has been seen to hide them, and a write
    # replaces the whole list, so they may be lost.
    switched_off: list[Period] = field(default_factory=list)

    @classmethod
    def from_fox(cls, result: dict[str, Any]) -> "Schedule":
        groups = result.get("groups") or []
        on = [Period.from_fox(g) for g in groups if flag(g.get("enable", 1))]
        off = [
            Period.from_fox(g)
            for g in groups
            if not flag(g.get("enable", 1))
            # V1 pads the list with empty switched-off 00:00–00:00 slots.
            and (g.get("startHour"), g.get("startMinute"), g.get("endHour"), g.get("endMinute"))
            != (0, 0, 0, 0)
        ]
        default = next((p for p in on if p.all_day), None)
        periods = sorted((p for p in on if p is not default), key=lambda p: p.start)
        ranges = dict(DEFAULT_RANGES)
        properties = result.get("properties") or {}
        for key, spec in properties.items():
            bounds = (spec or {}).get("range") or {}
            low, high = _number(bounds.get("min")), _number(bounds.get("max"))
            if low is not None and high is not None and low <= high:
                ranges[key.lower()] = (low, high)
        max_groups = _number(result.get("maxGroupCount"))
        return cls(
            enabled=flag(result.get("enable", 0)),
            periods=periods,
            max_groups=int(max_groups) if max_groups and max_groups > 0 else DEFAULT_MAX_GROUPS,
            ranges=ranges,
            reported=frozenset(k.lower() for k in properties),
            default=default,
            switched_off=off,
        )

    def to_snapshot(self) -> dict[str, Any]:
        groups = [p.to_fox() for p in self.periods]
        if self.default is not None:
            groups.insert(0, self.default.to_fox())
        return {"enable": int(self.enabled), "groups": groups}

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
    max_soc: float | None = None,
) -> list[Period]:
    """Force charge (or discharge) periods covering [start, stop), split at local midnight.

    Times with seconds are rounded up to the next minute at both ends, so two stages that
    meet at the same moment meet at the same minute and never overlap."""
    zone = ZoneInfo(tz)
    local_start, local_stop = _ceil_minute(start.astimezone(zone)), _ceil_minute(
        stop.astimezone(zone)
    )
    minutes = round((local_stop - local_start).total_seconds() / 60)
    if minutes <= 0:
        return []
    if minutes >= DAY_MINUTES:
        raise ValueError("A forced charge can't be a whole day or longer")
    first = local_start.hour * 60 + local_start.minute
    last = first + minutes - 1
    spans = [(first, last)] if last < DAY_MINUTES else [(first, DAY_MINUTES - 1), (0, last - DAY_MINUTES)]
    return [
        Period(
            s,
            e,
            work_mode,
            fd_soc=fd_soc,
            fd_pwr=fd_pwr,
            min_soc_on_grid=min_soc,
            max_soc=max_soc,
        )
        for s, e in spans
    ]


def _ceil_minute(moment: datetime) -> datetime:
    if moment.second or moment.microsecond:
        return moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return moment


def merge(
    existing: list[Period], added: list[Period]
) -> tuple[list[Period], list[Period], list[Period]]:
    """The schedule with `added` in place of whatever overlaps it. Returns the new schedule,
    the periods overlapped, and what's left of those outside `added`, kept with their settings.

    A forced period the plan overlaps goes completely (defect D12): what's left of it would
    force the battery at times the plan didn't choose, such as a one-minute grid charge to the
    user's old level just after the planner's charge ends, repeating every night."""
    removed = [p for p in existing if any(p.overlaps(a) for a in added)]
    trimmed = [piece for p in removed if not p.forced for piece in _outside(p, added)]
    kept = [p for p in existing if not any(p is r for r in removed)]
    return sorted(kept + trimmed + added, key=lambda p: p.start), removed, trimmed


def _outside(period: Period, covering: list[Period]) -> list[Period]:
    """The parts of `period` that none of `covering` overlaps, each as its own period."""
    taken = {m for c in covering for m in range(c.start, c.end + 1)}
    pieces, run = [], None
    for minute in range(period.start, period.end + 2):
        free = minute <= period.end and minute not in taken
        if free and run is None:
            run = minute
        elif not free and run is not None:
            pieces.append(replace(period, start=run, end=minute - 1))
            run = None
    return pieces


def fill_gaps(
    periods: list[Period], all_day: Period | None
) -> tuple[list[Period], list[Period]]:
    """The periods with every time none of them covers written as a copy of the all-day
    default, and the copies (defect D13).

    Fox drops its all-day default on any write, as seen on the owner's inverter on 5 November
    2026, and the times only it covered would then have no period. Each copy keeps every
    setting it had, so those times run as before. With no all-day period ever seen, the gaps
    were already running in the inverter's main work mode, which a write doesn't change, so
    they're left alone. An empty list is the schedule being cleared, and is left empty."""
    if all_day is None or not periods:
        return periods, []
    whole_day = replace(all_day, start=0, end=DAY_MINUTES - 1)
    pieces = sorted(periods + _outside(whole_day, periods), key=lambda p: p.start)
    # Neighbouring copies become one period, so the pieces left by each night's charge being
    # added and removed don't build up towards the inverter's limit.
    merged: list[Period] = []
    for p in pieces:
        last = merged[-1] if merged else None
        if last and last.end + 1 == p.start and is_copy(last, all_day) and is_copy(p, all_day):
            merged[-1] = replace(last, end=p.end)
        else:
            merged.append(p)
    filled = [p for p in merged if not any(p is q for q in periods)]
    return merged, filled


def _settings(p: Period) -> dict[str, Any]:
    """What a period sends, apart from its times."""
    times = ("startHour", "startMinute", "endHour", "endMinute")
    return {k: v for k, v in p.to_fox().items() if k not in times}


def is_copy(period: Period, all_day: Period | None) -> bool:
    """Whether a period runs exactly as the all-day default does, so its times can be given
    to another period and filled again afterwards with nothing changed."""
    return all_day is not None and not period.forced and _settings(period) == _settings(all_day)


def covers_day(periods: list[Period]) -> bool:
    return {m for p in periods for m in range(p.start, p.end + 1)} >= set(range(DAY_MINUTES))


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
            ("maximum charge", w.max_soc, found.max_soc),
        ):
            if a is not None and (b is None or abs(a - b) > 0.5):
                problems.append(f"The period {w.label()} reads back {name} {b}, not {a:g}.")
    extra = [r for r in read_back if not any(r.same_slot(w) for w in written)]
    for r in extra:
        problems.append(f"The inverter also has a {r.work_mode} period {r.label()}.")
    return problems
