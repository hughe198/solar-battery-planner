"""A plain look back at a past day: how its weather compared with the forecast made the night
before, what that did to its solar and usage, and what the charge and the day saved."""

from dataclasses import dataclass, field
from datetime import date

from app.forecasting.weather_story import DAYLIGHT_WM2, cloud_word

# Solar this far from the forecast (as a share) counts as sunnier or duller than forecast.
SOLAR_SURPRISE = 0.15
# Cloud cover this many points from the forecast is worth naming.
CLOUD_SURPRISE = 15.0
# Temperature this far from the forecast (°C) is worth naming.
TEMPERATURE_SURPRISE = 2.0
# A missed saving below this (pence) means the charge was about right.
ABOUT_RIGHT_PENCE = 5.0


@dataclass(frozen=True)
class DayActual:
    """What the day really did, from its readings and the observed weather."""

    day: date
    complete: bool
    load_kwh: float
    solar_kwh: float
    import_kwh: float
    export_kwh: float
    import_cost_pence: float
    export_revenue_pence: float
    grid_only_pence: float
    cloud_percent: float | None
    temperature_c: float | None
    has_panels: bool = True


@dataclass(frozen=True)
class DayForecastKept:
    """The forecast saved before the day began (a ForecastSnapshot)."""

    load_kwh: float
    solar_kwh: float
    cloud_percent: float | None
    temperature_c: float | None
    target_soc_percent: float | None


@dataclass(frozen=True)
class ChargeHindsight:
    charged_soc_percent: float
    best_soc_percent: float
    missed_saving_pence: float


@dataclass(frozen=True)
class LookBack:
    day: date
    # What the comparison was against: the forecast kept for the day, the home's recent days,
    # or nothing to compare with.
    basis: str  # "forecast" | "recent" | "none"
    headline: str  # "Sunnier than forecast"
    detail: str  # "30% cloud, not 70%"
    weather_headline: str | None  # "Sunny spells, 11°C"
    summary: str
    points: list[str] = field(default_factory=list)
    net_cost_pence: float = 0.0
    saved_pence: float = 0.0


def pence(value: float) -> str:
    value = round(value)
    return f"£{value / 100:.2f}" if abs(value) >= 100 else f"{value:.0f}p"


def daylight_cloud(cloud: list[float | None], sun: list[float | None]) -> float | None:
    """Mean cloud cover over the hours with some sun, or every hour if none had any."""
    pairs = [(c, s) for c, s in zip(cloud, sun) if c is not None]
    lit = [c for c, s in pairs if s is not None and s >= DAYLIGHT_WM2]
    picked = lit or [c for c, _ in pairs]
    return sum(picked) / len(picked) if picked else None


def _charge_clause(charge: ChargeHindsight | None) -> tuple[str, str | None]:
    """The end of the summary sentence and a detailed point about the charge."""
    if charge is None:
        return "", None
    point = (
        f"Charged to {charge.charged_soc_percent:.0f}%; with hindsight "
        f"{charge.best_soc_percent:.0f}% was best"
    )
    if charge.missed_saving_pence < ABOUT_RIGHT_PENCE:
        return ", and the charge was about right", point + ", so it was about right."
    side = "lower" if charge.best_soc_percent < charge.charged_soc_percent else "higher"
    saving = pence(charge.missed_saving_pence)
    return (
        f", and a {side} charge would have saved about {saving}",
        point + f", which would have saved about {saving}.",
    )


