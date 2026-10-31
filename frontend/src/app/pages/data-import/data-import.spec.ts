import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { ImportStatus } from '../../core/models';
import { DataImport } from './data-import';

const EMPTY: ImportStatus = {
  fox_ess: null,
  usage: { count: 0, first: null, last: null },
  weather: { count: 0, first: null, last: null },
  matched_intervals: 0,
  latest_job: null,
};

describe('DataImport', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [DataImport],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  function render(status: ImportStatus) {
    const fixture = TestBed.createComponent(DataImport);
    fixture.detectChanges();
    http.expectOne('/api/import/status').flush(status);
    fixture.detectChanges();
    return fixture;
  }

  it('explains how data is used and how to delete it', () => {
    const el = render(EMPTY).nativeElement as HTMLElement;
    const privacy = el.querySelector('.privacy')?.textContent ?? '';
    expect(privacy).toContain('not shared, sold or used for anything else');
    expect(privacy).toContain('delete');
    // The key is read-only unless the user turns inverter control on.
    expect(privacy).toContain('off unless you choose it');
    expect(privacy).toContain('write');
    expect(el.textContent).toContain('Delete imported data');
    expect(el.textContent).toContain('Delete account');
    expect(el.textContent).toContain('User Profile → API Management');
  });

  it('warns how many Fox API calls an import will use', () => {
    const fixture = render({
      ...EMPTY,
      fox_ess: { device_sn: 'INV1', api_key_hint: '1234', last_synced_at: null },
    });
    fixture.componentInstance.syncForm.setValue({
      start_date: '2024-06-01',
      end_date: '2024-06-30',
    });
    fixture.detectChanges();
    const usage = (fixture.nativeElement as HTMLElement).querySelector('.api-usage')?.textContent;
    expect(usage).toContain('up to 30 of the');
    expect(usage).toContain('1,440');
    expect(usage).toContain('shared with any other app');
  });

  it('deletes imported data after confirming', () => {
    const fixture = render(EMPTY);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    fixture.componentInstance.deleteData();
    const req = http.expectOne('/api/import/data');
    expect(req.request.method).toBe('DELETE');
    req.flush(null);
    http.expectOne('/api/import/status').flush(EMPTY);
    expect(fixture.componentInstance.notice()).toContain('deleted');
    http.verify();
  });
});
