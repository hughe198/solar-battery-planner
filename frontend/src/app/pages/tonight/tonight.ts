import { DecimalPipe } from '@angular/common';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { errorMessage } from '../../core/errors';
import { ForecastService } from '../../core/forecast.service';
import { ChargePlan, DayForecast, ForecastMethod, IntervalForecast } from '../../core/models';
import { MODE_LABELS, PlanService, formatPence } from '../../core/plan.service';
import { SystemService } from '../../core/system.service';
import { cheapestImport, rateRange } from '../../core/tariff';

const WIDTH = 720;
const HEIGHT = 280;
const PAD = { left: 40, right: 8, top: 10, bottom: 24 };
const PLOT_HEIGHT = HEIGHT - PAD.top - PAD.bottom;
const SLOT_MS = 30 * 60 * 1000;
const DAY_MS = 24 * 60 * 60 * 1000;
const MAX_DAYS_AHEAD = 7;
// A tenth of the panels' rating. Checked against real east and west facing panels, where a quarter
// is hardly ever reached: this one still separates good days from poor ones into October.
const GOOD_SOLAR_SHARE = 0.1;
/** Half hours from 21:00 to midnight, for days too far ahead to start the chart at now. */
const EVENING_SLOTS = 6;

function addDays(day: string, days: number): string {
  const [year, month, date] = day.split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, date + days)).toISOString().slice(0, 10);
}

export const METHOD_LABELS: Record<ForecastMethod, string> = {
  learned: 'learned from your history',
  blended: 'your history mixed with typical values (more data will sharpen it)',
  typical: 'a typical UK home, until you import usage data',
  physics: 'your panel size and orientation, until you import solar data',
};

/** One half hour on the chart; null where the forecast or plan doesn't reach. */
interface Slot {
  start: number;
  load: number | null;
  loadLow: number | null;
  loadHigh: number | null;
  solar: number | null;
  solarLow: number | null;
  solarHigh: number | null;
  soc: number | null;
}

/**
 * The main page: the next charge for the day picked, with the forecast it was planned from.
 * Merges what were the home, charge plan and forecast pages.
 */
@Component({
  selector: 'app-tonight',
  imports: [DecimalPipe, RouterLink],
  templateUrl: './tonight.html',
})
export class Tonight implements OnInit {
  private readonly forecasts = inject(ForecastService);
  private readonly plans = inject(PlanService);
  readonly system = toSignal(inject(SystemService).get());

  readonly forecast = signal<DayForecast | null>(null);
  readonly plan = signal<ChargePlan | null>(null);
  /** The day before, for solar between now and the planned day when that starts tomorrow. */
  private readonly leadIn = signal<DayForecast | null>(null);
  clock: () => number = () => Date.now();
  private readonly now = signal(0);
  readonly showBattery = signal(true);
  readonly showUsage = signal(true);
  readonly showSolar = signal(true);
  readonly hovered = signal<number | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly methodLabels = METHOD_LABELS;
  readonly modeLabels = MODE_LABELS;
  readonly pence = formatPence;
  readonly width = WIDTH;
  readonly pad = PAD;

  /** Today in the system's time zone to 7 days ahead, as the API allows. */
  readonly firstDay = computed(() => this.localDate(this.now()));
  readonly lastDay = computed(() => addDays(this.firstDay(), MAX_DAYS_AHEAD));

  /** The day asked for, which shows straight away while its forecast loads. */
  private readonly chosenDay = signal<string | null>(null);
  readonly shownDay = computed(() => this.chosenDay() ?? this.forecast()?.day ?? null);

  /** How long on the selected day the panels are forecast to make a useful amount. */
  readonly goodSolar = computed(() => {
    const intervals = this.forecast()?.intervals ?? [];
    const kwp = (this.system()?.arrays ?? []).reduce((total, a) => total + a.kwp, 0);
    if (!intervals.length || kwp <= 0) return null;
    const kw = kwp * GOOD_SOLAR_SHARE;
    const good = intervals.flatMap((i, index) => (i.solar_kwh * 2 >= kw ? [index] : []));
    if (!good.length) return { kw, kwp, text: 'No good solar hours' };
    const first = good[0];
    const last = good[good.length - 1];
    const hours = good.length / 2;
    const from = this.localTime(intervals[first].start);
    const until = this.localTime(intervals[last].end);
    const unbroken = last - first + 1 === good.length;
    const when = unbroken ? `${from}–${until}` : `between ${from} and ${until}`;
    return { kw, kwp, text: `${hours} good solar ${hours === 1 ? 'hour' : 'hours'}, ${when}` };
  });