def look_back(
    actual: DayActual,
    kept: DayForecastKept | None,
    recent_solar_kwh: float | None,
    charge: ChargeHindsight | None,
) -> LookBack:
    net = actual.import_cost_pence - actual.export_revenue_pence
    saved = actual.grid_only_pence - net
    weather_headline = None
    if actual.cloud_percent is not None and actual.temperature_c is not None:
        weather_headline = f"{cloud_word(actual.cloud_percent)}, {actual.temperature_c:.0f}°C"
    charge_end, charge_point = _charge_clause(charge)
    points: list[str] = []

    basis = "forecast" if kept is not None else "recent" if recent_solar_kwh else "none"
    expected_solar = (
        kept.solar_kwh if kept is not None else recent_solar_kwh if recent_solar_kwh else None
    )
    against = "forecast" if kept is not None else "your recent days"

    # How the sun compared, which is what most changes a battery day.
    if actual.has_panels and expected_solar:
        ratio = actual.solar_kwh / expected_solar
        if ratio > 1 + SOLAR_SURPRISE:
            headline = f"Sunnier than {against}"
        elif ratio < 1 - SOLAR_SURPRISE:
            headline = f"Duller than {against}"
        else:
            headline = "As forecast" if kept is not None else "A typical day"
        if headline in ("As forecast", "A typical day"):
            lead = (
                f"The sun was about as forecast: solar made {actual.solar_kwh:.1f} kWh "
                f"against {expected_solar:.1f}"
                if kept is not None
                else f"Solar made {actual.solar_kwh:.1f} kWh, close to your recent "
                f"{expected_solar:.1f} kWh a day"
            )
        else:
            compared = f"{expected_solar:.1f}"
            if kept is None:
                compared = f"a recent {compared}"
            lead = (
                f"It was {headline.lower()}, so solar made {actual.solar_kwh:.1f} kWh against "
                f"{compared}"
            )
    elif kept is not None:
        headline = "Usage against forecast"
        lead = f"You used {actual.load_kwh:.1f} kWh against {kept.load_kwh:.1f} forecast"
    else:
        headline = "How it went"
        lead = f"You used {actual.load_kwh:.1f} kWh"

    detail = ""
    if actual.cloud_percent is not None:
        detail = f"{actual.cloud_percent:.0f}% cloud"
        if (
            kept is not None
            and kept.cloud_percent is not None
            and abs(actual.cloud_percent - kept.cloud_percent) >= CLOUD_SURPRISE
        ):
            detail += f", not {kept.cloud_percent:.0f}%"

    # The detail, in the order someone would ask about it.
    if kept is not None:
        said = f"Forecast the night before: {kept.solar_kwh:.1f} kWh of solar"
        if kept.cloud_percent is not None and kept.temperature_c is not None:
            said += f" with {kept.cloud_percent:.0f}% cloud and {kept.temperature_c:.0f}°C"
        turned = f"It turned out {actual.solar_kwh:.1f} kWh"
        if actual.cloud_percent is not None and actual.temperature_c is not None:
            turned += f", with {actual.cloud_percent:.0f}% cloud and {actual.temperature_c:.0f}°C"
        points.append(f"{said}. {turned}." if actual.has_panels else f"{said}.")
    elif basis == "recent":
        points.append(
            "There's no forecast saved for that day, so it's compared with your recent days: "
            f"{recent_solar_kwh:.1f} kWh of solar a day on average."
        )
    if actual.has_panels and actual.export_kwh >= 0.5:
        points.append(f"{actual.export_kwh:.1f} kWh went to export.")

    usage = f"Usage was {actual.load_kwh:.1f} kWh"
    if kept is not None:
        usage += f" against {kept.load_kwh:.1f} forecast"
        if (
            kept.temperature_c is not None
            and actual.temperature_c is not None
            and abs(actual.temperature_c - kept.temperature_c) >= TEMPERATURE_SURPRISE
        ):
            word = "colder" if actual.temperature_c < kept.temperature_c else "milder"
            usage += f"; it was {word} than forecast"
    points.append(usage + ".")
    if charge_point:
        points.append(charge_point)
    points.append(
        f"It cost {pence(net)} instead of {pence(actual.grid_only_pence)} on grid import alone, "
        f"saving {pence(saved)}."
        if net >= 0
        else f"It earned {pence(-net)}, where grid import alone would have cost "
        f"{pence(actual.grid_only_pence)}."
    )
    if not actual.complete:
        points.append("Some readings for the day are still to come, so these figures may grow.")

    return LookBack(
        day=actual.day,
        basis=basis,
        headline=headline,
        detail=detail,
        weather_headline=weather_headline,
        summary=f"{lead}{charge_end}.",
        points=points,
        net_cost_pence=round(net, 1),
        saved_pence=round(saved, 1),
    )
