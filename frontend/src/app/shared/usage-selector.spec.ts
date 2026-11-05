import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { UsageExpectations } from '../core/models';
import { UsageSelector } from './usage-selector';

const DATA: UsageExpectations = {
  days: [{ day: '2026-06-03', level: 'away', car_kwh: null }],
  profile: { base_daily_kwh: 4.2, low_factor: 0.7, high_factor: 1.36, learned: true },
  has_heat_pump: false,
  has_electric_car: false,
  car_charger_kw: 7,
};

describe('UsageSelector', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UsageSelector],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render(day: string, data: UsageExpectations = DATA) {
    const fixture = TestBed.createComponent(UsageSelector);
    fixture.componentRef.setInput('day', day);
    const changed = vi.fn();
    const levels: string[] = [];
    fixture.componentInstance.changed.subscribe(changed);
    fixture.componentInstance.levelChange.subscribe((l) => levels.push(l));
    fixture.detectChanges();
    const req = http.expectOne((r) => r.url === '/api/usage-expectations');
    expect(req.request.params.get('start')).toBe(day);
    req.flush(data);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const pressed = () => el.querySelector('[aria-pressed="true"]')?.textContent?.trim();
    const button = (label: string) =>
      [...el.querySelectorAll<HTMLButtonElement>('.segmented button')].find(
        (b) => b.textContent?.trim() === label,
      )!;
    return { fixture, el, pressed, button, changed, levels };
  }

  it('shows the marked level and what it means in the home’s own figures', () => {
    const { el, pressed, levels } = render('2026-06-03');
    expect(pressed()).toBe('Away');
    expect(levels).toEqual(['away']);
    expect(el.textContent).toContain('your 4.2 kWh a day base load');
    expect(el.querySelector('input[name="car-kwh"]')).toBeNull();
  });

  it('saves a new level straight away and tells the page', () => {
    const { fixture, button, changed, pressed } = render('2026-06-04');
    expect(pressed()).toBe('Normal');
    button('High').click();
    const put = http.expectOne('/api/usage-expectations/2026-06-04');
    expect(put.request.method).toBe('PUT');
    expect(put.request.body).toEqual({ level: 'high', car_kwh: null });
    put.flush({ day: '2026-06-04', level: 'high', car_kwh: null });
    http
      .expectOne((r) => r.url === '/api/usage-expectations')
      .flush({ ...DATA, days: [{ day: '2026-06-04', level: 'high', car_kwh: null }] });
    fixture.detectChanges();
    expect(changed).toHaveBeenCalledTimes(1);
    expect(pressed()).toBe('High');
    expect(fixture.nativeElement.textContent).toContain('scaled by 1.36');
  });

  it('takes a car charge when the home has an electric car', () => {
    const { el } = render('2026-06-04', { ...DATA, has_electric_car: true });
    const input = el.querySelector<HTMLInputElement>('input[name="car-kwh"]')!;
    input.value = '18';
    el.querySelector<HTMLButtonElement>('.car-charge button')!.click();
    const put = http.expectOne('/api/usage-expectations/2026-06-04');
    expect(put.request.body).toEqual({ level: 'normal', car_kwh: 18 });
  });

  it('marks a holiday as a range of days', () => {
    const { el } = render('2026-06-04');
    el.querySelector<HTMLInputElement>('input[name="away-to"]')!.value = '2026-06-11';
    [...el.querySelectorAll<HTMLButtonElement>('.holiday button')]
      .find((b) => b.textContent?.includes('Mark as away'))!
      .click();
    const put = http.expectOne((r) => r.url === '/api/usage-expectations' && r.method === 'PUT');
    expect(put.request.body).toEqual({ start: '2026-06-04', end: '2026-06-11', level: 'away' });
  });

  it('hides the car and the range when marking a past day', () => {
    const fixture = TestBed.createComponent(UsageSelector);
    fixture.componentRef.setInput('day', '2026-05-01');
    fixture.componentRef.setInput('showCar', false);
    fixture.componentRef.setInput('showRange', false);
    fixture.detectChanges();
    http
      .expectOne((r) => r.url === '/api/usage-expectations')
      .flush({ ...DATA, has_electric_car: true });
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('input[name="car-kwh"]')).toBeNull();
    expect(el.querySelector('.holiday')).toBeNull();
  });
});
