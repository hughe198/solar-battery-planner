export type PlanningMode = 'maximise_own_usage' | 'maximise_export_revenue';
export type DataSourceKind = 'none' | 'fox_ess' | 'csv';

export interface User {
  id: number;
  email: string;
  has_system: boolean;
}

export interface Battery {
  capacity_kwh: number;
  min_soc_percent: number;
  max_charge_kw: number;
  max_discharge_kw: number;
  round_trip_efficiency_percent: number;
  /** Pence of wear per full charge cycle (battery price / rated cycles); 0 to ignore. */
  wear_pence_per_cycle: number;
}

export interface SolarArray {
  name: string;
  kwp: number;
  tilt_degrees: number;
  azimuth_degrees: number;
}

export type RateDirection = 'import' | 'export';
/** Weekdays are Monday to Friday. */
export type RateDays = 'all' | 'weekdays' | 'weekends';

/**
 * A rate for a window of the day, in local time on the hour or half hour ("HH:MM" or
 * "HH:MM:SS"). The window may cross midnight; end "00:00" is midnight and start == end is
 * all day.
 */
export interface TariffBand {
  direction: RateDirection;
  days: RateDays;
  start: string;
  end: string;
  /** Pence per kWh. */
  rate: number;
}

export interface Tariff {
  name: string;
  preset: string | null;
  standing_charge_pence_per_day: number;
  /** For each direction and kind of day, the bands cover all 24 hours exactly once. */
  bands: TariffBand[];
}

export interface TariffPreset {
  id: string;
  name: string;
  description: string;
  standing_charge_pence_per_day: number;
  bands: TariffBand[];
}

export interface SystemConfig {
  id?: number;
  name: string;
  latitude: number;
  longitude: number;
  timezone: string;
  planning_mode: PlanningMode;
  data_source: DataSourceKind;
  battery: Battery;
  tariff: Tariff;
  /** The local day changed rates began; today if left out. */
  tariff_effective_from?: string | null;
  arrays: SolarArray[];
}

/** A tariff and the local days it applied: no end is current, no start is from the beginning. */
export interface TariffPeriod {
  name: string;
  standing_charge_pence_per_day: number;
  valid_from: string | null;
  valid_until: string | null;
  bands: TariffBand[];
}

export interface FoxEssConnection {
  device_sn: string;
  /** Last four characters of the stored API key. */
  api_key_hint: string;
  last_synced_at: string | null;
  /** Set on connect when the battery's minimum charge was read from the inverter. */
  battery_min_soc_percent?: number | null;
  /** Set on connect when a forced charge power was found in the inverter's schedule. */
  battery_max_charge_kw?: number | null;
}

export type JobStatus = 'queued' | 'running' | 'done' | 'failed';

