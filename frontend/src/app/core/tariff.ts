import { RateDirection, TariffBand } from './models';

/** Same rules as the backend (app/tariffs.py): 48 half hours a day, local time. */
export const SLOTS_PER_DAY = 48;

export type BandTimes = Pick<TariffBand, 'days' | 'start' | 'end' | 'rate'>;

export function slotOf(time: string): number {
  const [h, m] = time.split(':').map(Number);
  return Math.floor((h * 60 + m) / 30);
}

export function slotLabel(slot: number): string {
  const minutes = (slot % SLOTS_PER_DAY) * 30;
  return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
}

/** The half hours a band covers; wraps past midnight when end <= start. */
export function bandSlots(start: string, end: string): number[] {
  const first = slotOf(start);
  let last = slotOf(end);
  if (last <= first) last += SLOTS_PER_DAY;
  const out: number[] = [];
  for (let s = first; s < last; s++) out.push(s % SLOTS_PER_DAY);
  return out;
}

function applies(days: TariffBand['days'], weekend: boolean): boolean {
  return days === 'all' || (days === 'weekends') === weekend;
}

/** Rate per half hour for one kind of day; null where no band covers it. */
export function slotRates(bands: BandTimes[], weekend: boolean): (number | null)[] {
  const rates: (number | null)[] = Array(SLOTS_PER_DAY).fill(null);
  for (const b of bands) {
    if (!applies(b.days, weekend) || !b.start || !b.end) continue;
    for (const s of bandSlots(b.start, b.end)) rates[s] = b.rate;
  }
  return rates;
}

function ranges(slots: number[]): string {
  if (slots.length > 0 && slots.length < SLOTS_PER_DAY && slots[0] === 0) {
    if (slots[slots.length - 1] === SLOTS_PER_DAY - 1) {
      const cut = slots.findIndex((s, i) => i > 0 && s !== slots[i - 1] + 1);
      slots = [...slots.slice(cut), ...slots.slice(0, cut).map((s) => s + SLOTS_PER_DAY)];
    }
  }
  const parts: string[] = [];
  let runStart: number | null = null;
  slots.forEach((s, i) => {
    if (runStart === null) runStart = s;
    if (i === slots.length - 1 || slots[i + 1] !== s + 1) {
      parts.push(`${slotLabel(runStart)}–${slotLabel(s + 1)}`);
      runStart = null;
    }
  });
  return parts.join(', ');
}

/** Plain-English gaps and overlaps for one direction; empty when complete. */
export function coverageProblems(bands: BandTimes[], direction: RateDirection): string[] {
  const label = direction === 'import' ? 'Import' : 'Export';
  const found = [false, true].map((weekend) => {
    const counts: number[] = Array(SLOTS_PER_DAY).fill(0);
    for (const b of bands) {
      if (!applies(b.days, weekend) || !b.start || !b.end) continue;
      for (const s of bandSlots(b.start, b.end)) counts[s]++;
    }
    return {
      gaps: ranges(counts.flatMap((c, s) => (c === 0 ? [s] : []))),
      overlaps: ranges(counts.flatMap((c, s) => (c > 1 ? [s] : []))),
    };
  });
  // Say it once when weekdays and weekends have the same problem.
  const same = found[0].gaps === found[1].gaps && found[0].overlaps === found[1].overlaps;
  const kinds = same
    ? [{ day: '', ...found[0] }]
    : [
        { day: ' on weekdays', ...found[0] },
        { day: ' on weekends', ...found[1] },
      ];
  const problems: string[] = [];
  for (const k of kinds) {
    if (k.gaps) problems.push(`${label} has no rate${k.day} for ${k.gaps}.`);
    if (k.overlaps) problems.push(`${label} has more than one rate${k.day} for ${k.overlaps}.`);
  }
  return problems;
}

/** Lowest and highest rate for a direction, for summaries. */
export function rateRange(bands: TariffBand[], direction: RateDirection) {
  const rates = bands.filter((b) => b.direction === direction).map((b) => b.rate);
  return rates.length ? { min: Math.min(...rates), max: Math.max(...rates) } : null;
}

/** The cheapest import band, e.g. for "7.5p 23:30–05:30". */
export function cheapestImport(bands: TariffBand[]): TariffBand | null {
  return bands
    .filter((b) => b.direction === 'import')
    .reduce<TariffBand | null>((best, b) => (best === null || b.rate < best.rate ? b : best), null);
}
