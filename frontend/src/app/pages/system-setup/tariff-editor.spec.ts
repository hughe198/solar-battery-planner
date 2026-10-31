import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { NonNullableFormBuilder } from '@angular/forms';
import { TariffPreset } from '../../core/models';
import { TariffEditor, tariffForm, tariffValue } from './tariff-editor';

const FLUX: TariffPreset = {
  id: 'octopus_flux',
  name: 'Octopus Flux',
  description: 'Cheap 02:00 to 05:00.',
  standing_charge_pence_per_day: 53,
  bands: [
    { direction: 'import', days: 'all', start: '02:00:00', end: '05:00:00', rate: 17 },
    { direction: 'import', days: 'all', start: '05:00:00', end: '02:00:00', rate: 28 },
    { direction: 'export', days: 'all', start: '02:00:00', end: '05:00:00', rate: 6 },
    { direction: 'export', days: 'all', start: '05:00:00', end: '02:00:00', rate: 16.5 },
  ],
};

describe('TariffEditor', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TariffEditor],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render() {
    const form = tariffForm(TestBed.inject(NonNullableFormBuilder));
    const fixture = TestBed.createComponent(TariffEditor);
    fixture.componentRef.setInput('form', form);
    fixture.detectChanges();
    http.expectOne('/api/system/tariff-presets').flush([FLUX]);
    fixture.detectChanges();
    return { fixture, form, el: fixture.nativeElement as HTMLElement };
  }

  it('starts with a valid two-rate tariff', () => {
    const { form, el } = render();
    expect(form.valid).toBe(true);
    expect(el.querySelectorAll('.band').length).toBe(3);
    expect(el.querySelector('.field-error')).toBeNull();
  });

  it('applies a preset with any number of rates', () => {
    const { fixture, form, el } = render();
    fixture.componentInstance.chosenPreset.set('octopus_flux');
    fixture.componentInstance.applyPreset();
    fixture.detectChanges();
    expect(el.querySelectorAll('.band').length).toBe(4);
    const value = tariffValue(form);
    expect(value.preset).toBe('octopus_flux');
    expect(value.bands[0]).toEqual({
      direction: 'import',
      days: 'all',
      start: '02:00',
      end: '05:00',
      rate: 17,
    });
    expect(form.valid).toBe(true);
  });

  it('explains gaps and blocks saving', () => {
    const { fixture, form, el } = render();
    fixture.componentInstance.removeBand('import', 0);
    fixture.detectChanges();
    expect(form.valid).toBe(false);
    expect(el.querySelector('.field-error')?.textContent).toContain(
      'Import has no rate for 23:30–05:30',
    );
    expect(el.querySelectorAll('.cells .gap').length).toBe(12);
  });

  it('adds a rate that starts where the last one ends', () => {
    const { fixture, form } = render();
    fixture.componentInstance.addBand('export');
    expect(form.controls.export.at(1).getRawValue().start).toBe('00:00');
    fixture.componentInstance.addBand('import');
    expect(form.controls.import.at(2).getRawValue().start).toBe('23:30');
  });
});
