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
  arrays: SolarArray[];
}

export interface FoxEssConnection {
  device_sn: string;
  /** Last four characters of the stored API key. */
  api_key_hint: string;
  last_synced_at: string | null;
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
