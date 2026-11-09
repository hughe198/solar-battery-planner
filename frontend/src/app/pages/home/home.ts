import { Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { switchMap, takeWhile, timer } from 'rxjs';
import { localDate, localTime, weekday } from '../../core/charge-summary';
import { ControlService } from '../../core/control.service';
import { errorMessage } from '../../core/errors';
import { ImportService } from '../../core/import.service';
import { ForecastService } from '../../core/forecast.service';
import {
  ChargePlan,
  ControlAction,
  DayForecast,
  ImportStatus,
  LookBack,
  History as RecentHistory,
  Replay,
} from '../../core/models';
import { PlanService } from '../../core/plan.service';
import { SystemService } from '../../core/system.service';
import { BatteryCard, TodayCard, TonightCard, YesterdayCard } from './home-cards';

const DAY_MS = 24 * 60 * 60 * 1000;
const SLOT_MS = 30 * 60 * 1000;
/** Fetch again when the readings are older than this. Each fetch is one or two Fox ESS calls. */
const STALE_MS = 30 * 60 * 1000;
const POLL_MS = 3000;
const ACTIVE = new Set(['queued', 'running']);

/** After this local hour, tonight's plan matters more than yesterday, so it moves up. */
const EVENING_HOUR = 16;

/**
 * What the user wants on opening the app, on a phone: the battery now and last night's charge,
 * then today's forecast, then yesterday's savings and tonight's plan (swapped in the evening).
 */
@Component({
  selector: 'app-home',
  imports: [BatteryCard, TodayCard, YesterdayCard, TonightCard],
  templateUrl: './home.html',
  styleUrl: './home.scss',
})
export class Home implements OnInit {
  private readonly plans = inject(PlanService);
  private readonly imports = inject(ImportService);
  private readonly forecasts = inject(ForecastService);
  private readonly control = inject(ControlService);
  private readonly destroyRef = inject(DestroyRef);
  readonly system = toSignal(inject(SystemService).get());
  clock: () => number = () => Date.now();

  readonly recent = signal<RecentHistory | null>(null);
  readonly replay = signal<Replay | null>(null);
  readonly plan = signal<ChargePlan | null>(null);
  readonly planError = signal<string | null>(null);
  readonly todayForecast = signal<DayForecast | null>(null);
  readonly forecastError = signal<string | null>(null);
  readonly lookBack = signal<LookBack | null>(null);
  readonly lookBackMissing = signal(false);
  readonly controlActions = signal<ControlAction[] | null>(null);
  readonly status = signal<ImportStatus | null>(null);
  readonly updating = signal(false);
  readonly updateError = signal<string | null>(null);

  readonly timeZone = computed(() => this.system()?.timezone);
  readonly today = computed(() => localDate(this.clock(), this.timeZone()));
  readonly yesterdayDate = computed(() => localDate(this.clock() - DAY_MS, this.timeZone()));

  /** From 16:00 local time. */
  readonly evening = computed(() => {
    const hour = new Intl.DateTimeFormat('en-GB', {
      hour: 'numeric',
      hourCycle: 'h23',
      timeZone: this.timeZone(),
    }).format(new Date(this.clock()));
    return Number(hour) >= EVENING_HOUR;
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
    this.plans.lookBack().subscribe({
      next: (y) => {
        this.lookBack.set(y);
        this.lookBackMissing.set(false);
      },
      error: () => this.lookBackMissing.set(true),
    });
    this.forecasts.day(this.today()).subscribe({
      next: (f) => {
        this.todayForecast.set(f);
        this.forecastError.set(null);
      },
      error: (err) => this.forecastError.set(errorMessage(err)),
    });
    this.control.status().subscribe({
      // Only once automation is on does "sent" mean anything.
      next: (c) => this.controlActions.set(c.enabled ? c.actions : null),
      error: () => this.controlActions.set(null),
    });
    this.plans.plan().subscribe({
      next: (plan) => {
        this.plan.set(plan);
        this.planError.set(null);
      },
      error: (err) => this.planError.set(errorMessage(err)),
    });
  }
}