  readonly recommendation = computed(() => this.plan()?.recommendations?.[0] ?? null);

  /** How the chosen charge plays out if the day is as expected, duller or brighter. */
  readonly scenarios = computed(() => {
    const r = this.recommendation();
    return r
      ? [
          { name: 'Expected', outcome: r.expected },
          { name: 'Dull day', outcome: r.dull },
          { name: 'Bright day', outcome: r.bright },
        ]
      : [];
  });

  readonly totalKwp = computed(() =>
    (this.system()?.arrays ?? []).reduce((sum, a) => sum + a.kwp, 0),
  );
  readonly cheapest = computed(() => cheapestImport(this.system()?.tariff.bands ?? []));
  readonly exportRange = computed(() => rateRange(this.system()?.tariff.bands ?? [], 'export'));
  readonly usableKwh = computed(() => {
    const battery = this.system()?.battery;
    return battery ? (battery.capacity_kwh * (100 - battery.min_soc_percent)) / 100 : 0;
  });

  /**
   * With a plan: from the current half hour until the next charge window opens. A day further
   * ahead than tomorrow starts at 21:00 the evening before instead, or at the window if earlier.
   */
  readonly slots = computed<Slot[]>(() => {
    const toSlot = (i: IntervalForecast) => ({
      start: new Date(i.start).getTime(),
      load: i.load_kwh,
      loadLow: i.load_low_kwh,
      loadHigh: i.load_high_kwh,
      solar: i.solar_kwh,
      solarLow: i.solar_low_kwh,
      solarHigh: i.solar_high_kwh,
    });
    const day = (this.forecast()?.intervals ?? []).map(toSlot);
    const lead = (this.leadIn()?.intervals ?? []).map(toSlot);
    const timeline = (this.plan()?.timeline ?? []).map((p) => ({
      ...p,
      time: new Date(p.start).getTime(),
    }));
    if (!day.length || !timeline.length) {
      return day.map((d) => ({ ...d, soc: null }));
    }
    const leadIn = (this.plan()?.lead_in ?? []).map((p) => ({
      ...p,
      time: new Date(p.start).getTime(),
    }));
    const planned = new Map([...leadIn, ...timeline].map((p) => [p.time, p]));
    const forecast = new Map([...lead, ...day].map((d) => [d.start, d]));
    const last = timeline[timeline.length - 1].time;
    const nowSlot = Math.floor(this.now() / SLOT_MS) * SLOT_MS;
    // Back to the reading the battery line starts from, which is usually before now.
    const opening = Math.min(nowSlot, leadIn[0]?.time ?? nowSlot);
    const first =
      this.startsNow(day[0].start) && nowSlot <= last
        ? opening
        : Math.min(day[0].start - EVENING_SLOTS * SLOT_MS, timeline[0].time);
    const slots: Slot[] = [];
    for (let start = first; start <= last; start += SLOT_MS) {
      const point = planned.get(start);
      const d = forecast.get(start);
      slots.push({
        start,
        load: point?.load_kwh ?? d?.load ?? null,
        loadLow: d?.loadLow ?? null,
        loadHigh: d?.loadHigh ?? null,
        solar: point?.solar_kwh ?? d?.solar ?? null,
        solarLow: d?.solarLow ?? null,
        solarHigh: d?.solarHigh ?? null,
        soc: point?.soc_percent ?? null,
      });
    }
    return slots;
  });

  readonly hasBattery = computed(() => this.slots().some((s) => s.soc !== null));

  readonly height = HEIGHT;

  /** Breaks where the plan has no value, such as before the charge window opens. */
  readonly batteryLine = computed(() =>
    this.line(
      (s) => s.soc,
      (soc) => this.batteryY(soc),
    ),
  );

  /** The assumed level, flat from the start of the chart to where the simulation begins. */
  readonly assumedBatteryLine = computed(() => {
    const plan = this.plan();
    const assumed = plan?.start_soc_percent;
    const first = this.slots().findIndex((s) => s.soc !== null);
    if (assumed === undefined || plan?.start_soc_source === 'measured' || first <= 0) return '';
    const y = this.batteryY(assumed).toFixed(1);
    return `M${this.x(0).toFixed(1)},${y} L${this.x(first).toFixed(1)},${y}`;
  });

