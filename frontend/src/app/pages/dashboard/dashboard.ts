import { DecimalPipe } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { errorMessage } from '../../core/errors';
import { ChargePlan, History } from '../../core/models';
import { PlanService, formatPence } from '../../core/plan.service';
import { SystemService } from '../../core/system.service';
import { cheapestImport, rateRange } from '../../core/tariff';
import { FlowChart } from '../../shared/flow-chart';
import { WeekChart } from '../../shared/week-chart';
import { MODE_LABELS } from '../plan/plan';

@Component({
  selector: 'app-dashboard',
  imports: [DecimalPipe, RouterLink, FlowChart, WeekChart],
  templateUrl: './dashboard.html',
})
export class Dashboard {
  private readonly plans = inject(PlanService);
  readonly system = toSignal(inject(SystemService).get());
  readonly history = signal<History | null>(null);
  readonly plan = signal<ChargePlan | null>(null);
  readonly planError = signal<string | null>(null);
  readonly modeLabels = MODE_LABELS;
  readonly pence = formatPence;

  readonly totalKwp = computed(() =>
    (this.system()?.arrays ?? []).reduce((sum, a) => sum + a.kwp, 0),
  );
  readonly cheapest = computed(() => cheapestImport(this.system()?.tariff.bands ?? []));
  readonly exportRange = computed(() => rateRange(this.system()?.tariff.bands ?? [], 'export'));
  readonly usableKwh = computed(() => {
    const battery = this.system()?.battery;
    return battery ? (battery.capacity_kwh * (100 - battery.min_soc_percent)) / 100 : 0;
  });

  /** Totals over the week shown. */
  readonly week = computed(() => {
    const days = this.history()?.days ?? [];
    if (!days.length) return null;
    const sum = (f: (d: (typeof days)[number]) => number) => days.reduce((t, d) => t + f(d), 0);
    const load = sum((d) => d.load_kwh);
    const imported = sum((d) => d.import_kwh);
    return {
      days: days.length,
      selfSufficiency: load > 0 ? Math.max(0, 1 - imported / load) * 100 : null,
      importCost: sum((d) => d.import_cost_pence),
      exportRevenue: sum((d) => d.export_revenue_pence),
      importKwh: imported,
      exportKwh: sum((d) => d.export_kwh),
    };
  });

  constructor() {
    this.plans.history().subscribe({ next: (h) => this.history.set(h), error: () => {} });
    this.plans.plan().subscribe({
      next: (p) => this.plan.set(p),
      error: (err) => this.planError.set(errorMessage(err)),
    });
  }
}
