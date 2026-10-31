import { DecimalPipe } from '@angular/common';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { errorMessage } from '../../core/errors';
import { ForecastService } from '../../core/forecast.service';
import { Backtest, History as WeekHistory, Replay, ReplayDetail } from '../../core/models';
import { PlanService, formatPence } from '../../core/plan.service';
import { SystemService } from '../../core/system.service';
import { ChargeCalendar } from '../../shared/charge-calendar';
import { FlowChart } from '../../shared/flow-chart';
import { WeekChart } from '../../shared/week-chart';

/** As far back as the API replays. */
const LOOK_BACK_DAYS = 120;

/**
 * Everything looking back: the last week, how each night's charge worked out, and how well the
 * forecast has done. Merges the backward-looking parts of the old home, plan and forecast pages.
 */
@Component({
  selector: 'app-history',
  imports: [DecimalPipe, RouterLink, ChargeCalendar, FlowChart, WeekChart],
  templateUrl: './history.html',
})
export class History implements OnInit {
  private readonly plans = inject(PlanService);
  private readonly forecasts = inject(ForecastService);
  readonly system = toSignal(inject(SystemService).get());
  readonly pence = formatPence;

  readonly history = signal<WeekHistory | null>(null);
  readonly replay = signal<Replay | null>(null);
  readonly backtest = signal<Backtest | null>(null);
  /** False until the forecast has answered, so "not enough history" doesn't flash up first. */
  readonly backtestLoaded = signal(false);

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

  readonly review = computed(() => this.replay()?.review ?? null);
  readonly reviewDays = computed(() => [...(this.replay()?.days ?? [])].reverse());

  /** The day picked on the calendar, starting at the latest. */
  readonly selectedDay = signal<string | null>(null);
  readonly detail = signal<ReplayDetail | null>(null);
  readonly detailError = signal<string | null>(null);

  readonly backtestMax = computed(() => {
    const days = this.backtest()?.days ?? [];
    return Math.max(
      1,
      ...days.flatMap((d) => [
        d.load_actual_kwh,
        d.load_predicted_kwh,
        d.solar_actual_kwh,
        d.solar_predicted_kwh,
      ]),
    );
  });

  ngOnInit(): void {
    this.plans.history().subscribe({ next: (h) => this.history.set(h), error: () => {} });
    this.plans.replay(LOOK_BACK_DAYS).subscribe({
      next: (r) => {
        this.replay.set(r);
        const latest = r.days[r.days.length - 1];
        if (latest) this.selectDay(latest.day);
      },
      error: () => {},
    });
    this.forecasts.accuracy().subscribe({
      next: (a) => {
        this.backtest.set(a.backtest);
        this.backtestLoaded.set(true);
      },
      error: () => this.backtestLoaded.set(true),
    });
  }

  selectDay(day: string): void {
    this.selectedDay.set(day);
    this.detail.set(null);
    this.detailError.set(null);
    this.plans.replayDay(day).subscribe({
      next: (detail) => {
        // Clicking quickly can leave an earlier day's answer arriving last.
        if (this.selectedDay() === day) this.detail.set(detail);
      },
      error: (err) => {
        if (this.selectedDay() === day) this.detailError.set(errorMessage(err));
      },
    });
  }

  dayLabel(day: string): string {
    return new Intl.DateTimeFormat('en-GB', {
      weekday: 'long',
      day: 'numeric',
      month: 'long',
      timeZone: 'UTC',
    }).format(new Date(`${day}T00:00:00Z`));
  }

  /** Width of a backtest bar as a percentage of the largest value. */
  barWidth(kwh: number): number {
    return (kwh / this.backtestMax()) * 100;
  }
}
