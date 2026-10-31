import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, tap } from 'rxjs';
import { AuthService } from './auth.service';
import { CsvImportResult, FoxEssConnection, ImportJob, ImportStatus } from './models';

@Injectable({ providedIn: 'root' })
export class ImportService {
  private readonly http = inject(HttpClient);
  private readonly auth = inject(AuthService);

  status(): Observable<ImportStatus> {
    return this.http.get<ImportStatus>('/api/import/status');
  }

  connectFoxEss(apiKey: string, deviceSn: string): Observable<FoxEssConnection> {
    return this.http.put<FoxEssConnection>('/api/import/fox-ess', {
      api_key: apiKey,
      device_sn: deviceSn,
    });
  }

  disconnectFoxEss(): Observable<void> {
    return this.http.delete<void>('/api/import/fox-ess');
  }

  syncFoxEss(startDate: string, endDate: string): Observable<ImportJob> {
    return this.http.post<ImportJob>('/api/import/fox-ess/sync', {
      start_date: startDate,
      end_date: endDate,
    });
  }

  uploadCsv(file: File): Observable<CsvImportResult> {
    const body = new FormData();
    body.append('file', file);
    return this.http.post<CsvImportResult>('/api/import/csv', body);
  }

  fetchMissingWeather(): Observable<CsvImportResult> {
    return this.http.post<CsvImportResult>('/api/import/weather', {});
  }

  deleteData(): Observable<void> {
    return this.http.delete<void>('/api/import/data');
  }

  deleteAccount(): Observable<void> {
    return this.http.delete<void>('/api/auth/me').pipe(tap(() => this.auth.logout()));
  }
}