  readonly targetLine = computed(() => {
    const percent = this.recommendation()?.target_soc_percent;
    return percent === undefined ? null : { percent, y: this.batteryY(percent) };
  });

  readonly minimumLine = computed(() => {
    const percent = this.system()?.battery?.min_soc_percent;
    return percent === undefined ? null : { percent, y: this.batteryY(percent) };
  });

  /**
   * Plain sentences naming the cheap period the decision is for, as opposed to the next one the
   * plan runs up to, which is usually the same clock time a day later.
   */
  readonly verdict = computed(() => {
    const plan = this.plan();
    const r = this.recommendation();
    const timeline = plan?.timeline ?? [];
    if (!plan || !r?.expected || !timeline.length) return null;
    const left = Math.round(r.expected.end_soc_percent);
    const lastStart = new Date(timeline[timeline.length - 1].start).getTime();
    const end = new Date(lastStart + SLOT_MS).toISOString();
    const reserve = r.expected.reserve_at;
    const hasWindow = timeline.some((p) => p.cheap);

    const period = `${this.weekday(plan.charge_start)}'s ${this.localTime(plan.charge_start)}–${this.localTime(plan.charge_end)} cheap period`;
    const decision = r.force_charge_start
      ? `Charge to ${Math.round(r.target_soc_percent)}% in ${period}, with the house on the grid throughout.`
      : hasWindow
        ? `No charge needed in ${period}, with ${Math.round(plan.start_soc_percent)}% going in: the battery is held and the house runs on the grid.`
        : 'There is no cheaper overnight rate to charge from.';

    if (reserve) {
      const minimum = this.system()?.battery?.min_soc_percent;
      const reached = new Date(reserve).getTime();
      const lowest = timeline.find((p) => new Date(p.start).getTime() === reached)?.soc_percent;
      const recovers =
        lowest != null &&
        timeline.some(
          (p) => new Date(p.start).getTime() > reached && (p.soc_percent ?? 0) > lowest + 1,
        );
      const level = minimum === undefined ? 'its minimum' : `its ${Math.round(minimum)}% minimum`;
      return `${decision} We predict the battery will reach ${level} on ${this.weekday(reserve)} at ${this.localTime(reserve)}${recovers ? ', before solar recharges it' : ''}.`;
    }

    // In self-use the level only rises after the window because of solar.
    const closing = hasWindow ? timeline.filter((p) => p.cheap).at(-1)?.soc_percent : null;
    const after = timeline.filter((p) => !p.cheap && p.soc_percent !== null);
    const peak = after.reduce<(typeof after)[number] | null>(
      (best, p) => (!best || p.soc_percent! > best.soc_percent! ? p : best),
      null,
    );
    const lift =
      closing != null && peak && peak.soc_percent! > closing + 1
        ? `solar will lift the battery to ${Math.round(peak.soc_percent!)}% around ${this.localTime(peak.start)}, and it`
        : 'the battery';
    return hasWindow
      ? `${decision} We predict ${lift} will still have ${left}% left when the next cheap period starts on ${this.weekday(end)} at ${this.localTime(end)}.`
      : `${decision} We predict ${lift} will still have ${left}% left at ${this.localTime(end)} on ${this.weekday(end)}.`;
  });

  /** When the inverter is forced to charge, across the battery panel. */
  readonly forceCharge = computed(() => {
    const r = this.recommendation();
    return this.periodBlock(r?.force_charge_start, r?.force_charge_stop);
  });

  /** When the battery is held, with the house running on the grid. */
  readonly holdBlock = computed(() => {
    const r = this.recommendation();
    return this.periodBlock(r?.hold_start, r?.hold_stop);
  });

  private periodBlock(start?: string | null, stop?: string | null) {
    const slots = this.slots();
    if (!start || !stop || !slots.length) return null;
    const x = (iso: string) =>
      this.slotLeft(0) + ((new Date(iso).getTime() - slots[0].start) / SLOT_MS) * this.slotWidth();
    // Clipped to the chart, which can start partway through the period.
    const left = Math.max(x(start), this.slotLeft(0));
    const right = Math.min(x(stop), this.slotLeft(slots.length));
    return right > left ? { x: left, width: right - left, height: PLOT_HEIGHT } : null;
  }

