import { InProgressCharge } from './models';

/** "YYYY-MM-DD" in a time zone. */
export function localDate(moment: number, timeZone?: string): string {
  return new Intl.DateTimeFormat('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    timeZone,
  }).format(new Date(moment));
}

/** "02:00" in a time zone. */
export function localTime(iso: string, timeZone?: string): string {
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    timeZone,
  }).format(new Date(iso));
}

/** "Sunday" in a time zone. */
export function weekday(iso: string, timeZone?: string): string {
  return new Intl.DateTimeFormat('en-GB', { weekday: 'long', timeZone }).format(new Date(iso));
}

/** "Saturday 31 October", for a local day that's already "YYYY-MM-DD". */
export function dayLabel(day: string): string {
  return new Intl.DateTimeFormat('en-GB', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    timeZone: 'UTC',
  }).format(new Date(`${day}T00:00:00Z`));
}

/** "Charge at 02:00 on Saturday 31 October", as the day alone doesn't say when it was. */
export function chargeLabel(
  charge: { day: string; charge_start?: string | null },
  timeZone?: string,
): string {
  if (!charge.charge_start) return dayLabel(charge.day);
  const on = new Intl.DateTimeFormat('en-GB', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    timeZone,
  }).format(new Date(charge.charge_start));
  return `Charge at ${localTime(charge.charge_start, timeZone)} on ${on}`;
}

/** The words for a charge that has happened but can't be scored until its day has played out. */
export function describeInProgress(
  c: InProgressCharge,
  timeZone: string | undefined,
  today: string,
): { title: string; summary: string; score: string } {
  const percent = (value: number) => `${Math.round(value)}%`;
  const window = `${localTime(c.charge_start, timeZone)}–${localTime(c.charge_end, timeZone)}`;
  const did = {
    charged: `charged to ${percent(c.soc_peak_percent)}`,
    held: `held at ${percent(c.soc_after_percent)}`,
    discharged: `fell to ${percent(c.soc_after_percent)}`,
  }[c.outcome];
  const soFar = c.window_finished ? '' : ' so far';
  const grid = c.grid_only ? ', with the house on the grid' : '';
  const scoredOn =
    localDate(new Date(c.scored_after).getTime(), timeZone) === today
      ? 'today'
      : `on ${weekday(c.scored_after, timeZone)}`;
  return {
    // Only if it really was: older data gets its date instead.
    title: c.day === today ? "Last night's charge" : chargeLabel(c, timeZone),
    summary:
      `In the ${window} cheap period, the battery went in at ` +
      `${percent(c.soc_before_percent)} and ${did}${soFar}${grid}.`,
    score:
      `In progress. Its score comes after ${localTime(c.scored_after, timeZone)} ${scoredOn}, ` +
      'once that data is imported.',
  };
}
