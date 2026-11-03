import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { vi } from 'vitest';
import { ChargePlan, History, ImportStatus, Replay, SystemConfig } from '../../core/models';
import { Home } from './home';

const NOW = Date.UTC(2026, 5, 2, 18, 40); // Tuesday 2 June, 19:40 in London.
const SYSTEM = { timezone: 'Europe/London', name: 'Home' } as SystemConfig;

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
    },
  ],
} as unknown as ChargePlan;

const fresh = (last = '2026-06-02T18:30:00'): ImportStatus =>
  ({
    fox_ess: { device_sn: 'X' },
    usage: { count: 10, first: null, last },
    latest_job: null,
  }) as unknown as ImportStatus;

describe('Home', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Home],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function flushData(
    data: { recent?: History; replay?: Replay; plan?: ChargePlan | null } = {},
  ): void {
    http.expectOne((r) => r.url === '/api/history').flush(data.recent ?? RECENT);
    http.expectOne((r) => r.url === '/api/plan/replay').flush(data.replay ?? REPLAY);
    const plan = http.expectOne((r) => r.url === '/api/plan');
    if (data.plan === null) {
      plan.flush({ detail: 'No weather forecast yet' }, { status: 502, statusText: 'Bad Gateway' });
    } else {
      plan.flush(data.plan ?? PLAN);
    }
  }

  function start() {
    const fixture = TestBed.createComponent(Home);
    fixture.componentInstance.clock = () => NOW;
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  function open(data: Parameters<typeof flushData>[0] = {}, status = fresh()) {
    const page = start();
    http.expectOne('/api/import/status').flush(status);
    flushData(data);
    page.fixture.detectChanges();
    return page;
  }

  it('answers what happened yesterday first', () => {
    const { el } = open();
    const first = el.querySelector('section')!;
    expect(first.classList).toContain('home-yesterday');
    expect(first.querySelector('h2')?.textContent).toBe('Yesterday, Monday 1 June');
    expect(first.textContent).toContain('7.2 kWh');
    expect(first.textContent).toContain('5.9 kWh');
    expect(first.textContent).toContain('Net cost');
    expect(first.textContent).toContain('65p');
    expect(first.textContent).toContain('21–89%');
    expect(first.textContent).toContain('Charge at 02:00 on Monday 1 June:');
    expect(first.textContent).toContain('so it cost 12p more than it needed to.');
  });

  it("then what last night's charge did", () => {
    const { el } = open();
    const card = el.querySelector('.home-charge')!;
    expect(card.querySelector('h2')?.textContent).toBe("Last night's charge");
    expect(card.textContent).toContain(
      'went in at 66% and held at 65%, with the house on the grid.',
    );
    expect(card.textContent).toContain('Its score comes after 02:00 on Wednesday');
    expect(card.querySelector('path.soc')).not.toBeNull();
  });

  it('then what is suggested for tonight, last', () => {
    const { el } = open();
    const sections = [...el.querySelectorAll('section')].map((s) => s.className);
    expect(sections[2]).toContain('home-tonight');
    const card = el.querySelector('.home-tonight')!;
    expect(card.querySelector('.target')?.textContent).toBe('No charge needed');
    expect(card.textContent).toContain(
      'Hold the battery 02:00–05:00 on Wednesday, house on the grid.',
    );
    expect(card.textContent).toContain("the battery's 59% is enough");
    expect(card.textContent).toContain(
      'Predicted 59% going in at 02:00, from your latest reading.',
    );
    expect(card.textContent).toContain('Expected to leave 39%');
    expect(card.querySelector('a')?.getAttribute('href')).toBe('/tonight');
  });

  it('gives the times and cost when a charge is suggested', () => {
    const plan = structuredClone(PLAN);
    plan.recommendations[0] = {
      ...plan.recommendations[0],
      target_soc_percent: 72,
      force_charge_start: '2026-06-03T02:30:00Z',
      force_charge_stop: '2026-06-03T04:00:00Z',
      charge_kwh: 2.5,
      charge_cost_pence: 42,
      hold_stop: '2026-06-03T02:30:00Z',
    };
    const card = open({ plan }).el.querySelector('.home-tonight')!;
    expect(card.querySelector('.target')?.textContent).toBe('Charge to 72%');
    expect(card.textContent).toContain(
      'Force charge 03:30–05:00 on Wednesday: 2.5 kWh, costing 42p.',
    );
  });

  it('says when yesterday has no score yet', () => {
    const { el } = open({ replay: { ...REPLAY, review: null } });
    expect(el.querySelector('.home-yesterday')?.textContent).toContain(
      'gets its score once its whole cycle is imported',
    );
  });

  it('does not pass off an older charge as last night or yesterday', () => {
    const replay = structuredClone(REPLAY);
    replay.in_progress!.day = '2026-05-28';
    replay.in_progress!.charge_start = '2026-05-28T01:00:00Z';
    replay.review!.day = { ...replay.review!.day, day: '2026-05-27' };
    const { el } = open({ replay });
    expect(el.querySelector('.home-charge h2')?.textContent).toBe(
      'Charge at 02:00 on Thursday 28 May',
    );
    expect(el.querySelector('.home-yesterday')?.textContent).not.toContain('Charge at');
  });

  it('still answers the first question when the plan fails', () => {
    const { el } = open({ plan: null });
    expect(el.querySelector('.home-yesterday')?.textContent).toContain('7.2 kWh');
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
      expect(el.querySelector('.home-yesterday')?.textContent).toContain('7.2 kWh');
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
