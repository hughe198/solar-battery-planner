import { Component, computed, effect, inject, input, output, signal } from '@angular/core';
import { Observable } from 'rxjs';
import { errorMessage } from '../core/errors';
import { UsageExpectations, UsageLevel } from '../core/models';
import { USAGE_LABELS, UsageService } from '../core/usage.service';

const LEVELS: UsageLevel[] = ['away', 'low', 'normal', 'high'];

/**
 * How the user expects a day's usage to go, and (with an electric car) how much the car will
 * take in the cheap window before it. Saves straight away; the page redraws on `changed`.
 */
@Component({
  selector: 'app-usage-selector',
  template: `
    <div class="usage-selector">
      <div class="segmented" role="group" aria-label="Expected usage">
        @for (level of levels; track level) {
          <button
            type="button"
            [class.secondary]="level !== current()"
            [attr.aria-pressed]="level === current()"
            [disabled]="busy()"
            (click)="choose(level)"
          >
            {{ labels[level] }}
          </button>
        }
      </div>
      @if (hint(); as h) {
        <p class="muted usage-hint">{{ h }}</p>
      }
      @if (showCar() && data()?.has_electric_car) {
        <label class="car-charge">
          Car charging in the cheap window before this day (kWh)
          <span class="row">
            <input
              #car
              type="number"
              min="0"
              max="150"
              step="1"
              name="car-kwh"
              [value]="carKwh() ?? ''"
              placeholder="None"
            />
            <button
              type="button"
              class="secondary"
              [disabled]="busy()"
              (click)="saveCar(car.value)"
            >
              Save
            </button>
          </span>
        </label>
      }
      @if (showRange()) {
        <details class="holiday">
          <summary>Mark a holiday</summary>
          <div class="row">
            <label>From <input #from type="date" name="away-from" [value]="day()" /></label>
            <label>To <input #to type="date" name="away-to" [value]="day()" /></label>
          </div>
          <div class="row">
            <button
              type="button"
              [disabled]="busy()"
              (click)="markRange(from.value, to.value, 'away')"
            >
              Mark as away
            </button>
            <button
              type="button"
              class="secondary"
              [disabled]="busy()"
              (click)="markRange(from.value, to.value, 'normal')"
            >
              Clear
            </button>
          </div>
          @if (rangeDone(); as done) {
            <p class="muted" role="status">{{ done }}</p>
          }
        </details>
      }
      @if (error(); as e) {
        <p class="form-error" role="alert">{{ e }}</p>
      }
    </div>
  `,
})
export class UsageSelector {
  private readonly usage = inject(UsageService);

  /** Local day, "YYYY-MM-DD". */
  readonly day = input.required<string>();
  /** Offer the car charge; not for past days, which only need their level. */
  readonly showCar = input(true);
  /** The holiday range; Look back marks one day at a time. */
  readonly showRange = input(true);
  /** The level for `day`, once known and after each change. */
  readonly levelChange = output<UsageLevel>();
  /** A mark changed that may affect the shown forecast. */
  readonly changed = output<void>();

  readonly levels = LEVELS;
  readonly labels = USAGE_LABELS;
  readonly data = signal<UsageExpectations | null>(null);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly rangeDone = signal<string | null>(null);

  private readonly entry = computed(() => this.data()?.days.find((d) => d.day === this.day()));
  readonly current = computed<UsageLevel>(() => this.entry()?.level ?? 'normal');
  readonly carKwh = computed(() => this.entry()?.car_kwh ?? null);

  /** What the chosen option does, in the home's own figures where there are enough. */
  readonly hint = computed(() => {
    const data = this.data();
    if (!data) return null;
    const p = data.profile;
    const fixed = p.learned ? '' : ' (fixed until there are 14 days of history)';
    switch (this.current()) {
      case 'away':
        return p.base_daily_kwh !== null
          ? `Usage based on your ${p.base_daily_kwh.toFixed(1)} kWh a day base load${data.has_heat_pump ? ', plus heating' : ''}.`
          : `Usage halved${fixed}.`;
      case 'low':
        return `Usage scaled by ${p.low_factor.toFixed(2)}${fixed}.`;
      case 'high':
        return `Usage scaled by ${p.high_factor.toFixed(2)}${fixed}.`;
      default:
        return null;
    }
  });

  constructor() {
    effect(() => {
      this.day();
      this.rangeDone.set(null);
      this.reload();
    });
  }

  private reload(): void {
    const day = this.day();
    this.usage.list({ start: day, end: day }).subscribe({
      next: (data) => {
        if (day !== this.day()) return;
        this.data.set(data);
        this.levelChange.emit(this.current());
      },
      error: () => this.data.set(null),
    });
  }

  choose(level: UsageLevel): void {
    if (level === this.current()) return;
    this.save(this.usage.setDay(this.day(), level, this.carKwh()));
  }

  saveCar(value: string): void {
    const kwh = value === '' ? null : Number(value);
    if (kwh !== null && !(kwh >= 0 && kwh <= 150)) {
      this.error.set('Enter between 0 and 150 kWh, or leave it empty for none.');
      return;
    }
    this.save(this.usage.setDay(this.day(), this.current(), kwh || null));
  }

  markRange(start: string, end: string, level: UsageLevel): void {
    if (!start || !end || end < start) {
      this.error.set('Pick a start day and an end day on or after it.');
      return;
    }
    this.save(this.usage.setRange(start, end, level), () => {
      const days = Math.round((Date.parse(end) - Date.parse(start)) / 86_400_000) + 1;
      this.rangeDone.set(
        level === 'away'
          ? `${days} day${days === 1 ? '' : 's'} marked as away.`
          : `${days} day${days === 1 ? '' : 's'} back to normal.`,
      );
    });
  }

  private save(request: Observable<unknown>, done?: () => void): void {
    this.busy.set(true);
    this.error.set(null);
    request.subscribe({
      next: () => {
        this.busy.set(false);
        done?.();
        this.reload();
        this.changed.emit();
      },
      error: (err) => {
        this.busy.set(false);
        this.error.set(errorMessage(err));
      },
    });
  }
}
