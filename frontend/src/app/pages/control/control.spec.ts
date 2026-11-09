import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { ControlProposal, InverterControl } from '../../core/models';
import { Control } from './control';

const STEPS = [1, 2, 3, 4, 5, 6].map((step) => ({ step, label: `Step ${step}`, passed: false }));

const OFF: InverterControl = {
  connected: true,
  enabled: false,
  auto_apply: false,
  auto_apply_available: false,
  writes_allowed: true,
  consented_at: null,
  test_level: 0,
  test_steps: STEPS,
  dry_run_nights: [],
  dry_run_nights_needed: 3,
  can_apply: false,
  can_auto_apply: false,
  has_original: false,
  original_saved_at: null,
  original: [],
  original_enabled: null,
  planner_periods: [],
  test_periods: [],
  actions: [],
};

const PROPOSAL: ControlProposal = {
  day: '2026-11-01',
  mode: 'maximise_own_usage',
  planned_soc_percent: 63.4,
  charge_kwh: 4.2,
  start: '2026-11-01T02:10:00Z',
  stop: '2026-11-01T05:30:00Z',
  hold_start: '2026-10-31T23:30:00Z',
  hold_stop: '2026-11-01T02:10:00Z',
  discharge_start: '2026-10-31T22:30:00Z',
  discharge_stop: '2026-10-31T23:30:00Z',
  discharge_soc_percent: 10,
  discharge_power_w: 5000,
  target_soc_percent: 64,
  power_w: 3600,
  min_soc_on_grid: 10,
  schedule_enabled: true,
  max_periods: 8,
  current: [],
  added: [
    {
      start: '02:10',
      end: '05:30',
      work_mode: 'ForceCharge',
      charge_to_percent: 64,
      power_w: 3600,
      min_soc_percent: 10,
    },
  ],
  removed: [],
  after: [
    {
      start: '02:10',
      end: '05:30',
      work_mode: 'ForceCharge',
      charge_to_percent: 64,
      power_w: 3600,
      min_soc_percent: 10,
    },
    {
      start: '05:30',
      end: '00:00',
      work_mode: 'SelfUse',
      charge_to_percent: null,
      power_w: null,
      min_soc_percent: 10,
    },
  ],
  filled: [
    {
      start: '05:30',
      end: '00:00',
      work_mode: 'SelfUse',
      charge_to_percent: null,
      power_w: null,
      min_soc_percent: 10,
    },
  ],
  request: { deviceSN: 'INV1', isDefault: false, groups: [] },
  calls: [
    {
      method: 'POST',
      path: '/op/v3/device/scheduler/get',
      body: { deviceSN: 'INV1' },
      purpose: 'Read your schedule again, and stop if it has changed since this preview.',
      writes: false,
    },
    {
      method: 'POST',
      path: '/op/v3/device/scheduler/enable',
      body: { deviceSN: 'INV1', isDefault: false, groups: [] },
      purpose: "Replace your inverter's schedule with the 1 period shown above.",
      writes: true,
    },
    {
      method: 'POST',
      path: '/op/v3/device/scheduler/get',
      body: { deviceSN: 'INV1' },
      purpose: 'Read the schedule back and check it matches what was sent.',
      writes: false,
    },
  ],
  notes: ['Fox schedules repeat every day'],
  fingerprint: 'abc123',
};

