from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.models import System


@dataclass(frozen=True)
class IntervalForecast:
    start: datetime
    end: datetime
    load_kwh: float
    solar_kwh: float


class Forecaster(Protocol):
    def forecast(self, system: System, day: date) -> list[IntervalForecast]: ...
