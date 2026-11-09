import { Component, computed, inject, input, output, signal } from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive } from '@angular/router';
import { filter, map } from 'rxjs';

/** The pages behind More: set up once, then visited rarely. */
export const MORE_PAGES = [
  { path: '/automation', label: 'Automation', hint: 'Send plans to the inverter' },
  { path: '/setup', label: 'System', hint: 'Battery, panels and tariff' },
  { path: '/data', label: 'Data', hint: 'Fox ESS and imports' },
] as const;

/**
 * The main navigation: Home, Tonight, History and More. On a phone it's a bar along the bottom of
 * the screen and More opens a sheet from below; on a wider screen it sits in the header and More
 * opens a menu under it.
 */
@Component({
  selector: 'app-tab-bar',
  imports: [RouterLink, RouterLinkActive],
  host: { '(document:keydown.escape)': 'close()' },
  styleUrl: './tab-bar.scss',
  template: `
    <nav class="tab-bar" aria-label="Main">
      <a
        class="tab"
        routerLink="/"
        routerLinkActive="active"
        [routerLinkActiveOptions]="{ exact: true }"
        ariaCurrentWhenActive="page"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 11 12 4l9 7v9h-6v-6H9v6H3z" /></svg>
        <span>Home</span>
      </a>
      <a class="tab" routerLink="/tonight" routerLinkActive="active" ariaCurrentWhenActive="page">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />
        </svg>
        <span>Tonight</span>
      </a>
      <a class="tab" routerLink="/history" routerLinkActive="active" ariaCurrentWhenActive="page">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 20V10M10 20V4M16 20v-8M22 20H2" />
        </svg>
        <span>History</span>
      </a>
      <button
        type="button"
        class="tab"
        [class.active]="onMorePage()"
        [attr.aria-expanded]="open()"
        aria-controls="more-menu"
        (click)="toggle()"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="5" cy="12" r="1.6" />
          <circle cx="12" cy="12" r="1.6" />
          <circle cx="19" cy="12" r="1.6" />
        </svg>
        <span>More</span>
      </button>
    </nav>

    @if (open()) {
      <div class="more-backdrop" (click)="close()"></div>
      <div id="more-menu" class="more-menu">
        <ul>
          @for (page of pages; track page.path) {
            <li>
              <a
                [routerLink]="page.path"
                routerLinkActive="active"
                ariaCurrentWhenActive="page"
                (click)="close()"
              >
                <span class="label">{{ page.label }}</span>
                <span class="hint">{{ page.hint }}</span>
              </a>
            </li>
          }
          <li class="account">
            <span class="hint">{{ email() }}</span>
            <button type="button" class="secondary" (click)="close(); signOut.emit()">
              Sign out
            </button>
          </li>
        </ul>
      </div>
    }
  `,
})
export class TabBar {
  private readonly router = inject(Router);

  /** The signed-in user's email, shown beside Sign out. */
  readonly email = input<string>('');
  readonly signOut = output<void>();

  protected readonly pages = MORE_PAGES;
  protected readonly open = signal(false);

  private readonly url = toSignal(
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
      map((e) => e.urlAfterRedirects),
    ),
    { initialValue: this.router.url },
  );

  /** More is highlighted while one of its pages is showing. */
  protected readonly onMorePage = computed(() =>
    MORE_PAGES.some((p) => this.url().startsWith(p.path)),
  );

  constructor() {
    // Any navigation, including the browser's back button, closes the menu.
    this.router.events
      .pipe(
        filter((e) => e instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe(() => this.open.set(false));
  }

  toggle(): void {
    this.open.update((o) => !o);
  }

  close(): void {
    this.open.set(false);
  }
}
