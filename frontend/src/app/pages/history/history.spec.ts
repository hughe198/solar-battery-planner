import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Backtest, History as WeekHistory, Replay, SystemConfig } from '../../core/models';
import { History } from './history';

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
    wear_pence_per_cycle: 0,
  },
  tariff: {
    name: 'Octopus Go',
    preset: null,
    standing_charge_pence_per_day: 53,
    bands: [
      { direction: 'import', days: 'all', start: '23:30', end: '05:30', rate: 7.5 },
      { direction: 'import', days: 'all', start: '05:30', end: '23:30', rate: 27.5 },
      { direction: 'export', days: 'all', start: '00:00', end: '00:00', rate: 15 },
    ],
  },
  arrays: [{ name: 'South', kwp: 4, tilt_degrees: 35, azimuth_degrees: 180 }],
};

const HISTORY: WeekHistory = {
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

const NO_BACKTEST = { history_days: 5, backtest: null };

const BACKTEST: Backtest = {
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
};

const REPLAY: Replay = {
  mode: 'maximise_own_usage',
  days: [
    {
      day: '2026-05-31',
      load_kwh: 10,
      solar_kwh: 14,
      soc_before_percent: 20,
      soc_after_charge_percent: 100,
      actual_import_kwh: 7,
      actual_export_kwh: 10.9,
      actual_net_cost_pence: -110,
      simulated_import_kwh: 7.1,
      simulated_export_kwh: 10.8,
      soc_error_percent: 2,
      best_soc_percent: 52,
      missed_saving_pence: 40,
      wasted_export_kwh: 5.3,
      shortfall_import_kwh: 0,
    },
  ],
  import_error_percent: 1.4,
  export_error_percent: 0.9,
  soc_error_percent: 2,
  missed_saving_pence: 40,
  review: { day: null as never, comments: ['That was more than the day needed.'] },
};
REPLAY.review!.day = REPLAY.days[0];

describe('History', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [History],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render(
    replay: Replay = REPLAY,
    options: { history?: WeekHistory; backtest?: Backtest } = {},
  ) {
    const fixture = TestBed.createComponent(History);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http
      .expectOne((r) => r.url === '/api/history')
      .flush(options.history ?? { days: [], points: [] });
    http.expectOne((r) => r.url === '/api/plan/replay').flush(replay);
    http
      .expectOne('/api/forecast/accuracy')
      .flush(options.backtest ? { history_days: 74, backtest: options.backtest } : NO_BACKTEST);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('charts the last week and summarises it', () => {
    const { fixture, el } = render(REPLAY, { history: HISTORY });
    http.expectOne('/api/plan/replay/2026-05-31').flush({ ...REPLAY.review, points: [] });
    fixture.detectChanges();
    expect(el.textContent).toContain('Your last 7 days');
    expect(el.textContent).toContain('60%');
    expect(el.textContent).toContain('£3.50'); // 7 × 50p of imports
    expect(el.querySelectorAll('app-week-chart rect.ss').length).toBe(7);
    expect(el.querySelectorAll('app-week-chart circle.soc-end').length).toBe(14);
    const week = el.querySelectorAll('app-flow-chart')[0];
    const days = [...week.querySelectorAll('text.axis')].map((t) => t.textContent?.trim());
    expect(days).toContain('Thu');
    expect(week.querySelectorAll('rect.cheap').length).toBe(7);
    expect(week.querySelectorAll('rect.export').length).toBe(7 * 5);
    // Gaps in battery data break the line rather than joining across them.
    expect(week.querySelector('path.soc')?.getAttribute('d')?.match(/M/g)?.length).toBe(7 * 24);
  });

  describe('the charge under way', () => {
    const IN_PROGRESS: NonNullable<Replay['in_progress']> = {
      day: '2026-06-01',
      charge_start: '2026-06-01T02:00:00Z',
      charge_end: '2026-06-01T05:00:00Z',
      scored_after: '2026-06-02T02:00:00Z',
      window_finished: true,
      soc_before_percent: 66,
      soc_peak_percent: 66,
      soc_after_percent: 65,
      outcome: 'held',
      grid_only: true,
      latest_reading_at: '2026-06-01T18:00:00Z',
      points: [
        {
          start: '2026-06-01T01:30:00Z',
          load_kwh: 0.1,
          solar_kwh: 0,
          import_kwh: 0,
          export_kwh: 0,
          soc_percent: 66,
          cheap: false,
          lead_up: true,
        },
        {
          start: '2026-06-01T02:00:00Z',
          load_kwh: 0.1,
          solar_kwh: 0,
          import_kwh: 0.1,
          export_kwh: 0,
          soc_percent: 65,
          cheap: true,
          lead_up: false,
        },
      ],
    };
    const open = (progress: Partial<typeof IN_PROGRESS> = {}, today = '2026-06-01T18:30:00Z') => {
      const fixture = TestBed.createComponent(History);
      fixture.componentInstance.clock = () => new Date(today).getTime();
      fixture.detectChanges();
      http.expectOne('/api/system').flush(SYSTEM);
      http.expectOne((r) => r.url === '/api/history').flush({ days: [], points: [] });
      http
        .expectOne((r) => r.url === '/api/plan/replay')
        .flush({ ...REPLAY, in_progress: { ...IN_PROGRESS, ...progress } });
      http.expectOne('/api/forecast/accuracy').flush(NO_BACKTEST);
      http.expectOne('/api/plan/replay/2026-05-31').flush({ ...REPLAY.review, points: [] });
      fixture.detectChanges();
      return { fixture, el: fixture.nativeElement as HTMLElement };
    };
    const section = (el: HTMLElement) => el.querySelector('.in-progress-charge');

    it("calls it last night's charge, says what it did and when it gets a score", () => {
      const card = section(open().el)!;
      expect(card.querySelector('h2')?.textContent).toBe("Last night's charge");
      expect(card.textContent).toContain(
        'In the 02:00–05:00 cheap period, the battery went in at 66% and held at 65%, with the house on the grid.',
      );
      expect(card.textContent).toContain(
        'In progress. Its score comes after 02:00 on Tuesday, once that data is imported.',
      );
    });

    it('uses the date when the data is too old for it to be last night', () => {
      const { el } = open({}, '2026-06-04T09:00:00Z');
      expect(section(el)?.querySelector('h2')?.textContent).toBe(
        'Charge at 02:00 on Monday 1 June',
      );
    });

    it('describes a charge, a fall and a window still open', () => {
      expect(section(open({ outcome: 'charged', soc_peak_percent: 90 }).el)?.textContent).toContain(
        'went in at 66% and charged to 90%, with the house on the grid.',
      );
    });

    it('does not claim grid only when the battery fell', () => {
      expect(
        section(open({ outcome: 'discharged', soc_after_percent: 55, grid_only: false }).el)
          ?.textContent,
      ).toContain('went in at 66% and fell to 55%.');
    });

    it('says so far while the window is open', () => {
      expect(section(open({ window_finished: false }).el)?.textContent).toContain(
        'held at 65% so far, with the house on the grid.',
      );
    });

    it('marks its calendar square and shows its chart without asking for a replay', () => {
      const { fixture, el } = open();
      const square = el.querySelector<HTMLButtonElement>('button.calendar-day.in-progress')!;
      expect(el.querySelector('.day-stepper-label strong')?.textContent).toBe('June 2026');
      expect(square.textContent).toContain('65%');
      expect(square.getAttribute('aria-label')).toContain('no score yet');

      square.click();
      fixture.detectChanges();

      http.expectNone('/api/plan/replay/2026-06-01');
      const lookBack = el.querySelector('.look-back')!;
      expect(lookBack.querySelector('h3')?.textContent).toBe("Last night's charge");
      expect(lookBack.querySelector('path.soc')?.getAttribute('d')).toMatch(/^M/);
      expect(lookBack.querySelectorAll('rect.lead-up').length).toBe(1);
      expect(lookBack.textContent).toContain('Evening before, not scored.');
      expect(lookBack.textContent).not.toContain('Loading');
    });
  });

  it('names the charge it reviews by date and time, and its cost', () => {
    const { el } = render({
      ...REPLAY,
      review: {
        day: { ...REPLAY.days[0], charge_start: '2026-05-30T23:30:00Z' },
        comments: REPLAY.review!.comments,
      },
    });
    expect(el.textContent).toContain('Charge at 23:30 on Saturday 30 May');
    expect(el.textContent).toContain('The latest charge with a full result.');
    expect(el.textContent).not.toContain('How your last charge');
    expect(el.textContent).toContain('100%');
    expect(el.textContent).toContain('40p');
    expect(el.textContent).toContain('That was more than the day needed.');
    expect(el.textContent).toContain('would have saved');
  });

  it('shows backtest accuracy when there is enough history', () => {
    const { el } = render(REPLAY, { backtest: BACKTEST });
    expect(el.textContent).toContain('Trained on 60 days');
    expect(el.textContent).toContain('12%');
    expect(el.textContent).toContain('panel physics alone: 22%');
    expect(el.querySelectorAll('table.backtest tbody tr').length).toBe(1);
  });

  it('says when there is not enough history to check accuracy', () => {
    const { el } = render();
    expect(el.textContent).toContain('three weeks of usage history');
  });

  describe('look back', () => {
    const day = (date: string, charged: number, lost: number) => ({
      ...REPLAY.days[0],
      day: date,
      soc_after_charge_percent: charged,
      missed_saving_pence: lost,
    });
    const LOOK_BACK: Replay = {
      ...REPLAY,
      days: [
        day('2026-04-30', 40, 0),
        day('2026-05-01', 60, 0.4),
        day('2026-05-02', 70, 12),
        day('2026-05-04', 90, 45),
      ],
    };
    const detailFor = (date: string) => ({
      day: LOOK_BACK.days.find((d) => d.day === date)!,
      comments: [`Review of ${date}`],
      points: [
        {
          start: '2026-05-04T00:00:00Z',
          load_kwh: 0.2,
          solar_kwh: 0,
          import_kwh: 0.2,
          export_kwh: 0,
          soc_percent: 50,
          cheap: true,
          best_soc_percent: null,
        },
        {
          start: '2026-05-04T00:30:00Z',
          load_kwh: 0.2,
          solar_kwh: 0,
          import_kwh: 0,
          export_kwh: 0,
          soc_percent: 49,
          cheap: false,
          best_soc_percent: 30,
        },
        {
          start: '2026-05-04T01:00:00Z',
          load_kwh: 0.2,
          solar_kwh: 0,
          import_kwh: 0,
          export_kwh: 0,
          soc_percent: 48,
          cheap: false,
          best_soc_percent: 29,
        },
      ],
    });
    const answer = (date: string) =>
      http.expectOne(`/api/plan/replay/${date}`).flush(detailFor(date));
    const open = () => {
      const { fixture, el } = render(LOOK_BACK);
      answer('2026-05-04');
      fixture.detectChanges();
      const square = (date: number) =>
        [...el.querySelectorAll<HTMLButtonElement>('button.calendar-day')].find(
          (b) => b.querySelector('.calendar-date')?.textContent === String(date),
        );
      return { fixture, el, square };
    };

    it('asks for as much history as it can', () => {
      const fixture = TestBed.createComponent(History);
      fixture.detectChanges();
      const req = http.expectOne((r) => r.url === '/api/plan/replay');
      expect(req.request.params.get('days')).toBe('120');
    });

    it('opens on the latest month and day, rated by what each charge lost', () => {
      const { el, square } = open();
      expect(el.querySelector('.day-stepper-label strong')?.textContent).toBe('May 2026');
      expect(el.querySelector('.day-stepper-label .muted')?.textContent).toBe(
        '57p lost, about right on 1 of 3 days',
      );
      // May 2026 starts on a Friday, so four blanks after the weekday row.
      const grid = [...el.querySelector('.calendar')!.children];
      expect(grid.slice(7, 11).every((c) => !c.textContent?.trim())).toBe(true);
      expect(grid[11].textContent).toContain('1');
      expect(el.querySelectorAll('.calendar-day').length).toBe(31);
      expect(el.querySelectorAll('.calendar-day.empty').length).toBe(28);

      expect(square(1)?.className).toContain('good');
      expect(square(2)?.className).toContain('fair');
      expect(square(4)?.className).toContain('poor');
      expect(square(4)?.textContent).toContain('90%');
      expect(square(4)?.className).toContain('selected');
      expect(square(4)?.getAttribute('aria-label')).toBe(
        'Monday 4 May: charged to 90%, best 52%, lost 45p',
      );
    });

    it('shows a picked day with the real and best battery lines and its review', () => {
      const { fixture, el, square } = open();
      square(2)!.click();
      fixture.detectChanges();
      expect(el.textContent).toContain('Saturday 2 May');
      expect(el.querySelector('.look-back')?.textContent).toContain('Loading');

      answer('2026-05-02');
      fixture.detectChanges();
      const lookBack = el.querySelector('.look-back')!;
      expect(lookBack.textContent).toContain('Review of 2026-05-02');
      expect(lookBack.querySelector('path.soc.best')?.getAttribute('d')).toMatch(/^M/);
      expect(lookBack.querySelector('path.soc:not(.best)')?.getAttribute('d')).toMatch(/^M/);
      expect(lookBack.textContent).toContain('With the best charge');
      expect(square(2)?.className).toContain('selected');
      expect(square(4)?.className).not.toContain('selected');
    });

    it('moves between months and stops at the ends of the data', () => {
      const { fixture, el } = open();
      const previous = () => el.querySelector<HTMLButtonElement>('[aria-label="Previous month"]')!;
      const next = () => el.querySelector<HTMLButtonElement>('[aria-label="Next month"]')!;
      expect(next().disabled).toBe(true);
      previous().click();
      fixture.detectChanges();
      expect(el.querySelector('.day-stepper-label strong')?.textContent).toBe('April 2026');
      expect(previous().disabled).toBe(true);
      expect(next().disabled).toBe(false);
    });

    it('keeps the last picked day when an earlier answer arrives last', () => {
      const { fixture, el, square } = open();
      square(1)!.click();
      square(2)!.click();
      fixture.detectChanges();
      const first = http.expectOne('/api/plan/replay/2026-05-01');
      answer('2026-05-02');
      first.flush(detailFor('2026-05-01'));
      fixture.detectChanges();
      expect(el.querySelector('.look-back')?.textContent).toContain('Review of 2026-05-02');
    });

    it('says so when a day cannot be loaded', () => {
      const { fixture, el, square } = open();
      square(2)!.click();
      http
        .expectOne('/api/plan/replay/2026-05-02')
        .flush({ detail: "That day can't be replayed" }, { status: 404, statusText: 'Not Found' });
      fixture.detectChanges();
      expect(el.querySelector('.look-back .form-error')?.textContent).toContain(
        "That day can't be replayed",
      );
    });
  });

  it('explains what is needed when there is no battery history', () => {
    const { el } = render({ ...REPLAY, days: [], review: null, missed_saving_pence: 0 });
    expect(el.textContent).toContain("Once you've imported days with battery level");
  });
});
