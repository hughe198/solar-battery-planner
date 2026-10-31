from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

INTERVAL = timedelta(minutes=30)


@dataclass(frozen=True)
class UsageInterval:
    """Energy totals for one interval, in kWh. Timestamps are UTC."""

    start: datetime
    end: datetime
    load_kwh: float
    solar_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_soc_percent: float | None = None


class UsageImporter(Protocol):
    def fetch(self, start: datetime, end: datetime) -> Iterable[UsageInterval]: ...


def to_utc(moment: datetime) -> datetime:
    """Aware datetime in UTC. Naive values are assumed to be UTC already."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def interval_start(moment: datetime) -> datetime:
    """Start of the half hour containing `moment`, in UTC."""
    moment = to_utc(moment)
    return moment.replace(minute=moment.minute - moment.minute % 30, second=0, microsecond=0)
