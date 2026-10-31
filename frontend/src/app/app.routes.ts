import { Routes } from '@angular/router';
import { authGuard, guestGuard, systemGuard } from './core/auth.guard';
import { AuthPage } from './pages/auth/auth-page';
import { Dashboard } from './pages/dashboard/dashboard';
import { SystemSetup } from './pages/system-setup/system-setup';

export const routes: Routes = [
  { path: '', component: Dashboard, canActivate: [systemGuard] },
  { path: 'setup', component: SystemSetup, canActivate: [authGuard], title: 'System setup' },
  {
    path: 'login',
    component: AuthPage,
    canActivate: [guestGuard],
    data: { mode: 'login' },
    title: 'Sign in',
  },
  {
    path: 'register',
    component: AuthPage,
    canActivate: [guestGuard],
    data: { mode: 'register' },
    title: 'Create account',
  },
  { path: '**', redirectTo: '' },
];