describe('Control', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [Control],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render(status: InverterControl) {
    const fixture = TestBed.createComponent(Control);
    fixture.detectChanges();
    http.expectOne('/api/system').flush({ timezone: 'UTC' });
    http.expectOne('/api/control').flush(status);
    fixture.detectChanges();
    return fixture;
  }

  it('is off until the user confirms they understand it changes the inverter', () => {
    const fixture = render(OFF);
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('change');
    const button = Array.from(el.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Turn on'),
    )!;
    expect(button.disabled).toBe(true);
    (el.querySelector('input[type=checkbox]') as HTMLInputElement).click();
    fixture.detectChanges();
    expect(button.disabled).toBe(false);
    button.click();
    const req = http.expectOne('/api/control');
    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ enabled: true, accept: true });
  });

  it('runs the write test in order and keeps apply locked until step 4', () => {
    const fixture = render({ ...OFF, enabled: true, test_level: 1 });
    const el = fixture.nativeElement as HTMLElement;
    const run = Array.from(el.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Run step'),
    )!;
    expect(run.textContent).toContain('Run step 2');
    run.click();
    const req = http.expectOne('/api/control/test');
    expect(req.request.body).toEqual({ step: 2 });

    fixture.componentInstance.proposal.set(PROPOSAL);
    fixture.detectChanges();
    const apply = Array.from(el.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Authorise and send'),
    )!;
    expect(apply.disabled).toBe(true);
    expect(el.textContent).toContain('Pass the write test up to step 4');
  });

  it('only lets the read-only step run while the server blocks writes', () => {
    const fixture = render({ ...OFF, enabled: true, test_level: 1, writes_allowed: false });
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Writes to inverters are switched off on this server');
    const buttons = Array.from(el.querySelectorAll('button'));
    const named = (text: string) => buttons.find((b) => b.textContent?.includes(text))!;
    expect(named('Run step 2').disabled).toBe(true);
    expect(named('Remove planner periods').disabled).toBe(true);
    expect(named('Preview').disabled).toBe(false);
  });

  it('runs dry runs with writes off and ticks off each night', () => {
    const night = {
      day: '2026-11-06',
      at: '2026-11-05T18:00:00Z',
      passed: true,
      target_soc_percent: 65,
      message: 'Dry run for Fri 06 Nov: hold 02:00–03:30. 9 checks passed, 2 notes.',
      verdict: null,
    };
    const judged = {
      ...night,
      day: '2026-11-05',
      verdict: {
        suggested_percent: 65,
        charged_percent: 67,
        enough_percent: 60,
        lost_pence: 0.4,
        charged_lost_pence: 0.2,
        reserve_at: '2026-11-05T21:30:00Z',
        peak_import_kwh: 0,
        verdict: 'about right' as const,
        summary: 'Suggested 65%; the battery was charged to 67%. Verdict: about right.',
      },
    };
    const fixture = render({
      ...OFF,
      enabled: true,
      test_level: 4,
      writes_allowed: false,
      dry_run_nights: [night, judged],
    });
    const el = fixture.nativeElement as HTMLElement;
    const section = Array.from(el.querySelectorAll('section')).find((s) =>
      s.textContent?.includes('Three nights of dry runs'),
    )!;
    const nights = Array.from(section.querySelectorAll('li'));
    expect(nights.length).toBe(3);
    // Oldest first: the judged night, then tonight's, waiting for its readings.
    expect(nights[0].classList).toContain('passed');
    expect(nights[0].textContent).toContain('Thu 5 Nov');
    expect(nights[0].querySelector('.night-verdict.rating-good')?.textContent).toContain(
      'About right',
    );
    expect(nights[1].textContent).toContain('Fri 6 Nov');
    expect(nights[1].textContent).toContain('waiting for the next day');
    expect(nights[2].textContent).toContain('Not run yet');
    const run = Array.from(section.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Run tonight'),
    )!;
    expect(run.disabled).toBe(false); // reads only, so writes off doesn't block it
    run.click();
    const req = http.expectOne('/api/control/dry-run');
    expect(req.request.method).toBe('POST');
    req.flush({
      action: { ...night, id: 9, kind: 'dry_run', status: 'ok' },
      checks: [{ name: 'Values within the inverter’s ranges', status: 'pass', detail: 'ok' }],
      requests: [{ path: '/op/v3/device/scheduler/enable', body: {} }],
      proposal: PROPOSAL,
    });
    http
      .expectOne('/api/control')
      .flush({ ...OFF, enabled: true, test_level: 4, dry_run_nights: [night] });
    fixture.detectChanges();
    expect(el.textContent).toContain('Values within the inverter’s ranges');
    expect(el.textContent).toContain('1 request would be sent');
  });

  it('lists every call and sends nothing until the user authorises them', () => {
    const fixture = render({ ...OFF, enabled: true, test_level: 4, can_apply: true });
    fixture.componentInstance.loadPreview();
    http.expectOne('/api/control/preview').flush(PROPOSAL);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const calls = Array.from(el.querySelectorAll('ol.api-calls > li'));
    expect(calls.length).toBe(3);
    expect(calls[1].textContent).toContain('Changes inverter');
    expect(calls[1].textContent).toContain('/op/v3/device/scheduler/enable');
    expect(calls[0].textContent).toContain('Read only');
    expect(el.textContent).toContain('1 of them changes your inverter');
    const send = Array.from(el.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Authorise and send'),
    )!;
    expect(send.disabled).toBe(true);
    send.click();
    http.expectNone('/api/control/apply');
  });

  it('says so when nothing would change the inverter', () => {
    const fixture = render({ ...OFF, enabled: true, test_level: 4, can_apply: true });
    fixture.componentInstance.loadPreview();
    http.expectOne('/api/control/preview').flush({ ...PROPOSAL, calls: [PROPOSAL.calls[0]] });
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('no call would change your inverter');
    expect(el.querySelector('ol.api-calls')).toBeNull();
  });

  it('applies exactly the previewed schedule once authorised', () => {
    const fixture = render({ ...OFF, enabled: true, test_level: 4, can_apply: true });
    fixture.componentInstance.loadPreview();
    http.expectOne('/api/control/preview').flush(PROPOSAL);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('64%');
    expect(el.textContent).toContain('02:10');
    expect(el.textContent).toContain('force discharge to 10%');
    expect(el.textContent).toContain("Planner's charge");
    // The rest of the day, which the planner writes as self-use.
    expect(el.textContent).toContain('Self-use');
    const apply = Array.from(el.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Authorise and send'),
    )!;
    const authorise = Array.from(el.querySelectorAll('input[type=checkbox]')).find((i) =>
      i.parentElement?.textContent?.includes('authorise them'),
    ) as HTMLInputElement;
    authorise.click();
    fixture.detectChanges();
    expect(apply.disabled).toBe(false);
    apply.click();
    const req = http.expectOne('/api/control/apply');
    expect(req.request.body).toEqual({ fingerprint: 'abc123', start_soc_percent: null });
  });
});
