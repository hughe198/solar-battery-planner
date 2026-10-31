from datetime import date, datetime, time, timezone
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models import (
    ControlActionKind,
    ControlStatus,
    ControlTrigger,
    DataSourceKind,
    JobStatus,
    PlanningMode,
    RateDays,
    RateDirection,
    UsageSource,
)
from app.tariffs import coverage_problems


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    has_system: bool


class BatteryIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    capacity_kwh: float = Field(gt=0, le=200)
    min_soc_percent: float = Field(default=10, ge=0, lt=100)
    max_charge_kw: float = Field(gt=0, le=50)
    max_discharge_kw: float = Field(gt=0, le=50)
    round_trip_efficiency_percent: float = Field(default=90, gt=50, le=100)


class SolarArrayIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(default="Array", max_length=100)
    kwp: float = Field(gt=0, le=100)
    tilt_degrees: float = Field(ge=0, le=90)
    azimuth_degrees: float = Field(ge=0, lt=360)


class TariffBandIn(BaseModel):
    """A rate for a window of the day, in local time on half-hour boundaries. The window
    may cross midnight; end 00:00 means midnight and start == end means all day."""

    model_config = ConfigDict(from_attributes=True)

    direction: RateDirection
    days: RateDays = RateDays.all
    start: time
    end: time
    rate: float = Field(ge=-100, le=200, description="Pence per kWh")

    @field_validator("start", "end")
    @classmethod
    def on_half_hour(cls, value: time) -> time:
        if value.minute % 30 or value.second or value.microsecond:
            raise ValueError("times must be on the hour or half hour")
        return value.replace(tzinfo=None)


class TariffIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(default="My tariff", max_length=100)
    preset: str | None = Field(default=None, max_length=40)
    standing_charge_pence_per_day: float = Field(default=0, ge=0, le=500)
    bands: list[TariffBandIn] = Field(min_length=2, max_length=96)

    @model_validator(mode="after")
    def complete(self) -> "TariffIn":
        problems = coverage_problems(self.bands)
        if problems:
            raise ValueError("; ".join(problems))
        return self


class TariffPresetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    standing_charge_pence_per_day: float
    bands: list[TariffBandIn]


class SystemIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(default="My system", max_length=100)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = "Europe/London"
    planning_mode: PlanningMode = PlanningMode.maximise_own_usage
    data_source: DataSourceKind = DataSourceKind.none
    battery: BatteryIn
    tariff: TariffIn
    arrays: list[SolarArrayIn] = Field(min_length=1, max_length=6)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        if value not in available_timezones():
            raise ValueError("unknown timezone")
        return value


class SystemOut(SystemIn):
    id: int


def _utc(value: datetime | None) -> datetime | None:
    """SQLite and naive Postgres columns drop the zone; stored times are always UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class FoxEssConnectIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=200)
    device_sn: str = Field(min_length=4, max_length=64)

    @field_validator("api_key", "device_sn")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()


class FoxEssOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    device_sn: str
    api_key_hint: str
    last_synced_at: datetime | None
    # Set on connect when the battery's minimum charge was read from the inverter.
    battery_min_soc_percent: float | None = None
    # Set on connect when a forced charge power was found in the inverter's schedule.
    battery_max_charge_kw: float | None = None

    _as_utc = field_validator("last_synced_at")(_utc)


class SyncRequest(BaseModel):
    """Local days in the system's time zone, inclusive."""

    start_date: date
    end_date: date

    @model_validator(mode="after")
    def ordered(self) -> "SyncRequest":
        if self.end_date < self.start_date:
            raise ValueError("end date must be on or after start date")
        return self


class ImportJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: JobStatus
    start_date: date
    end_date: date
    days_total: int
    days_done: int
    days_skipped: int
    intervals_imported: int
    message: str | None
    created_at: datetime
    finished_at: datetime | None

    _as_utc = field_validator("created_at", "finished_at")(_utc)


class CsvImportOut(BaseModel):
    intervals_imported: int
    weather_hours_added: int
    message: str | None = None


class Coverage(BaseModel):
    count: int
    first: datetime | None
    last: datetime | None


class ImportStatus(BaseModel):
    fox_ess: FoxEssOut | None
    usage: Coverage
    weather: Coverage
    # Half hours that have both usage and weather, i.e. usable for training.
    matched_intervals: int
    latest_job: ImportJobOut | None


class WeatherOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    temperature_c: float | None
    cloud_cover_percent: float | None
    shortwave_radiation_wm2: float | None
    direct_radiation_wm2: float | None
    diffuse_radiation_wm2: float | None


class MatchedInterval(BaseModel):
    """One half hour of usage with the weather for the hour it falls in. Times are UTC."""

    start: datetime
    end: datetime
    load_kwh: float
    solar_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_soc_percent: float | None
    source: UsageSource
    weather: WeatherOut | None


