import { Component, OnInit, inject, signal } from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { errorMessage } from '../../core/errors';
import { SolarArray, SystemConfig, TariffPeriod } from '../../core/models';
import { SystemService } from '../../core/system.service';
import { TariffEditor, patchTariff, tariffForm, tariffValue } from './tariff-editor';

function localIsoDate(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

const COMPASS = [
  { label: 'North', degrees: 0 },
  { label: 'North-east', degrees: 45 },
  { label: 'East', degrees: 90 },
  { label: 'South-east', degrees: 135 },
  { label: 'South', degrees: 180 },
  { label: 'South-west', degrees: 225 },
  { label: 'West', degrees: 270 },
  { label: 'North-west', degrees: 315 },
];

@Component({
  selector: 'app-system-setup',
  imports: [ReactiveFormsModule, RouterLink, TariffEditor],
  templateUrl: './system-setup.html',
})
export class SystemSetup implements OnInit {
  private readonly fb = inject(NonNullableFormBuilder);
  private readonly systems = inject(SystemService);
  private readonly router = inject(Router);

  readonly compass = COMPASS;
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly locating = signal(false);
  readonly isNew = signal(true);
  readonly tariffHistory = signal<TariffPeriod[]>([]);
  readonly today = localIsoDate(new Date());

  readonly form = this.fb.group({
    name: ['My system', Validators.required],
    latitude: [51.5, [Validators.required, Validators.min(-90), Validators.max(90)]],
    longitude: [-0.12, [Validators.required, Validators.min(-180), Validators.max(180)]],
    timezone: [Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/London'],
    planning_mode: ['maximise_own_usage' as SystemConfig['planning_mode']],
    data_source: ['none' as SystemConfig['data_source']],
    battery: this.fb.group({
      capacity_kwh: [10, [Validators.required, Validators.min(0.1)]],
      min_soc_percent: [10, [Validators.required, Validators.min(0), Validators.max(99)]],
      max_charge_kw: [5, [Validators.required, Validators.min(0.1)]],
      max_discharge_kw: [5, [Validators.required, Validators.min(0.1)]],
      round_trip_efficiency_percent: [
        90,
        [Validators.required, Validators.min(51), Validators.max(100)],
      ],
      wear_pence_per_cycle: [0, [Validators.required, Validators.min(0), Validators.max(1000)]],
    }),
    tariff: tariffForm(this.fb),
    tariff_effective_from: [this.today],
    arrays: this.fb.array([this.arrayGroup()]),
  });

  /** "7.5–27.5p" for what a past tariff charged to import. */
  importRange(period: TariffPeriod): string {
    const rates = period.bands.filter((b) => b.direction === 'import').map((b) => b.rate);
    const low = Math.min(...rates);
    const high = Math.max(...rates);
    return low === high ? `${low}p` : `${low}–${high}p`;
  }

  exportRange(period: TariffPeriod): string {
    const rates = period.bands.filter((b) => b.direction === 'export').map((b) => b.rate);
    const low = Math.min(...rates);
    const high = Math.max(...rates);
    return low === high ? `${low}p` : `${low}–${high}p`;
  }

  ngOnInit(): void {
    this.systems.get().subscribe({
      next: (system) => {
        this.isNew.set(false);
        this.form.controls.arrays.clear();
        system.arrays.forEach((a) => this.form.controls.arrays.push(this.arrayGroup(a)));
        // The effective date stays at today: it's for the next change, not stored on the system.
        const { tariff, tariff_effective_from: _unused, ...rest } = system;
        this.form.patchValue(rest);
        patchTariff(this.fb, this.form.controls.tariff, tariff);
        this.systems.tariffHistory().subscribe({
          next: (periods) => this.tariffHistory.set(periods),
          // The form still works without the list.
          error: () => undefined,
        });
      },
      // 404 means no system yet; keep the defaults.
      error: () => undefined,
    });
  }

  addArray(): void {
    this.form.controls.arrays.push(
      this.arrayGroup({ name: `Array ${this.form.controls.arrays.length + 1}` }),
    );
  }

  removeArray(index: number): void {
    this.form.controls.arrays.removeAt(index);
  }

  useMyLocation(): void {
    if (!navigator.geolocation) {
      this.error.set('Your browser cannot share its location. Enter it by hand.');
      return;
    }
    this.locating.set(true);
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        this.form.patchValue({
          latitude: Math.round(coords.latitude * 1000) / 1000,
          longitude: Math.round(coords.longitude * 1000) / 1000,
        });
        this.locating.set(false);
      },
      () => {
        this.error.set('Could not get your location. Enter it by hand.');
        this.locating.set(false);
      },
    );
  }

  submit(): void {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      this.error.set('Some fields need fixing before you can save.');
      return;
    }
    this.busy.set(true);
    this.error.set(null);
    const system = { ...this.form.getRawValue(), tariff: tariffValue(this.form.controls.tariff) };
    this.systems.save(system).subscribe({
      next: () => this.router.navigateByUrl('/'),
      error: (err) => {
        this.error.set(errorMessage(err));
        this.busy.set(false);
      },
    });
  }

  private arrayGroup(a: Partial<SolarArray> = {}) {
    return this.fb.group({
      name: [a.name ?? 'Main roof', Validators.required],
      kwp: [a.kwp ?? 4, [Validators.required, Validators.min(0.1)]],
      tilt_degrees: [
        a.tilt_degrees ?? 35,
        [Validators.required, Validators.min(0), Validators.max(90)],
      ],
      azimuth_degrees: [
        a.azimuth_degrees ?? 180,
        [Validators.required, Validators.min(0), Validators.max(359)],
      ],
    });
  }
}
