import { DecimalPipe } from '@angular/common';
import { Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { SystemService } from '../../core/system.service';

@Component({
  selector: 'app-dashboard',
  imports: [DecimalPipe, RouterLink],
  templateUrl: './dashboard.html',
})
export class Dashboard {
  readonly system = toSignal(inject(SystemService).get());

  readonly totalKwp = computed(() =>
    (this.system()?.arrays ?? []).reduce((sum, a) => sum + a.kwp, 0),
  );
  readonly usableKwh = computed(() => {
    const battery = this.system()?.battery;
    return battery ? (battery.capacity_kwh * (100 - battery.min_soc_percent)) / 100 : 0;
  });
}
