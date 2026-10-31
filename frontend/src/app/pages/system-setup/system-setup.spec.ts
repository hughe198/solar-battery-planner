import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { SystemConfig, TariffPeriod } from '../../core/models';
import { SystemSetup } from './system-setup';

const BANDS: TariffPeriod['bands'] = [
  { direction: 'import', days: 'all', start: '02:00:00', end: '05:00:00', rate: 7.5 },
  { direction: 'import', days: 'all', start: '05:00:00', end: '02:00:00', rate: 27.5 },
  { direction: 'export', days: 'all', start: '00:00:00', end: '00:00:00', rate: 15 },
];

const SYSTEM = {
  id: 1,
  name: 'Home',
  latitude: 51.5,
  longitude: -0.12,
  timezone: 'Europe/London',
  planning_mode: 'maximise_own_usage',
  data_source: 'fox_ess',
  battery: {
    capacity_kwh: 10,
    min_soc_percent: 10,
    max_charge_kw: 5,
    max_discharge_kw: 5,
    round_trip_efficiency_percent: 90,
    wear_pence_per_cycle: 0,
  },
  tariff: { name: 'Flux', preset: null, standing_charge_pence_per_day: 53, bands: BANDS },
  arrays: [{ name: 'South', kwp: 4, tilt_degrees: 35, azimuth_degrees: 180 }],
} as SystemConfig;

function period(from: string | null, until: string | null, cheap: number): TariffPeriod {
  return {
    name: `From ${from}`,
    standing_charge_pence_per_day: 53,
    valid_from: from,
    valid_until: until,
    bands: [{ ...BANDS[0], rate: cheap }, BANDS[1], BANDS[2]],
  };
}

describe('SystemSetup tariff history', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SystemSetup],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render(periods: TariffPeriod[]) {
    const fixture = TestBed.createComponent(SystemSetup);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne('/api/system/tariff-history').flush(periods);
    http.match('/api/system/tariff-presets').forEach((r) => r.flush([]));
    fixture.detectChanges();
    return fixture;
  }

  it('lists past tariffs with what each charged', () => {
    const el = render([period('2026-06-01', null, 9), period(null, '2026-06-01', 7.5)])
      .nativeElement as HTMLElement;
    const rows = [...el.querySelectorAll('table.tariff-history tbody tr')].map((r) =>
      [...r.querySelectorAll('td')].map((c) => c.textContent?.trim()),
    );
    expect(rows).toEqual([
      ['2026-06-01', 'Now', 'From 2026-06-01', '9–27.5p', '15p', '53p/day'],
      ['The start', '2026-06-01', 'From null', '7.5–27.5p', '15p', '53p/day'],
    ]);
  });

  it('has no list until the rates have changed', () => {
    const el = render([period(null, null, 7.5)]).nativeElement as HTMLElement;
    expect(el.querySelector('table.tariff-history')).toBeNull();
  });

  it('sends the day the rates apply from, defaulting to today', () => {
    const fixture = render([period(null, null, 7.5)]);
    const el = fixture.nativeElement as HTMLElement;
    const input = el.querySelector<HTMLInputElement>(
      'input[formControlName="tariff_effective_from"]',
    )!;
    expect(input.value).toBe(fixture.componentInstance.today);
    expect(input.max).toBe(fixture.componentInstance.today);

    fixture.componentInstance.form.controls.tariff_effective_from.setValue('2026-03-01');
    fixture.componentInstance.submit();
    const req = http.expectOne((r) => r.method === 'PUT' && r.url === '/api/system');
    expect(req.request.body.tariff_effective_from).toBe('2026-03-01');
  });
});
