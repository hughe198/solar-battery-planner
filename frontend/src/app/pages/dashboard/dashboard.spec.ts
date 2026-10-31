import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { History, SystemConfig } from '../../core/models';
import { Dashboard } from './dashboard';

const SYSTEM: SystemConfig = {
  name: 'Home',
  latitude: 51.5,
  longitude: 0,
  timezone: 'UTC',
  planning_mode: 'maximise_own_usage',
  data_source: 'csv',
  battery: {
    capacity_kwh: 10,
    min_soc_percent: 10,
    max_charge_kw: 5,
    max_discharge_kw: 5,
    round_trip_efficiency_percent: 90,
  },
  tariff: {
    day_import_rate: 27.5,
    cheap_import_rate: 7.5,
    export_rate: 15,
    cheap_start: '23:30',
    cheap_end: '05:30',
  },
  arrays: [{ name: 'South', kwp: 4, tilt_degrees: 35, azimuth_degrees: 180 }],
};

const HISTORY: History = {
  days: Array.from({ length: 7 }, (_, d) => ({
    day: `2026-05-2${d + 1}`,
    load_kwh: 10,
    solar_kwh: 12,
    import_kwh: 4,
    export_kwh: 5,
    self_sufficiency_percent: 60,
    import_cost_pence: 50,
    export_revenue_pence: 75,
    min_soc_percent: 10,
    max_soc_percent: 100,
  })),
  points: Array.from({ length: 7 * 48 }, (_, i) => ({
    start: new Date(Date.UTC(2026, 4, 21) + i * 1_800_000).toISOString(),
    load_kwh: 0.2,
    solar_kwh: 0,
    import_kwh: i % 48 < 11 ? 0.3 : 0,
    export_kwh: i % 48 > 24 && i % 48 < 30 ? 0.5 : 0,
    soc_percent: i % 2 ? 50 : null,
    cheap: i % 48 < 11,
  })),
};

describe('Dashboard', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Dashboard],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  it('charts the last week and summarises it', () => {
    const fixture = TestBed.createComponent(Dashboard);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne((r) => r.url === '/api/history').flush(HISTORY);
    http.expectOne('/api/plan').flush({}, { status: 404, statusText: 'Not found' });
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Your last 7 days');
    expect(el.textContent).toContain('60%');
    expect(el.textContent).toContain('£3.50'); // 7 × 50p of imports
    expect(el.querySelectorAll('app-week-chart rect.ss').length).toBe(7);
    expect(el.querySelectorAll('app-week-chart circle.soc-end').length).toBe(14);
    const days = [...el.querySelectorAll('app-flow-chart text.axis')].map((t) =>
      t.textContent?.trim(),
    );
    expect(days).toContain('Thu');
    expect(el.querySelectorAll('app-flow-chart rect.cheap').length).toBe(7);
    expect(el.querySelectorAll('app-flow-chart rect.export').length).toBe(7 * 5);
    // Gaps in battery data break the line rather than joining across them.
    expect(
      el.querySelector('app-flow-chart path.soc')?.getAttribute('d')?.match(/M/g)?.length,
    ).toBe(7 * 24);
  });
});
