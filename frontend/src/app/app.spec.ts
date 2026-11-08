import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { App } from './app';
import { AuthService } from './core/auth.service';

describe('App', () => {
  beforeEach(async () => {
    localStorage.clear();
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [
        provideRouter([
          { path: '', pathMatch: 'full', children: [] },
          { path: 'history', children: [] },
          { path: 'automation', children: [] },
          { path: 'login', children: [] },
        ]),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
  });

  it('shows the brand and no nav when signed out', async () => {
    const fixture = TestBed.createComponent(App);
    await fixture.whenStable();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.brand')?.textContent).toContain('Solar Battery Planner');
    expect(el.querySelector('nav')).toBeNull();
  });

  async function signedIn(url: string) {
    const auth = TestBed.inject(AuthService);
    const http = TestBed.inject(HttpTestingController);
    auth.login('hugh@example.com', 'sunshine123').subscribe();
    http.expectOne('/api/auth/login').flush({ access_token: 'abc' });
    http.expectOne('/api/auth/me').flush({ id: 1, email: 'hugh@example.com', has_system: true });

    const fixture = TestBed.createComponent(App);
    await TestBed.inject(Router).navigateByUrl(url);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const settle = async () => {
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();
    };
    return { fixture, el, settle };
  }

  it('shows Home, Tonight, History and More, marking the current page', async () => {
    const { el } = await signedIn('/history');
    const tabs = [...el.querySelectorAll<HTMLElement>('nav .tab')];
    expect(tabs.map((t) => t.textContent?.trim())).toEqual(['Home', 'Tonight', 'History', 'More']);
    const current = el.querySelectorAll('nav .tab.active');
    expect(current.length).toBe(1);
    expect(current[0].textContent?.trim()).toBe('History');
    expect(current[0].getAttribute('aria-current')).toBe('page');
    expect(el.querySelector('#more-menu')).toBeNull();
  });

  it('opens More with Automation, System, Data and Sign out', async () => {
    const { el, settle } = await signedIn('/history');
    const more = [...el.querySelectorAll<HTMLButtonElement>('nav .tab')].at(-1)!;
    expect(more.getAttribute('aria-expanded')).toBe('false');
    more.click();
    await settle();

    expect(more.getAttribute('aria-expanded')).toBe('true');
    const menu = el.querySelector('#more-menu')!;
    expect([...menu.querySelectorAll('a .label')].map((a) => a.textContent?.trim())).toEqual([
      'Automation',
      'System',
      'Data',
    ]);
    expect(menu.textContent).toContain('hugh@example.com');
    expect(menu.querySelector('button')?.textContent?.trim()).toBe('Sign out');

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    await settle();
    expect(el.querySelector('#more-menu')).toBeNull();
  });

  it('marks More as current on its pages and closes it on navigation', async () => {
    const { el, settle } = await signedIn('/history');
    [...el.querySelectorAll<HTMLButtonElement>('nav .tab')].at(-1)!.click();
    await settle();
    await TestBed.inject(Router).navigateByUrl('/automation');
    await settle();

    expect(el.querySelector('#more-menu')).toBeNull();
    const current = el.querySelector('nav .tab.active');
    expect(current?.textContent?.trim()).toBe('More');
  });

  it('signs out from the More menu', async () => {
    const { el, settle } = await signedIn('/history');
    [...el.querySelectorAll<HTMLButtonElement>('nav .tab')].at(-1)!.click();
    await settle();
    el.querySelector<HTMLButtonElement>('#more-menu button')!.click();
    await settle();
    expect(TestBed.inject(AuthService).isLoggedIn()).toBe(false);
  });

  it('stores the token and loads the user on login', () => {
    const auth = TestBed.inject(AuthService);
    const http = TestBed.inject(HttpTestingController);
    let email = '';
    auth.login('hugh@example.com', 'sunshine123').subscribe((u) => (email = u.email));

    http.expectOne('/api/auth/login').flush({ access_token: 'abc' });
    http.expectOne('/api/auth/me').flush({ id: 1, email: 'hugh@example.com', has_system: false });

    expect(email).toBe('hugh@example.com');
    expect(localStorage.getItem('sbp_token')).toBe('abc');
    expect(auth.isLoggedIn()).toBe(true);
    http.verify();
  });
});
