import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, tap } from 'rxjs';
import { AuthService } from './auth.service';
import { SystemConfig } from './models';

@Injectable({ providedIn: 'root' })
export class SystemService {
  private readonly http = inject(HttpClient);
  private readonly auth = inject(AuthService);

  get(): Observable<SystemConfig> {
    return this.http.get<SystemConfig>('/api/system');
  }

  save(system: SystemConfig): Observable<SystemConfig> {
    return this.http
      .put<SystemConfig>('/api/system', system)
      .pipe(tap(() => this.auth.markHasSystem()));
  }
}
