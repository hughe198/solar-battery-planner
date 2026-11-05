import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { UsageExpectation, UsageExpectations, UsageLevel } from './models';

export const USAGE_LABELS: Record<UsageLevel, string> = {
  away: 'Away',
  low: 'Low',
  normal: 'Normal',
  high: 'High',
};

/** Days marked Away, Low or High, and nights the electric car will charge. */
@Injectable({ providedIn: 'root' })
export class UsageService {
  private readonly http = inject(HttpClient);

  list(range: { start?: string; end?: string } = {}): Observable<UsageExpectations> {
    let params = new HttpParams();
    if (range.start) params = params.set('start', range.start);
    if (range.end) params = params.set('end', range.end);
    return this.http.get<UsageExpectations>('/api/usage-expectations', { params });
  }

  /** Normal with no car charge clears the day. */
  setDay(
    day: string,
    level: UsageLevel,
    carKwh: number | null = null,
  ): Observable<UsageExpectation> {
    return this.http.put<UsageExpectation>(`/api/usage-expectations/${day}`, {
      level,
      car_kwh: carKwh,
    });
  }

  /** The same level on every day from start to end, such as a holiday. */
  setRange(start: string, end: string, level: UsageLevel): Observable<UsageExpectation[]> {
    return this.http.put<UsageExpectation[]>('/api/usage-expectations', { start, end, level });
  }
}
