import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ChargePlan, History, Replay } from './models';

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

  /** Past days replayed from the charge actually made. */
  replay(days = 30): Observable<Replay> {
    return this.http.get<Replay>('/api/plan/replay', { params: { days } });
  }

  /** Metered half hours and daily totals for the last `days` days imported. */
  history(days = 7): Observable<History> {
    return this.http.get<History>('/api/history', { params: { days } });
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
