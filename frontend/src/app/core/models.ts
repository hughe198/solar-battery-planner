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
}

export interface SolarArray {
  name: string;
  kwp: number;
  tilt_degrees: number;
  azimuth_degrees: number;
}

export interface Tariff {
  /** Pence per kWh. */
  day_import_rate: number;
  cheap_import_rate: number;
  export_rate: number;
  /** Local time, "HH:MM" or "HH:MM:SS". */
  cheap_start: string;
  cheap_end: string;
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
  arrays: SolarArray[];
}

export interface FoxEssConnection {
  device_sn: string;
  /** Last four characters of the stored API key. */
  api_key_hint: string;
  last_synced_at: string | null;
  /** Set on connect when the battery's minimum charge was read from the inverter. */
  battery_min_soc_percent?: number | null;
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
  expected: PlanOutcome;
  dull: PlanOutcome;
  bright: PlanOutcome;
  reasons: string[];
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
}

export interface ChargePlan {
  day: string;
  mode: PlanningMode;
  start_soc_percent: number;
  max_soc_percent: number;
  charge_start: string;
  charge_end: string;
  /** The system's chosen mode first. */
  recommendations: Recommendation[];
  candidates: PlanCandidate[];
  timeline: FlowPoint[];
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
