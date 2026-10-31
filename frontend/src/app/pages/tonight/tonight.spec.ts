import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import {
  ChargePlan,
  DayForecast,
  FlowPoint,
  IntervalForecast,
  PlanOutcome,
  Recommendation,
  SystemConfig,
} from '../../core/models';
import { Tonight } from './tonight';

const SYSTEM = {
  name: 'Home',
  timezone: 'Europe/London',
  arrays: [{ name: 'South', kwp: 4, tilt_degrees: 35, azimuth_degrees: 180 }],
  battery: { capacity_kwh: 10, min_soc_percent: 10 },
  tariff: {
    name: 'Octopus Go',
    bands: [
      { direction: 'import', days: 'all', start: '23:30', end: '05:30', rate: 7.5 },
      { direction: 'import', days: 'all', start: '05:30', end: '23:30', rate: 27.5 },
      { direction: 'export', days: 'all', start: '00:00', end: '00:00', rate: 15 },
    ],
  },
} as unknown as SystemConfig;

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

describe('Tonight', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Tonight],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render(forecast: DayForecast, plan: ChargePlan | null = null) {
    const fixture = TestBed.createComponent(Tonight);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne('/api/forecast').flush(forecast);
    const req = http.expectOne(
      (r) => r.url === '/api/plan' && r.params.get('day') === forecast.day,
    );
    if (plan) req.flush(plan);
    else req.flush({ detail: 'No battery' }, { status: 422, statusText: 'Unprocessable' });
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  // The window opens at 23:30 local (22:30 UTC) the evening before; the plan ends 40 slots later.
  function planFrom(windowStart: number): ChargePlan {
    return {
      day: FORECAST.day,
      start_soc_percent: 12,
      charge_start: new Date(windowStart).toISOString(),
      charge_end: new Date(windowStart + 12 * 1_800_000).toISOString(),
      recommendations: [
        {
          ...recommendation('maximise_own_usage', 64),
          hold_start: null,
          hold_stop: null,
          discharge_start: null,
          discharge_stop: null,
          target_soc_percent: 64,
          force_charge_start: new Date(Date.UTC(2026, 5, 2, 2, 0)).toISOString(),
          force_charge_stop: new Date(Date.UTC(2026, 5, 2, 4, 0)).toISOString(),
          reasons: ['Solar takes over by mid-morning.'],
        },
      ],
      timeline: Array.from({ length: 40 }, (_, n) => ({
        start: new Date(windowStart + n * 1_800_000).toISOString(),
        load_kwh: 0.2,
        solar_kwh: 0,
        import_kwh: 0,
        export_kwh: 0,
        soc_percent: 20 + n,
        cheap: n < 12,
      })),
    } as unknown as ChargePlan;
  }
  const svgText = (el: HTMLElement) =>
    [...el.querySelectorAll('svg text')].map((t) => t.textContent?.trim());

  it('explains the charge from 21:00 the evening before', () => {
    const el = render(FORECAST, planFrom(Date.UTC(2026, 5, 1, 22, 30)));
    const line = el.querySelector('svg path.battery')?.getAttribute('d') ?? '';
    expect(line.match(/[ML]/g)?.length).toBe(40);
    expect(line.match(/M/g)?.length).toBe(1);
    // Dotted at the assumed level from the start of the chart up to the first planned point.
    const assumed = el.querySelector('svg path.battery-assumed')?.getAttribute('d') ?? '';
    const [start, end] = assumed.slice(1).split(' L');
    expect(end).toBe(line.slice(1).split(' ')[0].split(',')[0] + ',' + end.split(',')[1]);
    expect(start.split(',')[1]).toBe(end.split(',')[1]);
    expect(svgText(el)).toContain('21');
    expect(svgText(el)).toContain('Target 64%');
    expect(el.querySelector('svg rect.cheap')).toBeNull();
    expect(el.querySelectorAll('svg rect.force').length).toBe(1);
    // One plot: 100% is level with the top of the kWh axis, and usage is back.
    expect(el.querySelector('svg path.series.load')?.getAttribute('d')).toMatch(/^M/);
    expect(svgText(el)).toContain('100%');
    expect(el.textContent).toContain('Why charge to 64%');
    expect(el.textContent).toContain('Solar takes over by mid-morning.');
    expect(el.textContent).toContain('assumes 12% when the cheap window opens');
  });

  it('starts at the current half hour, with solar from the forecast for today', () => {
    const fixture = TestBed.createComponent(Tonight);
    fixture.componentInstance.clock = () => Date.UTC(2026, 5, 1, 15, 10);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne('/api/forecast').flush(FORECAST);
    http
      .expectOne((r) => r.url === '/api/plan' && r.params.get('day') === FORECAST.day)
      .flush(planFrom(Date.UTC(2026, 5, 1, 22, 30)));
    const lead = FORECAST.intervals.map((i) => ({
      ...i,
      start: new Date(new Date(i.start).getTime() - 86_400_000).toISOString(),
    }));
    http
      .expectOne((r) => r.url === '/api/forecast' && r.params.get('day') === '2026-06-01')
      .flush({ ...FORECAST, day: '2026-06-01', intervals: lead });
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;

    // 16:00 BST to the end of the plan: 15 half hours before the window, then 40 planned.
    const hours = svgText(el).filter((t) => /^\d\d$/.test(t ?? ''));
    expect(hours[0]).toBe('18');
    const solar = el.querySelector('svg path.series.solar')?.getAttribute('d') ?? '';
    expect(solar.match(/[ML]/g)?.length).toBe(55);
    expect(solar.match(/M/g)?.length).toBe(1);
    expect(el.querySelector('svg path.battery')?.getAttribute('d')?.match(/[ML]/g)?.length).toBe(
      40,
    );
  });

  describe('the one-sentence explanation', () => {
    let recharged = false;
    const verdict = (
      expected: { reserve_at: string | null; end_soc_percent: number },
      charging = true,
    ) => {
      const plan = planFrom(Date.UTC(2026, 5, 1, 22, 30));
      const r = plan.recommendations[0];
      r.expected = expected as typeof r.expected;
      if (!charging) r.force_charge_start = null;
      // Flat at the minimum, unless a test wants it back up after 04:00 UTC (half hour 11).
      plan.timeline.forEach((p, n) => (p.soc_percent = recharged && n > 11 ? 50 : 10));
      recharged = false;
      return render(FORECAST, plan).querySelector('.verdict')?.textContent;
    };

    it('says when the battery reaches its minimum', () => {
      // 16:30 UTC is 17:30 BST.
      const at = new Date(Date.UTC(2026, 5, 2, 16, 30)).toISOString();
      expect(verdict({ reserve_at: at, end_soc_percent: 10 })).toBe(
        "Charge to 64% in Monday's 23:30–05:30 cheap period, with the house on the grid throughout. We predict the battery will reach its 10% minimum on Tuesday at 17:30.",
      );
    });

    it('mentions solar when the level recovers after reaching the minimum', () => {
      recharged = true;
      const at = new Date(Date.UTC(2026, 5, 2, 4, 0)).toISOString();
      expect(verdict({ reserve_at: at, end_soc_percent: 10 })).toContain(
        'reach its 10% minimum on Tuesday at 05:00, before solar recharges it.',
      );
    });

    it('names the next cheap period and its day, separately from the one being decided', () => {
      // The plan's 40 half hours from Monday 23:30 BST end on Tuesday at 19:30 BST.
      expect(verdict({ reserve_at: null, end_soc_percent: 18.4 })).toBe(
        "Charge to 64% in Monday's 23:30–05:30 cheap period, with the house on the grid throughout. We predict the battery will still have 18% left when the next cheap period starts on Tuesday at 19:30.",
      );
    });

    it('says how far solar lifts the battery after the window', () => {
      recharged = true;
      expect(verdict({ reserve_at: null, end_soc_percent: 18.4 })).toContain(
        'We predict solar will lift the battery to 50% around 05:30, and it will still have 18% left when',
      );
    });

    it('explains a night with no charge', () => {
      expect(verdict({ reserve_at: null, end_soc_percent: 30 }, false)).toBe(
        "No charge needed in Monday's 23:30–05:30 cheap period, with 12% going in: the battery is held and the house runs on the grid. We predict the battery will still have 30% left when the next cheap period starts on Tuesday at 19:30.",
      );
    });

    it('marks the held period on the chart and in the hover reading', () => {
      const plan = planFrom(Date.UTC(2026, 5, 1, 22, 30));
      const r = plan.recommendations[0];
      r.force_charge_start = null;
      r.force_charge_stop = null;
      r.hold_start = plan.charge_start;
      r.hold_stop = plan.charge_end;
      const fixture = TestBed.createComponent(Tonight);
      fixture.detectChanges();
      http.expectOne('/api/system').flush(SYSTEM);
      http.expectOne('/api/forecast').flush(FORECAST);
      http
        .expectOne((q) => q.url === '/api/plan' && q.params.get('day') === FORECAST.day)
        .flush(plan);
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;
      expect(el.querySelectorAll('svg rect.hold').length).toBe(1);
      expect(el.querySelector('svg rect.force')).toBeNull();
      expect(el.textContent).toContain('Battery held, house on the grid');
      expect(el.textContent).toContain('set a Force Charge period for 23:30–05:30 in the Fox app');
      // Half hour 5 is the window's first (the chart opens at 21:00, the window at 23:30).
      el.querySelectorAll('svg rect.hit')[5].dispatchEvent(new MouseEvent('mouseenter'));
      fixture.detectChanges();
      expect(el.querySelector('.chart-readout')?.textContent).toContain('House on the grid');
      el.querySelectorAll('svg rect.hit')[4].dispatchEvent(new MouseEvent('mouseenter'));
      fixture.detectChanges();
      expect(el.querySelector('.chart-readout')?.textContent).not.toContain('House on the grid');
    });
  });

  it('reads the showing lines at the hovered half hour, and matches the axis to them', () => {
    const fixture = TestBed.createComponent(Tonight);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne('/api/forecast').flush(FORECAST);
    http
      .expectOne((r) => r.url === '/api/plan' && r.params.get('day') === FORECAST.day)
      .flush(planFrom(Date.UTC(2026, 5, 1, 22, 30)));
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const readout = () => el.querySelector('.chart-readout')?.textContent?.trim();

    expect(readout()).toContain('Hover over the chart');
    expect(el.querySelector('svg circle.dot')).toBeNull();

    // The chart opens at 21:00 BST and the window at 23:30, so half hour 5 is the first planned.
    const hit = el.querySelectorAll('svg rect.hit')[5];
    hit.dispatchEvent(new MouseEvent('mouseenter'));
    fixture.detectChanges();
    expect(readout()).toBe(
      'Mon 23:30 · Battery 20% · Usage 0.20 kWh · Solar 0.00 kWh · House on the grid',
    );
    expect(el.querySelectorAll('svg circle.dot').length).toBe(3);
    expect(el.querySelector('svg line.crosshair')).not.toBeNull();

    el.querySelector<HTMLInputElement>('input[name="show-battery"]')!.click();
    fixture.detectChanges();
    expect(readout()).toBe('Mon 23:30 · Usage 0.20 kWh · Solar 0.00 kWh · House on the grid');
    expect(svgText(el)).not.toContain('100%');

    el.querySelector('svg')!.dispatchEvent(new MouseEvent('mouseleave'));
    fixture.detectChanges();
    expect(readout()).toContain('Hover over the chart');
    expect(el.querySelector('svg line.crosshair')).toBeNull();
  });

  describe('the day stepper', () => {
    /** Answers everything outstanding: forecasts for the day asked, plans with an error. */
    function answerAll() {
      for (let pending = http.match(() => true); pending.length; pending = http.match(() => true)) {
        for (const req of pending) {
          if (req.request.url === '/api/forecast') {
            const day = req.request.params.get('day') ?? FORECAST.day;
            req.flush({ ...FORECAST, day });
          } else if (req.request.url === '/api/system') {
            req.flush(SYSTEM);
          } else {
            req.flush({ detail: 'No plan' }, { status: 422, statusText: 'Unprocessable' });
          }
        }
      }
    }

    function open() {
      const fixture = TestBed.createComponent(Tonight);
      // 23:30 UTC on 31 May is already 1 June in London.
      fixture.componentInstance.clock = () => Date.UTC(2026, 4, 31, 23, 30);
      fixture.detectChanges();
      answerAll();
      fixture.detectChanges();
      const el = fixture.nativeElement as HTMLElement;
      return {
        fixture,
        el,
        label: () =>
          [...(el.querySelector('.day-stepper-label')?.children ?? [])]
            .map((c) => c.textContent?.trim())
            .join(' ')
            .trim(),
        previous: () => el.querySelector<HTMLButtonElement>('[aria-label="Previous day"]')!,
        next: () => el.querySelector<HTMLButtonElement>('[aria-label="Next day"]')!,
      };
    }

    it('replaces the date input and shows the day between its arrows', () => {
      const { el, label } = open();
      expect(el.querySelector('input[type="date"]')).toBeNull();
      expect(label()).toBe('Tuesday 2 June Tomorrow');
    });

    it('stops at today and at 7 days ahead, in the system time zone', () => {
      const { fixture, label, previous, next } = open();
      const click = (button: HTMLButtonElement) => {
        button.click();
        answerAll();
        fixture.detectChanges();
      };

      click(previous());
      expect(label()).toBe('Monday 1 June Today');
      expect(previous().disabled).toBe(true);
      expect(next().disabled).toBe(false);

      for (let i = 0; i < 6; i++) click(next());
      expect(label()).toBe('Sunday 7 June');
      expect(next().disabled).toBe(false);
      click(next());
      expect(label()).toBe('Monday 8 June');
      expect(next().disabled).toBe(true);
    });

    it('keeps the latest day when an earlier answer arrives last', () => {
      const { fixture, next, label } = open();
      next().click();
      fixture.detectChanges();
      next().click();
      fixture.detectChanges();
      expect(label()).toBe('Thursday 4 June');

      const forecastFor = (day: string) =>
        http.expectOne((r) => r.url === '/api/forecast' && r.params.get('day') === day);
      const third = forecastFor('2026-06-03');
      forecastFor('2026-06-04').flush({ ...FORECAST, day: '2026-06-04' });
      third.flush({ ...FORECAST, day: '2026-06-03' });
      answerAll();
      fixture.detectChanges();

      expect(fixture.componentInstance.forecast()?.day).toBe('2026-06-04');
      expect(label()).toBe('Thursday 4 June');
    });
  });

  describe('good solar hours', () => {
    const withSolar = (solar: (i: number) => number): DayForecast => ({
      ...FORECAST,
      intervals: FORECAST.intervals.map((interval, i) => ({
        ...interval,
        end: new Date(new Date(interval.start).getTime() + 1_800_000).toISOString(),
        solar_kwh: solar(i),
      })),
    });
    const goodSolar = (forecast: DayForecast) =>
      render(forecast).querySelector('.good-solar')?.textContent?.trim();

    // A tenth of the system's 4 kWp is 0.4 kW, or 0.2 kWh per half hour. Half hour 0 is 00:00 BST.
    it('counts the half hours that reach a tenth of the panels, with when', () => {
      expect(goodSolar(withSolar((i) => (i >= 20 && i < 27 ? 0.2 : 0.19)))).toBe(
        '3.5 good solar hours, 10:00–13:30',
      );
    });

    it('says between when the good spells are broken up', () => {
      expect(goodSolar(withSolar((i) => (i === 20 || i === 25 ? 0.5 : 0)))).toBe(
        '1 good solar hour, between 10:00 and 13:00',
      );
    });

    it('says when there are none', () => {
      expect(goodSolar(withSolar(() => 0.19))).toBe('No good solar hours');
    });

    it('explains the measure with the panels own numbers', () => {
      expect(render(withSolar(() => 0)).textContent).toContain(
        'forecast to make at least 0.4 kW, a tenth of their 4 kWp',
      );
    });
  });

  it('toggles each line with its checkbox', () => {
    const fixture = TestBed.createComponent(Tonight);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne('/api/forecast').flush(FORECAST);
    http
      .expectOne((r) => r.url === '/api/plan' && r.params.get('day') === FORECAST.day)
      .flush(planFrom(Date.UTC(2026, 5, 1, 22, 30)));
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const lines = () =>
      ['battery', 'load', 'solar'].filter((c) => el.querySelector(`svg path.series.${c}`));
    const toggle = (name: string) => {
      el.querySelector<HTMLInputElement>(`input[name="show-${name}"]`)!.click();
      fixture.detectChanges();
    };

    expect(lines()).toEqual(['battery', 'load', 'solar']);
    toggle('battery');
    expect(lines()).toEqual(['load', 'solar']);
    expect(el.querySelector('svg rect.force')).toBeNull();
    expect(svgText(el)).not.toContain('Target 64%');
    toggle('usage');
    expect(lines()).toEqual(['solar']);
    expect(el.querySelector('svg path.band.load')).toBeNull();
    toggle('solar');
    expect(lines()).toEqual([]);
    toggle('battery');
    expect(lines()).toEqual(['battery']);
  });

  it('runs the battery line from the imported reading, with no assumed part', () => {
    const windowStart = Date.UTC(2026, 5, 1, 22, 30);
    const plan = planFrom(windowStart);
    plan.start_soc_source = 'measured';
    plan.reading_at = new Date(windowStart - 12 * 1_800_000).toISOString();
    plan.reading_soc_percent = 60;
    plan.lead_in = Array.from({ length: 6 }, (_, n) => ({
      ...plan.timeline[0],
      start: new Date(windowStart - (6 - n) * 1_800_000).toISOString(),
      soc_percent: 40 - n,
      cheap: false,
    }));
    const el = render(FORECAST, plan);
    expect(el.querySelector('svg path.battery-assumed')).toBeNull();
    const line = el.querySelector('svg path.battery')?.getAttribute('d') ?? '';
    // This day is far ahead, so the chart opens at 21:00 BST, clipping one of the six lead-in points.
    expect(line.match(/[ML]/g)?.length).toBe(45);
    expect(line.match(/M/g)?.length).toBe(1);
    expect(el.textContent).toContain('your last imported reading');
    expect(el.textContent).toContain('60% at Mon 17:30');
    expect(el.textContent).toContain('predicts 12% when the cheap window opens');
  });

  it('opens the chart at the last reading when that is before now', () => {
    const windowStart = Date.UTC(2026, 5, 1, 22, 30);
    const plan = planFrom(windowStart);
    plan.start_soc_source = 'measured';
    plan.reading_at = new Date(Date.UTC(2026, 5, 1, 12, 0)).toISOString();
    plan.reading_soc_percent = 60;
    plan.lead_in = Array.from({ length: 21 }, (_, n) => ({
      ...plan.timeline[0],
      start: new Date(Date.UTC(2026, 5, 1, 12, 0) + n * 1_800_000).toISOString(),
      soc_percent: 60 - n,
      cheap: false,
    }));
    const fixture = TestBed.createComponent(Tonight);
    fixture.componentInstance.clock = () => Date.UTC(2026, 5, 1, 15, 10);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne('/api/forecast').flush(FORECAST);
    http
      .expectOne((r) => r.url === '/api/plan' && r.params.get('day') === FORECAST.day)
      .flush(plan);
    http
      .expectOne((r) => r.url === '/api/forecast' && r.params.get('day') === '2026-06-01')
      .flush({ ...FORECAST, day: '2026-06-01', intervals: [] });
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const line = el.querySelector('svg path.battery')?.getAttribute('d') ?? '';
    expect(line.match(/[ML]/g)?.length).toBe(61);
    expect(line.match(/M/g)?.length).toBe(1);
    // 13:00 BST is the reading; the first three-hourly label after it is 15:00.
    expect(svgText(el).filter((t) => /^\d\d$/.test(t ?? ''))[0]).toBe('15');
  });

  it('starts earlier than 21:00 when the charge window does', () => {
    const el = render(FORECAST, planFrom(Date.UTC(2026, 5, 1, 17, 0)));
    expect(svgText(el)).toContain('18');
  });

  it('still shows the forecast when there is no plan', () => {
    const el = render(FORECAST);
    expect(el.querySelector('svg path.battery')).toBeNull();
    expect(el.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 720 280');
    expect(el.textContent).toContain('Half-hourly forecast');
    expect(el.textContent).not.toContain('cheap window');
  });

  it('shows totals, ranges and how the forecast was made', () => {
    const el = render(FORECAST);
    expect(el.textContent).toContain('Tuesday 2 June');
    expect(el.textContent).toContain('9.6 kWh');
    expect(el.textContent).toContain('8.1–11.2 kWh');
    expect(el.textContent).toContain('until you import solar data');
    expect(el.textContent).toContain('South (4 kWp) delivers about 82%');
    expect(el.querySelector('details summary')?.textContent).toBe('How this was predicted');
    expect(el.textContent).toContain('9 kWh usable');
    expect(el.textContent).toContain('7.5p');
    expect(el.querySelectorAll('svg path').length).toBe(4);
    const hours = [...el.querySelectorAll('svg text')].map((t) => t.textContent?.trim());
    expect(hours).toContain('00');
    expect(hours).toContain('12');
  });

  it('shows the recommendation for each mode and why', () => {
    const el = render(FORECAST, PLAN);
    expect(el.textContent).toContain('Tuesday 2 June');
    const modes = el.querySelectorAll('.plan-modes section');
    expect(modes.length).toBe(2);
    expect(modes[0].textContent).toContain('Maximise own usage (your mode)');
    expect(modes[0].textContent).toContain('52%');
    expect(modes[1].textContent).toContain('95%');
    expect(el.textContent).toContain('Why charge to 52%');
    expect(el.textContent).toContain('costing 33p');
    // Europe/London is an hour ahead of UTC in June.
    expect(modes[0].textContent).toMatch(/Force charge 05:20–\s*06:30/);
    expect(modes[0].textContent).toContain('1 h 10 min');
    expect(modes[0].textContent).toMatch(/Hold the battery 00:30–\s*05:20/);
    expect(modes[0].textContent).toMatch(/Force discharge 23:30–\s*00:30/);
    expect(modes[0].textContent).toContain('earning 48p');
    expect(modes[0].textContent).toContain('Downsides');
    expect(modes[0].textContent).toContain('0.40 of a full cycle');
    expect(el.textContent).toContain('3.6 kW, 1.2 kW above 90%');
    expect(el.querySelectorAll('table.plan-table')[0].querySelectorAll('tbody tr').length).toBe(3);
    expect(el.querySelector('svg path.battery')?.getAttribute('d')).toMatch(/^M/);
  });

  it('recalculates from a new starting level', () => {
    const fixture = TestBed.createComponent(Tonight);
    fixture.detectChanges();
    http.expectOne('/api/system').flush(SYSTEM);
    http.expectOne('/api/forecast').flush(FORECAST);
    http.expectOne((r) => r.url === '/api/plan').flush(PLAN);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const input = el.querySelector('input[type=number]') as HTMLInputElement;
    input.value = '45';
    (el.querySelector('.soc-input button') as HTMLButtonElement).click();
    const req = http.expectOne((r) => r.url === '/api/plan');
    expect(req.request.params.get('start_soc_percent')).toBe('45');
    expect(req.request.params.get('day')).toBe('2026-06-02');
    req.flush(PLAN);
    fixture.detectChanges();
  });
});
