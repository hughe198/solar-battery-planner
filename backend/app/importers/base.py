from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


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
