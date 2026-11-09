import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';
import {
  ChargePlan,
  DayForecast,
  History,
  ImportStatus,
  LookBack,
  Replay,
  SystemConfig,
} from '../../core/models';
import { Home } from './home';

const NOW = Date.UTC(2026, 5, 2, 18, 40); // Tuesday 2 June, 19:40 in London.
const SYSTEM = {
  timezone: 'Europe/London',
  name: 'Home',
  battery: { capacity_kwh: 10 },
} as SystemConfig;

const RECENT: History = {
  days: [
    {
      day: '2026-06-01',
      load_kwh: 7.2,
      solar_kwh: 5.9,
      import_kwh: 3,
      export_kwh: 1,
      self_sufficiency_percent: 60,
      import_cost_pence: 80,
      export_revenue_pence: 15,
      min_soc_percent: 21,
      max_soc_percent: 89,
    },
  ],
  points: [
    {
      start: '2026-06-02T18:00:00Z',
      load_kwh: 0.2,
      solar_kwh: 0,
      import_kwh: 0,
      export_kwh: 0,
      soc_percent: 70,
      cheap: false,
    },
  ],
};

const SCORED = {
  day: '2026-06-01',
  charge_start: '2026-06-01T01:00:00Z',
  soc_before_percent: 52,
  soc_after_charge_percent: 89,
  best_soc_percent: 76,
  missed_saving_pence: 11.5,
};

const REPLAY = {
  days: [SCORED],
  review: { day: SCORED, comments: [] },
  in_progress: {
    day: '2026-06-02',
    charge_start: '2026-06-02T01:00:00Z',
    charge_end: '2026-06-02T04:00:00Z',
    scored_after: '2026-06-03T01:00:00Z',
    window_finished: true,
    soc_before_percent: 66,
    soc_peak_percent: 66,
    soc_after_percent: 65,
    outcome: 'held',
    grid_only: true,
    latest_reading_at: '2026-06-02T18:30:00Z',
    points: [],
  },
} as unknown as Replay;

const PLAN = {
  day: '2026-06-03',
  charge_start: '2026-06-03T01:00:00Z',
  start_soc_percent: 59,
  start_soc_source: 'measured',
  recommendations: [
    {
      target_soc_percent: 59,
      force_charge_start: null,
      force_charge_stop: null,
      charge_kwh: 0,
      charge_cost_pence: 0,
      hold_start: '2026-06-03T01:00:00Z',
      hold_stop: '2026-06-03T04:00:00Z',
      expected: { end_soc_percent: 39 },
      reasons: ["No overnight charge: the battery's 59% is enough."],
      summary:
        "No charge needed: the battery's 59% should last until solar and the next cheap window.",
    },
  ],
} as unknown as ChargePlan;

const TODAY = {
  day: '2026-06-02',
  load: { expected: 7.6, low: 6.4, high: 9.1 },
  solar: { expected: 4.2, low: 2.8, high: 6.0 },
  weather: {
    headline: 'Cloudy, 4°C',
    detail: 'brighter 13:00–15:00',
    summary: 'Cloudy skies put solar about 40% below your recent average.',
    points: ['Solar: cloudy skies (85% cloud in daylight), so 4.2 kWh.'],
  },
} as unknown as DayForecast;

const LOOK_BACK = {
  day: '2026-06-01',
  headline: 'Sunnier than forecast',
  detail: '30% cloud, not 70%',
  weather_headline: 'Sunny spells, 11°C',
  summary: 'It was sunnier than forecast, so solar made 11.2 kWh against 7.5.',
  points: ['2.1 kWh went to export.'],
  net_cost_pence: 64,
  grid_only_pence: 184,
  saved_pence: 120,
} as unknown as LookBack;

const CONTROL = {
  enabled: true,
  actions: [{ kind: 'apply', day: '2026-06-03', status: 'ok' }],
};

const fresh = (last = '2026-06-02T18:30:00'): ImportStatus =>
  ({
    fox_ess: { device_sn: 'X' },
    usage: { count: 10, first: null, last },
    latest_job: null,
  }) as unknown as ImportStatus;

