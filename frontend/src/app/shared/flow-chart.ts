import { DecimalPipe } from '@angular/common';
import { Component, computed, input, signal } from '@angular/core';
import { FlowPoint } from '../core/models';

const WIDTH = 720;
const PAD = { left: 52, right: 8, top: 8 };
const SOC_HEIGHT = 90;
const GAP = 22;
const FLOW_HEIGHT = 120;
const AXIS = 20;
const HEIGHT = PAD.top + SOC_HEIGHT + GAP + FLOW_HEIGHT + AXIS;
const FLOW_TOP = PAD.top + SOC_HEIGHT + GAP;
const PLOT_WIDTH = WIDTH - PAD.left - PAD.right;

/**
 * Battery level above, grid import (up) and export (down) below, sharing one time axis, with
 * the cheap window shaded. Two panels rather than two y-scales on one plot, so neither scale
 * is read against the other.
 */
@Component({
  selector: 'app-flow-chart',
  imports: [DecimalPipe],
  template: `
    <svg
      class="flow-chart"
      [attr.viewBox]="'0 0 ' + width + ' ' + height"
      role="img"
      [attr.aria-label]="label()"
      (mouseleave)="hovered.set(null)"
    >
      @for (run of leadUpRuns(); track run.x) {
        <rect
          class="lead-up"
          [attr.x]="run.x"
          [attr.y]="pad.top"
          [attr.width]="run.width"
          [attr.height]="flowTop + flowHeight - pad.top"
        />
      }
      @for (run of cheapRuns(); track run.x) {
        <rect
          class="cheap"
          [attr.x]="run.x"
          [attr.y]="pad.top"
          [attr.width]="run.width"
          [attr.height]="flowTop + flowHeight - pad.top"
        />
      }
      @for (tick of xTicks(); track tick.x) {
        @if (tick.divider) {
          <line
            class="divider"
            [attr.x1]="tick.x"
            [attr.x2]="tick.x"
            [attr.y1]="pad.top"
            [attr.y2]="flowTop + flowHeight"
          />
        }
        <text class="axis" [attr.x]="tick.labelX" [attr.y]="height - 6" text-anchor="middle">
          {{ tick.label }}
        </text>
      }
      @for (pct of [0, 50, 100]; track pct) {
        <line
          class="grid"
          [attr.x1]="pad.left"
          [attr.x2]="width - pad.right"
          [attr.y1]="socY(pct)"
          [attr.y2]="socY(pct)"
        />
        <text class="axis" [attr.x]="pad.left - 6" [attr.y]="socY(pct) + 4" text-anchor="end">
          {{ pct }}%
        </text>
      }
      @if (bestLine(); as best) {
        <path class="soc best" [attr.d]="best" />
      }
      <path class="soc" [attr.d]="socLine()" />

      <line
        class="grid"
        [attr.x1]="pad.left"
        [attr.x2]="width - pad.right"
        [attr.y1]="zeroY()"
        [attr.y2]="zeroY()"
      />
      <text class="axis" [attr.x]="pad.left - 6" [attr.y]="flowTop + 8" text-anchor="end">
        {{ flowMax().import | number: '1.1-1' }} kWh
      </text>
      <text class="axis" [attr.x]="pad.left - 6" [attr.y]="flowTop + flowHeight" text-anchor="end">
        {{ flowMax().export | number: '1.1-1' }} kWh
      </text>
      @for (bar of bars(); track $index) {
        @if (bar.importHeight > 0) {
          <rect
            class="import"
            [attr.x]="bar.x"
            [attr.y]="zeroY() - bar.importHeight"
            [attr.width]="bar.width"
            [attr.height]="bar.importHeight"
            rx="1"
          />
        }
        @if (bar.exportHeight > 0) {
          <rect
            class="export"
            [attr.x]="bar.x"
            [attr.y]="zeroY()"
            [attr.width]="bar.width"
            [attr.height]="bar.exportHeight"
            rx="1"
          />
        }
      }

      @if (hovered() !== null) {
        <line
          class="crosshair"
          [attr.x1]="centre(hovered()!)"
          [attr.x2]="centre(hovered()!)"
          [attr.y1]="pad.top"
          [attr.y2]="flowTop + flowHeight"
        />
      }
      @for (point of points(); track point.start; let i = $index) {
        <rect
          class="hit"
          [attr.x]="slotX(i)"
          [attr.y]="pad.top"
          [attr.width]="slotWidth()"
          [attr.height]="flowTop + flowHeight - pad.top"
          (mouseenter)="hovered.set(i)"
        >
          <title>{{ describe(point) }}</title>
        </rect>
      }
    </svg>
    <p class="legend muted">
      @if (hovered() !== null) {
        <strong>{{ describe(points()[hovered()!]) }}</strong>
      } @else {
        <span class="key battery"></span> Battery
        @if (bestLine()) {
          <span class="key best"></span> With the best charge
        }
        <span class="key import"></span> Import (up) <span class="key export"></span> Export (down)
        · kWh per half hour. <span class="key cheap"></span> Cheap window.
        @if (leadUpRuns().length) {
          <span class="key lead-up"></span> Evening before, not scored.
        }
        Hover for values.
      }
    </p>
  `,
})
export class FlowChart {
  readonly points = input.required<FlowPoint[]>();
  readonly timeZone = input<string | undefined>(undefined);
  readonly label = input('Battery level and grid import and export per half hour');

