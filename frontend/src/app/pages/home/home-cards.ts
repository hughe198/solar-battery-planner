import { DecimalPipe } from '@angular/common';
import { Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import { describeInProgress, localDate, localTime, weekday } from '../../core/charge-summary';
import {
  ControlAction,
  DayForecast,
  History as RecentHistory,
  InProgressCharge,
  LookBack,
  Recommendation,
  ChargePlan,
  WeatherStory,
} from '../../core/models';
import { formatPence } from '../../core/plan.service';
import { Explain } from '../../shared/explain';
import { FlowChart } from '../../shared/flow-chart';

/** An emoji for the sky in a weather headline such as "Partly cloudy, 4°C". */
export function weatherIcon(headline: string | null | undefined): string {
  const sky = (headline ?? '').toLowerCase();
  if (sky.startsWith('sunny spells')) return '🌤️';
  if (sky.startsWith('sunny')) return '☀️';
  if (sky.startsWith('partly')) return '⛅';
  if (sky.startsWith('cloudy')) return '☁️';
  return '🌡️';
}

/** Card 1: how full the battery is now, and what last night's charge did. */
@Component({
  selector: 'app-battery-card',
  imports: [DecimalPipe, Explain, FlowChart],
  template: `
    <section class="card home-battery">
      <p class="eyebrow">
        <span>{{ title() }}</span>
        @if (readingTime(); as at) {
          <span>{{ at }}</span>
        }
      </p>
      @if (latestSoc() !== null) {
        <p class="figure">
          {{ latestSoc() | number: '1.0-0' }}%
          <small
            >battery now
            @if (storedKwh() !== null) {
              · {{ storedKwh() | number: '1.1-1' }} kWh
            }
          </small>
        </p>
        <div
          class="soc-bar"
          role="meter"
          aria-label="Battery level"
          aria-valuemin="0"
          aria-valuemax="100"
          [attr.aria-valuenow]="latestSoc()"
        >
          <span [style.width.%]="latestSoc()"></span>
        </div>
      } @else {
        <p class="muted">No battery reading yet.</p>
      }
      @if (charge(); as c) {
        <p class="status">{{ c.status }}</p>
        <app-explain key="home-battery" [sentence]="c.summary" [points]="[c.score]" />
      } @else {
        <p class="status muted">
          Last night's charge shows once readings from the cheap period are imported.
        </p>
      }
      @if (recent()?.points?.length) {
        <details class="disclosure chart-disclosure">
          <summary>Battery and grid since yesterday</summary>
          <app-flow-chart
            [points]="recent()!.points"
            [timeZone]="timeZone()"
            label="Battery level and grid import and export per half hour, yesterday and today so far"
          />
        </details>
      }
    </section>
  `,
})
export class BatteryCard {
  readonly recent = input<RecentHistory | null>(null);
  readonly inProgress = input<InProgressCharge | null | undefined>(null);
  readonly capacityKwh = input<number | null>(null);
  readonly timeZone = input<string | undefined>(undefined);
  readonly today = input('');
  readonly morning = input(true);

  readonly title = computed(() => (this.morning() ? 'This morning' : 'Battery'));

  private readonly latestPoint = computed(() => {
    const points = this.recent()?.points ?? [];
    for (let i = points.length - 1; i >= 0; i--) {
      if (points[i].soc_percent !== null) return points[i];
    }
    return null;
  });

  readonly latestSoc = computed(() => this.latestPoint()?.soc_percent ?? null);

  readonly storedKwh = computed(() => {
    const soc = this.latestSoc();
    const capacity = this.capacityKwh();
    return soc !== null && capacity ? (soc / 100) * capacity : null;
  });

  /** The end of the latest reading, as "08:10" today or "Monday 22:00" before that. */
  readonly readingTime = computed(() => {
    const point = this.latestPoint();
    if (!point) return null;
    const end = new Date(new Date(point.start).getTime() + 30 * 60 * 1000).toISOString();
    const zone = this.timeZone();
    const time = localTime(end, zone);
    return localDate(new Date(end).getTime(), zone) === this.today()
      ? time
      : `${weekday(end, zone)} ${time}`;
  });

  readonly charge = computed(() => {
    const c = this.inProgress();
    if (!c) return null;
    const words = describeInProgress(c, this.timeZone(), this.today());
    const percent = (v: number) => `${Math.round(v)}%`;
    const when = c.day === this.today() ? ' overnight' : '';
    const status = {
      charged: `Charged to ${percent(c.soc_peak_percent)}${when}`,
      held: `Held at ${percent(c.soc_after_percent)}${when}, house on the grid`,
      discharged: `Fell to ${percent(c.soc_after_percent)}${when}`,
    }[c.outcome];
    return { status: `${status}.`, summary: words.summary, score: words.score };
  });
}

/** Card 2: today's forecast use and solar, and what the weather does to them. */
@Component({
  selector: 'app-today-card',
  imports: [DecimalPipe, Explain],
  template: `
    <section class="card home-today">
      <p class="eyebrow">
        <span>{{ morning() ? 'Today, ' + dayName() : 'Today so far' }}</span>
        <span>{{ morning() ? 'forecast' : 'to ' + (soFar()?.until ?? '') }}</span>
      </p>
      @if (forecast(); as f) {
        <div class="pair">
          <div>
            @if (morning() || !soFar()) {
              <p class="figure">{{ f.load.expected | number: '1.1-1' }} <small>kWh use</small></p>
              <div class="range" aria-hidden="true">
                <span
                  class="load"
                  [style.left.%]="pos(f.load.low)"
                  [style.width.%]="span(f.load)"
                ></span>
                <i [style.left.%]="pos(f.load.expected)"></i>
              </div>
              <span class="label"
                >{{ f.load.low | number: '1.1-1' }}–{{ f.load.high | number: '1.1-1' }} likely</span
              >
            } @else {
              <p class="figure">{{ soFar()!.load | number: '1.1-1' }} <small>kWh used</small></p>
              <span class="label"
                >forecast {{ f.load.expected | number: '1.1-1' }} for the day</span
              >
            }
          </div>
          <div>
            @if (morning() || !soFar()) {
              <p class="figure">
                {{ f.solar.expected | number: '1.1-1' }} <small>kWh solar</small>
              </p>
              <div class="range" aria-hidden="true">
                <span
                  class="solar"
                  [style.left.%]="pos(f.solar.low)"
                  [style.width.%]="span(f.solar)"
                ></span>
                <i [style.left.%]="pos(f.solar.expected)"></i>
              </div>
              <span class="label"
                >{{ f.solar.low | number: '1.1-1' }}–{{
                  f.solar.high | number: '1.1-1'
                }}
                likely</span
              >
            } @else {
              <p class="figure">{{ soFar()!.solar | number: '1.1-1' }} <small>kWh solar</small></p>
              <span class="label">forecast {{ f.solar.expected | number: '1.1-1' }}</span>
            }
          </div>
        </div>
        @if (f.weather; as w) {
          <p class="weather-line">
            <span class="icon" aria-hidden="true">{{ icon(w) }}</span>
            <span
              ><strong>{{ w.headline }}</strong>
              @if (w.detail) {
                · {{ w.detail }}
              }
            </span>
          </p>
          <app-explain
            key="home-today"
            label="Weather"
            [sentence]="w.summary"
            [points]="w.points"
          />
        }
      } @else if (error(); as message) {
        <p class="form-error" role="alert">{{ message }}</p>
      } @else {
        <p class="muted">Working out today's forecast…</p>
      }
    </section>
  `,
})
export class TodayCard {
  readonly forecast = input<DayForecast | null>(null);
  readonly error = input<string | null>(null);
  readonly recent = input<RecentHistory | null>(null);
  readonly timeZone = input<string | undefined>(undefined);
  readonly today = input('');
  readonly morning = input(true);

  readonly dayName = computed(() =>
    this.today() ? weekday(`${this.today()}T12:00:00Z`, 'UTC') : '',
  );

  /** The scale for the range bars: the larger of the two highs, so they compare. */
  private readonly scale = computed(() => {
    const f = this.forecast();
    return f ? Math.max(f.load.high, f.solar.high, 1) : 1;
  });

  /** What the readings say so far today. */
  readonly soFar = computed(() => {
    const zone = this.timeZone();
    const points = (this.recent()?.points ?? []).filter(
      (p) => localDate(new Date(p.start).getTime(), zone) === this.today(),
    );
    if (!points.length) return null;
    const last = points[points.length - 1];
    return {
      load: points.reduce((sum, p) => sum + p.load_kwh, 0),
      solar: points.reduce((sum, p) => sum + p.solar_kwh, 0),
      until: localTime(
        new Date(new Date(last.start).getTime() + 30 * 60 * 1000).toISOString(),
        zone,
      ),
    };
  });

  pos(value: number): number {
    return Math.min(100, (value / this.scale()) * 100);
  }

  span(range: { low: number; high: number }): number {
    return Math.max(1, ((range.high - range.low) / this.scale()) * 100);
  }

  icon(w: WeatherStory): string {
    return weatherIcon(w.headline);
  }
}

/** Card 3: what yesterday saved, and how its weather compared with the forecast. */
@Component({
  selector: 'app-yesterday-card',
  imports: [Explain, RouterLink],
  template: `
    <section class="card home-yesterday">
      <p class="eyebrow">
        <span>Yesterday, {{ dayName() }}</span>
        <span>savings</span>
      </p>
      @if (lookBack(); as y) {
        <p class="figure">
          {{ pence(Math.abs(y.saved_pence)) }}
          <small>{{ y.saved_pence >= 0 ? 'saved' : 'more than grid alone' }}</small>
        </p>
        <p class="status">
          @if (y.net_cost_pence >= 0) {
            Cost {{ pence(y.net_cost_pence) }} instead of {{ pence(y.grid_only_pence) }} on grid
            import alone.
          } @else {
            Earned {{ pence(-y.net_cost_pence) }}, where grid import alone would have cost
            {{ pence(y.grid_only_pence) }}.
          }
        </p>
        <p class="weather-line">
          <span class="icon" aria-hidden="true">{{ icon(y) }}</span>
          <span
            ><strong>{{ y.headline }}</strong>
            @if (y.detail) {
              · {{ y.detail }}
            }
          </span>
        </p>
        <app-explain
          key="home-yesterday"
          label="Weather"
          [sentence]="y.summary"
          [points]="y.points"
        />
        <p class="card-link"><a routerLink="/history">See History ›</a></p>
      } @else if (missing()) {
        <p class="muted">
          No readings for yesterday yet.
          @if (!connected()) {
            <a routerLink="/data">Import your data</a>
          }
        </p>
      } @else {
        <p class="muted">Looking back at yesterday…</p>
      }
    </section>
  `,
})
export class YesterdayCard {
  readonly lookBack = input<LookBack | null>(null);
  /** No readings for the day, so there's nothing to look back on. */
  readonly missing = input(false);
  readonly connected = input(false);
  readonly day = input('');
  protected readonly Math = Math;
  readonly pence = formatPence;

  readonly dayName = computed(() => (this.day() ? weekday(`${this.day()}T12:00:00Z`, 'UTC') : ''));

  icon(y: LookBack): string {
    return weatherIcon(y.weather_headline);
  }
}

/** Card 4: the charge planned for tonight, and whether it has been sent to the inverter. */
@Component({
  selector: 'app-tonight-card',
  imports: [Explain, RouterLink],
  template: `
    <section class="card home-tonight">
      <p class="eyebrow">
        <span>Tonight</span>
        @if (sent(); as s) {
          <span class="badge" [class.good]="s.sent" [class.warn]="!s.sent">{{ s.label }}</span>
        }
      </p>
      @if (recommendation(); as r) {
        <p class="figure">
          {{ headline() }}
          @if (when()) {
            <small>{{ when() }}</small>
          }
        </p>
        <app-explain
          key="home-tonight"
          label="Plan"
          [sentence]="r.summary ?? ''"
          [points]="r.reasons"
        />
        <p class="card-link"><a routerLink="/tonight">See tonight's full plan ›</a></p>
      } @else if (error(); as message) {
        <p class="form-error" role="alert">{{ message }}</p>
      } @else {
        <p class="muted">Working out tonight's charge…</p>
      }
    </section>
  `,
})
export class TonightCard {
  readonly plan = input<ChargePlan | null>(null);
  readonly error = input<string | null>(null);
  /** The inverter control's recent actions, or null when automation isn't set up. */
  readonly actions = input<ControlAction[] | null>(null);
  readonly timeZone = input<string | undefined>(undefined);

  readonly recommendation = computed<Recommendation | null>(
    () => this.plan()?.recommendations[0] ?? null,
  );

  readonly headline = computed(() => {
    const r = this.recommendation();
    if (!r) return '';
    return r.force_charge_start ? `${Math.round(r.target_soc_percent)}%` : 'No charge';
  });

  /** "02:00–05:00 · about 98p", or when the battery is held instead. */
  readonly when = computed(() => {
    const r = this.recommendation();
    if (!r) return '';
    const zone = this.timeZone();
    const span = (a: string, b: string | null) =>
      `${localTime(a, zone)}–${b ? localTime(b, zone) : ''}`;
    if (r.force_charge_start) {
      return `${span(r.force_charge_start, r.force_charge_stop)} · about ${formatPence(r.charge_cost_pence)}`;
    }
    if (r.hold_start) return `battery held ${span(r.hold_start, r.hold_stop)}`;
    return 'the battery has enough';
  });

  /** Whether tonight's plan has been sent to the inverter; null when automation isn't on. */
  readonly sent = computed(() => {
    const actions = this.actions();
    const plan = this.plan();
    if (!actions || !plan) return null;
    const applied = actions.find(
      (a) => a.kind === 'apply' && a.day === plan.day && a.status === 'ok',
    );
    return applied
      ? { sent: true, label: 'Sent to inverter' }
      : { sent: false, label: 'Not sent yet' };
  });
}