class IntervalForecastOut(BaseModel):
    """Predicted kWh for one half hour (UTC), with a 10th-90th percentile range."""

    model_config = ConfigDict(from_attributes=True)

    start: datetime
    end: datetime
    load_kwh: float
    load_low_kwh: float
    load_high_kwh: float
    solar_kwh: float
    solar_low_kwh: float
    solar_high_kwh: float
    temperature_c: float | None
    cloud_cover_percent: float | None


class RangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    expected: float
    low: float
    high: float


class BacktestDayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date
    load_actual_kwh: float
    load_predicted_kwh: float
    solar_actual_kwh: float
    solar_predicted_kwh: float
    solar_physics_kwh: float


class BacktestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    train_days: int
    test_days: int
    load_mae_kwh: float
    load_error_percent: float
    solar_mae_kwh: float
    solar_error_percent: float | None
    solar_physics_error_percent: float | None
    days: list[BacktestDayOut]


class DayForecastOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date
    intervals: list[IntervalForecastOut]
    load: RangeOut
    solar: RangeOut
    load_method: str
    solar_method: str
    history_days: int
    array_performance: list[float]
    backtest: BacktestOut | None


class AccuracyOut(BaseModel):
    history_days: int
    backtest: BacktestOut | None


class OutcomeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    load_kwh: float
    solar_kwh: float
    import_kwh: float
    export_kwh: float
    import_cost_pence: float
    export_revenue_pence: float
    net_cost_pence: float
    end_soc_percent: float
    full_at: datetime | None
    reserve_at: datetime | None


class RecommendationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mode: PlanningMode
    target_soc_percent: float
    stored_kwh: float
    charge_kwh: float
    charge_cost_pence: float
    force_charge_start: datetime | None
    force_charge_stop: datetime | None
    force_charge_minutes: int
    force_charge_reaches_target: bool
    expected: OutcomeOut
    dull: OutcomeOut
    bright: OutcomeOut
    reasons: list[str]


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    target_soc_percent: float
    own_usage_pence: float
    export_revenue_pence: float


class TimelinePointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    start: datetime
    load_kwh: float
    solar_kwh: float
    import_kwh: float
    export_kwh: float
    soc_percent: float
    cheap: bool


class ChargePlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: date
    mode: PlanningMode
    start_soc_percent: float
    max_soc_percent: float
    charge_start: datetime
    charge_end: datetime
    charge_rate_kw: float
    taper_rate_kw: float
    taper_from_percent: float
    charge_rate_samples: int
    recommendations: list[RecommendationOut]
    candidates: list[CandidateOut]
    timeline: list[TimelinePointOut]
    load: RangeOut
    solar: RangeOut
    load_method: str
    solar_method: str
    history_days: int


class ReplayDayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day: ReplayDayOut
    comments: list[str]


class ReplayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mode: PlanningMode
    days: list[ReplayDayOut]
    import_error_percent: float | None
    export_error_percent: float | None
    soc_error_percent: float | None
    missed_saving_pence: float
    review: ReviewOut | None


class PeriodOut(BaseModel):
    """One period of the inverter's schedule, in local wall-clock time; `end` is exclusive."""

    start: str
    end: str
    work_mode: str
    charge_to_percent: float | None
    power_w: float | None
    min_soc_percent: float | None


class ControlActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    kind: ControlActionKind
    trigger: ControlTrigger
    status: ControlStatus
    day: date | None
    target_soc_percent: float | None
    message: str
    sent: dict | None
    read_back: dict | None

    _as_utc = field_validator("created_at")(_utc)


class TestStepOut(BaseModel):
    step: int
    label: str
    passed: bool


class ControlStatusOut(BaseModel):
    connected: bool
    enabled: bool
    auto_apply: bool
    # Whether this server runs the nightly automatic apply at all.
    auto_apply_available: bool
    consented_at: datetime | None
    test_level: int
    test_steps: list[TestStepOut]
    can_apply: bool
    can_auto_apply: bool
    has_original: bool
    original_saved_at: datetime | None
    original: list[PeriodOut]
    original_enabled: bool | None
    planner_periods: list[PeriodOut]
    test_periods: list[PeriodOut]
    actions: list[ControlActionOut]

    _as_utc = field_validator("consented_at", "original_saved_at")(_utc)


class ControlSettingsIn(BaseModel):
    enabled: bool
    auto_apply: bool = False
    # Must be true to turn control on: the user has read what it does.
    accept: bool = False


class ProposalOut(BaseModel):
    day: date
    mode: PlanningMode
    planned_soc_percent: float
    charge_kwh: float
    start: datetime | None
    stop: datetime | None
    target_soc_percent: float | None
    power_w: float | None
    min_soc_on_grid: float | None
    schedule_enabled: bool
    max_periods: int
    current: list[PeriodOut]
    added: list[PeriodOut]
    removed: list[PeriodOut]
    after: list[PeriodOut]
    # Exactly what would be sent to Fox ESS.
    request: dict
    notes: list[str]
    fingerprint: str


class ApplyIn(BaseModel):
    # From the preview, so what is written is what was shown.
    fingerprint: str
    start_soc_percent: float | None = Field(default=None, ge=0, le=100)


class TestStepIn(BaseModel):
    step: int = Field(ge=1, le=6)
