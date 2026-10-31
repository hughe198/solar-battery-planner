"""Time-of-use tariffs: price any moment for import or export.

A tariff is a list of rate bands per direction (import, export). Each band has a local
start and end time on a half-hour boundary, a rate in pence per kWh and the days it applies
to (every day, weekdays or weekends). For each direction and each kind of day, the bands
cover all 48 half hours exactly once. A band whose start equals its end covers the whole day.

The planner should only use `TariffSchedule`:

    schedule = TariffSchedule.from_system(system)
    schedule.rate_at(interval_start, "import")   # p/kWh for the half hour starting then
    schedule.day_rates(day, "export")            # [(utc_start, p/kWh), ...] for a local day
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from app.models import RateDays, RateDirection

SLOTS_PER_DAY = 48
HALF_HOUR = timedelta(minutes=30)

Direction = Literal["import", "export"] | RateDirection


class BandLike(Protocol):
    direction: RateDirection
    days: RateDays
    start: time
    end: time
    rate: float


@dataclass(frozen=True)
class Band:
    direction: RateDirection
    days: RateDays
    start: time
    end: time
    rate: float


def slot_of(t: time) -> int:
    """Half-hour index of a local time, 0 = 00:00-00:30."""
    return (t.hour * 60 + t.minute) // 30


def band_slots(start: time, end: time) -> list[int]:
    """The half hours a band covers. It wraps past midnight when end <= start."""
    first, last = slot_of(start), slot_of(end)
    if last <= first:
        last += SLOTS_PER_DAY
    return [s % SLOTS_PER_DAY for s in range(first, last)]


def _fmt(slot: int) -> str:
    minutes = (slot % SLOTS_PER_DAY) * 30
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _ranges(slots: list[int]) -> str:
    """'07:30-16:00, 19:00-23:30' for a sorted list of half-hour indexes."""
    if 0 < len(slots) < SLOTS_PER_DAY and slots[0] == 0 and slots[-1] == SLOTS_PER_DAY - 1:
        # Start after the first gap so a run across midnight reads as one range.
        cut = next(i for i in range(1, len(slots)) if slots[i] != slots[i - 1] + 1)
        slots = slots[cut:] + [s + SLOTS_PER_DAY for s in slots[:cut]]
    parts, run_start, prev = [], None, None
    for s in slots + [None]:
        if run_start is not None and s != prev + 1:
            parts.append(f"{_fmt(run_start)}-{_fmt(prev + 1)}")
            run_start = None
        if s is not None and run_start is None:
            run_start = s
        prev = s
    return ", ".join(parts)


def _applies(days: RateDays, weekend: bool) -> bool:
    return days == RateDays.all or (days == RateDays.weekends) == weekend


def _grid(bands: Iterable[BandLike]) -> tuple[dict, list[str]]:
    """Rates per (direction, weekend) as 48-slot lists, plus any coverage problems."""
    bands = list(bands)
    grids: dict[tuple[RateDirection, bool], list[float | None]] = {}
    problems: list[str] = []
    for direction in RateDirection:
        mine = [b for b in bands if RateDirection(b.direction) == direction]
        label = "Import" if direction == RateDirection.import_ else "Export"
        found: dict[bool, tuple[str, str]] = {}
        for weekend in (False, True):
            counts = [0] * SLOTS_PER_DAY
            rates: list[float | None] = [None] * SLOTS_PER_DAY
            for b in mine:
                if _applies(RateDays(b.days), weekend):
                    for s in band_slots(b.start, b.end):
                        counts[s] += 1
                        rates[s] = b.rate
            found[weekend] = (
                _ranges([s for s, c in enumerate(counts) if c == 0]),
                _ranges([s for s, c in enumerate(counts) if c > 1]),
            )
            grids[(direction, weekend)] = rates
        # Say it once when weekdays and weekends have the same problem.
        if found[False] == found[True]:
            kinds = [("", found[False])]
        else:
            kinds = [(" on weekdays", found[False]), (" on weekends", found[True])]
        for day_label, (gaps, overlaps) in kinds:
            if gaps:
                problems.append(f"{label} has no rate{day_label} for {gaps}")
            if overlaps:
                problems.append(f"{label} has more than one rate{day_label} for {overlaps}")
    return grids, problems


def coverage_problems(bands: Iterable[BandLike]) -> list[str]:
    """Plain-English gaps and overlaps; empty when the bands make a complete tariff."""
    return _grid(bands)[1]


@dataclass(frozen=True)
class TariffSchedule:
    name: str
    zone: ZoneInfo
    standing_charge_pence_per_day: float
    _rates: dict  # (RateDirection, weekend) -> tuple of 48 p/kWh

    @classmethod
    def from_bands(
        cls,
        bands: Iterable[BandLike],
        tz: str = "Europe/London",
        name: str = "Tariff",
        standing_charge_pence_per_day: float = 0,
    ) -> "TariffSchedule":
        grids, problems = _grid(bands)
        if problems:
            raise ValueError("; ".join(problems))
        rates = {key: tuple(values) for key, values in grids.items()}
        return cls(name, ZoneInfo(tz), standing_charge_pence_per_day, rates)

    @classmethod
    def from_system(cls, system) -> "TariffSchedule":
        tariff = system.tariff
        return cls.from_bands(
            tariff.bands, system.timezone, tariff.name, tariff.standing_charge_pence_per_day
        )

    def rate_at(self, when: datetime, direction: Direction) -> float:
        """Pence per kWh at a moment. Naive datetimes are UTC, like stored usage times."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        local = when.astimezone(self.zone)
        weekend = local.weekday() >= 5
        return self._rates[(RateDirection(direction), weekend)][slot_of(local.time())]

    def day_rates(self, day: date, direction: Direction) -> list[tuple[datetime, float]]:
        """(UTC start, p/kWh) for every half hour of a local day: 48, or 46/50 when the
        clocks change."""
        start = datetime.combine(day, time(0), self.zone).astimezone(timezone.utc)
        end = datetime.combine(day + timedelta(days=1), time(0), self.zone).astimezone(
            timezone.utc
        )
        out = []
        t = start
        while t < end:
            out.append((t, self.rate_at(t, direction)))
            t += HALF_HOUR
        return out


