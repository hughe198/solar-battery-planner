import { DecimalPipe } from '@angular/common';
import { Component, computed, input, signal } from '@angular/core';
import { SavingsDay } from '../core/models';
import {
  SavingsWeek,
  axisMoney,
  change,
  dayMonth,
  defaultWeek,
  money,
  niceTicks,
  savingsRunning,
  savingsTotals,
  savingsWeeks,
} from '../core/savings';

const WIDTH = 720;
const HEIGHT = 240;
const PAD = { left: 56, right: 12, top: 18, bottom: 26 };
const PLOT_WIDTH = WIDTH - PAD.left - PAD.right;
const PLOT_HEIGHT = HEIGHT - PAD.top - PAD.bottom;
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

type Mode = 'period' | 'week';

/** A vertical scale over `ticks`, which run from the lowest value to the highest. */
function scale(ticks: number[]) {
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const y = (v: number) => PAD.top + PLOT_HEIGHT * (1 - (v - lo) / (hi - lo || 1));
  return { y, ticks: ticks.map((v) => ({ v, y: y(v), label: axisMoney(v) })) };
}

/** A bar from zero to `value`, whichever side of zero it falls. */
function bar(y: (v: number) => number, from: number, to: number) {
  const top = Math.min(y(from), y(to));
  return { y: top, height: Math.max(1, Math.abs(y(to) - y(from))) };
}

/**
 * What the panels and battery saved against buying every kWh from the grid, over the whole
 * period or one week at a time.
 */
