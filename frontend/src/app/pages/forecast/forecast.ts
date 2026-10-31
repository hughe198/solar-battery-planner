import { DecimalPipe } from '@angular/common';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { errorMessage } from '../../core/errors';
import { ForecastService } from '../../core/forecast.service';
import { DayForecast, ForecastMethod, IntervalForecast } from '../../core/models';
import { SystemService } from '../../core/system.service';

const WIDTH = 720;
const HEIGHT = 220;
const PAD = { left: 40, right: 8, top: 10, bottom: 24 };

export const METHOD_LABELS: Record<ForecastMethod, string> = {
  learned: 'learned from your history',
  blended: 'your history mixed with typical values (more data will sharpen it)',
  typical: 'a typical UK home, until you import usage data',
  physics: 'your panel size and orientation, until you import solar data',
};

type Series = (i: IntervalForecast) => number;

@Component({
  selector: 'app-forecast',
  imports: [DecimalPipe, RouterLink],
  templateUrl: './forecast.html',
})
export class Forecast implements OnInit {
  private readonly forecasts = inject(ForecastService);
  readonly system = toSignal(inject(SystemService).get());

  readonly forecast = signal<DayForecast | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly methodLabels = METHOD_LABELS;
  readonly width = WIDTH;
  readonly height = HEIGHT;
  readonly pad = PAD;

  readonly intervals = computed(() => this.forecast()?.intervals ?? []);

  readonly maxKwh = computed(() => {
    const top = Math.max(
      0.1,
      ...this.intervals().map((i) => Math.max(i.load_high_kwh, i.solar_high_kwh)),
    );
    return Math.ceil(top * 4) / 4;
  });

  readonly yTicks = computed(() => {
    const max = this.maxKwh();
    return [0, max / 2, max].map((kwh) => ({ kwh, y: this.y(kwh) }));
  });

  /** Labels every three hours, in the system's local time. */
  readonly xTicks = computed(() =>
    this.intervals()
      .map((interval, index) => ({ index, label: this.localTime(interval.start) }))
      .filter((t) => /^(00|03|06|09|12|15|18|21):00$/.test(t.label))
      .map((t) => ({ x: this.x(t.index), label: t.label.slice(0, 2) })),
  );

  readonly loadLine = computed(() => this.line((i) => i.load_kwh));
  readonly loadBand = computed(() =>
    this.band(
      (i) => i.load_low_kwh,
      (i) => i.load_high_kwh,
    ),
  );
  readonly solarLine = computed(() => this.line((i) => i.solar_kwh));
  readonly solarBand = computed(() =>
    this.band(
      (i) => i.solar_low_kwh,
      (i) => i.solar_high_kwh,
    ),
  );

  readonly backtestMax = computed(() => {
    const days = this.forecast()?.backtest?.days ?? [];
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
    this.load();
  }

  load(day?: string): void {
    this.loading.set(true);
    this.error.set(null);
    this.forecasts.day(day || undefined).subscribe({
      next: (forecast) => {
        this.forecast.set(forecast);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(errorMessage(err));
        this.loading.set(false);
      },
    });
  }

  /** "HH:MM" in the system's time zone. */
  localTime(iso: string): string {
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

  /** Width of a backtest bar as a percentage of the largest value. */
  barWidth(kwh: number): number {
    return (kwh / this.backtestMax()) * 100;
  }

  private x(index: number): number {
    const n = Math.max(1, this.intervals().length);
    return PAD.left + ((index + 0.5) / n) * (WIDTH - PAD.left - PAD.right);
  }

  private y(kwh: number): number {
    return HEIGHT - PAD.bottom - (kwh / this.maxKwh()) * (HEIGHT - PAD.top - PAD.bottom);
  }

  private line(value: Series): string {
    return this.intervals()
      .map(
        (interval, i) =>
          `${i ? 'L' : 'M'}${this.x(i).toFixed(1)},${this.y(value(interval)).toFixed(1)}`,
      )
      .join(' ');
  }

  private band(low: Series, high: Series): string {
    const items = this.intervals();
    if (!items.length) return '';
    const top = items.map((it, i) => `${this.x(i).toFixed(1)},${this.y(high(it)).toFixed(1)}`);
    const bottom = items
      .map((it, i) => `${this.x(i).toFixed(1)},${this.y(low(it)).toFixed(1)}`)
      .reverse();
    return `M${top.join(' L')} L${bottom.join(' L')} Z`;
  }
}
