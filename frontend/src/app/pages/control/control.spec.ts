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
  consented_at: null,
  test_level: 0,
  test_steps: STEPS,
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
  ],
  request: { deviceSN: 'INV1', groups: [] },
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
      b.textContent?.includes('Apply to inverter'),
    )!;
    expect(apply.disabled).toBe(true);
    expect(el.textContent).toContain('Pass the write test up to step 4');
  });

  it('applies exactly the previewed schedule', () => {
    const fixture = render({ ...OFF, enabled: true, test_level: 4, can_apply: true });
    fixture.componentInstance.loadPreview();
    http.expectOne('/api/control/preview').flush(PROPOSAL);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('64%');
    expect(el.textContent).toContain('02:10');
    expect(el.textContent).toContain('force discharge to 10%');
    expect(el.textContent).toContain('Added');
    const apply = Array.from(el.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Apply to inverter'),
    )!;
    expect(apply.disabled).toBe(false);
    apply.click();
    const req = http.expectOne('/api/control/apply');
    expect(req.request.body).toEqual({ fingerprint: 'abc123', start_soc_percent: null });
  });
});
