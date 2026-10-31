import { TariffBand } from './models';
import { bandSlots, cheapestImport, coverageProblems, rateRange, slotRates } from './tariff';

const band = (start: string, end: string, rate: number, days: TariffBand['days'] = 'all') => ({
  days,
  start,
  end,
  rate,
});

describe('tariff helpers', () => {
  it('wraps bands past midnight and treats start == end as all day', () => {
    expect(bandSlots('23:00', '01:00')).toEqual([46, 47, 0, 1]);
    expect(bandSlots('00:00', '00:00').length).toBe(48);
  });

  it('reports gaps and overlaps per kind of day', () => {
    const bands = [
      band('02:00', '05:00', 15, 'weekdays'),
      band('05:00', '02:00', 25, 'weekdays'),
      band('00:00', '00:00', 20, 'weekends'),
    ];
    expect(coverageProblems(bands, 'import')).toEqual([]);
    expect(coverageProblems(bands.slice(1), 'import')).toEqual([
      'Import has no rate on weekdays for 02:00–05:00.',
    ]);
    expect(coverageProblems([...bands, band('22:00', '01:00', 9)], 'export')).toEqual([
      'Export has more than one rate for 22:00–01:00.',
    ]);
    expect(slotRates(bands, true)[4]).toBe(20);
    expect(slotRates(bands, false)[4]).toBe(15);
  });

  it('summarises rates', () => {
    const bands: TariffBand[] = [
      { direction: 'import', ...band('23:30', '05:30', 7.5) },
      { direction: 'import', ...band('05:30', '23:30', 27) },
      { direction: 'export', ...band('00:00', '00:00', 15) },
    ];
    expect(cheapestImport(bands)?.start).toBe('23:30');
    expect(rateRange(bands, 'export')).toEqual({ min: 15, max: 15 });
  });
});