  readonly maxKwh = computed(() => {
    const top = Math.max(
      0.1,
      ...this.slots().map((s) => Math.max(s.solarHigh ?? s.solar ?? 0, s.loadHigh ?? s.load ?? 0)),
    );
    return Math.ceil(top * 4) / 4;
  });

  readonly batteryShown = computed(() => this.hasBattery() && this.showBattery());

  /** Battery % while its line is showing; otherwise kWh, so the axis matches what's drawn. */
  readonly yTicks = computed(() => {
    if (this.batteryShown()) {
      return [0, 50, 100].map((percent) => ({
        label: `${percent}%`,
        y: this.batteryY(percent),
      }));
    }
    const max = this.maxKwh();
    return [0, max / 2, max].map((kwh) => ({ label: `${+kwh.toFixed(1)}`, y: this.y(kwh) }));
  });

  readonly hitAreas = computed(() =>
    this.slots().map((_, i) => ({ x: this.slotLeft(i), width: this.slotWidth() })),
  );

  /** The hovered half hour: a crosshair, a dot on each showing line, and the values as text. */
  readonly hover = computed(() => {
    const index = this.hovered();
    const slot = index === null ? undefined : this.slots()[index];
    if (index === null || !slot) return null;
    const parts = [this.readingTime(new Date(slot.start).toISOString())];
    const dots: { kind: string; y: number }[] = [];
    if (this.batteryShown() && slot.soc !== null) {
      parts.push(`Battery ${Math.round(slot.soc)}%`);
      dots.push({ kind: 'battery', y: this.batteryY(slot.soc) });
    }
    if (this.showUsage() && slot.load !== null) {
      parts.push(`Usage ${slot.load.toFixed(2)} kWh`);
      dots.push({ kind: 'load', y: this.y(slot.load) });
    }
    if (this.showSolar() && slot.solar !== null) {
      parts.push(`Solar ${slot.solar.toFixed(2)} kWh`);
      dots.push({ kind: 'solar', y: this.y(slot.solar) });
    }
    const plan = this.plan();
    if (
      plan?.charge_start &&
      slot.start >= new Date(plan.charge_start).getTime() &&
      slot.start < new Date(plan.charge_end).getTime()
    ) {
      parts.push('House on the grid');
    }
    return { x: this.x(index), text: parts.join(' · '), dots };
  });

  /** Labels every three hours, in the system's local time. */
  readonly xTicks = computed(() =>
    this.slots()
      .map((slot, index) => ({ index, label: this.localTime(new Date(slot.start).toISOString()) }))
      .filter((t) => /^(00|03|06|09|12|15|18|21):00$/.test(t.label))
      .map((t) => ({ x: this.x(t.index), label: t.label.slice(0, 2) })),
  );

  readonly solarLine = computed(() =>
    this.line(
      (s) => s.solar,
      (kwh) => this.y(kwh),
    ),
  );
  readonly solarBand = computed(() =>
    this.band(
      (s) => s.solarLow,
      (s) => s.solarHigh,
    ),
  );
  readonly loadLine = computed(() =>
    this.line(
      (s) => s.load,
      (kwh) => this.y(kwh),
    ),
  );
  readonly loadBand = computed(() =>
    this.band(
      (s) => s.loadLow,
      (s) => s.loadHigh,
    ),
  );

  ngOnInit(): void {
    this.load();
  }

  load(day?: string): void {
    this.loading.set(true);
    this.error.set(null);
    this.plan.set(null);
    this.leadIn.set(null);
    this.hovered.set(null);
    this.now.set(this.clock());
    this.chosenDay.set(day ?? null);
    this.forecasts.day(day || undefined).subscribe({
      next: (forecast) => {
        // Stepping quickly can leave an earlier day's answer arriving last.
        if (day && this.chosenDay() !== day) return;
        this.forecast.set(forecast);
        this.loading.set(false);
        this.loadPlan(forecast.day);
        this.loadLeadIn(forecast);
      },
      error: (err) => {
        if (day && this.chosenDay() !== day) return;
        this.error.set(errorMessage(err));
        this.loading.set(false);
      },
    });
  }

