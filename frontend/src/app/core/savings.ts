import { SavingsDay } from './models';

/**
 * What the panels and battery saved: each day's metered cost (imports less export income)
 * against what the home's use would have cost bought from the grid at the same rates. All
 * amounts are pence.
 */

export interface SavingsTotals {
  days: number;
  first: string;
  last: string;
  houseKwh: number;
  gridOnly: number;
  imports: number;
  exports: number;
  /** Imports less export income: what was really paid. */
  net: number;
  saving: number;
  perDay: number;
}

export interface SavingsWeek {
  /** The Monday it starts on. */
  week: string;
  /** Days with a full set of readings: fewer than 7 at either end or after a gap. */
  days: number;
  gridOnly: number;
  imports: number;
  exports: number;
  net: number;
  /** What the home's use would have cost from the grid, less what was really imported. */
  importsAvoided: number;
  saving: number;
  /** Change from the week before, or null for the first week. */
  change: { gridOnly: number; net: number; saving: number } | null;
  /** True when this week or the one before has days missing, so the change is rough. */
  partWeek: boolean;
}

export interface SavingsRunning {
  day: string;
  gridOnly: number;
  paid: number;
}

const net = (d: SavingsDay) => d.import_cost_pence - d.export_revenue_pence;
const saved = (d: SavingsDay) => d.grid_only_pence - net(d);

/** The Monday of the week holding `day` (YYYY-MM-DD). */
export function weekStart(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  const back = (d.getUTCDay() + 6) % 7;
  return new Date(d.getTime() - back * 86_400_000).toISOString().slice(0, 10);
}

export function savingsTotals(days: SavingsDay[]): SavingsTotals | null {
  if (!days.length) return null;
  const sum = (f: (d: SavingsDay) => number) => days.reduce((t, d) => t + f(d), 0);
  const saving = sum(saved);
  return {
    days: days.length,
    first: days[0].day,
    last: days[days.length - 1].day,
    houseKwh: sum((d) => d.house_kwh),
    gridOnly: sum((d) => d.grid_only_pence),
    imports: sum((d) => d.import_cost_pence),
    exports: sum((d) => d.export_revenue_pence),
    net: sum(net),
    saving,
    perDay: saving / days.length,
  };
}

/** Monday-to-Sunday weeks, oldest first, each with its change from the week before. */
export function savingsWeeks(days: SavingsDay[]): SavingsWeek[] {
  const byWeek = new Map<string, SavingsDay[]>();
  for (const d of days) {
    const key = weekStart(d.day);
    byWeek.set(key, [...(byWeek.get(key) ?? []), d]);
  }
  const weeks: SavingsWeek[] = [];
  for (const [week, items] of [...byWeek].sort(([a], [b]) => a.localeCompare(b))) {
    const t = savingsTotals(items)!;
    const before = weeks[weeks.length - 1];
    weeks.push({
      week,
      days: items.length,
      gridOnly: t.gridOnly,
      imports: t.imports,
      exports: t.exports,
      net: t.net,
      importsAvoided: t.gridOnly - t.imports,
      saving: t.saving,
      change: before
        ? {
            gridOnly: t.gridOnly - before.gridOnly,
            net: t.net - before.net,
            saving: t.saving - before.saving,
          }
        : null,
      partWeek: items.length < 7 || (before ? before.days < 7 : false),
    });
  }
  return weeks;
}

/** Grid-only cost and what was really paid, added up day by day. */
export function savingsRunning(days: SavingsDay[]): SavingsRunning[] {
  let gridOnly = 0;
  let paid = 0;
  return days.map((d) => {
    gridOnly += d.grid_only_pence;
    paid += net(d);
    return { day: d.day, gridOnly, paid };
  });
}

/** The week to open on: the latest with at least six days of readings. */
export function defaultWeek(weeks: SavingsWeek[]): string | null {
  const full = weeks.filter((w) => w.days >= 6);
  return (full[full.length - 1] ?? weeks[weeks.length - 1])?.week ?? null;
}

/** "£220.72", "-£44.93" or "37p". */
export function money(pence: number): string {
  const rounded = Math.round(pence);
  const sign = rounded < 0 ? '-' : '';
  const abs = Math.abs(rounded);
  return abs >= 100 ? `${sign}£${(abs / 100).toFixed(2)}` : `${sign}${abs}p`;
}

/** A change, always signed: "+£1.46", "-48p", "no change". */
export function change(pence: number): string {
  const rounded = Math.round(pence);
  if (rounded === 0) return 'no change';
  return (rounded > 0 ? '+' : '') + money(rounded);
}

/** Evenly spaced round values covering `lo` to `hi`, for a chart axis. */
export function niceTicks(lo: number, hi: number, count = 5): number[] {
  if (hi <= lo) hi = lo + 1;
  const raw = (hi - lo) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / magnitude;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * magnitude;
  const ticks: number[] = [];
  for (
    let v = Math.floor(lo / step) * step;
    v <= Math.ceil(hi / step) * step + step / 2;
    v += step
  ) {
    ticks.push(Math.round(v * 1e6) / 1e6);
  }
  return ticks;
}

/** An axis label: whole pounds once the steps are a pound or more. */
export function axisMoney(pence: number): string {
  const sign = pence < 0 ? '-' : '';
  const abs = Math.abs(pence);
  if (abs === 0) return '£0';
  return abs >= 100 ? `${sign}£${+(abs / 100).toFixed(2)}` : `${sign}${abs}p`;
}

export function dayMonth(day: string, long = false): string {
  return new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: long ? 'long' : 'short',
    timeZone: 'UTC',
  }).format(new Date(`${day}T00:00:00Z`));
}
