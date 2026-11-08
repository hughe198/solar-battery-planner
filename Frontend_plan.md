# Plan: New layout and information priority

Status: draft, written 8 November 2026. The decisions at the end need confirming with the user
before any code is written.

The app is used mostly on a phone, at two times of day:

- **In the morning,** to see how last night's charge went, how full the battery is now, and what
  today should bring.
- **In the late evening,** to see the plan chosen for tonight.

The layout is rebuilt phone first around that. Each explanation shrinks to one sentence, and the
full detail is a tap away.

## What the user wants first

On opening the app, in this order:

1. **Last night's charge and the battery now.** For example: "Charged to 62% overnight. The
   battery is at 48% now (08:10)."
2. **Today's forecast usage and solar,** with what the weather is expected to do to them.
3. **Yesterday's savings,** with a look back at how yesterday's weather affected it.
4. **Tonight's plan.** This is roughly equal with yesterday's savings, so in the evening it moves up
   to second place (see decision 1).

## The three-step explanation

Every explanation on the plan, the forecast and the look back at yesterday works the same way. A
new shared component, `<app-explain>`, has three states:

| State | What shows | Tap once more |
| --- | --- | --- |
| **Hidden** (default) | The card's headline figure, plus a small "Why?" button | Shows the sentence |
| **Sentence** | One plain sentence, such as "Charging to 70% because tomorrow looks dull and cold." | Shows the points |
| **Points** | The full list of points, as the Tonight page shows now | Hides it again |

- The button says what the next tap does ("Why?", "More detail", "Hide"), and uses `aria-expanded`
  so screen readers announce it.
- Each card remembers its last state on that phone (in `localStorage`, decision 3), so if the user
  always wants the sentence it stays open.
- The sentence comes from the backend, so it matches the points, and both are written in one place.

## Weather explanations (new)

### Today and tomorrow: what the weather does to the forecast

The forecast already has hourly cloud cover, temperature and radiation, and the usage model already
has cold-weather terms. The backend adds a `weather` block to the day forecast with one sentence
and a few points:

- **Solar:** forecast solar against the average of the last 14 days, and why. For example: "Solar
  should be about 40% below your recent average. It's cloudy all day (85% cover), with a brighter
  spell from 13:00 to 15:00."
- **Usage:** the cold-weather share the model adds, in kWh. For example: "A cold day (4°C average)
  adds about 0.9 kWh to your usage, mostly in the evening." When the weather makes little
  difference, it says so.
- **Uncertainty:** when the forecast range is wide because the weather is changeable, it says so.
  This ties in with the plan's lean towards a lower charge on uncertain days.

### Yesterday: how the weather affected it

An honest look back has to compare yesterday with **what was forecast the night before**. Today
that forecast isn't kept: it's recalculated, and recalculating it now would use the actual weather.
So:

- **New table `forecast_snapshots`:** one row per system and day, saved when that day's plan is
  first made after the cheap window's price is known, and updated until the charge starts. It holds
  the day's expected usage and solar with their ranges, daytime cloud cover and mean temperature,
  and the charge target. That's a few hundred bytes a day.
- **The look back:** compares yesterday's actual weather and outcome with the snapshot. For example:
  "Yesterday was sunnier than forecast (30% cloud, not 70%), so solar made 11.2 kWh against 7.5. The
  battery was full by 13:00 and 2.1 kWh went to export at 15p, so a lower charge would have saved
  about 18p."
- **Until a snapshot exists** (the first day, or a gap), it falls back to the replay that History
  already uses and says that it's a replay.

The weather for yesterday itself comes from the weather records the app already imports.

## Layout

### Navigation

- **Phone:** a bottom tab bar with Home, Tonight, History and More. More opens a short list with
  Automation, System, Data and Sign out. Today the six links plus Sign out wrap onto several lines
  at the top.
- **Computer:** the same four groups across the top, with More as a menu.
- The routes stay the same (`/`, `/tonight`, `/history`, `/automation`, `/setup`, `/data`).

### Home, phone first

Each card leads with one big figure, then a one-line status, then the explanation, which starts
hidden.

