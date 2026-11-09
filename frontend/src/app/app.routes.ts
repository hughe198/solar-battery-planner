import { Routes } from '@angular/router';
import { authGuard, guestGuard, systemGuard } from './core/auth.guard';

// Each page loads when first visited, so the first screen on a phone arrives sooner.
export const routes: Routes = [
  {
    path: '',
    loadComponent: () => import('./pages/home/home').then((m) => m.Home),
    canActivate: [systemGuard],
    pathMatch: 'full',
    title: 'Home',
  },
  {
    path: 'tonight',
    loadComponent: () => import('./pages/tonight/tonight').then((m) => m.Tonight),
    canActivate: [systemGuard],
    title: 'Tonight',
  },
  {
    path: 'history',
    loadComponent: () => import('./pages/history/history').then((m) => m.History),
    canActivate: [systemGuard],
    title: 'History',
  },
  // The charge plan and forecast pages were merged into Tonight; keep old bookmarks working.
  { path: 'plan', redirectTo: 'tonight' },
  { path: 'forecast', redirectTo: 'tonight' },
  {
    path: 'automation',
    loadComponent: () => import('./pages/control/control').then((m) => m.Control),
    canActivate: [systemGuard],
    title: 'Automation',
  },
  // The Inverter page was renamed Automation; keep old bookmarks working.
  { path: 'inverter', redirectTo: 'automation' },
  {
    path: 'data',
    loadComponent: () => import('./pages/data-import/data-import').then((m) => m.DataImport),
    canActivate: [systemGuard],
    title: 'Your data',
  },
  {
    path: 'setup',
    loadComponent: () => import('./pages/system-setup/system-setup').then((m) => m.SystemSetup),
    canActivate: [authGuard],
    title: 'System setup',
  },
  {
    path: 'login',
    loadComponent: () => import('./pages/auth/auth-page').then((m) => m.AuthPage),
    canActivate: [guestGuard],
    data: { mode: 'login' },
    title: 'Sign in',
  },
  {
    path: 'register',
    loadComponent: () => import('./pages/auth/auth-page').then((m) => m.AuthPage),
    canActivate: [guestGuard],
    data: { mode: 'register' },
    title: 'Create account',
  },
  { path: '**', redirectTo: '' },
];