@Component({
  selector: 'app-savings',
  imports: [DecimalPipe],
  template: `
    @if (totals(); as t) {
      <div class="savings-controls">
        <div class="switch" role="group" aria-label="Show">
          <button
            type="button"
            [attr.aria-pressed]="mode() === 'period'"
            (click)="mode.set('period')"
          >
            Whole period
          </button>
          <button type="button" [attr.aria-pressed]="mode() === 'week'" (click)="showWeek()">
            One week
          </button>
        </div>
        @if (mode() === 'week' && week(); as w) {
          <select class="week-pick" aria-label="Week" (change)="pick($event)">
            @for (o of weeksNewestFirst(); track o.week) {
              <option [value]="o.week" [selected]="o.week === w.week">
                Week of {{ label(o.week, true) }}{{ o.days < 7 ? ' (' + o.days + ' days)' : '' }}
              </option>
            }
          </select>
        }
      </div>

      @if (mode() === 'period') {
        <p>
          Over {{ t.days }} complete days, {{ label(t.first, true) }} to {{ label(t.last, true) }},
          your panels and battery left you <strong>{{ money(t.saving) }}</strong> better off than
          buying every kWh your home used from the grid.
        </p>
        <div class="tiles">
          <div class="tile">
            <span class="muted">From the grid alone</span>
            <strong>{{ money(t.gridOnly) }}</strong>
            <span class="muted">{{ t.houseKwh | number: '1.0-0' }} kWh at your import rates</span>
          </div>
          <div class="tile">
            <span class="muted">What you actually paid</span>
            <strong>{{ money(t.net) }}</strong>
            <span class="muted"
              >Imports {{ money(t.imports) }} less export income {{ money(t.exports) }}</span
            >
          </div>
          <div class="tile">
            <span class="muted">Saving</span>
            <strong class="better">{{ money(t.saving) }}</strong>
            <span class="muted">About {{ money(t.perDay) }} a day</span>
          </div>
        </div>
      } @else if (week(); as w) {
        <p>
          In the week of {{ label(w.week, true)
          }}{{ w.days < 7 ? ' (' + w.days + ' days of readings)' : '' }}, your panels and battery
          saved <strong>{{ money(w.saving) }}</strong
          >{{ w.change ? ', ' + comparedWith(w.change.saving) + ' the week before' : '' }}.
        </p>
        <div class="tiles">
          <div class="tile">
            <span class="muted">From the grid alone</span>
            <strong>{{ money(w.gridOnly) }}</strong>
            <span class="muted">Week before: {{ changeText(w, w.change?.gridOnly) }}</span>
          </div>
          <div class="tile">
            <span class="muted">What you actually paid</span>
            <strong>{{ money(w.net) }}</strong>
            <span class="muted"
              >Imports {{ money(w.imports) }} less export income {{ money(w.exports) }}</span
            >
            <span class="muted"
              >Week before:
              <span [class]="direction(w.change?.net, -1)">{{
                changeText(w, w.change?.net)
              }}</span></span
            >
          </div>
          <div class="tile">
            <span class="muted">Saving</span>
            <strong class="better">{{ money(w.saving) }}</strong>
            <span class="muted"
              >Week before:
              <span [class]="direction(w.change?.saving, 1)">{{
                changeText(w, w.change?.saving)
              }}</span></span
            >
          </div>
        </div>
      }

      @if (weekly(); as c) {
        <h3>Saving each week</h3>
        <svg
          class="savings-chart"
          [attr.viewBox]="'0 0 ' + width + ' ' + height"
          role="img"
          aria-label="Saving each week: imports avoided and export income"
        >
          @for (tk of c.ticks; track tk.v) {
            <line
              [class]="tk.v === 0 ? 'zero' : 'grid'"
              [attr.x1]="pad.left"
              [attr.x2]="width - pad.right"
              [attr.y1]="tk.y"
              [attr.y2]="tk.y"
            />
            <text class="axis" [attr.x]="pad.left - 6" [attr.y]="tk.y + 4" text-anchor="end">
              {{ tk.label }}
            </text>
          }
          @for (b of c.bars; track b.week) {
            <g
              class="week-bar"
              [class.dim]="mode() === 'week' && b.week !== week()?.week"
              (click)="showWeek(b.week)"
            >
              <title>{{ b.title }}</title>
              <rect
                class="avoided"
                [attr.x]="b.x"
                [attr.width]="b.w"
                [attr.y]="b.avoided.y"
                [attr.height]="b.avoided.height"
                rx="2"
              />
              <rect
                class="income"
                [attr.x]="b.x"
                [attr.width]="b.w"
                [attr.y]="b.income.y"
                [attr.height]="b.income.height"
                rx="2"
              />
              <rect
                class="hit"
                [attr.x]="b.slotX"
                [attr.y]="pad.top"
                [attr.width]="b.slotW"
                [attr.height]="plotHeight"
              />
              @if (b.label) {
                <text
                  class="axis"
                  [attr.x]="b.x + b.w / 2"
                  [attr.y]="height - 6"
                  text-anchor="middle"
                >
                  {{ b.label }}
                </text>
              }
            </g>
          }
        </svg>
        <p class="legend muted">
          <span class="key avoided"></span> Imports avoided: house use covered by your panels and
          battery, at your import rates, less any extra the battery drew from the grid.
          <span class="key income"></span> Export income. Weeks run Monday to Sunday; click one to
          look at it on its own.
        </p>
      }

      @if (mode() === 'period') {
        @if (running(); as c) {
          <h3>Running total</h3>
          <svg
            class="savings-chart"
            [attr.viewBox]="'0 0 ' + width + ' ' + height"
            role="img"
            aria-label="Running total: cost from the grid alone against what you paid"
          >
            @for (tk of c.ticks; track tk.v) {
              <line
                [class]="tk.v === 0 ? 'zero' : 'grid'"
                [attr.x1]="pad.left"
                [attr.x2]="width - pad.right"
                [attr.y1]="tk.y"
                [attr.y2]="tk.y"
              />
              <text class="axis" [attr.x]="pad.left - 6" [attr.y]="tk.y + 4" text-anchor="end">
                {{ tk.label }}
              </text>
            }
            @for (m of c.months; track m.x) {
              <text class="axis" [attr.x]="m.x" [attr.y]="height - 6" text-anchor="middle">
                {{ m.label }}
              </text>
            }
            <path class="gap" [attr.d]="c.area" />
            <path class="line grid-only" [attr.d]="c.gridLine" />
            <path class="line paid" [attr.d]="c.paidLine" />
            <text
              class="value"
              [attr.x]="width - pad.right"
              [attr.y]="c.gridEnd.y - 8"
              text-anchor="end"
            >
              {{ c.gridEnd.label }}
            </text>
            <text
              class="value"
              [attr.x]="width - pad.right"
              [attr.y]="c.paidEnd.y + 16"
              text-anchor="end"
            >
              {{ c.paidEnd.label }}
            </text>
            @for (p of c.points; track p.day) {
              <rect
                class="hit"
                [attr.x]="p.x - c.step / 2"
                [attr.y]="pad.top"
                [attr.width]="c.step"
                [attr.height]="plotHeight"
              >
                <title>{{ p.title }}</title>
              </rect>
            }
          </svg>
          <p class="legend muted">
            <span class="key grid-only"></span> From the grid alone.
            <span class="key paid"></span> What you paid, after export income. The shaded gap is the
            saving; below £0, exports have paid for more than your imports.
          </p>
        }
      } @else if (dayByDay(); as c) {
        <h3>Day by day</h3>
        <svg
          class="savings-chart"
          [attr.viewBox]="'0 0 ' + width + ' ' + height"
          role="img"
          aria-label="Each day of the week: cost from the grid alone against what you paid"
        >
          @for (tk of c.ticks; track tk.v) {
            <line
              [class]="tk.v === 0 ? 'zero' : 'grid'"
              [attr.x1]="pad.left"
              [attr.x2]="width - pad.right"
              [attr.y1]="tk.y"
              [attr.y2]="tk.y"
            />
            <text class="axis" [attr.x]="pad.left - 6" [attr.y]="tk.y + 4" text-anchor="end">
              {{ tk.label }}
            </text>
          }
          @for (d of c.days; track d.label) {
            <g>
              <title>{{ d.title }}</title>
              @if (d.grid; as g) {
                <rect
                  class="grid-only"
                  [attr.x]="d.x"
                  [attr.width]="c.barW"
                  [attr.y]="g.y"
                  [attr.height]="g.height"
                  rx="2"
                />
              }
              @if (d.paid; as p) {
                <rect
                  class="paid"
                  [attr.x]="d.x + c.barW + 3"
                  [attr.width]="c.barW"
                  [attr.y]="p.y"
                  [attr.height]="p.height"
                  rx="2"
                />
              }
              @if (!d.grid) {
                <text class="axis" [attr.x]="d.centre" [attr.y]="c.zeroY - 6" text-anchor="middle">
                  no data
                </text>
              }
              <text class="axis" [attr.x]="d.centre" [attr.y]="height - 6" text-anchor="middle">
                {{ d.label }}
              </text>
              <rect
                class="hit"
                [attr.x]="d.slotX"
                [attr.y]="pad.top"
                [attr.width]="c.slotW"
                [attr.height]="plotHeight"
              />
            </g>
          }
        </svg>
        <p class="legend muted">
          <span class="key grid-only"></span> From the grid alone.
          <span class="key paid"></span> What you paid, after export income. Hover a day for the
          figures.
        </p>
      }

      <details class="savings-notes">
        <summary>How the saving is worked out</summary>
        <p>
          Each half hour's house use, as your inverter metered it, is priced at the import rate in
          force then, as if it had all come from the grid. What you paid is your metered imports at
          the same rates, less your exports at the export rates. The saving is the difference, from
          the panels and battery together: the battery mostly stores solar, so the two can't be
          split cleanly.
        </p>
        <p>
          The standing charge is left out, as you pay it either way. So are days without a full set
          of half-hourly readings, including today. A home without panels or a battery might choose
          a different tariff, which would change the grid-only figure a little.
        </p>
      </details>
    } @else {
      <p class="muted">
        No complete days of readings yet. Import your usage data and this shows what your panels and
        battery have saved.
      </p>
    }
  `,
})
export class SavingsView {
  readonly days = input.required<SavingsDay[]>();
  readonly width = WIDTH;
  readonly height = HEIGHT;
  readonly pad = PAD;
  readonly plotHeight = PLOT_HEIGHT;
  readonly money = money;

