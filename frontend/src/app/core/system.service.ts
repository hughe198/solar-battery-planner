import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, tap } from 'rxjs';
import { AuthService } from './auth.service';
import { SystemConfig, TariffPreset } from './models';

@Injectable({ providedIn: 'root' })
export class SystemService {
  private readonly http = inject(HttpClient);
  private readonly auth = inject(AuthService);

  get(): Observable<SystemConfig> {
    return this.http.get<SystemConfig>('/api/system');
  }

  tariffPresets(): Observable<TariffPreset[]> {
    return this.http.get<TariffPreset[]>('/api/system/tariff-presets');
  }

  save(system: SystemConfig): Observable<SystemConfig> {
    return this.http
      .put<SystemConfig>('/api/system', system)
      .pipe(tap(() => this.auth.markHasSystem()));
  }
}