1. **This morning:** "Battery 48% now". Below it: "Charged to 62% overnight, about right
   (hindsight best 60%)." Readings are refreshed on opening if the latest is over 30 minutes old
   (decision 5). The battery and grid chart sits below the explanation, shown by a tap.
2. **Today:** "7.4 kWh use · 9.1 kWh solar", with a small range bar. Then a weather line with an
   icon, such as "Cloudy, 4°C", and the weather explanation.
3. **Yesterday:** "Saved £1.20", against everything on grid import. Then the weather look back
   explanation.
4. **Tonight:** "Charge to 70%, 02:00–05:00", with a sent to inverter or not sent badge from
   Automation. Then the plan explanation, and a link to Tonight.

After 16:00 local time, cards 3 and 4 swap places (decision 1).

### Tonight

- At the top: the target, times and cost, a badge showing whether it's been sent, and the
  three-step explanation.
- The usage selector (Away, Low, Normal, High) stays near the top, because it changes the plan.
- Below that: the forecast card for tomorrow with its weather explanation. The half-hourly chart,
  the "if the day turns out differently" scenarios and the downsides become sections that open on a
  tap, closed by default.

### History, Automation, System, Data

The same card pattern and phone layout, with no change to what they contain. Automation keeps every
step and safety check exactly as it is, and is only restyled.

## How it fits the code

- **Styles:** `styles.scss` (1,150 lines, one phone-width rule) is split into design tokens
  (colours, spacing, type sizes, light and dark), a few shared patterns (card, headline figure,
  badge, disclosure), and each component's own styles. Layout is built for 360px wide first and
  widened at larger sizes.
- **Shared components:** `app-explain` (three steps), `app-headline` (big figure plus status line),
  `app-disclosure` (a section that opens on a tap), and `app-tab-bar`.
- **Big pages split:** Tonight (about 1,050 lines) and Home are broken into card components, so the
  order can change without rewriting the page. Each card loads its own data with Angular's
  `resource` / `httpResource`, so one slow call (such as weather) doesn't hold up the others.
- **Backend:** add `summary` (one sentence) alongside the existing `reasons` on each recommendation.
  Add the `weather` block to the day forecast, a `forecast_snapshots` table created at startup like
  `usage_expectations`, and a `GET /api/history/yesterday` that returns the look back (outcome,
  weather, snapshot comparison, sentence and points).
- **Tests:** component tests for `app-explain` and the card order, backend tests for the weather
  sentences (sunny, dull, cold, mild, changeable) and the snapshot comparison, and phone-width
  screenshots of each page for review.

## Order of work

Each step is its own pull request and leaves the app working.

1. **Foundations:** tokens, split styles, the tab bar and More menu, and `app-explain`. The look
   changes, but the content stays where it is.
2. **Backend explanations:** plan summaries, the forecast weather block, snapshots, and the
   yesterday endpoint.
3. **New Home:** the four cards in the new order, with the evening swap.
4. **Tonight:** the new top section and sections that open on a tap.
5. **The other pages:** History, Automation, System and Data in the new pattern.

Snapshots start saving in step 2, so the look back at yesterday has real forecasts to compare with
by the time Home ships.

## Decisions to confirm

1. **Evening order:** after 16:00, tonight's plan moves above yesterday's savings. *(Recommended.)*
   Otherwise the order stays fixed.
2. **Navigation:** bottom tab bar on phones with Home, Tonight, History and More. *(Recommended.)*
3. **Explanation memory:** each card remembers its last state on that phone. *(Recommended.)*
   Otherwise every card starts hidden on every visit.
4. **Weather comparison:** solar is compared with the last 14 days' average. *(Recommended, because
   it follows the seasons.)* The alternative is the best recent day, which shows how far from a
   clear day it is.
5. **Fresh readings on opening:** Home fetches from Fox ESS when the latest reading is over 30
   minutes old. This is one or two calls of the 1,440 a day. *(Recommended.)* Otherwise only the
   "Update now" button fetches.
6. **Mock-up first:** phone-width mock-ups of Home and Tonight to review before step 3.
   *(Recommended.)*
