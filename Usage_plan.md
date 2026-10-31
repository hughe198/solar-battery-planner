# Plan: Expected usage

Status: planned, not started.

The user marks a day as Away, Low, Normal or High on the Forecast page, and the app adjusts that
day's usage forecast. The plan, battery line and statement all update from it. For example, when
they're away the forecast can use the home's base load.

## What the data suggests

From 90 days of one home's data, 2 August to 30 October 2026:

| Measure | kWh per day |
| --- | --- |
| Base load (typical 00:30–05:00 usage, scaled to a full day) | 4.2 |
| Lowest day | 3.9 |
| Quietest 10% of days | 5.0 or less |
| Median day | 7.0 |
| Busiest 10% of days | 9.5 or more |
| Highest day | 12.3 |

The base load is a realistic figure for time away. A typical 7.4 kWh day marked Away falls to about
4.2 kWh, leaving roughly 3 kWh more in a 10 kWh battery at the end of the day.

## The options

| Option | Effect on the forecast | Based on |
| --- | --- | --- |
| **Normal** (default) | None | Current model |
| **Away** | Each half hour capped at the base load, so the daily shape goes almost flat | Typical overnight usage over the last 28 days, so it follows the seasons |
| **Low** | Scaled down, about ×0.7 for this home | The quietest 10% of days against the median |
| **High** | Scaled up, about ×1.35 for this home | The busiest 10% of days against the median |

With under 14 days of history, use fixed values: Away at half the forecast, Low at ×0.7 and High at
×1.35.

## How it fits the code

- **One central change:** apply the adjustment in `HistoryForecaster.forecast`
  (`backend/app/forecasting/service.py`), right after the prediction. The Forecast page, plan,
  lead-in and inverter preview then all pick it up.
- **Per half hour, by local date:** a charge cycle can run from 02:00 to 02:00, so marking Sunday as
  Away leaves Monday 00:00–02:00 as normal.
- **Storage:** a small new table with one row per system and day, for non-Normal days only. It's
  created at startup by `create_all`, like `past_tariffs`.
- **Likely range:** Away gets a narrow range, because an empty house is predictable. Otherwise the
  planner's "dull day" still assumes high usage (`scenarios` in `planner/optimiser.py` scales from
  the day's range) and might recommend charging that isn't needed.

## Protecting what the model learns

The usage model learns from all history, so a week away would drag down normal forecasts for weeks.
Days marked Away, Low or High must be **left out of usage training and accuracy checks** (`fit` and
`backtest` in `forecasting/service.py`). They still count for solar, because the panels don't depend
on whether anyone's home.

So marks are kept after the day has passed, and it should be possible to mark past days, such as a
holiday already in the history.

## On screen

- **Forecast page:** a four-button selector under the day arrows, for today to 7 days ahead. Changing
  it saves straight away and redraws the chart.
- **Usage tile:** shows the option in use, such as "4.2 kWh, away".
- **Statement:** adds the reason, for example: "You've marked Sunday as away, so usage is based on
  your 4.2 kWh base load."
- **Look back calendar:** a small marker on those days, so a cheap day is explained.

## Build order

1. **Backend:** table, API (`GET` and `PUT /api/usage-expectations`), and the calculation of base
   load and the Low and High factors.
2. **Forecast:** apply the adjustment and narrower ranges, and exclude marked days from training.
   Test that an Away day changes the plan, that a cycle crossing midnight is handled correctly, and
   that normal forecasts stay unaffected by a marked week.
3. **Forecast page:** selector, tile and statement.
4. **Look back:** calendar markers and marking past days.

## Decisions to confirm before starting

These are recommendations the user hasn't confirmed yet:

1. **Options:** four, as above. Away is the main case, and High and Low are cheap to add alongside it.
2. **Long holidays:** add a "from–to" range for Away in step 3. The day arrows only reach 7 days
   ahead, and clicking through each day is tedious. Marks beyond 7 days are stored and take effect
   when those days come into range.
3. **Heating:** leave it out of Away for now, because this home's usage doesn't look heating-driven
   so far. A heat pump or frost protection would add usage that base load misses, so check this as
   winter arrives.
4. **Specific extra loads:** something like "charging the car, +20 kWh, overnight" is a different
   feature, so leave it for later. High won't handle it well, because it scales the whole day evenly.
5. **Auto-apply:** marks apply to it too, with no extra setting. The existing preview check refuses a
   stale apply if a mark changes after previewing.
