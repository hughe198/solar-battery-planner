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
