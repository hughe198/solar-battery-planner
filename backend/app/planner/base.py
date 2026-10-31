"""Charge planner (step 4): turn a day's forecast, the battery and the tariff into an overnight
charge target for each planning mode, and look back at how past charge decisions worked out."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from app.forecasting.base import Method, Range
from app.models import PlanningMode

StartSocSource = Literal["measured", "given", "assumed"]


@dataclass(frozen=True)
class Outcome:
    """One charge target played out over one version of the day. Money in pence."""

    load_kwh: float
    solar_kwh: float
    import_kwh: float
    export_kwh: float
    import_cost_pence: float
    export_revenue_pence: float
    net_cost_pence: float
    end_soc_percent: float
    # When the battery first fills, or first runs down to its reserve, during the day.
    full_at: datetime | None
    reserve_at: datetime | None


@dataclass(frozen=True)
class Recommendation:
    mode: PlanningMode
    target_soc_percent: float
    # Energy stored by the charge, and what it takes from the grid (more, after losses).
    stored_kwh: float
    charge_kwh: float
    charge_cost_pence: float
    # When to force charge (UTC), and whether that reaches the target before the window closes.
    force_charge_start: datetime | None
    force_charge_stop: datetime | None
    force_charge_minutes: int
    force_charge_reaches_target: bool
    # The battery is held (the house runs on the grid) from the window opening until the
    # forced charge starts, or for the whole window when no charge is needed.
    hold_start: datetime | None
    hold_stop: datetime | None
    # Before a free window the battery is force discharged (exported) down to this level,
    # ending as the window opens; it then refills for nothing. None when it isn't worth it.
    discharge_start: datetime | None
    discharge_stop: datetime | None
    discharge_kwh: float
    discharge_revenue_pence: float
    discharge_to_percent: float | None
    expected: Outcome
    # Usage at the top of its likely range with solar at the bottom, and the reverse.
    dull: Outcome
    bright: Outcome
    reasons: list[str]
    # What the strategy costs besides money: battery wear and the risks it takes on.
    downsides: list[str]


@dataclass(frozen=True)
class Candidate:
    """Average effective cost of one target under each mode (lower is better), in pence."""

    target_soc_percent: float
    own_usage_pence: float
    export_revenue_pence: float


@dataclass(frozen=True)
class TimelinePoint:
    """Expected flows for one half hour under the recommendation for the chosen mode."""

    start: datetime
    load_kwh: float
    solar_kwh: float
    import_kwh: float
    export_kwh: float
    soc_percent: float  # at the end of the half hour
    cheap: bool


@dataclass(frozen=True)
class ChargePlan:
    day: date
    mode: PlanningMode
    start_soc_percent: float
    max_soc_percent: float
    charge_start: datetime
    charge_end: datetime
    # Power into the cells while force charging, below and above the taper level, and how many
    # half hours of history it was learned from (0: from the battery settings).
    charge_rate_kw: float
    taper_rate_kw: float
    taper_from_percent: float
    charge_rate_samples: int
    # The chosen mode first.
    recommendations: list[Recommendation]
    candidates: list[Candidate]
    timeline: list[TimelinePoint]
    load: Range
    solar: Range
    load_method: Method
    solar_method: Method
    history_days: int
    # Where start_soc_percent came from: predicted from an imported reading, entered by the user
    # or assumed to be the battery's minimum. A prediction has the reading and the half hours
    # simulated from it up to the window opening.
    start_soc_source: StartSocSource = "assumed"
    reading_at: datetime | None = None
    reading_soc_percent: float | None = None
    lead_in: list[TimelinePoint] = field(default_factory=list)


@dataclass(frozen=True)
class ReplayDay:
    """A past day replayed through the simulator from the charge that was actually made.

    `actual_*` are metered; `simulated_*` are what the simulator says the same load and solar
    would have done from the same start, which checks the simulator. `best_*` is the target
    that would have cost least with hindsight, and `missed_saving_pence` what it would have
    saved against the charge actually made (both simulated, so like is compared with like).
    """

    day: date
    load_kwh: float
    solar_kwh: float
    soc_before_percent: float
    soc_after_charge_percent: float
    actual_import_kwh: float
    actual_export_kwh: float
    actual_net_cost_pence: float
    simulated_import_kwh: float
    simulated_export_kwh: float
    soc_error_percent: float | None
    best_soc_percent: float
    missed_saving_pence: float
    wasted_export_kwh: float
    shortfall_import_kwh: float


@dataclass(frozen=True)
class ReplayPoint:
    """One metered half hour of a replayed cycle, with the battery level the hindsight best charge
    would have given at the end of it (None during the charge window)."""

    start: datetime
    load_kwh: float
    solar_kwh: float
    import_kwh: float
    export_kwh: float
    soc_percent: float | None
    cheap: bool
    best_soc_percent: float | None


@dataclass(frozen=True)
class ReplayDetail:
    """One replayed day in full, for looking back at it."""

    day: ReplayDay
    comments: list[str]
    points: list[ReplayPoint]


@dataclass(frozen=True)
class Review:
    """Plain-language look back at the most recent replayed day."""

    day: ReplayDay
    comments: list[str]


@dataclass(frozen=True)
class Replay:
    mode: PlanningMode
    days: list[ReplayDay]
    # Simulated against metered totals over all days, as % of metered.
    import_error_percent: float | None
    export_error_percent: float | None
    soc_error_percent: float | None
    missed_saving_pence: float
    review: Review | None
