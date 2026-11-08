import { SavingsDay } from './models';
import {
  axisMoney,
  change,
  defaultWeek,
  money,
  niceTicks,
  savingsRunning,
  savingsTotals,
  savingsWeeks,
  weekStart,
} from './savings';

function day(date: string, grid: number, imports: number, exports: number): SavingsDay {
  return {
    day: date,
    house_kwh: 10,
    grid_only_pence: grid,
    import_cost_pence: imports,
    export_revenue_pence: exports,
  };
}

// Mon 19 to Sun 25 October, then Mon 26 to Fri 30 (Sat 31 and Sun 1 missing).
const DAYS = [
  ...Array.from({ length: 7 }, (_, i) => day(`2026-10-${19 + i}`, 150, 60, 20)),
  ...Array.from({ length: 5 }, (_, i) => day(`2026-10-${26 + i}`, 160, 50, 10)),
];

describe('savings', () => {
  it('adds up what was paid against the grid alone', () => {
    const t = savingsTotals(DAYS)!;
    expect(t.days).toBe(12);
    expect(t.gridOnly).toBe(7 * 150 + 5 * 160);
    expect(t.net).toBe(7 * 40 + 5 * 40);
    expect(t.saving).toBe(7 * 110 + 5 * 120);
    expect(t.perDay).toBeCloseTo(t.saving / 12);
    expect(savingsTotals([])).toBeNull();
  });

  it('groups days into Monday-to-Sunday weeks with the change from the week before', () => {
    expect(weekStart('2026-10-25')).toBe('2026-10-19'); // a Sunday
    expect(weekStart('2026-10-26')).toBe('2026-10-26'); // a Monday
    const [first, second] = savingsWeeks(DAYS);
    expect(first).toMatchObject({ week: '2026-10-19', days: 7, change: null, partWeek: false });
    expect(first.importsAvoided).toBe(7 * 90);
    expect(second).toMatchObject({ week: '2026-10-26', days: 5, partWeek: true });
    expect(second.change).toEqual({
      gridOnly: 5 * 160 - 7 * 150,
      net: 5 * 40 - 7 * 40,
      saving: 5 * 120 - 7 * 110,
    });
  });

  it('opens on the latest week with at least six days of readings', () => {
    expect(defaultWeek(savingsWeeks(DAYS))).toBe('2026-10-19');
    expect(defaultWeek(savingsWeeks(DAYS.slice(7)))).toBe('2026-10-26'); // the only week
    expect(defaultWeek([])).toBeNull();
  });

  it('keeps running totals day by day', () => {
    const running = savingsRunning(DAYS.slice(0, 2));
    expect(running).toEqual([
      { day: '2026-10-19', gridOnly: 150, paid: 40 },
      { day: '2026-10-20', gridOnly: 300, paid: 80 },
    ]);
  });

  it('writes money and changes plainly', () => {
    expect(money(22036)).toBe('£220.36');
    expect(money(-4493)).toBe('-£44.93');
    expect(money(37.4)).toBe('37p');
    expect(change(146)).toBe('+£1.46');
    expect(change(-48)).toBe('-48p');
    expect(change(0.2)).toBe('no change');
  });

  it('picks round axis steps', () => {
    expect(niceTicks(-4493, 17743)).toEqual([-5000, 0, 5000, 10000, 15000, 20000]);
    expect(niceTicks(0, 90, 3)).toEqual([0, 50, 100]);
    expect(axisMoney(5000)).toBe('£50');
    expect(axisMoney(-250)).toBe('-£2.5');
    expect(axisMoney(50)).toBe('50p');
  });
});
