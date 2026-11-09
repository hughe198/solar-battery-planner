"""Plain explanations of what the weather does to a day: its solar, against the home's recent
days, and its usage, through the model's cold-weather terms.

Everything here is worked out from numbers the forecast already has, so it explains the
forecast rather than adding a second opinion to it.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np

# Daytime cloud cover (%) at or below each limit, and how it's described.
CLOUD_WORDS = ((20, "Sunny"), (45, "Sunny spells"), (75, "Partly cloudy"), (101, "Cloudy"))
# A spell this much clearer than the day's average, for an hour or more, is worth naming.
BRIGHT_SPELL_MARGIN = 25.0
BRIGHT_SPELL_MAX_CLOUD = 60.0
# Solar within this share of the recent average counts as about average.
ABOUT_AVERAGE = 0.15
# Usage the cold adds below this (kWh) isn't worth a mention.
COLD_KWH_WORTH_SAYING = 0.3
# Temperature this far from recent days (°C) is worth comparing.
TEMPERATURE_DIFFERENCE = 2.0
# Daytime cloud this changeable (standard deviation, %) makes solar uncertain.
CHANGEABLE_CLOUD = 25.0
# A half hour counts as daylight from this much expected solar (kWh), or this much sun (W/m²).
DAYLIGHT_KWH = 0.01
DAYLIGHT_WM2 = 20.0
RECENT_DAYS = 14


@dataclass(frozen=True)
class WeatherStory:
    """One sentence and its detail, plus the figures they came from."""

    headline: str  # "Cloudy, 4°C"
    detail: str  # "brighter 13:00–15:00", or empty
    summary: str
    points: list[str] = field(default_factory=list)
    daylight_cloud_percent: float | None = None
    mean_temperature_c: float | None = None
    solar_kwh: float | None = None
    recent_solar_kwh: float | None = None
    recent_temperature_c: float | None = None
    cold_kwh: float = 0.0


@dataclass(frozen=True)
class DayWeather:
    """One local day's half hours: when they start, their weather and the predicted energy."""

    starts: list[datetime]
    cloud_percent: np.ndarray
    temperature_c: np.ndarray
    sun_wm2: np.ndarray
    solar_kwh: np.ndarray
    load_kwh: np.ndarray
    # The part of each half hour's predicted usage that comes from the cold-weather terms.
    cold_kwh: np.ndarray
    has_panels: bool = True
    solar_low_kwh: float | None = None
    solar_high_kwh: float | None = None


def recent_daily_solar(days: list[date], solar_kwh: np.ndarray, before: date) -> float | None:
    """The average daily solar over the RECENT_DAYS complete local days before `before`."""
    totals: dict[date, float] = {}
    counts: dict[date, int] = {}
    for d, kwh in zip(days, solar_kwh):
        if d < before and (before - d).days <= RECENT_DAYS:
            totals[d] = totals.get(d, 0.0) + float(kwh)
            counts[d] = counts.get(d, 0) + 1
    # A day with under 20 hours of readings is partial; leave it out.
    whole = [totals[d] for d in totals if counts[d] >= 40]
    return float(np.mean(whole)) if whole else None


def recent_mean_temperature(
    days: list[date], temperature_c: np.ndarray, before: date
) -> float | None:
    picked = [
        float(t)
        for d, t in zip(days, temperature_c)
        if d < before and (before - d).days <= RECENT_DAYS and np.isfinite(t)
    ]
    return float(np.mean(picked)) if picked else None


def _clock(moment: datetime, tz: str) -> str:
    return moment.astimezone(ZoneInfo(tz)).strftime("%H:%M")


def cloud_word(cloud: float) -> str:
    return next(word for limit, word in CLOUD_WORDS if cloud <= limit)


def _bright_spell(w: DayWeather, daylight: np.ndarray, mean_cloud: float, tz: str) -> str:
    """The longest run of daylight half hours clearly brighter than the rest, if an hour long."""
    limit = min(mean_cloud - BRIGHT_SPELL_MARGIN, BRIGHT_SPELL_MAX_CLOUD)
    clear = daylight & (w.cloud_percent <= limit)
    best, start = (0, 0), None
    for i, c in enumerate([*clear, False]):
        if c and start is None:
            start = i
        elif not c and start is not None:
            if i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    if best[1] - best[0] < 2:
        return ""
    step = w.starts[1] - w.starts[0]
    return f"brighter {_clock(w.starts[best[0]], tz)}–{_clock(w.starts[best[1] - 1] + step, tz)}"


