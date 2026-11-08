import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { AuthPage } from './auth-page';

describe('AuthPage', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AuthPage],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render(mode: 'login' | 'register', open: boolean | 'error') {
    const fixture = TestBed.createComponent(AuthPage);
    fixture.componentRef.setInput('mode', mode);
    fixture.detectChanges();
    const req = http.expectOne('/api/auth/options');
    if (open === 'error') req.flush('down', { status: 502, statusText: 'Bad Gateway' });
    else req.flush({ registration_open: open });
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('offers sign-up from the sign-in page while sign-ups are open', () => {
    const el = render('login', true);
    expect(el.querySelector('form')).not.toBeNull();
    expect(el.textContent).toContain('Create an account');
  });

  it('stops offering sign-up once sign-ups are closed', () => {
    const el = render('login', false);
    expect(el.querySelector('form')).not.toBeNull(); // existing accounts still sign in
    expect(el.textContent).not.toContain('Create an account');
  });

  it('says so on the sign-up page when sign-ups are closed', () => {
    const el = render('register', false);
    expect(el.querySelector('form')).toBeNull();
    expect(el.textContent).toContain("New accounts aren't being taken at the moment");
    expect(el.querySelector('a[href="/login"]')).not.toBeNull();
  });

  it('keeps the sign-up form when the server can’t be asked', () => {
    const el = render('register', 'error');
    expect(el.querySelector('form')).not.toBeNull();
  });
});
