import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import {
  ChargePlan,
  History,
  LookBack,
  PlanLock,
  PlanLockMode,
  PlanningMode,
  Replay,
  ReplayDetail,
  Savings,
} from './models';

export const MODE_LABELS: Record<PlanningMode, string> = {
  maximise_own_usage: 'Maximise own usage',
  maximise_export_revenue: 'Maximise export revenue',
};

@Injectable({ providedIn: 'root' })
export class PlanService {
  private readonly http = inject(HttpClient);

  /** Plan for the next charge; `startSoc` is the battery level when the cheap window opens. */
  plan(options: { day?: string; startSoc?: number } = {}): Observable<ChargePlan> {
    let params = new HttpParams();
    if (options.day) params = params.set('day', options.day);
    if (options.startSoc !== undefined) params = params.set('start_soc_percent', options.startSoc);
    return this.http.get<ChargePlan>('/api/plan', { params });
  }

  /** When the next charge's plan locks, and whether it has. */
  lock(): Observable<PlanLock> {
    return this.http.get<PlanLock>('/api/plan/lock');
  }

  /** Choose when plans lock; `time` is "HH:MM" for a set time. */
  setLock(mode: PlanLockMode, time: string | null = null): Observable<PlanLock> {
    return this.http.put<PlanLock>('/api/plan/lock', { mode, time });
  }

  /** Past days replayed from the charge actually made. */
  replay(days = 30): Observable<Replay> {
    return this.http.get<Replay>('/api/plan/replay', { params: { days } });
  }

  /** One past day in full, for looking back at it. */
  replayDay(day: string): Observable<ReplayDetail> {
    return this.http.get<ReplayDetail>(`/api/plan/replay/${day}`);
  }

  /** Metered half hours and daily totals for the last `days` days imported. */
  history(days = 7): Observable<History> {
    return this.http.get<History>('/api/history', { params: { days } });
  }

  /** Every complete day's cost beside what the grid alone would have charged. */
  savings(): Observable<Savings> {
    return this.http.get<Savings>('/api/history/savings');
  }

  /** How a past day (yesterday by default) went against the forecast kept for it. */
  lookBack(day?: string): Observable<LookBack> {
    const params = day ? new HttpParams().set('day', day) : undefined;
    return this.http.get<LookBack>('/api/history/look-back', { params });
  }
}

/** "37p" or "£1.42"; negative amounts keep their sign. */
export function formatPence(pence: number): string {
  const rounded = Math.round(pence);
  if (Math.abs(rounded) >= 100) {
    return `${rounded < 0 ? '-' : ''}£${(Math.abs(rounded) / 100).toFixed(2)}`;
  }
  return `${rounded}p`;
}
