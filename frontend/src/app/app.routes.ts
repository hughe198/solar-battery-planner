import { Routes } from '@angular/router';
import { authGuard, guestGuard, systemGuard } from './core/auth.guard';
import { AuthPage } from './pages/auth/auth-page';
import { Dashboard } from './pages/dashboard/dashboard';
import { DataImport } from './pages/data-import/data-import';
import { Forecast } from './pages/forecast/forecast';
import { Plan } from './pages/plan/plan';
import { SystemSetup } from './pages/system-setup/system-setup';

export const routes: Routes = [
  { path: '', component: Dashboard, canActivate: [systemGuard] },
  { path: 'plan', component: Plan, canActivate: [systemGuard], title: 'Overnight charge' },
  { path: 'forecast', component: Forecast, canActivate: [systemGuard], title: 'Forecast' },
  { path: 'data', component: DataImport, canActivate: [systemGuard], title: 'Your data' },
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