def _when(w: DayWeather, share: np.ndarray, tz: str) -> str:
    """Where most of a daily amount falls: morning, afternoon, evening or overnight."""
    if share.sum() <= 0:
        return ""
    hours = np.array([s.astimezone(ZoneInfo(tz)).hour for s in w.starts])
    parts = {
        "overnight": (hours < 6),
        "in the morning": (hours >= 6) & (hours < 12),
        "in the afternoon": (hours >= 12) & (hours < 17),
        "in the evening": (hours >= 17),
    }
    name, mask = max(parts.items(), key=lambda p: float(share[p[1]].sum()))
    return name if share[mask].sum() >= 0.4 * share.sum() else "through the day"


def tell(
    w: DayWeather, tz: str, recent_solar: float | None, recent_temp: float | None
) -> WeatherStory:
    """Explain the weather's effect on one day's forecast."""
    daylight = (w.solar_kwh >= DAYLIGHT_KWH) | (w.sun_wm2 >= DAYLIGHT_WM2)
    if not daylight.any():
        daylight = np.ones(len(w.starts), dtype=bool)
    cloud = float(np.mean(w.cloud_percent[daylight]))
    temp = float(np.mean(w.temperature_c))
    solar = float(w.solar_kwh.sum())
    load = float(w.load_kwh.sum())
    cold = float(w.cold_kwh.sum())
    word = cloud_word(cloud)
    spell = _bright_spell(w, daylight, cloud, tz) if word in ("Partly cloudy", "Cloudy") else ""
    headline = f"{word}, {temp:.0f}°C"

    points: list[str] = []
    clauses: list[str] = []

    # Solar.
    if w.has_panels:
        sky = f"{word.lower()} skies ({cloud:.0f}% cloud in daylight)"
        spell_note = (
            f" A brighter spell from {spell.removeprefix('brighter ').replace('–', ' to ')} "
            "helps."
            if spell
            else ""
        )
        if recent_solar and recent_solar > 0.1:
            ratio = solar / recent_solar
            if abs(ratio - 1) <= ABOUT_AVERAGE:
                clause = f"{word.lower()} skies should give about your usual solar"
            else:
                side = "below" if ratio < 1 else "above"
                compare = f"about {abs(ratio - 1) * 100:.0f}% {side} your recent average"
                clause = f"{word.lower()} skies put solar {compare}"
            points.append(
                f"Solar: {sky}, so {solar:.1f} kWh against your {RECENT_DAYS}-day average of "
                f"{recent_solar:.1f} kWh.{spell_note}"
            )
        else:
            clause = f"{word.lower()} skies should give about {solar:.1f} kWh of solar"
            points.append(f"Solar: {sky}, so about {solar:.1f} kWh.{spell_note}")
        clauses.append(clause)

    # Usage and the cold.
    colder = recent_temp is not None and temp <= recent_temp - TEMPERATURE_DIFFERENCE
    milder = recent_temp is not None and temp >= recent_temp + TEMPERATURE_DIFFERENCE
    than = f" (recent days averaged {recent_temp:.0f}°C)" if colder or milder else ""
    if cold >= COLD_KWH_WORTH_SAYING:
        when = _when(w, w.cold_kwh, tz)
        points.append(
            f"Usage: an average of {temp:.0f}°C{than} adds about {cold:.1f} kWh of the "
            f"{load:.1f} kWh forecast, {when}, from how your usage has risen on cold days."
        )
        cause = "the cold" if temp < 10 else "the temperature"
        clauses.append(f"{cause} adds about {cold:.1f} kWh to your usage")
    else:
        points.append(
            f"Usage: {load:.1f} kWh. At {temp:.0f}°C{than} the temperature makes little "
            "difference to your usage."
        )

    # Uncertainty.
    spread = float(np.std(w.cloud_percent[daylight])) if daylight.sum() > 1 else 0.0
    if w.has_panels and w.solar_low_kwh is not None and w.solar_high_kwh is not None:
        if spread >= CHANGEABLE_CLOUD:
            points.append(
                f"The cloud is changeable, so solar could be anywhere from "
                f"{w.solar_low_kwh:.1f} to {w.solar_high_kwh:.1f} kWh; the plan leans towards "
                "a smaller charge when it's this uncertain."
            )
        else:
            points.append(
                f"Solar is likely between {w.solar_low_kwh:.1f} and {w.solar_high_kwh:.1f} kWh."
            )

    if clauses:
        sentence = ", and ".join(clauses)
        summary = sentence[0].upper() + sentence[1:] + "."
    else:
        summary = f"{headline}: the temperature makes little difference to your usage."

    return WeatherStory(
        headline=headline,
        detail=spell,
        summary=summary,
        points=points,
        daylight_cloud_percent=round(cloud, 1),
        mean_temperature_c=round(temp, 1),
        solar_kwh=round(solar, 2) if w.has_panels else None,
        recent_solar_kwh=round(recent_solar, 2) if recent_solar is not None else None,
        recent_temperature_c=round(recent_temp, 1) if recent_temp is not None else None,
        cold_kwh=round(cold, 2),
    )
