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

  it('shows the menu as buttons, marking the current page', async () => {
    const auth = TestBed.inject(AuthService);
    const http = TestBed.inject(HttpTestingController);
    auth.login('hugh@example.com', 'sunshine123').subscribe();
    http.expectOne('/api/auth/login').flush({ access_token: 'abc' });
    http.expectOne('/api/auth/me').flush({ id: 1, email: 'hugh@example.com', has_system: true });

    const fixture = TestBed.createComponent(App);
    await TestBed.inject(Router).navigateByUrl('/history');
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const buttons = [...el.querySelectorAll<HTMLElement>('nav .nav-button')];
    expect(buttons.map((b) => b.textContent?.trim())).toEqual([
      'Home',
      'Tonight',
      'History',
      'Inverter',
      'System',
      'Data',
      'Sign out',
    ]);
    const current = el.querySelectorAll('nav .nav-button.active');
    expect(current.length).toBe(1);
    expect(current[0].textContent?.trim()).toBe('History');
    expect(current[0].getAttribute('aria-current')).toBe('page');
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