  /** Move to the previous or next day, within today to 7 days ahead. */
  step(days: -1 | 1): void {
    const current = this.shownDay();
    if (!current) return;
    const day = addDays(current, days);
    if (day >= this.firstDay() && day <= this.lastDay()) this.load(day);
  }

  relativeDay(day: string): string {
    if (day === this.firstDay()) return 'Today';
    if (day === addDays(this.firstDay(), 1)) return 'Tomorrow';
    return '';
  }

  /** Plan again from a different battery level when the cheap window opens. */
  recalculate(value: string): void {
    const soc = Number(value);
    const day = this.forecast()?.day;
    if (day && value !== '' && soc >= 0 && soc <= 100) this.loadPlan(day, soc);
  }

  /** The forecast still shows without a plan, e.g. when no battery is set up. */
  private loadPlan(day: string, startSoc?: number): void {
    this.plans.plan({ day, startSoc }).subscribe({
      next: (plan) => {
        if (this.forecast()?.day === day) this.plan.set(plan);
      },
      error: () => this.plan.set(null),
    });
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

  /** The chart starts now for today and tomorrow, not for days further ahead. */
  private startsNow(dayStart: number): boolean {
    return dayStart - this.now() <= DAY_MS;
  }

  /** Without it the chart just has no solar line before the planned day. */
  private loadLeadIn(forecast: DayForecast): void {
    const start = forecast.intervals[0] && new Date(forecast.intervals[0].start).getTime();
    if (!start || start <= this.now() || !this.startsNow(start)) return;
    const previous = new Date(new Date(`${forecast.day}T00:00:00Z`).getTime() - DAY_MS)
      .toISOString()
      .slice(0, 10);
    this.forecasts.day(previous).subscribe({
      next: (lead) => {
        if (this.forecast()?.day === forecast.day) this.leadIn.set(lead);
      },
      error: () => this.leadIn.set(null),
    });
  }

  /** "YYYY-MM-DD" in the system's time zone. */
  private localDate(moment: number): string {
    return new Intl.DateTimeFormat('en-CA', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      timeZone: this.system()?.timezone,
    }).format(new Date(moment));
  }

  /** "Sunday" in the system's time zone. */
  private weekday(iso: string): string {
    return new Intl.DateTimeFormat('en-GB', {
      weekday: 'long',
      timeZone: this.system()?.timezone,
    }).format(new Date(iso));
  }

  /** "Sat 14:30" in the system's time zone. */
  readingTime(iso: string): string {
    return new Intl.DateTimeFormat('en-GB', {
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
      timeZone: this.system()?.timezone,
    }).format(new Date(iso));
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

  private slotWidth(): number {
    return (WIDTH - PAD.left - PAD.right) / Math.max(1, this.slots().length);
  }

  private slotLeft(index: number): number {
    return PAD.left + index * this.slotWidth();
  }

  private x(index: number): number {
    return this.slotLeft(index) + this.slotWidth() / 2;
  }

  private y(kwh: number): number {
    return HEIGHT - PAD.bottom - (kwh / this.maxKwh()) * PLOT_HEIGHT;
  }

  /** 0–100% fills the plot's height, as a visual aid over the kWh scale. */
  private batteryY(percent: number): number {
    return HEIGHT - PAD.bottom - (percent / 100) * PLOT_HEIGHT;
  }

  private band(low: (slot: Slot) => number | null, high: (slot: Slot) => number | null): string {
    const points = this.slots()
      .map((slot, i) => ({ low: low(slot), high: high(slot), x: this.x(i).toFixed(1) }))
      .filter((p) => p.low !== null && p.high !== null);
    if (!points.length) return '';
    const top = points.map((p) => `${p.x},${this.y(p.high!).toFixed(1)}`);
    const bottom = points.map((p) => `${p.x},${this.y(p.low!).toFixed(1)}`).reverse();
    return `M${top.join(' L')} L${bottom.join(' L')} Z`;
  }

  private line(value: (slot: Slot) => number | null, toY: (value: number) => number): string {
    let path = '';
    let drawing = false;
    this.slots().forEach((slot, i) => {
      const v = value(slot);
      if (v === null) {
        drawing = false;
        return;
      }
      path += `${drawing ? 'L' : 'M'}${this.x(i).toFixed(1)},${toY(v).toFixed(1)} `;
      drawing = true;
    });
    return path.trim();
  }
}
