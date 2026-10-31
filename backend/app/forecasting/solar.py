"""Physics estimate of PV output from weather and panel orientation.

Sun position follows the NOAA approximations (good to a fraction of a degree), plane-of-array
irradiance uses the isotropic-sky transposition, and output applies a cell temperature
derating. This is both the fallback when a user has little history and the input the learned
model calibrates per array.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

# Fraction of nameplate a typical installation delivers after inverter, wiring, soiling and
# mismatch losses. The learned model starts here and moves towards the user's own data.
DEFAULT_PERFORMANCE_RATIO = 0.85
TEMP_COEFFICIENT_PER_C = -0.004
# Cell temperature rise above air per W/m² on the panel (roughly NOCT 45 °C, open rack).
CELL_HEATING_C_PER_WM2 = 0.03
GROUND_ALBEDO = 0.2


@dataclass(frozen=True)
class Orientation:
    kwp: float
    tilt_degrees: float
    azimuth_degrees: float


def sun_position(moments: np.ndarray, lat: float, lon: float) -> tuple[np.ndarray, np.ndarray]:
    """Cosine of the solar zenith angle and solar azimuth (degrees from north) at each moment.

    `moments` holds POSIX timestamps (seconds, UTC).
    """
    days = moments / 86400.0
    # Days since J2000 and the fractional year angle used by NOAA's series.
    day_of_year = (days + 2440587.5 - 2451545.0) % 365.2422
    gamma = 2 * math.pi * day_of_year / 365.2422
    decl = (
        0.006918
        - 0.399912 * np.cos(gamma)
        + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2 * gamma)
        + 0.000907 * np.sin(2 * gamma)
        - 0.002697 * np.cos(3 * gamma)
        + 0.00148 * np.sin(3 * gamma)
    )
    eq_time = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma)
        - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2 * gamma)
        - 0.040849 * np.sin(2 * gamma)
    )
    utc_minutes = (moments % 86400.0) / 60.0
    solar_minutes = utc_minutes + eq_time + 4 * lon
    hour_angle = np.radians(solar_minutes / 4.0 - 180.0)
    phi = math.radians(lat)

    cos_zenith = math.sin(phi) * np.sin(decl) + math.cos(phi) * np.cos(decl) * np.cos(hour_angle)
    cos_zenith = np.clip(cos_zenith, -1.0, 1.0)
    sin_zenith = np.sqrt(1 - cos_zenith**2)
    with np.errstate(invalid="ignore", divide="ignore"):
        cos_az = (np.sin(decl) - math.sin(phi) * cos_zenith) / (math.cos(phi) * sin_zenith)
    azimuth = np.degrees(np.arccos(np.clip(np.nan_to_num(cos_az), -1.0, 1.0)))
    # Morning sun is in the east, afternoon in the west.
    azimuth = np.where(np.sin(hour_angle) > 0, 360 - azimuth, azimuth)
    return cos_zenith, azimuth


def plane_of_array(
    ghi: np.ndarray,
    dhi: np.ndarray,
    cos_zenith: np.ndarray,
    sun_azimuth: np.ndarray,
    tilt_degrees: float,
    azimuth_degrees: float,
) -> np.ndarray:
    """Irradiance on a tilted plane (W/m²), isotropic sky model."""
    tilt = math.radians(tilt_degrees)
    beam_horizontal = np.clip(ghi - dhi, 0, None)
    # Below ~5° elevation direct-normal estimates explode; treat that light as diffuse.
    low_sun = cos_zenith < 0.087
    dni = np.where(low_sun, 0.0, beam_horizontal / np.where(low_sun, 1.0, cos_zenith))
    diffuse = dhi + np.where(low_sun, beam_horizontal, 0.0)
    sin_zenith = np.sqrt(np.clip(1 - cos_zenith**2, 0, None))
    cos_incidence = cos_zenith * math.cos(tilt) + sin_zenith * math.sin(tilt) * np.cos(
        np.radians(sun_azimuth - azimuth_degrees)
    )
    beam = dni * np.clip(cos_incidence, 0, None)
    sky = diffuse * (1 + math.cos(tilt)) / 2
    ground = ghi * GROUND_ALBEDO * (1 - math.cos(tilt)) / 2
    return np.where(cos_zenith > 0, beam + sky + ground, 0.0)


@dataclass(frozen=True)
class HalfHourWeather:
    """Weather for each half hour. Radiation is the mean over the half hour, W/m²."""

    starts: list[datetime]
    ghi: np.ndarray
    dhi: np.ndarray
    temperature_c: np.ndarray
    cloud_cover_percent: np.ndarray

    def take(self, index: np.ndarray) -> "HalfHourWeather":
        return HalfHourWeather(
            starts=[self.starts[i] for i in index],
            ghi=self.ghi[index],
            dhi=self.dhi[index],
            temperature_c=self.temperature_c[index],
            cloud_cover_percent=self.cloud_cover_percent[index],
        )


def split_hours(
    hour_starts: list[datetime],
    ghi: list[float | None],
    dhi: list[float | None],
    temperature_c: list[float | None],
    cloud_cover: list[float | None],
    lat: float,
    lon: float,
) -> HalfHourWeather:
    """Turn hourly weather into half hours.

    Radiation for the hour is shared between its halves in proportion to how high the sun is in
    each, so a sunrise hour doesn't put light in the dark first half. Missing values become 0
    radiation and 10 °C, which only matters for gaps in the record.
    """
    starts = []
    for h in hour_starts:
        starts += [h, h + timedelta(minutes=30)]
    if not hour_starts:
        empty = np.zeros(0)
        return HalfHourWeather([], empty, empty, empty, empty)
    mids = np.array([(s + timedelta(minutes=15)).timestamp() for s in starts])
    cos_z, _ = sun_position(mids, lat, lon)
    weight = np.clip(cos_z, 0, None).reshape(-1, 2)
    total = weight.sum(axis=1, keepdims=True)
    share = np.where(total > 0, 2 * weight / np.where(total > 0, total, 1), 1.0).ravel()

    def arr(values: list[float | None], default: float) -> np.ndarray:
        return np.array([default if v is None else v for v in values], dtype=float)

    return HalfHourWeather(
        starts=starts,
        ghi=np.repeat(arr(ghi, 0.0), 2) * share,
        dhi=np.repeat(arr(dhi, 0.0), 2) * share,
        temperature_c=np.repeat(arr(temperature_c, 10.0), 2),
        cloud_cover_percent=np.repeat(arr(cloud_cover, 50.0), 2),
    )


def array_features(
    weather: HalfHourWeather, arrays: list[Orientation], lat: float, lon: float
) -> np.ndarray:
    """Ideal energy (kWh) each array would make per half hour at a performance ratio of 1.

    Shape (half hours, arrays). The PV model is a per-array multiple of these columns.
    """
    if not weather.starts:
        return np.zeros((0, len(arrays)))
    mids = np.array([(s + timedelta(minutes=15)).timestamp() for s in weather.starts])
    cos_z, sun_az = sun_position(mids, lat, lon)
    columns = []
    for a in arrays:
        poa = plane_of_array(
            weather.ghi, weather.dhi, cos_z, sun_az, a.tilt_degrees, a.azimuth_degrees
        )
        cell_c = weather.temperature_c + CELL_HEATING_C_PER_WM2 * poa
        derate = np.clip(1 + TEMP_COEFFICIENT_PER_C * (cell_c - 25), 0.5, 1.1)
        columns.append(a.kwp * poa / 1000.0 * derate * 0.5)
    return np.column_stack(columns)
