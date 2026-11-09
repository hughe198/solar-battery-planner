import { TestBed } from '@angular/core/testing';
import { Explain } from './explain';

const POINTS = ['Tomorrow: 9.0 kWh use and 3.1 kWh solar.', 'Charging higher would gain little.'];

describe('Explain', () => {
  beforeEach(async () => {
    localStorage.clear();
    await TestBed.configureTestingModule({ imports: [Explain] }).compileComponents();
  });

  async function render(inputs: {
    key?: string;
    label?: string;
    sentence?: string;
    points?: string[];
  }) {
    const fixture = TestBed.createComponent(Explain);
    for (const [name, value] of Object.entries(inputs)) fixture.componentRef.setInput(name, value);
    fixture.detectChanges();
    await fixture.whenStable();
    const el = fixture.nativeElement as HTMLElement;
    const button = () => el.querySelector<HTMLButtonElement>('button');
    const tap = async () => {
      button()!.click();
      fixture.detectChanges();
      await fixture.whenStable();
    };
    const sentence = () => el.querySelector('.explain-sentence')?.textContent?.trim() ?? null;
    const points = () =>
      [...el.querySelectorAll('.explain-points li')].map((li) => li.textContent?.trim());
    const action = () => button()?.textContent?.replace(/\s+/g, ' ').trim();
    return { el, button, tap, sentence, points, action };
  }

  it('steps from hidden to the sentence to the points and back', async () => {
    const v = await render({
      label: 'Plan',
      sentence: 'Charging to 70% because tomorrow looks dull.',
      points: POINTS,
    });

    expect(v.action()).toBe('Plan · Why?');
    expect(v.button()!.getAttribute('aria-expanded')).toBe('false');
    expect(v.sentence()).toBeNull();

    await v.tap();
    expect(v.sentence()).toBe('Charging to 70% because tomorrow looks dull.');
    expect(v.points()).toEqual([]);
    expect(v.action()).toBe('Plan · More detail');
    expect(v.button()!.getAttribute('aria-expanded')).toBe('true');

    await v.tap();
    expect(v.sentence()).not.toBeNull();
    expect(v.points()).toEqual(POINTS);
    expect(v.action()).toBe('Plan · Hide');

    await v.tap();
    expect(v.sentence()).toBeNull();
    expect(v.points()).toEqual([]);
    expect(v.action()).toBe('Plan · Why?');
  });

  it('skips the points step when there are none', async () => {
    const v = await render({ sentence: 'The day went as forecast.' });
    await v.tap();
    expect(v.sentence()).toBe('The day went as forecast.');
    expect(v.action()).toBe('Hide');
    await v.tap();
    expect(v.sentence()).toBeNull();
  });

  it('goes straight to the points when there is no sentence', async () => {
    const v = await render({ points: POINTS });
    await v.tap();
    expect(v.points()).toEqual(POINTS);
    expect(v.action()).toBe('Hide');
  });

  it('shows nothing when there is nothing to explain', async () => {
    const v = await render({});
    expect(v.button()).toBeNull();
  });

  it('remembers the last state on this device by key', async () => {
    const first = await render({ key: 'home-tonight', sentence: 'One sentence.', points: POINTS });
    await first.tap();
    await first.tap();
    expect(localStorage.getItem('sbp_explain_home-tonight')).toBe('points');

    const again = await render({ key: 'home-tonight', sentence: 'One sentence.', points: POINTS });
    expect(again.points()).toEqual(POINTS);

    const other = await render({ key: 'home-today', sentence: 'One sentence.', points: POINTS });
    expect(other.sentence()).toBeNull();
  });

  it('shows the sentence when the points it remembers are missing', async () => {
    localStorage.setItem('sbp_explain_weather', 'points');
    const v = await render({ key: 'weather', sentence: 'Cloudy all day.' });
    expect(v.sentence()).toBe('Cloudy all day.');
  });

  it('still works when storage is blocked', async () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const v = await render({ key: 'blocked', sentence: 'One sentence.' });
    expect(v.sentence()).toBeNull();
    await v.tap();
    expect(v.sentence()).toBe('One sentence.');
    get.mockRestore();
    set.mockRestore();
  });
});