def _stored_band(band: dict) -> Band:
    return Band(
        RateDirection(band["direction"]),
        RateDays(band["days"]),
        time.fromisoformat(band["start"]),
        time.fromisoformat(band["end"]),
        band["rate"],
    )


class TariffTimeline:
    """Prices any moment at the tariff in force then. Use it for past days; plans for the days
    ahead use the current tariff, `TariffSchedule`."""

    def __init__(self, past: list[tuple[datetime, TariffSchedule]], current: TariffSchedule):
        self._past = sorted(past, key=lambda entry: entry[0])
        self.current = current

    @classmethod
    def from_system(cls, system) -> "TariffTimeline":
        past = [
            (
                row.valid_until.replace(tzinfo=timezone.utc),
                TariffSchedule.from_bands(
                    [_stored_band(b) for b in row.bands],
                    system.timezone,
                    row.name,
                    row.standing_charge_pence_per_day,
                ),
            )
            for row in system.tariff_history
        ]
        return cls(past, TariffSchedule.from_system(system))

    def schedule_at(self, when: datetime) -> TariffSchedule:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        for valid_until, schedule in self._past:
            if when < valid_until:
                return schedule
        return self.current

    def rate_at(self, when: datetime, direction: Direction) -> float:
        return self.schedule_at(when).rate_at(when, direction)


# Presets. Rates are typical examples and vary by region and over time; the form tells
# users to check them against their bill.


def _b(direction: str, start: str, end: str, rate: float, days: str = "all") -> Band:
    return Band(
        RateDirection(direction), RateDays(days), time.fromisoformat(start),
        time.fromisoformat(end), rate,
    )


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    description: str
    standing_charge_pence_per_day: float
    bands: list[Band]


SEG_EXPORT = 15.0

PRESETS: list[Preset] = [
    Preset(
        "flat",
        "Single rate",
        "One import price all day, one export price all day.",
        53.0,
        [_b("import", "00:00", "00:00", 25.0), _b("export", "00:00", "00:00", SEG_EXPORT)],
    ),
    Preset(
        "economy7",
        "Economy 7",
        "Seven cheap night hours. Many meters switch on GMT all year, so check your times.",
        53.0,
        [
            _b("import", "00:30", "07:30", 15.0),
            _b("import", "07:30", "00:30", 30.0),
            _b("export", "00:00", "00:00", SEG_EXPORT),
        ],
    ),
    Preset(
        "octopus_go",
        "Octopus Go",
        "Cheap 00:30 to 05:30 every night.",
        53.0,
        [
            _b("import", "00:30", "05:30", 8.5),
            _b("import", "05:30", "00:30", 26.0),
            _b("export", "00:00", "00:00", SEG_EXPORT),
        ],
    ),
    Preset(
        "intelligent_octopus_go",
        "Intelligent Octopus Go",
        "Cheap 23:30 to 05:30 every night.",
        53.0,
        [
            _b("import", "23:30", "05:30", 7.0),
            _b("import", "05:30", "23:30", 26.0),
            _b("export", "00:00", "00:00", SEG_EXPORT),
        ],
    ),
    Preset(
        "octopus_flux",
        "Octopus Flux",
        "Cheap 02:00 to 05:00, peak 16:00 to 19:00, for import and export alike.",
        53.0,
        [
            _b("import", "02:00", "05:00", 17.0),
            _b("import", "05:00", "16:00", 28.0),
            _b("import", "16:00", "19:00", 39.0),
            _b("import", "19:00", "02:00", 28.0),
            _b("export", "02:00", "05:00", 6.0),
            _b("export", "05:00", "16:00", 16.5),
            _b("export", "16:00", "19:00", 29.0),
            _b("export", "19:00", "02:00", 16.5),
        ],
    ),
    Preset(
        "octopus_cosy",
        "Cosy Octopus",
        "Three cheap windows a day and a 16:00 to 19:00 peak.",
        53.0,
        [
            _b("import", "04:00", "07:00", 13.5),
            _b("import", "07:00", "13:00", 27.0),
            _b("import", "13:00", "16:00", 13.5),
            _b("import", "16:00", "19:00", 40.0),
            _b("import", "19:00", "22:00", 27.0),
            _b("import", "22:00", "00:00", 13.5),
            _b("import", "00:00", "04:00", 27.0),
            _b("export", "00:00", "00:00", SEG_EXPORT),
        ],
    ),
]

PRESETS_BY_ID = {p.id: p for p in PRESETS}