  readonly mode = signal<Mode>('period');
  /** The week picked, or null for the default. */
  private readonly chosen = signal<string | null>(null);

  readonly totals = computed(() => savingsTotals(this.days()));
  readonly weeks = computed(() => savingsWeeks(this.days()));
  readonly weeksNewestFirst = computed(() => [...this.weeks()].reverse());
  readonly week = computed(() => {
    const weeks = this.weeks();
    const key = this.chosen() ?? defaultWeek(weeks);
    return weeks.find((w) => w.week === key) ?? null;
  });

  showWeek(week?: string): void {
    if (week) this.chosen.set(week);
    this.mode.set('week');
  }

  pick(event: Event): void {
    this.chosen.set((event.target as HTMLSelectElement).value);
  }

  label(day: string, long = false): string {
    return dayMonth(day, long);
  }

  /** "£1.46 more than", "48p less than" or "the same as". */
  comparedWith(pence: number): string {
    const rounded = Math.round(pence);
    if (rounded === 0) return 'the same as';
    return `${money(Math.abs(rounded))} ${rounded > 0 ? 'more' : 'less'} than`;
  }

  changeText(w: SavingsWeek, pence: number | undefined): string {
    if (pence === undefined) return 'none to compare';
    return change(pence) + (w.partWeek ? ', part week' : '');
  }

  /** "better" or "worse" for a change, where `good` is the sign that helps you. */
  direction(pence: number | undefined, good: 1 | -1): string {
    if (pence === undefined || Math.round(pence) === 0) return '';
    return pence * good > 0 ? 'better' : 'worse';
  }