describe('Home', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    localStorage.clear();
    await TestBed.configureTestingModule({
      imports: [Home],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function flushData(
    data: {
      recent?: History;
      replay?: Replay;
      plan?: ChargePlan | null;
      lookBack?: LookBack | null;
      control?: object;
    } = {},
  ): void {
    http.expectOne((r) => r.url === '/api/history').flush(data.recent ?? RECENT);
    http.expectOne((r) => r.url === '/api/plan/replay').flush(data.replay ?? REPLAY);
    const look = http.expectOne((r) => r.url === '/api/history/look-back');
    if (data.lookBack === null) {
      look.flush({ detail: 'No readings' }, { status: 404, statusText: 'Not Found' });
    } else {
      look.flush(data.lookBack ?? LOOK_BACK);
    }
    http.expectOne((r) => r.url === '/api/forecast').flush(TODAY);
    http.expectOne('/api/control').flush(data.control ?? CONTROL);
    const plan = http.expectOne((r) => r.url === '/api/plan');
    if (data.plan === null) {
      plan.flush({ detail: 'No weather forecast yet' }, { status: 502, statusText: 'Bad Gateway' });
    } else {
      plan.flush(data.plan ?? PLAN);
    }
  }

  function start(now = NOW) {
    const fixture = TestBed.createComponent(Home);
    fixture.componentInstance.clock = () => now;
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  function open(data: Parameters<typeof flushData>[0] = {}, status = fresh(), now = NOW) {
    const page = start(now);
    http.expectOne('/api/import/status').flush(status);
    flushData(data);
    page.fixture.detectChanges();
    return page;
  }

  const cards = (el: HTMLElement) =>
    [...el.querySelectorAll('.home-cards > *')].map((c) => c.tagName.toLowerCase());

  it('leads with the battery and today, then yesterday and tonight', () => {
    const { el } = open({}, fresh(), Date.UTC(2026, 5, 2, 7, 10)); // 08:10 in London
    expect(cards(el)).toEqual([
      'app-battery-card',
      'app-today-card',
      'app-yesterday-card',
      'app-tonight-card',
    ]);
    expect(el.querySelector('.home-cards')?.classList).not.toContain('evening');
    expect(el.querySelector('.home-battery .eyebrow')?.textContent).toContain('This morning');
    expect(el.querySelector('.home-today .eyebrow')?.textContent).toContain('Today, Tuesday');
  });

  it('moves tonight above yesterday in the evening', () => {
    const { el } = open(); // 19:40
    expect(el.querySelector('.home-cards')?.classList).toContain('evening');
    expect(el.querySelector('.home-today .eyebrow')?.textContent).toContain('Today so far');
  });

  it("shows the battery now and last night's charge", () => {
    const card = open().el.querySelector('.home-battery')!;
    expect(card.querySelector('.figure')?.textContent).toContain('70%');
    expect(card.querySelector('.figure')?.textContent).toContain('7.0 kWh');
    expect(card.querySelector('[role="meter"]')?.getAttribute('aria-valuenow')).toBe('70');
    expect(card.querySelector('.status')?.textContent).toContain(
      'Held at 65% overnight, house on the grid.',
    );
    expect(card.querySelector('.explain-toggle')?.textContent).toContain('Why?');
    expect(card.querySelector('details path.soc')).not.toBeNull();
  });

  it("gives today's forecast with the weather and its explanation", () => {
    const card = open({}, fresh(), Date.UTC(2026, 5, 2, 7, 10)).el.querySelector('.home-today')!;
    const figures = [...card.querySelectorAll('.figure')].map((f) => f.textContent?.trim());
    expect(figures).toEqual(['7.6 kWh use', '4.2 kWh solar']);
    expect(card.textContent).toContain('6.4–9.1 likely');
    expect(card.querySelector('.weather-line')?.textContent).toContain(
      'Cloudy, 4°C · brighter 13:00–15:00',
    );
    expect(card.querySelector('.explain-toggle')?.textContent).toContain('Weather · Why?');
  });

  it('shows how much today has used so far in the evening', () => {
    const card = open().el.querySelector('.home-today')!;
    expect(card.querySelector('.figure')?.textContent).toContain('0.2 kWh used');
    expect(card.textContent).toContain('forecast 7.6 for the day');
  });

  it("gives yesterday's savings and how its weather compared", () => {
    const card = open().el.querySelector('.home-yesterday')!;
    expect(card.querySelector('.eyebrow')?.textContent).toContain('Yesterday, Monday');
    expect(card.querySelector('.figure')?.textContent).toContain('£1.20');
    expect(card.querySelector('.figure')?.textContent).toContain('saved');
    expect(card.querySelector('.status')?.textContent).toContain(
      'Cost 64p instead of £1.84 on grid import alone.',
    );
    expect(card.querySelector('.weather-line')?.textContent).toContain(
      'Sunnier than forecast · 30% cloud, not 70%',
    );
  });

  it('says when yesterday has no readings yet', () => {
    const { el } = open({ lookBack: null });
    expect(el.querySelector('.home-yesterday')?.textContent).toContain(
      'No readings for yesterday yet.',
    );
  });

  it("gives tonight's plan, its one-sentence reason and whether it was sent", () => {
    const card = open().el.querySelector('.home-tonight')!;
    expect(card.querySelector('.figure')?.textContent).toContain('No charge');
    expect(card.querySelector('.figure')?.textContent).toContain('battery held 02:00–05:00');
    expect(card.querySelector('.badge')?.textContent).toBe('Sent to inverter');
    (card.querySelector('.explain-toggle') as HTMLButtonElement).click();
    expect(card.querySelector('a[href="/tonight"]')).not.toBeNull();
  });

  it('gives the times and cost when a charge is planned, and says when it is not sent', () => {
    const plan = structuredClone(PLAN);
    plan.recommendations[0] = {
      ...plan.recommendations[0],
      target_soc_percent: 72,
      force_charge_start: '2026-06-03T02:30:00Z',
      force_charge_stop: '2026-06-03T04:00:00Z',
      charge_kwh: 2.5,
      charge_cost_pence: 42,
    };
    const control = { enabled: true, actions: [] };
    const card = open({ plan, control }).el.querySelector('.home-tonight')!;
    expect(card.querySelector('.figure')?.textContent).toContain('72%');
    expect(card.querySelector('.figure')?.textContent).toContain('03:30–05:00 · about 42p');
    expect(card.querySelector('.badge')?.textContent).toBe('Not sent yet');
  });

  it('shows no sent badge when automation is off', () => {
    const card = open({ control: { enabled: false, actions: [] } }).el.querySelector(
      '.home-tonight',
    )!;
    expect(card.querySelector('.badge')).toBeNull();
  });

  it('still shows the other cards when the plan fails', () => {
    const { el } = open({ plan: null });
    expect(el.querySelector('.home-yesterday')?.textContent).toContain('£1.20');
    expect(el.querySelector('.home-tonight .form-error')?.textContent).toContain(
      'No weather forecast yet',
    );
  });

  it('shows how fresh the readings are and does not fetch when they are recent', () => {
    const { el } = open();
    expect(el.querySelector('.data-freshness')?.textContent).toContain(
      'Readings up to Tuesday 19:30.',
    );
    http.expectNone('/api/import/fox-ess/sync');
  });

  it('does not call Fox ESS without a connection', () => {
    open({}, { ...fresh('2026-05-01T00:00:00'), fox_ess: null });
    http.expectNone('/api/import/fox-ess/sync');
  });

  describe('fetching the latest readings', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('fetches yesterday and today when readings are stale, then reloads', () => {
      const { fixture, el } = start();
      http.expectOne('/api/import/status').flush(fresh('2026-06-02T12:00:00'));
      flushData();
      const sync = http.expectOne('/api/import/fox-ess/sync');
      expect(sync.request.body).toEqual({ start_date: '2026-06-01', end_date: '2026-06-02' });
      sync.flush({ id: 2, status: 'queued' });
      fixture.detectChanges();
      expect(el.querySelector('.data-freshness')?.textContent).toContain('Fetching your latest');

      vi.advanceTimersByTime(3000);
      http
        .expectOne('/api/import/status')
        .flush({ ...fresh(), latest_job: { id: 2, status: 'running' } });
      http.expectNone((r) => r.url === '/api/history');

      vi.advanceTimersByTime(3000);
      http
        .expectOne('/api/import/status')
        .flush({ ...fresh(), latest_job: { id: 2, status: 'done' } });
      flushData();
      fixture.detectChanges();
      expect(el.querySelector('.data-freshness')?.textContent).toContain('Readings up to');

      // It stops asking once the import has finished.
      vi.advanceTimersByTime(30000);
      http.expectNone('/api/import/status');
    });

    it('waits for an import already running instead of starting another', () => {
      start();
      http
        .expectOne('/api/import/status')
        .flush({ ...fresh('2026-06-02T12:00:00'), latest_job: { id: 2, status: 'running' } });
      flushData();
      http.expectNone('/api/import/fox-ess/sync');
      vi.advanceTimersByTime(3000);
      http
        .expectOne('/api/import/status')
        .flush({ ...fresh(), latest_job: { id: 2, status: 'done' } });
      flushData();
      http.expectNone('/api/import/fox-ess/sync');
    });

    it('keeps showing what it has when the fetch fails', () => {
      const { fixture, el } = start();
      http.expectOne('/api/import/status').flush(fresh('2026-06-02T12:00:00'));
      flushData();
      http
        .expectOne('/api/import/fox-ess/sync')
        .flush({ detail: 'Fox ESS is unavailable' }, { status: 502, statusText: 'Bad Gateway' });
      fixture.detectChanges();
      expect(el.querySelector('.form-error')?.textContent).toContain('Fox ESS is unavailable');
      expect(el.querySelector('.home-yesterday')?.textContent).toContain('£1.20');
    });

    it('says when the import itself failed, and still reloads', () => {
      const { fixture, el } = start();
      http
        .expectOne('/api/import/status')
        .flush({ ...fresh('2026-06-02T12:00:00'), latest_job: { id: 2, status: 'running' } });
      flushData();
      vi.advanceTimersByTime(3000);
      http.expectOne('/api/import/status').flush({
        ...fresh('2026-06-02T12:00:00'),
        latest_job: { id: 2, status: 'failed', message: 'Fox ESS rejected the API key' },
      });
      flushData();
      fixture.detectChanges();
      expect(el.querySelector('.form-error')?.textContent).toContain(
        'Fox ESS rejected the API key',
      );
    });
  });
});
