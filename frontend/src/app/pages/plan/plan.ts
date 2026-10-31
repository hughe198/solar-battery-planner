import { DecimalPipe } from '@angular/common';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { errorMessage } from '../../core/errors';
import { ChargePlan, PlanningMode, Replay } from '../../core/models';
import { PlanService, formatPence } from '../../core/plan.service';
import { SystemService } from '../../core/system.service';
import { FlowChart } from '../../shared/flow-chart';

export const MODE_LABELS: Record<PlanningMode, string> = {
  maximise_own_usage: 'Maximise own usage',
  maximise_export_revenue: 'Maximise export revenue',
};

@Component({
  selector: 'app-plan',
  imports: [DecimalPipe, RouterLink, FlowChart],
  templateUrl: './plan.html',
})
export class Plan implements OnInit {
  private readonly plans = inject(PlanService);
  readonly system = toSignal(inject(SystemService).get());

  readonly plan = signal<ChargePlan | null>(null);
  readonly replay = signal<Replay | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly modeLabels = MODE_LABELS;
  readonly pence = formatPence;

  readonly chosen = computed(() => this.plan()?.recommendations[0] ?? null);
  readonly other = computed(() => this.plan()?.recommendations[1] ?? null);
  readonly scenarios = computed(() => {
    const r = this.chosen();
    return r
      ? [
          { name: 'Expected', outcome: r.expected },
          { name: 'Dull day', outcome: r.dull },
          { name: 'Bright day', outcome: r.bright },
        ]
      : [];
  });
  readonly review = computed(() => this.replay()?.review ?? null);
  readonly reviewDays = computed(() => [...(this.replay()?.days ?? [])].reverse());

  ngOnInit(): void {
    this.load();
    this.plans.replay().subscribe({ next: (r) => this.replay.set(r), error: () => {} });
  }

  load(startSoc?: number): void {
    this.loading.set(true);
    this.error.set(null);
    this.plans.plan({ day: this.plan()?.day, startSoc }).subscribe({
      next: (plan) => {
        this.plan.set(plan);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(errorMessage(err));
        this.loading.set(false);
      },
    });
  }

  recalculate(value: string): void {
    const soc = Number(value);
    if (value !== '' && soc >= 0 && soc <= 100) this.load(soc);
  }

  /** "HH:MM" in the system's time zone. */
  localTime(iso: string | null): string {
    if (!iso) return '–';
    return new Intl.DateTimeFormat('en-GB', {
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
      timeZone: this.system()?.timezone,
    }).format(new Date(iso));
  }

  dayLabel(day: string): string {
    return new Intl.DateTimeFormat('en-GB', {
      weekday: 'long',
      day: 'numeric',
      month: 'long',
      timeZone: 'UTC',
    }).format(new Date(`${day}T00:00:00Z`));
  }

  /** "1 h 50 min" or "35 min". */
  duration(minutes: number): string {
    const h = Math.floor(minutes / 60);
    return h ? `${h} h ${minutes % 60} min` : `${minutes} min`;
  }

  /** Net cost as words: "costs 37p" or "earns £1.10". */
  net(pence: number): string {
    return pence >= 0 ? `costs ${formatPence(pence)}` : `earns ${formatPence(-pence)}`;
  }
}
