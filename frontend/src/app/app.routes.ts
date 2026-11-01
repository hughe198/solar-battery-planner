import { Routes } from '@angular/router';
import { authGuard, guestGuard, systemGuard } from './core/auth.guard';
import { AuthPage } from './pages/auth/auth-page';
import { Control } from './pages/control/control';
import { DataImport } from './pages/data-import/data-import';
import { History } from './pages/history/history';
import { Home } from './pages/home/home';
import { SystemSetup } from './pages/system-setup/system-setup';
import { Tonight } from './pages/tonight/tonight';

export const routes: Routes = [
  { path: '', component: Home, canActivate: [systemGuard], pathMatch: 'full', title: 'Home' },
  { path: 'tonight', component: Tonight, canActivate: [systemGuard], title: 'Tonight' },
  { path: 'history', component: History, canActivate: [systemGuard], title: 'History' },
  // The charge plan and forecast pages were merged into Tonight; keep old bookmarks working.
  { path: 'plan', redirectTo: 'tonight' },
  { path: 'forecast', redirectTo: 'tonight' },
  { path: 'inverter', component: Control, canActivate: [systemGuard], title: 'Inverter control' },
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