export interface ImportJob {
  id: number;
  status: JobStatus;
  start_date: string;
  end_date: string;
  days_total: number;
  days_done: number;
  days_skipped: number;
  intervals_imported: number;
  message: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface Coverage {
  count: number;
  first: string | null;
  last: string | null;
}

export interface ImportStatus {
  fox_ess: FoxEssConnection | null;
  usage: Coverage;
  weather: Coverage;
  matched_intervals: number;
  latest_job: ImportJob | null;
}

export interface CsvImportResult {
  intervals_imported: number;
  weather_hours_added: number;
  message: string | null;
}

/** How a forecast was made: from the user's history, a generic prior, or a mix of both. */
export type ForecastMethod = 'learned' | 'blended' | 'typical' | 'physics';

/** Predicted kWh for one half hour (UTC times), with a 10th–90th percentile range. */
export interface IntervalForecast {
  start: string;
  end: string;
  load_kwh: number;
  load_low_kwh: number;
  load_high_kwh: number;
  solar_kwh: number;
  solar_low_kwh: number;
  solar_high_kwh: number;
  temperature_c: number | null;
  cloud_cover_percent: number | null;
}

export interface EnergyRange {
  expected: number;
  low: number;
  high: number;
}

export interface BacktestDay {
  day: string;
  load_actual_kwh: number;
  load_predicted_kwh: number;
  solar_actual_kwh: number;
  solar_predicted_kwh: number;
  solar_physics_kwh: number;
}

/** Accuracy on the latest days, predicted by a model trained only on the days before them. */
export interface Backtest {
  train_days: number;
  test_days: number;
  load_mae_kwh: number;
  load_error_percent: number;
  solar_mae_kwh: number;
  solar_error_percent: number | null;
  solar_physics_error_percent: number | null;
  days: BacktestDay[];
}

export interface DayForecast {
  day: string;
  intervals: IntervalForecast[];
  load: EnergyRange;
  solar: EnergyRange;
  load_method: ForecastMethod;
  solar_method: ForecastMethod;
  history_days: number;
  /** Learned multiplier on each array's physics estimate, in array order. */
  array_performance: number[];
  backtest: Backtest | null;
}

/** One charge target played out over one version of the day. Money in pence. */
export interface PlanOutcome {
  load_kwh: number;
  solar_kwh: number;
  import_kwh: number;
  export_kwh: number;
  import_cost_pence: number;
  export_revenue_pence: number;
  net_cost_pence: number;
  end_soc_percent: number;
  full_at: string | null;
  reserve_at: string | null;
}

export interface Recommendation {
  mode: PlanningMode;
  target_soc_percent: number;
  /** Energy the charge stores, and what it draws from the grid after losses. */
  stored_kwh: number;
  charge_kwh: number;
  charge_cost_pence: number;
  /** When to force charge (UTC); null when no charge is needed. */
  force_charge_start: string | null;
  force_charge_stop: string | null;
  force_charge_minutes: number;
  force_charge_reaches_target: boolean;
  /** The battery is held (the house on the grid) until the forced charge, or all window. */
  hold_start: string | null;
  hold_stop: string | null;
  /** Before a free window the battery is exported down to discharge_to_percent. */
  discharge_start: string | null;
  discharge_stop: string | null;
  discharge_kwh: number;
  discharge_revenue_pence: number;
  discharge_to_percent: number | null;
  expected: PlanOutcome;
  dull: PlanOutcome;
  bright: PlanOutcome;
  reasons: string[];
  /** Costs besides money: battery wear and the risks the strategy takes on. */
  downsides: string[];
}

/** Average effective cost of one target under each mode (lower is better), in pence. */
export interface PlanCandidate {
  target_soc_percent: number;
  own_usage_pence: number;
  export_revenue_pence: number;
}

/** Half-hourly flows (UTC start); used for both planned and metered days. */
export interface FlowPoint {
  start: string;
  load_kwh: number;
  solar_kwh: number;
  import_kwh: number;
  export_kwh: number;
  soc_percent: number | null;
  cheap: boolean;
  /** Looking back: the level the hindsight best charge would have given. */
  best_soc_percent?: number | null;
}

export interface ChargePlan {
  day: string;
  mode: PlanningMode;
  start_soc_percent: number;
  max_soc_percent: number;
  charge_start: string;
  charge_end: string;
  /** Power into the cells while force charging, kW, below and above the taper level. */
  charge_rate_kw: number;
  taper_rate_kw: number;
  taper_from_percent: number;
  /** Half hours of history the rate was learned from; 0 = from the battery settings. */
  charge_rate_samples: number;
  /** The system's chosen mode first. */
  recommendations: Recommendation[];
  candidates: PlanCandidate[];
  timeline: FlowPoint[];
  /** Where start_soc_percent came from: an imported reading, the user, or the battery's minimum. */
  start_soc_source: 'measured' | 'given' | 'assumed';
  reading_at: string | null;
  reading_soc_percent: number | null;
  /** Half hours simulated from the reading up to the window opening. */
  lead_in: FlowPoint[];
  load: EnergyRange;
  solar: EnergyRange;
  load_method: ForecastMethod;
  solar_method: ForecastMethod;
  history_days: number;
}

export interface ReplayDay {
  day: string;
  load_kwh: number;
  solar_kwh: number;
  soc_before_percent: number;
  soc_after_charge_percent: number;
  actual_import_kwh: number;
  actual_export_kwh: number;
  actual_net_cost_pence: number;
  simulated_import_kwh: number;
  simulated_export_kwh: number;
  soc_error_percent: number | null;
  best_soc_percent: number;
  missed_saving_pence: number;
  wasted_export_kwh: number;
  shortfall_import_kwh: number;
}

export interface Replay {
  mode: PlanningMode;
  days: ReplayDay[];
  import_error_percent: number | null;
  export_error_percent: number | null;
  soc_error_percent: number | null;
  missed_saving_pence: number;
  review: { day: ReplayDay; comments: string[] } | null;
}

export interface ReplayDetail {
  day: ReplayDay;
  comments: string[];
  points: FlowPoint[];
}

export interface HistoryDay {
  day: string;
  load_kwh: number;
  solar_kwh: number;
  import_kwh: number;
  export_kwh: number;
  self_sufficiency_percent: number | null;
  import_cost_pence: number;
  export_revenue_pence: number;
  min_soc_percent: number | null;
  max_soc_percent: number | null;
}

export interface History {
  points: FlowPoint[];
  days: HistoryDay[];
}

/** One period of the inverter's schedule, local time; `end` is exclusive. */
export interface SchedulePeriod {
  start: string;
  end: string;
  work_mode: string;
  charge_to_percent: number | null;
  power_w: number | null;
  min_soc_percent: number | null;
}

export type ControlActionKind = 'opt_in' | 'opt_out' | 'apply' | 'clear' | 'restore' | 'test';
export type ControlStatus = 'ok' | 'mismatch' | 'failed' | 'skipped';

export interface ControlAction {
  id: number;
  created_at: string;
  kind: ControlActionKind;
  trigger: 'manual' | 'auto';
  status: ControlStatus;
  day: string | null;
  target_soc_percent: number | null;
  message: string;
  sent: Record<string, unknown> | null;
  read_back: Record<string, unknown> | null;
}

export interface TestStep {
  step: number;
  label: string;
  passed: boolean;
}

export interface InverterControl {
  connected: boolean;
  enabled: boolean;
  auto_apply: boolean;
  auto_apply_available: boolean;
  consented_at: string | null;
  test_level: number;
  test_steps: TestStep[];
  can_apply: boolean;
  can_auto_apply: boolean;
  has_original: boolean;
  original_saved_at: string | null;
  original: SchedulePeriod[];
  original_enabled: boolean | null;
  planner_periods: SchedulePeriod[];
  test_periods: SchedulePeriod[];
  actions: ControlAction[];
}

export interface ControlProposal {
  day: string;
  mode: PlanningMode;
  planned_soc_percent: number;
  charge_kwh: number;
  start: string | null;
  stop: string | null;
  hold_start: string | null;
  hold_stop: string | null;
  discharge_start: string | null;
  discharge_stop: string | null;
  discharge_soc_percent: number | null;
  discharge_power_w: number | null;
  target_soc_percent: number | null;
  power_w: number | null;
  min_soc_on_grid: number | null;
  schedule_enabled: boolean;
  max_periods: number;
  current: SchedulePeriod[];
  added: SchedulePeriod[];
  removed: SchedulePeriod[];
  after: SchedulePeriod[];
  request: Record<string, unknown>;
  notes: string[];
  fingerprint: string;
}
