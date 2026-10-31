import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { DayForecast } from './models';

@Injectable({ providedIn: 'root' })
export class ForecastService {
  private readonly http = inject(HttpClient);

  /** Forecast for a local day ("YYYY-MM-DD"); tomorrow when omitted. */
  day(day?: string): Observable<DayForecast> {
    const params = day ? new HttpParams().set('day', day) : undefined;
    return this.http.get<DayForecast>('/api/forecast', { params });
  }
}