  readonly hovered = signal<number | null>(null);
  readonly width = WIDTH;
  readonly height = HEIGHT;
  readonly pad = PAD;
  readonly flowTop = FLOW_TOP;
  readonly flowHeight = FLOW_HEIGHT;

  readonly slotWidth = computed(() => PLOT_WIDTH / Math.max(1, this.points().length));

  readonly flowMax = computed(() => {
    const pts = this.points();
    return {
      import: Math.max(0.1, ...pts.map((p) => p.import_kwh)),
      export: Math.max(0.1, ...pts.map((p) => p.export_kwh)),
    };
  });

  /** Import and export share one kWh scale, so the zero line sits where their maxima meet. */
  readonly zeroY = computed(() => {
    const { import: up, export: down } = this.flowMax();
    return FLOW_TOP + (up / (up + down)) * FLOW_HEIGHT;
  });

  readonly bars = computed(() => {
    const { import: up, export: down } = this.flowMax();
    const scale = FLOW_HEIGHT / (up + down);
    const slot = this.slotWidth();
    // A 2px gap between bars once they are wide enough to keep one.
    const gap = slot >= 6 ? 2 : 0;
    return this.points().map((p, i) => ({
      x: this.slotX(i) + gap / 2,
      width: Math.max(0.5, slot - gap),
      importHeight: p.import_kwh * scale,
      exportHeight: p.export_kwh * scale,
    }));
  });

  readonly socLine = computed(() => this.levelLine((p) => p.soc_percent));
  readonly bestLine = computed(() => this.levelLine((p) => p.best_soc_percent ?? null));

  private levelLine(level: (p: FlowPoint) => number | null): string {
    let path = '';
    let drawing = false;
    this.points().forEach((p, i) => {
      const value = level(p);
      if (value === null) {
        drawing = false;
        return;
      }
      path += `${drawing ? 'L' : 'M'}${this.centre(i).toFixed(1)},${this.socY(value).toFixed(1)} `;
      drawing = true;
    });
    return path.trim();
  }

  readonly cheapRuns = computed(() => this.runs((p) => p.cheap));
  readonly leadUpRuns = computed(() => this.runs((p) => !!p.lead_up));

  private runs(within: (p: FlowPoint) => boolean): { x: number; width: number }[] {
    const runs: { x: number; width: number }[] = [];
    let start: number | null = null;
    const pts = this.points();
    pts.forEach((p, i) => {
      const inside = within(p);
      if (inside && start === null) start = i;
      if ((!inside || i === pts.length - 1) && start !== null) {
        const end = inside ? i + 1 : i;
        runs.push({ x: this.slotX(start), width: (end - start) * this.slotWidth() });
        start = null;
      }
    });
    return runs;
  }

  /** Day names at local midnight for multi-day charts, otherwise every three hours. */
  readonly xTicks = computed(() => {
    const pts = this.points();
    const multiDay = pts.length > 60;
    const ticks: { x: number; labelX: number; label: string; divider: boolean }[] = [];
    pts.forEach((p, i) => {
      const time = this.format(p.start, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
      if (multiDay && time === '00:00') {
        const label = this.format(p.start, { weekday: 'short' });
        const labelX = Math.min(this.slotX(i) + 24 * this.slotWidth(), WIDTH - PAD.right - 12);
        ticks.push({ x: this.slotX(i), labelX, label, divider: true });
      } else if (!multiDay && /^(00|03|06|09|12|15|18|21):00$/.test(time)) {
        ticks.push({
          x: this.slotX(i),
          labelX: this.slotX(i),
          label: time.slice(0, 2),
          divider: false,
        });
      }
    });
    return ticks;
  });

  socY(percent: number): number {
    return PAD.top + SOC_HEIGHT - (percent / 100) * SOC_HEIGHT;
  }

  slotX(index: number): number {
    return PAD.left + index * this.slotWidth();
  }

  centre(index: number): number {
    return this.slotX(index) + this.slotWidth() / 2;
  }

  describe(p: FlowPoint): string {
    const when = this.format(p.start, {
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    });
    const best =
      p.best_soc_percent == null ? '' : ` (best charge ${Math.round(p.best_soc_percent)}%)`;
    const soc = p.soc_percent === null ? '' : ` · battery ${Math.round(p.soc_percent)}%${best}`;
    return (
      `${when}${soc} · import ${p.import_kwh.toFixed(2)} kWh · export ${p.export_kwh.toFixed(2)} kWh` +
      ` · use ${p.load_kwh.toFixed(2)} · solar ${p.solar_kwh.toFixed(2)}${p.cheap ? ' · cheap rate' : ''}`
    );
  }

  private format(iso: string, options: Intl.DateTimeFormatOptions): string {
    return new Intl.DateTimeFormat('en-GB', { ...options, timeZone: this.timeZone() }).format(
      new Date(iso),
    );
  }
}
