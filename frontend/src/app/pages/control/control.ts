import { DatePipe, DecimalPipe, JsonPipe } from '@angular/common';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { RouterLink } from '@angular/router';
import { Observable } from 'rxjs';
import { ControlService } from '../../core/control.service';
import { errorMessage } from '../../core/errors';
import {
  ControlAction,
  ControlActionKind,
  ControlProposal,
  ControlStatus,
  DryRun,
  DryRunNight,
  DryRunVerdict,
  InverterControl,
  PlanLock,
  PlanLockMode,
  SchedulePeriod,
} from '../../core/models';
import { MODE_LABELS, PlanService } from '../../core/plan.service';
import { SystemService } from '../../core/system.service';

/** What each test step does, shown before the user runs it. */
export const STEP_DETAILS: Record<number, string> = {
  1: 'Reads your schedule from Fox ESS and saves it as the starting point. Nothing is written.',
  2: 'Writes your schedule back exactly as it was read, then reads it again. Nothing should change.',
  3: 'Adds a 10-minute force charge at 500 W, at least 3 hours away so it never runs, and reads it back. Check it appears in the Fox app.',
  4: 'Removes the test period and checks your schedule matches step 1 exactly. Passing unlocks applying the plan.',
  5: 'Optional: adds a 10-minute force charge at 500 W starting in about 5 minutes. Watch the battery charge in the Fox app.',
  6: 'Removes the live test period and checks your schedule matches step 1 again.',
};

const STATUS_LABELS: Record<ControlStatus, string> = {
  ok: 'Done',
  mismatch: 'Written, but read back differently',
  failed: 'Failed',
  skipped: 'Nothing to do',
};

const KIND_LABELS: Record<ControlActionKind, string> = {
  opt_in: 'Turned on',
  opt_out: 'Turned off',
  apply: 'Apply plan',
  clear: 'Remove periods',
  restore: 'Restore',
  test: 'Write test',
  dry_run: 'Dry run',
};

@Component({
  selector: 'app-control',
  imports: [DatePipe, DecimalPipe, JsonPipe, RouterLink],
  templateUrl: './control.html',
})
export class Control implements OnInit {
  private readonly control = inject(ControlService);
  private readonly plans = inject(PlanService);
  readonly system = toSignal(inject(SystemService).get());

  readonly status = signal<InverterControl | null>(null);
  readonly proposal = signal<ControlProposal | null>(null);
  readonly dryRun = signal<DryRun | null>(null);
  /** Charge nights with a dry run that passed, out of those needed. */
  readonly nightsPassed = computed(
    () => (this.status()?.dry_run_nights ?? []).filter((n) => n.passed).length,
  );
  readonly checkLabels = { pass: 'Pass', warn: 'Note', fail: 'FAIL' } as const;
  /** Oldest first, one slot per night needed (more once extra nights have been run). */
  readonly nightSlots = computed<(DryRunNight | null)[]>(() => {
    const s = this.status();
    if (!s) return [];
    const run = [...(s.dry_run_nights ?? [])].reverse();
    const slots = Math.max(s.dry_run_nights_needed ?? 3, run.length);
    return Array.from({ length: slots }, (_, i) => run[i] ?? null);
  });
  readonly last = signal<ControlAction | null>(null);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly accepted = signal(false);
  /** The user has read the calls in the current preview and authorised them. */
  readonly authorised = signal(false);
  readonly writeCount = computed(() => this.proposal()?.calls.filter((c) => c.writes).length ?? 0);
  readonly modeLabels = MODE_LABELS;
  readonly stepDetails = STEP_DETAILS;
  readonly statusLabels = STATUS_LABELS;
  readonly kindLabels = KIND_LABELS;

  /** The step to run next; step 6 is offered whenever a live test period may be left. */
  readonly nextStep = computed(() => {
    const s = this.status();
    if (!s) return null;
    if (s.test_level >= 6) return null;
    if (s.test_level === 5) return 6;
    return s.test_level + 1;
  });
  readonly testDone = computed(() => (this.status()?.test_level ?? 0) >= 4);

  /** When the plan locks, as saved, and the choice being edited. */
  readonly lock = signal<PlanLock | null>(null);
  readonly lockMode = signal<PlanLockMode>('before_window');
  readonly lockTime = signal('21:00');
  readonly lockChanged = computed(() => {
    const l = this.lock();
    if (!l) return false;
    if (this.lockMode() !== l.mode) return true;
    return l.mode === 'set_time' && this.lockTime() !== l.time;
  });
  /** "23:20" or "21:00 Wednesday": when the next plan locks (or locked). */
  readonly lockAt = computed(() => {
    const l = this.lock();
    return l ? this.when(l.locked_at ?? l.locks_at) : '';
  });
  /** The user has authorised one send at the last minute. */
  readonly lastMinuteAuthorised = signal(false);

  ngOnInit(): void {
    this.refresh();
    this.loadLock();
  }

  private loadLock(): void {
    this.plans.lock().subscribe({
      next: (l) => this.showLock(l),
      error: () => this.lock.set(null), // no system yet: nothing to lock
    });
  }

  private showLock(l: PlanLock): void {
    this.lock.set(l);
    this.lockMode.set(l.mode);
    if (l.time) this.lockTime.set(l.time);
  }

