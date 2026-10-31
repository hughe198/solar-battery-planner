import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { Observable, catchError, map, of, switchMap, tap } from 'rxjs';
import { User } from './models';

const TOKEN_KEY = 'sbp_token';

function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly http = inject(HttpClient);

  readonly token = signal<string | null>(readToken());
  readonly user = signal<User | null>(null);
  readonly isLoggedIn = computed(() => this.token() !== null);

  register(email: string, password: string): Observable<User> {
    return this.authenticate('/api/auth/register', email, password);
  }

  login(email: string, password: string): Observable<User> {
    return this.authenticate('/api/auth/login', email, password);
  }

  logout(): void {
    this.setToken(null);
    this.user.set(null);
  }

  /** Loads the current user, or null if there is no valid session. */
  loadUser(): Observable<User | null> {
    if (!this.token()) return of(null);
    if (this.user()) return of(this.user());
    return this.http.get<User>('/api/auth/me').pipe(
      tap((user) => this.user.set(user)),
      catchError(() => {
        this.logout();
        return of(null);
      }),
    );
  }

  markHasSystem(): void {
    const user = this.user();
    if (user) this.user.set({ ...user, has_system: true });
  }

  private authenticate(url: string, email: string, password: string): Observable<User> {
    return this.http.post<{ access_token: string }>(url, { email, password }).pipe(
      tap(({ access_token }) => {
        this.setToken(access_token);
        this.user.set(null);
      }),
      switchMap(() => this.loadUser()),
      map((user) => user!),
    );
  }

  private setToken(token: string | null): void {
    this.token.set(token);
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      // Storage unavailable (private mode); the session lasts until reload.
    }
  }
}