  readonly weekly = computed(() => {
    const weeks = this.weeks();
    if (!weeks.length) return null;
    const top = Math.max(...weeks.map((w) => Math.max(0, w.importsAvoided) + w.exports));
    const low = Math.min(0, ...weeks.map((w) => w.importsAvoided));
    const { y, ticks } = scale(niceTicks(low, top));
    const slotW = PLOT_WIDTH / weeks.length;
    const w = Math.min(28, slotW * 0.7);
    const every = Math.max(1, Math.ceil(weeks.length / Math.floor(PLOT_WIDTH / 56)));
    return {
      ticks,
      bars: weeks.map((wk, i) => {
        const slotX = PAD.left + i * slotW;
        const base = Math.max(0, wk.importsAvoided);
        return {
          week: wk.week,
          slotX,
          slotW,
          x: slotX + (slotW - w) / 2,
          w,
          avoided: bar(y, 0, wk.importsAvoided),
          income: bar(y, base, base + wk.exports),
          label: i % every === 0 ? dayMonth(wk.week) : '',
          title:
            `Week of ${dayMonth(wk.week, true)}` +
            (wk.days < 7 ? ` (${wk.days} days)` : '') +
            `: saved ${money(wk.saving)}. Imports avoided ${money(wk.importsAvoided)}, ` +
            `export income ${money(wk.exports)}.`,
        };
      }),
    };
  });

  readonly running = computed(() => {
    const points = savingsRunning(this.days());
    if (points.length < 2) return null;
    const time = (day: string) => new Date(`${day}T00:00:00Z`).getTime();
    const t0 = time(points[0].day);
    const span = time(points[points.length - 1].day) - t0 || 1;
    const x = (day: string) => PAD.left + (PLOT_WIDTH * (time(day) - t0)) / span;
    const low = Math.min(0, ...points.map((p) => p.paid));
    const top = Math.max(...points.map((p) => p.gridOnly));
    const { y, ticks } = scale(niceTicks(low, top));
    const path = (f: (p: (typeof points)[number]) => number, list = points) =>
      list.map((p, i) => `${i ? 'L' : 'M'}${x(p.day).toFixed(1)},${y(f(p)).toFixed(1)}`).join('');
    const back = [...points].reverse();
    const last = points[points.length - 1];
    // A label at the first of each month that fits.
    const months: { x: number; label: string }[] = [];
    for (const p of points) {
      if (p.day.endsWith('-01') || p === points[0]) {
        const at = x(p.day);
        if (!months.length || at - months[months.length - 1].x > 60) {
          months.push({ x: Math.max(PAD.left + 16, at), label: dayMonth(p.day) });
        }
      }
    }
    return {
      ticks,
      months,
      gridLine: path((p) => p.gridOnly),
      paidLine: path((p) => p.paid),
      area: `${path((p) => p.gridOnly)}${path((p) => p.paid, back).replace('M', 'L')}Z`,
      gridEnd: { y: y(last.gridOnly), label: money(last.gridOnly) },
      paidEnd: { y: y(last.paid), label: money(last.paid) },
      step: PLOT_WIDTH / points.length,
      points: points.map((p) => ({
        day: p.day,
        x: x(p.day),
        title:
          `To ${dayMonth(p.day, true)}: from the grid alone ${money(p.gridOnly)}, ` +
          `you paid ${money(p.paid)}, saving ${money(p.gridOnly - p.paid)}.`,
      })),
    };
  });

  readonly dayByDay = computed(() => {
    const wk = this.week();
    if (!wk) return null;
    const monday = new Date(`${wk.week}T00:00:00Z`).getTime();
    const byDay = new Map(this.days().map((d) => [d.day, d]));
    const slots = WEEKDAYS.map((name, i) => {
      const day = new Date(monday + i * 86_400_000).toISOString().slice(0, 10);
      return { name, day, data: byDay.get(day) };
    });
    const values = slots.flatMap((s) =>
      s.data
        ? [s.data.grid_only_pence, s.data.import_cost_pence - s.data.export_revenue_pence]
        : [],
    );
    const { y, ticks } = scale(niceTicks(Math.min(0, ...values), Math.max(0, ...values)));
    const slotW = PLOT_WIDTH / 7;
    const barW = Math.min(26, (slotW - 14) / 2);
    return {
      ticks,
      slotW,
      barW,
      zeroY: y(0),
      days: slots.map((s, i) => {
        const slotX = PAD.left + i * slotW;
        const centre = slotX + slotW / 2;
        const label = `${s.name} ${new Date(`${s.day}T00:00:00Z`).getUTCDate()}`;
        if (!s.data) {
          return {
            label,
            slotX,
            centre,
            x: 0,
            grid: null,
            paid: null,
            title: `${label}: no full set of readings`,
          };
        }
        const paid = s.data.import_cost_pence - s.data.export_revenue_pence;
        return {
          label,
          slotX,
          centre,
          x: centre - barW - 1.5,
          grid: bar(y, 0, s.data.grid_only_pence),
          paid: bar(y, 0, paid),
          title:
            `${label}: from the grid alone ${money(s.data.grid_only_pence)}, ` +
            `you paid ${money(paid)}, saving ${money(s.data.grid_only_pence - paid)}.`,
        };
      }),
    };
  });
}
