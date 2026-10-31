import { DecimalPipe } from '@angular/common';
import { Component, computed, input } from '@angular/core';
import { HistoryDay } from '../core/models';

const WIDTH = 720;
const HEIGHT = 220;
const PAD = { left: 52, right: 8, top: 18, bottom: 24 };
const PLOT_HEIGHT = HEIGHT - PAD.top - PAD.bottom;

/**
 * Daily self-sufficiency (bars) beside the battery's lowest and highest level that day (a
 * range). Both are percentages, so they share one axis honestly. A day whose battery never
 * got near its reserve but still drew from the grid points at charge that wasn't needed.
 */
@Component({
  selector: 'app-week-chart',
  template: `
    <svg
      class="week-chart"
      [attr.viewBox]="'0 0 ' + width + ' ' + height"
      role="img"
      aria-label="Daily self-sufficiency and battery range for the last week"
    >
      @for (pct of [0, 50, 100]; track pct) {
        <line
          class="grid"
          [attr.x1]="pad.left"
          [attr.x2]="width - pad.right"
          [attr.y1]="y(pct)"
          [attr.y2]="y(pct)"
        />
        <text class="axis" [attr.x]="pad.left - 6" [attr.y]="y(pct) + 4" text-anchor="end">
          {{ pct }}%
        </text>
      }
      @for (d of days(); track d.day) {
        <g>
          <title>{{ d.title }}</title>
          @if (d.ss !== null) {
            <rect
              class="ss"
              [attr.x]="d.barX"
              [attr.y]="y(d.ss)"
              [attr.width]="d.barWidth"
              [attr.height]="y(0) - y(d.ss)"
              rx="4"
            />
            <text
              class="value"
              [attr.x]="d.barX + d.barWidth / 2"
              [attr.y]="y(d.ss) - 4"
              text-anchor="middle"
            >
              {{ d.ss | number: '1.0-0' }}%
            </text>
          }
          @if (d.socMin !== null && d.socMax !== null) {
            <line
              class="soc-range"
              [attr.x1]="d.rangeX"
              [attr.x2]="d.rangeX"
              [attr.y1]="y(d.socMin)"
              [attr.y2]="y(d.socMax)"
            />
            <circle class="soc-end" [attr.cx]="d.rangeX" [attr.cy]="y(d.socMin)" r="4" />
            <circle class="soc-end" [attr.cx]="d.rangeX" [attr.cy]="y(d.socMax)" r="4" />
          }
          <text class="axis" [attr.x]="d.centre" [attr.y]="height - 6" text-anchor="middle">
            {{ d.label }}
          </text>
          <rect
            class="hit"
            [attr.x]="d.slotX"
            [attr.y]="pad.top"
            [attr.width]="d.slotWidth"
            [attr.height]="height - pad.top - pad.bottom"
          />
        </g>
      }
    </svg>
    <p class="legend muted">
      <span class="key ss"></span> Self-sufficiency: share of the day's use not drawn from the grid
      (grid charging counts as drawn). <span class="key battery"></span> Battery lowest to highest
      that day. Hover a day for details.
    </p>
  `,
  imports: [DecimalPipe],
})
export class WeekChart {
  readonly history = input.required<HistoryDay[]>();
  readonly width = WIDTH;
  readonly height = HEIGHT;
  readonly pad = PAD;

  readonly days = computed(() => {
    const items = this.history();
    const slotWidth = (WIDTH - PAD.left - PAD.right) / Math.max(1, items.length);
    return items.map((d, i) => {
      const slotX = PAD.left + i * slotWidth;
      const barWidth = Math.min(48, slotWidth * 0.45);
      const centre = slotX + slotWidth / 2;
      const label = new Intl.DateTimeFormat('en-GB', {
        weekday: 'short',
        day: 'numeric',
        timeZone: 'UTC',
      }).format(new Date(`${d.day}T00:00:00Z`));
      const soc =
        d.min_soc_percent === null
          ? ''
          : `, battery ${Math.round(d.min_soc_percent)}–${Math.round(d.max_soc_percent ?? 0)}%`;
      return {
        day: d.day,
        label,
        slotX,
        slotWidth,
        centre,
        barX: centre - barWidth - 3,
        barWidth,
        rangeX: centre + 10,
        ss: d.self_sufficiency_percent,
        socMin: d.min_soc_percent,
        socMax: d.max_soc_percent,
        title:
          `${label}: ${d.self_sufficiency_percent ?? '–'}% self-sufficient${soc}. ` +
          `Used ${d.load_kwh.toFixed(1)} kWh, solar ${d.solar_kwh.toFixed(1)}, ` +
          `imported ${d.import_kwh.toFixed(1)}, exported ${d.export_kwh.toFixed(1)}.`,
      };
    });
  });

  y(percent: number): number {
    return PAD.top + PLOT_HEIGHT - (percent / 100) * PLOT_HEIGHT;
  }
}
