import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { map } from 'rxjs';
import { AuthService } from './auth.service';

/** Requires a signed-in user. */
export const authGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  return auth.loadUser().pipe(map((user) => (user ? true : router.parseUrl('/login'))));
};

/** Requires a signed-in user who has finished system setup. */
export const systemGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  return auth
    .loadUser()
    .pipe(
      map((user) =>
        !user ? router.parseUrl('/login') : user.has_system ? true : router.parseUrl('/setup'),
      ),
    );
};

/** Keeps signed-in users off the login and register pages. */
export const guestGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  return auth.loadUser().pipe(map((user) => (user ? router.parseUrl('/') : true)));
};
