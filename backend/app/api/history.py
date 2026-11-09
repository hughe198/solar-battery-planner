"""Recent metered history for the home page charts."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.forecast import _system
from app.database import get_db
from app.forecasting.look_back import (
    ChargeHindsight,
    DayActual,
    DayForecastKept,
    daylight_cloud,
    look_back,
)
from app.forecasting.weather_story import RECENT_DAYS, recent_daily_solar
from app.models import ForecastSnapshot, UsageRecord, User, WeatherRecord
from app.planner.replay import Replayer
from app.planner.simulator import Cycle, cycle_for
from app.security import current_user
from app.tariffs import TariffTimeline

router = APIRouter(prefix="/api/history", tags=["history"])


class HistoryPoint(BaseModel):
    start: datetime
    load_kwh: float
    solar_kwh: float
    import_kwh: float
    export_kwh: float
    soc_percent: float | None
    cheap: bool


class HistoryDay(BaseModel):
    day: date
    load_kwh: float
    solar_kwh: float
    import_kwh: float
    export_kwh: float
    # Share of the home's use not drawn from the grid (grid charging counts as drawn).
    self_sufficiency_percent: float | None
    import_cost_pence: float
    export_revenue_pence: float
    min_soc_percent: float | None
    max_soc_percent: float | None


class History(BaseModel):
    points: list[HistoryPoint]
    days: list[HistoryDay]


class SavingsDay(BaseModel):
    day: date
    house_kwh: float
    # The home's use priced at the import rate of each half hour, as if it all came from the
    # grid, beside what was really imported and exported.
    grid_only_pence: float
    import_cost_pence: float
    export_revenue_pence: float


class Savings(BaseModel):
    days: list[SavingsDay]


def half_hours_in(day: date, zone: ZoneInfo) -> int:
    """48, or 46 or 50 on the days the clocks change."""
    # Two times in the same zone subtract by the wall clock, so both go to UTC first.
    start = datetime.combine(day, time(), zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(timezone.utc)
    return int((end - start).total_seconds()) // 1800


def self_sufficiency(load: float, imported: float) -> float | None:
    if load <= 0:
        return None
    return round(max(0.0, 1 - imported / load) * 100, 1)


@router.get("", response_model=History)
def recent_history(
    days: int = Query(default=7, ge=1, le=31),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> History:
    """The last `days` local days of imported data, ending on the latest day imported."""
    system = _system(user)
    zone = ZoneInfo(system.timezone)
    schedule = TariffTimeline.from_system(system)
    cycles: dict[date, Cycle] = {}

    def charging(start: datetime, local_day: date) -> bool:
        # A window that crosses midnight belongs to the next day's cycle.
        for d in (local_day, local_day + timedelta(days=1)):
            if d not in cycles:
                cycles[d] = cycle_for(d, schedule, system.timezone)
            if cycles[d].night_start <= start < cycles[d].night_end:
                return True
        return False

    latest = db.scalar(
        select(func.max(UsageRecord.start)).where(UsageRecord.system_id == system.id)
    )
    if latest is None:
        return History(points=[], days=[])
    last_day = latest.replace(tzinfo=timezone.utc).astimezone(zone).date()
    first_day = last_day - timedelta(days=days - 1)
    since = datetime.combine(first_day, time(), zone).astimezone(timezone.utc)
    rows = db.scalars(
        select(UsageRecord)
        .where(
            UsageRecord.system_id == system.id,
            UsageRecord.start >= since.replace(tzinfo=None),
        )
        .order_by(UsageRecord.start)
    ).all()

    points: list[HistoryPoint] = []
    by_day: dict[date, list[HistoryPoint]] = {}
    for r in rows:
        start = r.start.replace(tzinfo=timezone.utc)
        local = start.astimezone(zone)
        point = HistoryPoint(
            start=start,
            load_kwh=r.load_kwh,
            solar_kwh=r.solar_kwh,
            import_kwh=r.grid_import_kwh,
            export_kwh=r.grid_export_kwh,
            soc_percent=r.battery_soc_percent,
            cheap=charging(start, local.date()),
        )
        points.append(point)
        by_day.setdefault(local.date(), []).append(point)

    summaries = []
    for day, items in sorted(by_day.items()):
        load = sum(p.load_kwh for p in items)
        imported = sum(p.import_kwh for p in items)
        exported = sum(p.export_kwh for p in items)
        socs = [p.soc_percent for p in items if p.soc_percent is not None]
        summaries.append(
            HistoryDay(
                day=day,
                load_kwh=round(load, 2),
                solar_kwh=round(sum(p.solar_kwh for p in items), 2),
                import_kwh=round(imported, 2),
                export_kwh=round(exported, 2),
                self_sufficiency_percent=self_sufficiency(load, imported),
                import_cost_pence=round(
                    sum(p.import_kwh * schedule.rate_at(p.start, "import") for p in items), 1
                ),
                export_revenue_pence=round(
                    sum(p.export_kwh * schedule.rate_at(p.start, "export") for p in items), 1
                ),
                min_soc_percent=min(socs) if socs else None,
                max_soc_percent=max(socs) if socs else None,
            )
        )
    return History(points=points, days=summaries)


class LookBackOut(BaseModel):
    """A past day in a sentence, with the detail: its weather against the forecast made the night
    before, and what the charge and the day saved."""

    day: date
    complete: bool
    basis: str  # "forecast", "recent" or "none"
    headline: str
    detail: str
    weather_headline: str | None
    summary: str
    points: list[str]
    load_kwh: float
    solar_kwh: float
    net_cost_pence: float
    grid_only_pence: float
    saved_pence: float
    forecast_load_kwh: float | None
    forecast_solar_kwh: float | None
    charged_soc_percent: float | None
    best_soc_percent: float | None


@router.get("/look-back", response_model=LookBackOut)
def look_back_day(
    day: date | None = Query(default=None, description="Local day; yesterday by default"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> LookBackOut:
    """How a past day went against what was forecast for it. 404 when it has no readings."""
    system = _system(user)
    zone = ZoneInfo(system.timezone)
    today = datetime.now(zone).date()
    day = day or today - timedelta(days=1)
    if day >= today:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Pick a day that has ended")
    start = datetime.combine(day, time(), zone).astimezone(timezone.utc).replace(tzinfo=None)
    end = (
        datetime.combine(day + timedelta(days=1), time(), zone)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    rows = db.scalars(
        select(UsageRecord).where(
            UsageRecord.system_id == system.id,
            UsageRecord.start >= start,
            UsageRecord.start < end,
        )
    ).all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No readings for that day yet")
    book = TariffTimeline.from_system(system)
    imp = exp = grid_only = 0.0
    for r in rows:
        at = r.start.replace(tzinfo=timezone.utc)
        rate = book.rate_at(at, "import")
        imp += r.grid_import_kwh * rate
        exp += r.grid_export_kwh * book.rate_at(at, "export")
        grid_only += r.load_kwh * rate
    weather = db.scalars(
        select(WeatherRecord).where(
            WeatherRecord.system_id == system.id,
            WeatherRecord.start >= start,
            WeatherRecord.start < end,
        )
    ).all()
    temps = [w.temperature_c for w in weather if w.temperature_c is not None]
    actual = DayActual(
        day=day,
        complete=len(rows) >= half_hours_in(day, zone),
        load_kwh=sum(r.load_kwh for r in rows),
        solar_kwh=sum(r.solar_kwh for r in rows),
        import_kwh=sum(r.grid_import_kwh for r in rows),
        export_kwh=sum(r.grid_export_kwh for r in rows),
        import_cost_pence=imp,
        export_revenue_pence=exp,
        grid_only_pence=grid_only,
        cloud_percent=daylight_cloud(
            [w.cloud_cover_percent for w in weather],
            [w.shortwave_radiation_wm2 for w in weather],
        ),
        temperature_c=sum(temps) / len(temps) if temps else None,
        has_panels=bool(system.arrays),
    )

    snap = db.scalar(
        select(ForecastSnapshot).where(
            ForecastSnapshot.system_id == system.id, ForecastSnapshot.day == day
        )
    )
    kept = (
        DayForecastKept(
            load_kwh=snap.load_kwh,
            solar_kwh=snap.solar_kwh,
            cloud_percent=snap.cloud_percent,
            temperature_c=snap.temperature_c,
            target_soc_percent=snap.target_soc_percent,
        )
        if snap
        else None
    )
    recent = db.scalars(
        select(UsageRecord).where(
            UsageRecord.system_id == system.id,
            UsageRecord.start >= start - timedelta(days=RECENT_DAYS + 1),
            UsageRecord.start < start,
        )
    ).all()
    recent_solar = recent_daily_solar(
        [r.start.replace(tzinfo=timezone.utc).astimezone(zone).date() for r in recent],
        np.array([r.solar_kwh for r in recent], dtype=float),
        day,
    )

    charge = None
    if system.battery is not None:
        replayed = Replayer(db, system).replay(system.planning_mode, days=3)
        found = next((d for d in replayed.days if d.day == day), None)
        if found is not None:
            charge = ChargeHindsight(
                charged_soc_percent=found.soc_after_charge_percent,
                best_soc_percent=found.best_soc_percent,
                missed_saving_pence=found.missed_saving_pence,
            )

    story = look_back(actual, kept, recent_solar, charge)
    return LookBackOut(
        day=day,
        complete=actual.complete,
        basis=story.basis,
        headline=story.headline,
        detail=story.detail,
        weather_headline=story.weather_headline,
        summary=story.summary,
        points=story.points,
        load_kwh=round(actual.load_kwh, 2),
        solar_kwh=round(actual.solar_kwh, 2),
        net_cost_pence=story.net_cost_pence,
        grid_only_pence=round(grid_only, 1),
        saved_pence=story.saved_pence,
        forecast_load_kwh=kept.load_kwh if kept else None,
        forecast_solar_kwh=kept.solar_kwh if kept else None,
        charged_soc_percent=charge.charged_soc_percent if charge else None,
        best_soc_percent=charge.best_soc_percent if charge else None,
    )


@router.get("/savings", response_model=Savings)
def savings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> Savings:
    """Every complete local day imported: what it cost as metered, beside what the home's use
    would have cost bought from the grid at the same rates. Days missing any half hour are left
    out, so a gap in the data isn't counted as a saving."""
    system = _system(user)
    zone = ZoneInfo(system.timezone)
    book = TariffTimeline.from_system(system)
    totals: dict[date, list[float]] = {}
    for r in db.scalars(
        select(UsageRecord).where(UsageRecord.system_id == system.id).order_by(UsageRecord.start)
    ):
        start = r.start.replace(tzinfo=timezone.utc)
        rate = book.rate_at(start, "import")
        t = totals.setdefault(start.astimezone(zone).date(), [0, 0.0, 0.0, 0.0, 0.0])
        t[0] += 1
        t[1] += r.load_kwh
        t[2] += r.load_kwh * rate
        t[3] += r.grid_import_kwh * rate
        t[4] += r.grid_export_kwh * book.rate_at(start, "export")
    return Savings(
        days=[
            SavingsDay(
                day=day,
                house_kwh=round(house, 2),
                grid_only_pence=round(grid, 1),
                import_cost_pence=round(imported, 1),
                export_revenue_pence=round(exported, 1),
            )
            for day, (count, house, grid, imported, exported) in sorted(totals.items())
            if count == half_hours_in(day, zone)
        ]
    )
