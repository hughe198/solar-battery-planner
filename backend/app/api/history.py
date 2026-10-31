"""Recent metered history for the home page charts."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.forecast import _system
from app.database import get_db
from app.models import UsageRecord, User
from app.planner.simulator import Cycle, cycle_for
from app.security import current_user
from app.tariffs import TariffSchedule

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
    schedule = TariffSchedule.from_system(system)
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
