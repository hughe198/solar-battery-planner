import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ControlAction, ControlProposal, InverterControl } from './models';

/** Opt-in control of the user's Fox ESS inverter. Only `apply`, `clear`, `restore` and test
 * steps 2 to 6 write to the inverter. */
@Injectable({ providedIn: 'root' })
export class ControlService {
  private readonly http = inject(HttpClient);

  status(): Observable<InverterControl> {
    return this.http.get<InverterControl>('/api/control');
  }

  update(settings: {
    enabled: boolean;
    auto_apply?: boolean;
    accept?: boolean;
  }): Observable<InverterControl> {
    return this.http.put<InverterControl>('/api/control', settings);
  }

  preview(startSoc?: number): Observable<ControlProposal> {
    let params = new HttpParams();
    if (startSoc !== undefined) params = params.set('start_soc_percent', startSoc);
    return this.http.get<ControlProposal>('/api/control/preview', { params });
  }

  apply(fingerprint: string, startSoc?: number): Observable<ControlAction> {
    return this.http.post<ControlAction>('/api/control/apply', {
      fingerprint,
      start_soc_percent: startSoc ?? null,
    });
  }

  clear(): Observable<ControlAction> {
    return this.http.post<ControlAction>('/api/control/clear', {});
  }

  restore(): Observable<ControlAction> {
    return this.http.post<ControlAction>('/api/control/restore', {});
  }

  testStep(step: number): Observable<ControlAction> {
    return this.http.post<ControlAction>('/api/control/test', { step });
  }
}
