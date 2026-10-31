import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { DayForecast, IntervalForecast, SystemConfig } from '../../core/models';
import { Forecast } from './forecast';

const SYSTEM = {
  name: 'Home',
  timezone: 'Europe/London',
  arrays: [{ name: 'South', kwp: 4, tilt_degrees: 35, azimuth_degrees: 180 }],
} as SystemConfig;

function interval(i: number): IntervalForecast {
  const start = new Date(Date.UTC(2026, 5, 1, 23, 0) + i * 1_800_000).toISOString();
  return {
    start,
    end: start,
    load_kwh: 0.2,
    load_low_kwh: 0.1,
    load_high_kwh: 0.3,
    solar_kwh: i > 12 && i < 36 ? 0.8 : 0,
    solar_low_kwh: 0,
    solar_high_kwh: i > 12 && i < 36 ? 1 : 0,
    temperature_c: 15,
    cloud_cover_percent: 20,
  };
}

const FORECAST: DayForecast = {
  day: '2026-06-02',
  intervals: Array.from({ length: 48 }, (_, i) => interval(i)),
  load: { expected: 9.6, low: 8.1, high: 11.2 },
  solar: { expected: 18.4, low: 14, high: 21 },
  load_method: 'blended',
  solar_method: 'physics',
  history_days: 5,
  array_performance: [0.82],
  backtest: null,
};

describe('Forecast', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Forecast],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render(forecast: DayForecast) {
    const fixture = TestBed.createComponent(Forecast);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne('/api/forecast').flush(forecast);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('shows totals, ranges and how the forecast was made', () => {
    const el = render(FORECAST);
    expect(el.textContent).toContain('Tuesday 2 June');
    expect(el.textContent).toContain('9.6 kWh');
    expect(el.textContent).toContain('8.1–11.2 kWh');
    expect(el.textContent).toContain('until you import solar data');
    expect(el.textContent).toContain('South (4 kWp) delivers about 82%');
    expect(el.textContent).toContain('three weeks of usage history');
    expect(el.querySelectorAll('svg path').length).toBe(4);
    const hours = [...el.querySelectorAll('svg text')].map((t) => t.textContent?.trim());
    expect(hours).toContain('00');
    expect(hours).toContain('12');
  });

  it('shows backtest accuracy when there is enough history', () => {
    const el = render({
      ...FORECAST,
      load_method: 'learned',
      backtest: {
        train_days: 60,
        test_days: 14,
        load_mae_kwh: 1.2,
        load_error_percent: 11.5,
        solar_mae_kwh: 2.1,
        solar_error_percent: 14,
        solar_physics_error_percent: 22,
        days: [
          {
            day: '2026-05-31',
            load_actual_kwh: 10,
            load_predicted_kwh: 9,
            solar_actual_kwh: 15,
            solar_predicted_kwh: 16,
            solar_physics_kwh: 19,
          },
        ],
      },
    });
    expect(el.textContent).toContain('Trained on 60 days');
    expect(el.textContent).toContain('12%');
    expect(el.textContent).toContain('panel physics alone: 22%');
    expect(el.querySelectorAll('table.backtest tbody tr').length).toBe(1);
  });
});
