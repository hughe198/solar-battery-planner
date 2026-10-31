import { DecimalPipe } from '@angular/common';
import { Component, DestroyRef, OnInit, inject, input, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import {
  AbstractControl,
  FormArray,
  FormControl,
  FormGroup,
  NonNullableFormBuilder,
  ReactiveFormsModule,
  ValidationErrors,
  Validators,
} from '@angular/forms';
import { RateDays, RateDirection, Tariff, TariffBand, TariffPreset } from '../../core/models';
import { SystemService } from '../../core/system.service';
import { BandTimes, coverageProblems, slotLabel, slotRates } from '../../core/tariff';

type BandForm = FormGroup<{
  days: FormControl<RateDays>;
  start: FormControl<string>;
  end: FormControl<string>;
  rate: FormControl<number>;
}>;

export type TariffForm = FormGroup<{
  name: FormControl<string>;
  preset: FormControl<string | null>;
  standing_charge_pence_per_day: FormControl<number>;
  import: FormArray<BandForm>;
  export: FormArray<BandForm>;
}>;

const DIRECTIONS: { key: RateDirection; label: string }[] = [
  { key: 'import', label: 'Import rates (what you pay)' },
  { key: 'export', label: 'Export rates (what you are paid)' },
];

interface TimelineRow {
  label: string;
  cells: { title: string; shade: number | null }[];
}

function bandGroup(fb: NonNullableFormBuilder, b: Partial<BandTimes> = {}): BandForm {
  return fb.group({
    days: [b.days ?? ('all' as RateDays)],
    start: [(b.start ?? '00:00').slice(0, 5), Validators.required],
    end: [(b.end ?? '00:00').slice(0, 5), Validators.required],
    rate: [b.rate ?? 0, [Validators.required, Validators.min(-100), Validators.max(200)]],
  });
}

function completeTariff(control: AbstractControl): ValidationErrors | null {
  const value = (control as TariffForm).getRawValue();
  const problems = DIRECTIONS.flatMap((d) => coverageProblems(value[d.key], d.key));
  return problems.length ? { coverage: problems } : null;
}

const DEFAULT_TARIFF: Tariff = {
  name: 'My tariff',
  preset: null,
  standing_charge_pence_per_day: 53,
  bands: [
    { direction: 'import', days: 'all', start: '23:30', end: '05:30', rate: 7.5 },
    { direction: 'import', days: 'all', start: '05:30', end: '23:30', rate: 27 },
    { direction: 'export', days: 'all', start: '00:00', end: '00:00', rate: 15 },
  ],
};

export function tariffForm(
  fb: NonNullableFormBuilder,
  tariff: Tariff = DEFAULT_TARIFF,
): TariffForm {
  const group: TariffForm = fb.group(
    {
      name: [tariff.name, Validators.required],
      preset: new FormControl<string | null>(tariff.preset),
      standing_charge_pence_per_day: [
        tariff.standing_charge_pence_per_day,
        [Validators.required, Validators.min(0)],
      ],
      import: fb.array<BandForm>([]),
      export: fb.array<BandForm>([]),
    },
    { validators: completeTariff },
  );
  setBands(fb, group, tariff.bands);
  return group;
}

export function patchTariff(fb: NonNullableFormBuilder, group: TariffForm, tariff: Tariff): void {
  group.patchValue({
    name: tariff.name,
    preset: tariff.preset,
    standing_charge_pence_per_day: tariff.standing_charge_pence_per_day,
  });
  setBands(fb, group, tariff.bands);
}

function setBands(fb: NonNullableFormBuilder, group: TariffForm, bands: TariffBand[]): void {
  for (const d of DIRECTIONS) {
    const array = group.controls[d.key];
    array.clear({ emitEvent: false });
    bands.filter((b) => b.direction === d.key).forEach((b) => array.push(bandGroup(fb, b)));
  }
  group.updateValueAndValidity();
}

/** The API shape: one band list with a direction on each band. */
export function tariffValue(group: TariffForm): Tariff {
  const { import: imports, export: exports, ...rest } = group.getRawValue();
  return {
    ...rest,
    bands: [
      ...imports.map((b) => ({ ...b, direction: 'import' as const })),
      ...exports.map((b) => ({ ...b, direction: 'export' as const })),
    ],
  };
}

@Component({
  selector: 'app-tariff-editor',
  imports: [ReactiveFormsModule, DecimalPipe],
  templateUrl: './tariff-editor.html',
})
export class TariffEditor implements OnInit {
  private readonly fb = inject(NonNullableFormBuilder);
  private readonly systems = inject(SystemService);
  private readonly destroyRef = inject(DestroyRef);

  readonly form = input.required<TariffForm>();
  readonly directions = DIRECTIONS;
  readonly presets = signal<TariffPreset[]>([]);
  readonly chosenPreset = signal('');
  readonly timelines = signal<Record<RateDirection, TimelineRow[]>>({ import: [], export: [] });
  readonly problems = signal<string[]>([]);
  readonly hours = [0, 6, 12, 18, 24];

  ngOnInit(): void {
    this.systems.tariffPresets().subscribe({
      next: (presets) => this.presets.set(presets),
      error: () => undefined,
    });
    this.refresh();
    this.form()
      .valueChanges.pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.refresh());
  }

  bands(direction: RateDirection): FormArray<BandForm> {
    return this.form().controls[direction];
  }

  presetDescription(): string {
    return this.presets().find((p) => p.id === this.chosenPreset())?.description ?? '';
  }

  applyPreset(): void {
    const preset = this.presets().find((p) => p.id === this.chosenPreset());
    if (!preset) return;
    patchTariff(this.fb, this.form(), {
      name: preset.name,
      preset: preset.id,
      standing_charge_pence_per_day: preset.standing_charge_pence_per_day,
      bands: preset.bands,
    });
    this.form().markAsDirty();
  }

  /** A new band picks up where the last one ends. */
  addBand(direction: RateDirection): void {
    const array = this.bands(direction);
    const last = array.at(array.length - 1)?.getRawValue();
    array.push(bandGroup(this.fb, { start: last?.end ?? '00:00', end: '00:00', rate: last?.rate }));
  }

  removeBand(direction: RateDirection, index: number): void {
    this.bands(direction).removeAt(index);
  }

  private refresh(): void {
    const value = this.form().getRawValue();
    this.problems.set(DIRECTIONS.flatMap((d) => coverageProblems(value[d.key], d.key)));
    this.timelines.set({ import: timeline(value.import), export: timeline(value.export) });
  }
}

function timeline(bands: BandTimes[]): TimelineRow[] {
  const split = bands.some((b) => b.days !== 'all');
  const kinds = split
    ? [
        { label: 'Weekdays', weekend: false },
        { label: 'Weekends', weekend: true },
      ]
    : [{ label: 'Every day', weekend: false }];
  const rows = kinds.map((k) => ({ label: k.label, rates: slotRates(bands, k.weekend) }));
  const all = rows.flatMap((r) => r.rates).filter((r): r is number => r !== null);
  const min = Math.min(...all);
  const max = Math.max(...all);
  return rows.map((r) => ({
    label: r.label,
    cells: r.rates.map((rate, s) => ({
      title: `${slotLabel(s)}–${slotLabel(s + 1)}: ${rate === null ? 'no rate' : `${rate}p`}`,
      // 0.15 for the cheapest to 1 for the dearest.
      shade: rate === null ? null : max > min ? 0.15 + (0.85 * (rate - min)) / (max - min) : 0.5,
    })),
  }));
}
