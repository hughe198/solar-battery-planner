from dataclasses import dataclass
from typing import Protocol

from app.forecasting.base import IntervalForecast
from app.models import PlanningMode, System


@dataclass(frozen=True)
class ChargePlan:
    target_soc_percent: float
    charge_kwh: float
    expected_import_cost_pence: float
    expected_export_revenue_pence: float
    mode: PlanningMode


class Planner(Protocol):
    def plan(
        self, system: System, forecast: list[IntervalForecast], current_soc_percent: float
    ) -> ChargePlan: ...
