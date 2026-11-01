import { DecimalPipe } from '@angular/common';
import { Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { switchMap, takeWhile, timer } from 'rxjs';
import {
  chargeLabel,
  dayLabel,
  describeInProgress,
  localDate,
  localTime,
  weekday,
} from '../../core/charge-summary';
import { errorMessage } from '../../core/errors';
import { ImportService } from '../../core/import.service';
import {
  ChargePlan,
  History as RecentHistory,
  ImportStatus,
  Recommendation,
  Replay,
} from '../../core/models';
import { PlanService, formatPence } from '../../core/plan.service';
import { SystemService } from '../../core/system.service';
import { FlowChart } from '../../shared/flow-chart';

const DAY_MS = 24 * 60 * 60 * 1000;
const SLOT_MS = 30 * 60 * 1000;
/** Fetch again when the readings are older than this. Each fetch is one or two Fox ESS calls. */
const STALE_MS = 30 * 60 * 1000;
const POLL_MS = 3000;
const ACTIVE = new Set(['queued', 'running']);

/**
 * What the user wants on signing in: what happened yesterday and with last night's charge, then
 * what's suggested for tonight.
 */
@Component({
  selector: 'app-home',
  imports: [DecimalPipe, RouterLink, FlowChart],
  templateUrl: './home.html',
})
export class Home implements OnInit {
  private readonly plans = inject(PlanService);
  private readonly imports = inject(ImportService);
  private readonly destroyRef = inject(DestroyRef);
  readonly system = toSignal(inject(SystemService).get());
  readonly pence = formatPence;
  readonly dayLabel = dayLabel;
  clock: () => number = () => Date.now();

  readonly recent = signal<RecentHistory | null>(null);
  readonly replay = signal<Replay | null>(null);
  readonly plan = signal<ChargePlan | null>(null);
  readonly planError = signal<string | null>(null);
  readonly status = signal<ImportStatus | null>(null);
  readonly updating = signal(false);
  readonly updateError = signal<string | null>(null);

  private readonly timeZone = computed(() => this.system()?.timezone);
  private readonly today = computed(() => localDate(this.clock(), this.timeZone()));
  readonly yesterdayDate = computed(() => localDate(this.clock() - DAY_MS, this.timeZone()));

  readonly yesterday = computed(
    () => this.recent()?.days.find((d) => d.day === this.yesterdayDate()) ?? null,
  );

  readonly net = computed(() => {
    const y = this.yesterday();
    return y ? y.import_cost_pence - y.export_revenue_pence : 0;
  });

  /** Yesterday's charge, once its whole cycle is in. */
  readonly yesterdaysCharge = computed(() => {
    const day = this.replay()?.review?.day;
    return day?.day === this.yesterdayDate() ? day : null;
  });

  readonly chargeLabel = computed(() => {
    const day = this.yesterdaysCharge();
    return day ? chargeLabel(day, this.timeZone()) : '';
  });

  readonly latestCharge = computed(() => {
    const current = this.replay()?.in_progress;
    return current ? describeInProgress(current, this.timeZone(), this.today()) : null;
  });

  /** The readings' end, as "Sunday 19:00". */
  readonly dataUpTo = computed(() => {
    const latest = this.latestReading();
    if (latest === null) return null;
    const iso = new Date(latest).toISOString();
    return `${weekday(iso, this.timeZone())} ${localTime(iso, this.timeZone())}`;
  });

  private readonly latestReading = computed(() => {
    const points = this.recent()?.points ?? [];
    const last = points[points.length - 1];
    return last ? new Date(last.start).getTime() + SLOT_MS : null;
  });

  readonly recommendation = computed<Recommendation | null>(
    () => this.plan()?.recommendations[0] ?? null,
  );

  /** "Force charge to 62%, 02:00–03:10 on Monday" or "No charge needed…". */
  readonly suggestion = computed(() => {
    const plan = this.plan();
    const r = this.recommendation();
    if (!plan || !r) return null;
    const zone = this.timeZone();
    const on = `on ${weekday(plan.charge_start, zone)}`;
    const span = (start: string, stop: string | null) =>
      `${localTime(start, zone)}–${stop ? localTime(stop, zone) : ''}`;
    const headline = r.force_charge_start
      ? `Charge to ${Math.round(r.target_soc_percent)}%`
      : 'No charge needed';
    const steps: string[] = [];
    if (r.hold_start) {
      steps.push(`Hold the battery ${span(r.hold_start, r.hold_stop)} ${on}, house on the grid.`);
    }
    if (r.force_charge_start) {
      steps.push(
        `Force charge ${span(r.force_charge_start, r.force_charge_stop)} ${on}: ` +
          `${r.charge_kwh.toFixed(1)} kWh, costing ${formatPence(r.charge_cost_pence)}.`,
      );
    }
    const before = Math.round(plan.start_soc_percent);
    const opening = localTime(plan.charge_start, zone);
    const going =
      plan.start_soc_source === 'measured'
        ? `Predicted ${before}% going in at ${opening}, from your latest reading.`
        : plan.start_soc_source === 'assumed'
          ? `Assumes ${before}% going in at ${opening}, as there's no recent reading.`
          : '';
    return {
      headline,
      steps,
      going,
      left: `Expected to leave ${Math.round(r.expected.end_soc_percent)}% when the next cheap period starts.`,
      why: r.reasons[0] ?? '',
    };
  });

  ngOnInit(): void {
    this.load();
    this.imports.status().subscribe({
      next: (status) => {
        this.status.set(status);
        if (status.latest_job && ACTIVE.has(status.latest_job.status)) {
          this.updating.set(true);
          this.waitForImport();
        } else if (status.fox_ess && this.isStale(status)) {
          this.update();
        }
      },
      error: () => {},
    });
  }

  /** Fetch yesterday and today from Fox ESS. Only reads, and skips any complete days stored. */
  update(): void {
    this.updating.set(true);
    this.updateError.set(null);
    const today = this.today();
    this.imports.syncFoxEss(this.yesterdayDate(), today).subscribe({
      next: () => this.waitForImport(),
      error: (err) => {
        this.updating.set(false);
        this.updateError.set(errorMessage(err));
      },
    });
  }

  private isStale(status: ImportStatus): boolean {
    const last = status.usage.last;
    if (!last) return true;
    // Stored times are UTC, with or without saying so.
    const latest = new Date(/(Z|[+-]\d\d:\d\d)$/.test(last) ? last : `${last}Z`).getTime();
    return this.clock() - latest > STALE_MS;
  }

  private waitForImport(): void {
    timer(POLL_MS, POLL_MS)
      .pipe(
        switchMap(() => this.imports.status()),
        takeWhile((status) => {
          this.status.set(status);
          const job = status.latest_job;
          return !!job && ACTIVE.has(job.status);
        }, true),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((status) => {
        const job = status.latest_job;
        if (job && ACTIVE.has(job.status)) return;
        this.updating.set(false);
        if (job?.status === 'failed') {
          this.updateError.set(job.message ?? "The latest readings couldn't be fetched.");
        }
        this.load();
      });
  }

  private load(): void {
    this.plans.history(2).subscribe({ next: (h) => this.recent.set(h), error: () => {} });
    this.plans.replay(2).subscribe({ next: (r) => this.replay.set(r), error: () => {} });
    this.plans.plan().subscribe({
      next: (plan) => {
        this.plan.set(plan);
        this.planError.set(null);
      },
      error: (err) => this.planError.set(errorMessage(err)),
    });
  }
}
