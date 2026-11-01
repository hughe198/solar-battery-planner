import { Component, computed, input, output, signal } from '@angular/core';
import { InProgressCharge, ReplayDay } from '../core/models';
import { formatPence } from '../core/plan.service';

/** Lost less than this and the charge was about right, as the replay's own review says. */
const ABOUT_RIGHT_PENCE = 1;
const COSTLY_PENCE = 20;

type Rating = 'good' | 'fair' | 'poor';

export function rating(lostPence: number): Rating {
  if (lostPence < ABOUT_RIGHT_PENCE) return 'good';
  return lostPence < COSTLY_PENCE ? 'fair' : 'poor';
}

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function monthOf(day: string): string {
  return day.slice(0, 7);
}

function shiftMonth(month: string, by: number): string {
  const [year, number] = month.split('-').map(Number);
  return new Date(Date.UTC(year, number - 1 + by, 1)).toISOString().slice(0, 7);
}

/** A month of replayed charges, one square per day, coloured by what the decision lost. */
@Component({
  selector: 'app-charge-calendar',
  template: `
    @if (shownMonth(); as month) {
      <nav class="day-stepper" aria-label="Choose a month">
        <button
          type="button"
          class="secondary"
          aria-label="Previous month"
          [disabled]="month <= firstMonth()"
          (click)="chosenMonth.set(shift(month, -1))"
        >
          ‹
        </button>
        <div class="day-stepper-label" aria-live="polite">
          <strong>{{ monthLabel(month) }}</strong>
          <span class="muted">{{ summary() }}</span>
        </div>
        <button
          type="button"
          class="secondary"
          aria-label="Next month"
          [disabled]="month >= lastMonth()"
          (click)="chosenMonth.set(shift(month, 1))"
        >
          ›
        </button>
      </nav>
      <div class="calendar" role="group" [attr.aria-label]="monthLabel(month)">
        @for (name of weekdays; track name) {
          <span class="calendar-weekday muted" aria-hidden="true">{{ name }}</span>
        }
        @for (cell of cells(); track cell.key) {
          @if (cell.replay; as d) {
            <button
              type="button"
              class="calendar-day"
              [class]="'calendar-day ' + rate(d.missed_saving_pence)"
              [class.selected]="d.day === selected()"
              [attr.aria-pressed]="d.day === selected()"
              [attr.aria-label]="describe(d)"
              (click)="pick.emit(d.day)"
            >
              <span class="calendar-date">{{ cell.date }}</span>
              <strong>{{ round(d.soc_after_charge_percent) }}%</strong>
            </button>
          } @else if (cell.key === inProgress()?.day) {
            <button
              type="button"
              class="calendar-day in-progress"
              [class.selected]="cell.key === selected()"
              [attr.aria-pressed]="cell.key === selected()"
              [attr.aria-label]="inProgressLabel()"
              (click)="pick.emit(cell.key)"
            >
              <span class="calendar-date">{{ cell.date }}</span>
              <strong>{{ round(inProgress()!.soc_after_percent) }}%</strong>
            </button>
          } @else if (cell.date) {
            <span class="calendar-day empty">
              <span class="calendar-date muted">{{ cell.date }}</span>
            </span>
          } @else {
            <span aria-hidden="true"></span>
          }
        }
      </div>
      <p class="legend muted">
        Each square shows the level the battery was charged to, and what that lost against the best
        charge with hindsight: <span class="key rating-good"></span> about right
        <span class="key rating-fair"></span> under {{ costly }}p
        <span class="key rating-poor"></span> {{ costly }}p or more.
        @if (inProgress()) {
          <span class="key in-progress"></span> in progress, with no score yet.
        }
        Grey days have no complete data.
      </p>
    }
  `,
})
export class ChargeCalendar {
  /** Oldest first. */
  readonly days = input.required<ReplayDay[]>();
  readonly selected = input<string | null>(null);
  /** The cycle under way, which has no score yet. */
  readonly inProgress = input<InProgressCharge | null>(null);
  readonly pick = output<string>();

  readonly chosenMonth = signal<string | null>(null);
  readonly weekdays = WEEKDAYS;
  readonly costly = COSTLY_PENCE;
  readonly shift = shiftMonth;
  readonly rate = rating;
  readonly round = Math.round;

  readonly firstMonth = computed(() => {
    const days = this.days();
    return days.length ? monthOf(days[0].day) : '';
  });
  readonly lastMonth = computed(() => {
    const days = this.days();
    const latest = this.inProgress()?.day ?? days[days.length - 1]?.day;
    return latest ? monthOf(latest) : '';
  });
  readonly shownMonth = computed(() => this.chosenMonth() ?? (this.lastMonth() || null));

  private readonly monthDays = computed(() => {
    const month = this.shownMonth();
    return this.days().filter((d) => monthOf(d.day) === month);
  });

  /** "£1.20 lost, about right on 12 of 30 days". */
  readonly summary = computed(() => {
    const days = this.monthDays();
    if (!days.length) return 'No days to replay';
    const lost = days.reduce((total, d) => total + d.missed_saving_pence, 0);
    const right = days.filter((d) => rating(d.missed_saving_pence) === 'good').length;
    return `${formatPence(lost)} lost, about right on ${right} of ${days.length} days`;
  });

  /** Blank cells up to the first Monday-based column, then every day of the month. */
  readonly cells = computed(() => {
    const month = this.shownMonth();
    if (!month) return [];
    const [year, number] = month.split('-').map(Number);
    const leading = (new Date(Date.UTC(year, number - 1, 1)).getUTCDay() + 6) % 7;
    const length = new Date(Date.UTC(year, number, 0)).getUTCDate();
    const byDay = new Map(this.monthDays().map((d) => [d.day, d]));
    return [
      ...Array.from({ length: leading }, (_, i) => ({
        key: `blank-${i}`,
        date: 0,
        replay: undefined,
      })),
      ...Array.from({ length }, (_, i) => {
        const key = `${month}-${String(i + 1).padStart(2, '0')}`;
        return { key, date: i + 1, replay: byDay.get(key) };
      }),
    ];
  });

  monthLabel(month: string): string {
    return new Intl.DateTimeFormat('en-GB', {
      month: 'long',
      year: 'numeric',
      timeZone: 'UTC',
    }).format(new Date(`${month}-01T00:00:00Z`));
  }

  inProgressLabel(): string {
    const current = this.inProgress();
    if (!current) return '';
    const date = new Intl.DateTimeFormat('en-GB', {
      weekday: 'long',
      day: 'numeric',
      month: 'long',
      timeZone: 'UTC',
    }).format(new Date(`${current.day}T00:00:00Z`));
    return `${date}: in progress, at ${Math.round(current.soc_after_percent)}% after the cheap window, no score yet`;
  }

  describe(d: ReplayDay): string {
    const date = new Intl.DateTimeFormat('en-GB', {
      weekday: 'long',
      day: 'numeric',
      month: 'long',
      timeZone: 'UTC',
    }).format(new Date(`${d.day}T00:00:00Z`));
    const lost =
      rating(d.missed_saving_pence) === 'good'
        ? 'about right'
        : `lost ${formatPence(d.missed_saving_pence)}`;
    return `${date}: charged to ${Math.round(d.soc_after_charge_percent)}%, best ${Math.round(d.best_soc_percent)}%, ${lost}`;
  }
}
