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
  InverterControl,
  SchedulePeriod,
} from '../../core/models';
import { MODE_LABELS } from '../../core/plan.service';
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
};

@Component({
  selector: 'app-control',
  imports: [DatePipe, DecimalPipe, JsonPipe, RouterLink],
  templateUrl: './control.html',
})
export class Control implements OnInit {
  private readonly control = inject(ControlService);
  readonly system = toSignal(inject(SystemService).get());

  readonly status = signal<InverterControl | null>(null);
  readonly proposal = signal<ControlProposal | null>(null);
  readonly last = signal<ControlAction | null>(null);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);
  readonly accepted = signal(false);
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

  ngOnInit(): void {
    this.refresh();
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

  loadPreview(): void {
    this.run(this.control.preview(), (p) => this.proposal.set(p));
  }

  apply(p: ControlProposal): void {
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

  isAdded(period: SchedulePeriod, p: ControlProposal): boolean {
    return p.added.some((a) => a.start === period.start && a.end === period.end);
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
