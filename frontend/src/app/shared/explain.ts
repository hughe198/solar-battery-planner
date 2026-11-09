import { Component, computed, effect, input, signal } from '@angular/core';

/** How much of an explanation shows: none, the one sentence, or the sentence and every point. */
export type ExplainLevel = 'hidden' | 'sentence' | 'points';

const STORAGE_PREFIX = 'sbp_explain_';
let nextId = 0;

/**
 * A three-step explanation. Hidden at first; one tap shows a single sentence, the next shows the
 * detailed points, and the next hides it again. The last state is remembered per `key` on this
 * device, so a card the user likes open stays open.
 */
@Component({
  selector: 'app-explain',
  styleUrl: './explain.scss',
  template: `
    @if (hasContent()) {
      <button
        type="button"
        class="explain-toggle"
        [attr.aria-expanded]="level() !== 'hidden'"
        [attr.aria-controls]="bodyId"
        (click)="next()"
      >
        @if (label()) {
          <span class="explain-label">{{ label() }} ·</span>
        }
        {{ action() }}
        <span class="explain-chevron" aria-hidden="true" [class]="level()"></span>
      </button>
      <div [id]="bodyId" class="explain-body" aria-live="polite">
        @if (level() !== 'hidden' && sentence()) {
          <p class="explain-sentence">{{ sentence() }}</p>
        }
        @if (level() === 'points' && points().length) {
          <ul class="explain-points">
            @for (point of points(); track $index) {
              <li>{{ point }}</li>
            }
          </ul>
        }
      </div>
    }
  `,
})
export class Explain {
  /** Remembers this explanation's state on the device, e.g. 'home-tonight'. Empty: not kept. */
  readonly key = input<string>('');
  /** What it explains, shown before the action, e.g. "Weather" or "Plan". */
  readonly label = input<string>('');
  readonly sentence = input<string>('');
  readonly points = input<readonly string[]>([]);

  protected readonly bodyId = `explain-${nextId++}`;
  private readonly chosen = signal<ExplainLevel | null>(null);

  protected readonly hasContent = computed(() => !!this.sentence() || this.points().length > 0);

  /** The steps this explanation has: no sentence skips that step, no points skips the last. */
  private readonly steps = computed<ExplainLevel[]>(() => {
    const steps: ExplainLevel[] = ['hidden'];
    if (this.sentence()) steps.push('sentence');
    if (this.points().length) steps.push('points');
    return steps;
  });

  /** The level showing, kept within the steps this explanation has. */
  readonly level = computed<ExplainLevel>(() => {
    const wanted = this.chosen() ?? readStored(this.key());
    const steps = this.steps();
    if (steps.includes(wanted)) return wanted;
    // Remembered as the points but there are none here: show the sentence instead.
    return wanted === 'points' && steps.includes('sentence') ? 'sentence' : 'hidden';
  });

  /** What the button does next. */
  protected readonly action = computed(() => {
    const steps = this.steps();
    const after = steps[(steps.indexOf(this.level()) + 1) % steps.length];
    if (after === 'hidden') return 'Hide';
    return this.level() === 'hidden' ? 'Why?' : 'More detail';
  });

  constructor() {
    effect(() => {
      const key = this.key();
      const chosen = this.chosen();
      if (key && chosen) writeStored(key, chosen);
    });
  }

  next(): void {
    const steps = this.steps();
    this.chosen.set(steps[(steps.indexOf(this.level()) + 1) % steps.length]);
  }
}

function readStored(key: string): ExplainLevel {
  if (!key) return 'hidden';
  try {
    const value = localStorage.getItem(STORAGE_PREFIX + key);
    return value === 'sentence' || value === 'points' ? value : 'hidden';
  } catch {
    // Storage can be blocked (private browsing, strict settings); start hidden.
    return 'hidden';
  }
}

function writeStored(key: string, level: ExplainLevel): void {
  try {
    localStorage.setItem(STORAGE_PREFIX + key, level);
  } catch {
    // Not remembered on this device; the explanation still works.
  }
}
