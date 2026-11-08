import { TestBed } from '@angular/core/testing';
import { SavingsDay } from '../core/models';
import { SavingsView } from './savings-view';

function day(date: string, grid: number, imports: number, exports: number): SavingsDay {
  return {
    day: date,
    house_kwh: 10,
    grid_only_pence: grid,
    import_cost_pence: imports,
    export_revenue_pence: exports,
  };
}

// Two whole weeks, Mon 12 to Sun 25 October; the second saved more.
const DAYS = [
  ...Array.from({ length: 7 }, (_, i) => day(`2026-10-${12 + i}`, 150, 60, 20)),
  ...Array.from({ length: 7 }, (_, i) => day(`2026-10-${19 + i}`, 160, 50, 10)),
];

describe('SavingsView', () => {
  function render(days: SavingsDay[] = DAYS) {
    const fixture = TestBed.createComponent(SavingsView);
    fixture.componentRef.setInput('days', days);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const button = (text: string) =>
      [...el.querySelectorAll('button')].find((b) => b.textContent?.includes(text))!;
    return { fixture, el, button };
  }

  it('shows the whole period first, with a bar a week and the running total', () => {
    const { el, button } = render();
    expect(button('Whole period').getAttribute('aria-pressed')).toBe('true');
    // 14 days saved 7 × 110p + 7 × 120p.
    expect(el.textContent).toContain('Over 14 complete days');
    expect(el.textContent).toContain('£16.10');
    expect(el.querySelectorAll('.week-bar').length).toBe(2);
    expect(el.textContent).toContain('Running total');
    expect(el.querySelector('path.line.paid')?.getAttribute('d')?.match(/L/g)?.length).toBe(13);
  });

  it('switches to one week, compared with the week before', () => {
    const { fixture, el, button } = render();
    button('One week').click();
    fixture.detectChanges();
    // Opens on the latest whole week.
    expect(el.textContent).toContain('In the week of 19 October');
    expect(el.textContent).toContain('£8.40'); // 7 × 120p
    expect(el.textContent).toContain('70p more than the week before');
    expect(el.querySelector('.better')?.textContent).toContain('£8.40');
    expect(el.textContent).toContain('Day by day');
    expect(el.querySelectorAll('rect.paid').length).toBe(7);
    expect(el.querySelectorAll('.week-bar.dim').length).toBe(1);
    expect((el.querySelector('select.week-pick') as HTMLSelectElement).value).toBe('2026-10-19');
  });

  it('picks a week by clicking its bar, and says when it has no week before it', () => {
    const { fixture, el } = render();
    (el.querySelectorAll('.week-bar')[0] as SVGGElement).dispatchEvent(new MouseEvent('click'));
    fixture.detectChanges();
    expect(el.textContent).toContain('In the week of 12 October');
    expect(el.textContent).not.toContain('the week before.');
    expect(el.textContent).toContain('none to compare');
  });

  it('marks days without a full set of readings in a week', () => {
    const { fixture, el, button } = render(DAYS.filter((d) => d.day !== '2026-10-22'));
    button('One week').click();
    fixture.detectChanges();
    expect(el.textContent).toContain('(6 days of readings)');
    expect(el.textContent).toContain('no data');
    expect(el.textContent).toContain('part week');
  });

  it('says what to do when there are no complete days', () => {
    const { el } = render([]);
    expect(el.textContent).toContain('No complete days of readings yet');
  });
});