  saveLock(): void {
    const mode = this.lockMode();
    this.run(this.plans.setLock(mode, mode === 'set_time' ? this.lockTime() : null), (l) =>
      this.showLock(l),
    );
  }

  sendLastMinute(): void {
    if (!this.lastMinuteAuthorised()) return;
    this.lastMinuteAuthorised.set(false);
    this.run(this.control.sendLastMinute(), (s) => {
      this.status.set(s);
      this.loadLock();
    });
  }

  cancelLastMinute(): void {
    this.run(this.control.cancelLastMinute(), (s) => this.status.set(s));
  }

  /** Why "send at the last minute" isn't available, or null when it is. */
  lastMinuteBlocked(s: InverterControl): string | null {
    if (s.can_send_last_minute) return null;
    if (!s.can_apply) return 'Pass the write test up to step 4 to unlock this.';
    if (!s.writes_allowed) return 'Writes to inverters are switched off on this server.';
    return (
      "This server doesn't run the automatic apply (SBP_AUTO_APPLY), which is what sends it " +
      'at the last minute. Use Authorise and send instead.'
    );
  }

  /** "23:20" today, otherwise "23:20 Wednesday", in the system's time zone. */
  private when(iso: string): string {
    const zone = this.system()?.timezone;
    const day = (t: number) =>
      new Intl.DateTimeFormat('en-CA', { dateStyle: 'short', timeZone: zone }).format(t);
    const time = this.localTime(iso);
    if (day(new Date(iso).getTime()) === day(Date.now())) return time;
    const weekday = new Intl.DateTimeFormat('en-GB', { weekday: 'long', timeZone: zone });
    return `${time} ${weekday.format(new Date(iso))}`;
  }

  refresh(): void {
    this.control.status().subscribe({
      next: (s) => this.status.set(s),
      error: (err) => this.error.set(errorMessage(err)),
    });
  }

  private run<T>(call: Observable<T>, done: (value: T) => void): void {
    this.busy.set(true);
    this.error.set(null);
    call.subscribe({
      next: (value) => {
        done(value);
        this.busy.set(false);
      },
      error: (err) => {
        this.error.set(errorMessage(err));
        this.busy.set(false);
      },
    });
  }

  private afterWrite = (action: ControlAction): void => {
    this.last.set(action);
    this.proposal.set(null);
    this.refresh();
  };

  turnOn(): void {
    this.run(this.control.update({ enabled: true, accept: true }), (s) => this.status.set(s));
  }

  turnOff(): void {
    if (!confirm('Turn inverter control off? Your inverter is left as it is now.')) return;
    this.run(this.control.update({ enabled: false }), (s) => {
      this.status.set(s);
      this.proposal.set(null);
    });
  }

  setAutoApply(on: boolean): void {
    this.run(this.control.update({ enabled: true, auto_apply: on }), (s) => this.status.set(s));
  }

  runStep(step: number): void {
    if (step === 5 && !confirm('Start a real 10-minute charge at 500 W in about 5 minutes?')) {
      return;
    }
    this.run(this.control.testStep(step), this.afterWrite);
  }

  /** Trust ladder stage 3: one read, nothing written, logged for tonight's charge. */
  runDryRun(): void {
    this.run(this.control.dryRun(), (d) => {
      this.dryRun.set(d);
      this.refresh();
    });
  }

  verdictLabel(verdict: DryRunVerdict['verdict']): string {
    return verdict.charAt(0).toUpperCase() + verdict.slice(1);
  }

  /** Reuses the calendar's colours: about right is good, either way off is fair. */
  verdictClass(verdict: DryRunVerdict['verdict']): string {
    return verdict === 'about right' ? 'rating-good' : 'rating-fair';
  }

  /** "Fri 6 Nov" for a charge night. */
  nightLabel(day: string): string {
    return new Intl.DateTimeFormat('en-GB', {
      weekday: 'short',
      day: 'numeric',
      month: 'short',
      timeZone: 'UTC',
    }).format(new Date(`${day}T00:00:00Z`));
  }

  loadPreview(): void {
    this.authorised.set(false);
    this.run(this.control.preview(), (p) => this.proposal.set(p));
  }

  apply(p: ControlProposal): void {
    if (!this.authorised()) return;
    this.authorised.set(false);
    this.run(this.control.apply(p.fingerprint), this.afterWrite);
  }

  clear(): void {
    if (!confirm("Remove the planner's charge and test periods from your inverter?")) return;
    this.run(this.control.clear(), this.afterWrite);
  }

  restore(): void {
    if (!confirm('Put back the schedule your inverter had before the planner changed it?')) return;
    this.run(this.control.restore(), this.afterWrite);
  }

  /** "Added", "Fills a gap" (a copy of Fox's all-day period, which a write drops) or "Kept". */
  changeOf(period: SchedulePeriod, p: ControlProposal): string {
    const same = (a: SchedulePeriod) => a.start === period.start && a.end === period.end;
    if (p.added.some(same)) return 'Added';
    return (p.filled ?? []).some(same) ? 'Fills a gap' : 'Kept';
  }

  /** "HH:MM" in the system's time zone. */
  localTime(iso: string | null): string {
    if (!iso) return '–';
    return new Intl.DateTimeFormat('en-GB', {
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
      timeZone: this.system()?.timezone,
    }).format(new Date(iso));
  }
}
