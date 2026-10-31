import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import {
  ChargePlan,
  FlowPoint,
  PlanOutcome,
  Recommendation,
  Replay,
  SystemConfig,
} from '../../core/models';
import { Plan } from './plan';

const SYSTEM = { name: 'Home', timezone: 'UTC', arrays: [] } as unknown as SystemConfig;

const OUTCOME: PlanOutcome = {
  load_kwh: 10,
  solar_kwh: 12,
  import_kwh: 4,
  export_kwh: 3,
  import_cost_pence: 45,
  export_revenue_pence: 12,
  net_cost_pence: 33,
  end_soc_percent: 40,
  full_at: '2026-06-02T11:00:00Z',
  reserve_at: null,
};

function recommendation(mode: Recommendation['mode'], target: number): Recommendation {
  return {
    mode,
    target_soc_percent: target,
    stored_kwh: 4,
    charge_kwh: 4.4,
    charge_cost_pence: 33,
    force_charge_start: '2026-06-02T04:20:00Z',
    force_charge_stop: '2026-06-02T05:30:00Z',
    force_charge_minutes: 70,
    force_charge_reaches_target: true,
    hold_start: '2026-06-01T23:30:00Z',
    hold_stop: '2026-06-02T04:20:00Z',
    discharge_start: '2026-06-01T22:30:00Z',
    discharge_stop: '2026-06-01T23:30:00Z',
    discharge_kwh: 3.2,
    discharge_revenue_pence: 48,
    discharge_to_percent: 10,
    expected: OUTCOME,
    dull: { ...OUTCOME, solar_kwh: 6, net_cost_pence: 120 },
    bright: { ...OUTCOME, solar_kwh: 16, net_cost_pence: -20 },
    reasons: [`Charge to ${target}%: 4.4 kWh from the grid at 7.5p, costing 33p.`],
    downsides: ['Battery wear: charging puts 0.40 of a full cycle through the battery.'],
  };
}

function point(i: number): FlowPoint {
  return {
    start: new Date(Date.UTC(2026, 5, 2, 0, 0) + i * 1_800_000).toISOString(),
    load_kwh: 0.3,
    solar_kwh: i > 16 && i < 36 ? 0.8 : 0,
    import_kwh: i < 11 ? 0.3 : 0,
    export_kwh: i > 20 && i < 30 ? 0.4 : 0,
    soc_percent: 50,
    cheap: i < 11,
  };
}

const PLAN: ChargePlan = {
  day: '2026-06-02',
  mode: 'maximise_own_usage',
  start_soc_percent: 10,
  max_soc_percent: 100,
  charge_start: '2026-06-01T23:30:00Z',
  charge_end: '2026-06-02T05:30:00Z',
  charge_rate_kw: 3.6,
  taper_rate_kw: 1.2,
  taper_from_percent: 90,
  charge_rate_samples: 40,
  recommendations: [
    recommendation('maximise_own_usage', 52),
    recommendation('maximise_export_revenue', 95),
  ],
  candidates: [],
  timeline: Array.from({ length: 47 }, (_, i) => point(i)),
  start_soc_source: 'assumed',
  reading_at: null,
  reading_soc_percent: null,
  lead_in: [],
  load: { expected: 10, low: 8, high: 12 },
  solar: { expected: 12, low: 6, high: 16 },
  load_method: 'learned',
  solar_method: 'learned',
  history_days: 60,
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

describe('Plan', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Plan],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render(replay: Replay = REPLAY) {
    const fixture = TestBed.createComponent(Plan);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne('/api/plan').flush(PLAN);
    http.expectOne((r) => r.url === '/api/plan/replay').flush(replay);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  it('shows the recommendation for each mode and why', () => {
    const { el } = render();
    expect(el.textContent).toContain('Tuesday 2 June');
    const modes = el.querySelectorAll('.plan-modes section');
    expect(modes.length).toBe(2);
    expect(modes[0].textContent).toContain('Maximise own usage (your mode)');
    expect(modes[0].textContent).toContain('52%');
    expect(modes[1].textContent).toContain('95%');
    expect(el.textContent).toContain('Why 52%');
    expect(el.textContent).toContain('costing 33p');
    expect(modes[0].textContent).toMatch(/Force charge 04:20–\s*05:30/);
    expect(modes[0].textContent).toContain('1 h 10 min');
    expect(modes[0].textContent).toMatch(/Hold the battery 23:30–\s*04:20/);
    expect(modes[0].textContent).toMatch(/Force discharge 22:30–\s*23:30/);
    expect(modes[0].textContent).toContain('earning 48p');
    expect(modes[0].textContent).toContain('Downsides');
    expect(modes[0].textContent).toContain('0.40 of a full cycle');
    expect(el.textContent).toContain('3.6 kW, 1.2 kW above 90%');
    expect(el.querySelectorAll('table.plan-table')[0].querySelectorAll('tbody tr').length).toBe(3);
    expect(el.querySelector('app-flow-chart path.soc')?.getAttribute('d')).toMatch(/^M/);
  });

  it('reviews the last charge and its cost', () => {
    const { el } = render();
    expect(el.textContent).toContain('How your last charge worked out');
    expect(el.textContent).toContain('100%');
    expect(el.textContent).toContain('40p');
    expect(el.textContent).toContain('That was more than the day needed.');
    expect(el.textContent).toContain('would have saved');
  });

  it('recalculates from a new starting level', () => {
    const { fixture, el } = render();
    const input = el.querySelector('input[type=number]') as HTMLInputElement;
    input.value = '45';
    (el.querySelector('.soc-input button') as HTMLButtonElement).click();
    const req = http.expectOne((r) => r.url === '/api/plan');
    expect(req.request.params.get('start_soc_percent')).toBe('45');
    expect(req.request.params.get('day')).toBe('2026-06-02');
    req.flush(PLAN);
    fixture.detectChanges();
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
      const fixture = TestBed.createComponent(Plan);
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
