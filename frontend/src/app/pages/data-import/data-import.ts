import { DatePipe, DecimalPipe } from '@angular/common';
import { Component, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { Observable, Subscription, switchMap, takeWhile, timer } from 'rxjs';
import { errorMessage } from '../../core/errors';
import { ImportService } from '../../core/import.service';
import { ImportStatus } from '../../core/models';

/** Fox ESS allows this many API calls per inverter per day, shared by every app using it. */
export const FOX_DAILY_CALLS = 1440;
const MAX_SYNC_DAYS = 366;

function isoDate(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

function daysAgo(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() - days);
  return isoDate(date);
}

@Component({
  selector: 'app-data-import',
  imports: [ReactiveFormsModule, DatePipe, DecimalPipe, RouterLink],
  templateUrl: './data-import.html',
})
export class DataImport implements OnInit {
  private readonly fb = inject(NonNullableFormBuilder);
  private readonly imports = inject(ImportService);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private polling?: Subscription;

  readonly dailyCalls = FOX_DAILY_CALLS;
  readonly maxSyncDays = MAX_SYNC_DAYS;
  readonly today = isoDate(new Date());
  readonly status = signal<ImportStatus | null>(null);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly notice = signal<string | null>(null);
  readonly csvFile = signal<File | null>(null);

  readonly connectForm = this.fb.group({
    api_key: ['', [Validators.required, Validators.minLength(8)]],
    device_sn: ['', [Validators.required, Validators.minLength(4)]],
  });

  readonly syncForm = this.fb.group({
    start_date: [daysAgo(90), Validators.required],
    // Today too: the plan starts from the latest battery reading. A part day is fetched again.
    end_date: [daysAgo(0), Validators.required],
  });

  private readonly syncRange = signal(this.syncForm.getRawValue());

  /** Days in the chosen range, i.e. the most Fox API calls this import can use. */
  readonly syncDays = computed(() => {
    const { start_date, end_date } = this.syncRange();
    const days = (Date.parse(end_date) - Date.parse(start_date)) / 86_400_000 + 1;
    return Number.isFinite(days) && days > 0 ? Math.round(days) : 0;
  });

  /** Fox allows about one call a second. */
  readonly syncMinutes = computed(() => Math.max(1, Math.ceil((this.syncDays() * 1.1) / 60)));

  readonly jobActive = computed(() => {
    const job = this.status()?.latest_job;
    return job?.status === 'queued' || job?.status === 'running';
  });

  ngOnInit(): void {
    this.syncForm.valueChanges
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.syncRange.set(this.syncForm.getRawValue()));
    this.refresh();
  }

  connect(): void {
    if (this.connectForm.invalid) {
      this.connectForm.markAllAsTouched();
      return;
    }
    const { api_key, device_sn } = this.connectForm.getRawValue();
    this.run(this.imports.connectFoxEss(api_key, device_sn), (connection) => {
      this.connectForm.reset();
      const read = [
        connection.battery_min_soc_percent != null
          ? `minimum charge (${connection.battery_min_soc_percent}%)`
          : null,
        connection.battery_max_charge_kw != null
          ? `forced charge power (${connection.battery_max_charge_kw} kW)`
          : null,
      ].filter(Boolean);
      this.notice.set(
        'Fox ESS connected. ' +
          (read.length
            ? `Your battery's ${read.join(' and ')} were read from the inverter. `
            : '') +
          'Choose how much history to import.',
      );
    });
  }

  disconnect(): void {
    if (!confirm('Remove your Fox ESS API key? Data already imported is kept.')) return;
    this.run(this.imports.disconnectFoxEss(), () =>
      this.notice.set('Fox ESS disconnected and your API key deleted.'),
    );
  }

  sync(): void {
    const { start_date, end_date } = this.syncForm.getRawValue();
    this.run(this.imports.syncFoxEss(start_date, end_date), () =>
      this.notice.set('Import started. You can leave this page; it carries on in the background.'),
    );
  }

  chooseFile(event: Event): void {
    this.csvFile.set((event.target as HTMLInputElement).files?.[0] ?? null);
  }

  upload(): void {
    const file = this.csvFile();
    if (!file) return;
    this.run(this.imports.uploadCsv(file), (result) =>
      this.notice.set(
        result.message ??
          `Imported ${result.intervals_imported} half hours and matched them to weather.`,
      ),
    );
  }

  fetchWeather(): void {
    this.run(this.imports.fetchMissingWeather(), (result) =>
      this.notice.set(`Added ${result.weather_hours_added} hours of weather.`),
    );
  }

  deleteData(): void {
    const ok = confirm(
      'Delete all imported usage and weather data, your import history and your Fox ESS API key? ' +
        'Your system details are kept. This cannot be undone.',
    );
    if (!ok) return;
    this.run(this.imports.deleteData(), () => this.notice.set('All imported data deleted.'));
  }

  deleteAccount(): void {
    const ok = confirm(
      'Permanently delete your account, your system and all imported data? This cannot be undone.',
    );
    if (!ok) return;
    this.busy.set(true);
    this.imports.deleteAccount().subscribe({
      next: () => this.router.navigateByUrl('/register'),
      error: (err) => {
        this.error.set(errorMessage(err));
        this.busy.set(false);
      },
    });
  }

  private run<T>(request: Observable<T>, done: (result: T) => void): void {
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    request.subscribe({
      next: (result) => {
        done(result);
        this.busy.set(false);
        this.refresh();
      },
      error: (err) => {
        this.error.set(errorMessage(err));
        this.busy.set(false);
      },
    });
  }

  private refresh(): void {
    this.imports.status().subscribe({
      next: (status) => {
        this.status.set(status);
        if (this.jobActive()) this.poll();
      },
      error: (err) => this.error.set(errorMessage(err)),
    });
  }

  /** While an import runs, re-read the status every few seconds until it finishes. */
  private poll(): void {
    if (this.polling && !this.polling.closed) return;
    this.polling = timer(3000, 3000)
      .pipe(
        switchMap(() => this.imports.status()),
        takeWhile((status) => {
          this.status.set(status);
          return this.jobActive();
        }),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe();
  }
}
