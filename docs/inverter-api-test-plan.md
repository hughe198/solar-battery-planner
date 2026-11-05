# Inverter API: white-box test plan

Status (5 November 2026): the client has moved to Scheduler V3, and D1 to D13 and D15 are fixed or
settled, each with a test (see "Known defects" below). Layer 1, most of Layer 2, part of Layer 3,
Layer 4's invariants and Layer 5 are built (see [the trust ladder](inverter-trust-ladder.md),
which turns this plan into stages with automated, AI and human checks). The defects were found by
reading the code and confirmed against an in-memory fake inverter on 1 November 2026. No live
write has been made by the app.

The goal is to prove, one call type at a time, that every request sent to Fox ESS is the request we
intended, and that nothing else can be sent. `docs/inverter-control-test-protocol.md` is the
user-facing ladder for a real inverter; this plan covers what has to be true before and during it.

## How a change reaches the inverter

A multi-stage plan isn't several requests. All of its stages go in **one** request, as periods in
one list, and that list **replaces the whole schedule**.

| Order | Call | Purpose |
| --- | --- | --- |
| 1 | `POST /op/v3/device/scheduler/get` | Read the current schedule |
| 2 | `POST /op/v3/device/scheduler/enable` | Replace every period, with `isDefault: false`. An empty list removes them all (D3). Despite its name, this doesn't switch anything on |
| 3 | `POST /op/v1/device/scheduler/set/flag` | Only when the schedule is off and must go on, or is to be off. V3 has no switch of its own |
| 4 | `POST /op/v3/device/scheduler/get` | Read back and compare, including that the all-day default is still there |

Fox's all-day period (00:00 to 23:59) is its default for times no other period covers. It's read,
never written: a Fox community forum thread (foxesscommunity.com) says the Open API can't set it and sending
it can stop the other periods working. Writes leave it out, and the read-back checks it survived.

The Automation page lists these calls, with their bodies, before the user authorises them, and a
test checks that what's listed is exactly what's sent.

### How stages are built

`propose` (`control/service.py`) builds up to three kinds of stage, then merges them with the user's
periods:

| Stage | Written as | `fdSoc` | `fdPwr` |
| --- | --- | --- | --- |
| Discharge before a free window | `ForceDischarge` | Target level, clamped | Max discharge |
| Hold | `ForceCharge` | Lowest level the inverter accepts | Max charge |
| Charge | `ForceCharge` | Target rounded up, clamped | Max charge |

Each stage covering midnight becomes two periods. Ends are inclusive to the minute, so "to 05:00" is
written as `endHour: 4, endMinute: 59`.

### Example: hold 02:00–02:45, charge to 53% until 05:00, with the user's own 16:00–19:00

This is the body of step 2, plus `deviceSN`. The golden files in
`backend/tests/golden/inverter/` hold the exact bodies for each scenario.

```json
{"isDefault": false, "groups": [
  {"startHour": 2, "startMinute": 0, "endHour": 2, "endMinute": 44, "workMode": "ForceCharge",
   "extraParam": {"fdSoc": 10, "fdPwr": 5000, "minSocOnGrid": 10}},
  {"startHour": 2, "startMinute": 45, "endHour": 4, "endMinute": 59, "workMode": "ForceCharge",
   "extraParam": {"fdSoc": 53, "fdPwr": 5000, "minSocOnGrid": 10}},
  {"startHour": 16, "startMinute": 0, "endHour": 18, "endMinute": 59, "workMode": "SelfUse"}
]}
```

## What could go wrong

Damage to the hardware looks unlikely, as Fox's API sits between us and the inverter, and its
documented ranges limit the values. That's an assumption about Fox, and nothing here can prove it.
The realistic harms are these:

| Harm | How it could happen | Worst case |
| --- | --- | --- |
| Paying more | A charge outside the cheap window, or a discharge then a recharge | Max charge power for the period's length, at that time's rate |
| Empty battery | `ForceDischarge` to a low level before a dear period | Evening use from the grid at peak rate |
| Repeating nightly | A period left behind, as schedules repeat | The same mistake every night until noticed |
| Losing the user's settings | The write replaces the whole list | The user's periods gone, with Restore as the fallback |
| No backup reserve | Too low a level before a power cut, if backup matters | Less or no backup |
| Unwanted writes | Automatic apply or a bug | Any of the above, unattended |

## Invariants to prove

Each becomes one or more automated tests. A failure blocks live testing.

1. Only `scheduler/enable` and `scheduler/set/flag` ever write, and only from `_write`.
2. No write happens without opt-in, or below the write-test level that unlocks it.
3. Connecting, importing, previewing, planning and opening Home send no write.
4. Every write is preceded by a read, followed by a read-back, and logged with what was sent.
5. Every value sent is within the range the inverter reports.
6. Periods in one write never overlap, always start at or before their end, and fit in the maximum.
7. Every one of the user's periods not overlapped goes back unchanged, field for field.
8. What's applied is byte-for-byte what was previewed (`fingerprint`).
9. `ForceCharge` and `ForceDischarge` only appear in the charge window or the discharge span the plan
   gave. Hold periods are never outside the charge window.
10. After Restore, the inverter matches the saved schedule, including whether it was on.

## Known defects, now fixed

D1 to D5 and D8 were reproduced against a fake inverter. D6 and D7 were seen there and judged against
Fox's document, and D9 and D10 come from comparing the code with that document. Each now has a test
named after it in `backend/tests/test_fox_requests.py`.

| # | Fix |
| --- | --- |
| D1 | The success message is built from the stages written, so a hold-only night reads "Hold 00:30–05:30 written…" (golden `S2-hold-only`) |
| D2 | V3 hides switched-off periods, so they can't be kept. The owner accepted that. The preview names any that are reported, and otherwise warns that any in the Fox app may be lost |
| D3 | The empty list is sent through V3, then the schedule is switched off (golden `A3-clear-last-period`) |
| D4 | If the periods reached the inverter, or a timeout means they may have, the app keeps track of them so Remove and Restore still find them, and says so in the log |
| D5 | Only the overlapping part of a user's period is replaced; the rest is kept with every setting (golden `E3-partly-overlapped`) |
| D6 | The minimum written is at least 10% (owner's decision), and no forced period stops below it |
| D7 | `maxSoc` goes on every forced period when the inverter reports one |
| D8 | Both ends of a stage round up to the minute, so stages meeting on a second don't overlap, and overlapping stages are refused |
| D9 | The master switch is read as `"0"`/`"1"` or a number |
| D10 | Numeric strings in settings are sent back as numbers |
| D11 | `40400`, `40404` and `41201` are retried; `41811` counts as a rejected key; `40256`, `40257` and `44098` are explained in the message |
| D12 | A user's forced charge or discharge that the plan overlaps is replaced completely, never trimmed, and the dry run fails if any piece of one is kept (`test_merge_replaces_overlapped_force_charge_wholly` in `test_control.py`) |
| D13 | Every write that leaves the schedule on covers the whole day: times only Fox's all-day default covered are written as copies of it, with all its settings, taken from the inverter or, once dropped, from the copy saved before the first write. Neighbouring copies merge into one period. With no all-day period ever seen, gaps are left to the main work mode as before. Write test steps 3 and 4 and the dry run allow for it (`test_write_test_keeps_the_whole_day_covered_when_fox_drops_its_default`, golden `E9-all-day-default`) |
| D15 | A read-back that differs from what was written is re-read after 10, 20 and 30 seconds, and only reported as a mismatch if it still differs after a minute (`test_read_back_waits_for_fox_to_catch_up`) |

D14 is still open: see the findings below.

The original findings:

| # | Severity | Defect | Where |
| --- | --- | --- | --- |
| D1 | **High** | **A no-charge night crashes Apply.** A hold-only plan has no target, and formatting the success message raises `TypeError`. Nothing is written, and the nightly job would fail on every such night | `service.py:304` |
| D2 | **High** | **Switched-off user periods are deleted.** `from_fox` drops periods with `enable: 0`, and the write replaces the whole list. The saved original drops them too, so Restore can't bring them back. Contradicts the protocol's "written back exactly" | `schedule.py:110` |
| D3 | **High** | **Removing the last period doesn't remove it.** With an empty list, only the schedule is switched off, and the read-back reports success. Switching the scheduler on in the Fox app brings the charge back | `service.py:206` |
| D4 | Medium | **A failure between the two writes loses track.** If the flag call fails after the periods were written, the app logs a failure and doesn't record them, so "Remove planner periods" can't find them. Restore works only if an original was saved | `service.py:323` |
| D5 | Medium | **Partly overlapped user periods go entirely.** A user's all-day `SelfUse` is removed for a two-hour charge, leaving the rest of the day outside any period. It's mentioned in a note, but the rest of the period isn't preserved | `schedule.py:170` |
| D6 | **High** | **Hold level can break Fox's documented rule.** Fox says `fdSoc` "must be equal or greater than minSocOnGrid". With a 20% minimum charge, a hold writes `fdSoc` 10 with `minSocOnGrid` 20, and a discharge or low charge target could do the same | `service.py:94` |
| D7 | Low | **`maxSoc` only goes on charge periods.** Fox documents it as optional, so omitting it from holds and discharges should be accepted | `service.py:104` |
| D8 | Low | **Stages can overlap by a minute.** A boundary with seconds rounds the stop up and the start down, and stages aren't checked against each other. The planner's 5-minute steps should prevent it, but nothing enforces it | `schedule.py:152` |
| D9 | **High**, from the document | **A schedule that's off may read as on.** Fox documents the master `enable` in `scheduler/get` as a string, `"0"` or `"1"`. The code uses `bool(...)`, and `bool("0")` is true. Writes could then misjudge whether to warn, what to switch on and whether read-back matches. Check the real type in L1 | `schedule.py:121` |
| D10 | Medium, from the document | **`maxSoc` may go back as the wrong type.** Fox documents it as a string when read and an integer when set. Kept periods are written back as read, so they could send a string | `schedule.py:60` |
| D12 | **High**, found at stage 2 | **A one-minute forced charge is left behind.** Found in the owner's first live Preview, 5 November 2026. Their own ForceCharge 02:45–05:00 to 67% at 12 kW overlapped the planner's 02:10–05:00. D5's trimming kept the minute the planner's didn't cover, so Apply would have written a ForceCharge 05:00–05:01 to 67%, repeating every night. A leftover at the start of a window could grid charge at the day rate. A user's forced period now goes completely | `schedule.py`, `merge` |
| D13 | **High**, found at stage 4 | **A write drops Fox's all-day default, leaving times with no period.** Write test step 2 on the owner's inverter, 5 November 2026, wrote their four periods back exactly, but Fox stopped reporting the all-day SelfUse default. 02:00–02:45 and 16:01–19:01 (where a switched-off summer ForceDischarge had been, also lost, as D2 expected) were left with no period, and what the inverter does then isn't documented. The fakes kept the default on a write, so no test caught it | `service.py`, `_run_write`, `propose` |
| D14 | Low, found at stage 4, **open** | **Read-back doesn't compare the limits.** After step 2 Fox reported `pvLimit` 20,000 and 24,000 W swapped between periods, and after step 4 it read back as sent. Read-back compares only `fdSoc`, `fdPwr`, `minSocOnGrid` and `maxSoc`, so a change to `pvLimit`, `importLimit`, `exportLimit` or `reactivePower` passes silently. Harmless with 10 kWp of panels | `schedule.py`, `matches` |
| D15 | Medium, found at stage 5 | **A good write is logged as a mismatch.** Twice on 5 November 2026 (steps 5 and 6) the read straight after the write showed Fox's old schedule, while a read a minute later showed it exactly as sent. Step 6 stayed unticked though it had worked | `service.py`, `_verify` |

## Test layers

### Layer 1: Exact requests (no inverter)

Today's tests replace `FoxEssClient` with a fake, so **the real HTTP layer isn't tested for writes**.
Run the real client over `httpx.MockTransport`, and record method, path, headers and JSON body.

For each scenario below, keep the expected request sequence in a reviewable file
(`backend/tests/golden/inverter/<scenario>.json`). Any change to a request then shows up as a diff
for a person to approve.

Also check the signature against a value worked out by hand, per Fox's documented formula.

### Layer 2: The scenario matrix

Every row is a golden file, with expected periods worked out by hand before the test runs.

**By stage**

| # | Scenario | Key check |
| --- | --- | --- |
| S1 | Charge only | One `ForceCharge`; `fdSoc` rounded up |
| S2 | Hold only (D1) | One `ForceCharge` at the lowest level, and the message makes sense |
| S3 | Hold then charge | Two adjoining periods with no gap or overlap |
| S4 | Discharge then charge | `ForceDischarge` before the window |
| S5 | Discharge, hold, charge | All three, in order |
| S6 | Nothing to do | Reads only, no write |

**By time**

| # | Scenario | Key check |
| --- | --- | --- |
| T1 | Window crossing midnight | Split into `..23:59` and `00:00..` |
| T2 | Stop exactly at midnight | Ends 23:59, with no empty second period |
| T3 | Clocks go back, and forward | Local times correct; the period stays right on later nights |
| T4 | One-minute and 5-minute periods | Inclusive ends correct |
| T5 | Time zone away from UTC | Local wall clock is written |
| T6 | Stop with seconds (D8) | No overlap |

**By existing schedule**

| # | Scenario | Key check |
| --- | --- | --- |
| E1 | Empty, scheduler off | Switched on, with only ours present |
| E2 | User's periods clear of ours | Returned field for field, including unknown fields |
| E3 | User's period overlapped (D5) | Decision on what's kept, and clearly reported |
| E4 | User's switched-off period (D2) | Survives the write and Restore |
| E5 | Yesterday's planner periods | Replaced, even if they don't overlap |
| E6 | User's period identical to ours | Not mistaken for ours by `without` |
| E7 | Scheduler off with user periods | Warned that they'll switch on too |
| E8 | Schedule full | Refused, with no write |

**By range and values**

| # | Scenario | Key check |
| --- | --- | --- |
| R1 | Target, power and minimum outside reported ranges | Clamped, with a note |
| R2 | No `properties` from Fox | Documented defaults are used |
| R3 | `maxSoc` reported (D7) | Consistent across stages |
| R4 | Minimum above the hold level (D6) | Decision on what's written |
| R5 | Fractional values | Whole numbers sent |

**By action**

| # | Scenario | Key check |
| --- | --- | --- |
| A1 | Apply after a changed schedule or plan | Refused by the fingerprint, with no write |
| A2 | Clear with user periods left | Only ours are removed |
| A3 | Clear of the last period (D3) | The period is really gone |
| A4 | Restore, including an empty original | Identical, on or off as it was |
| A5 | Each write test step, 1 to 6 | Exact sequence, and steps can't be skipped |
| A6 | Opt-out, and disconnect | No write; later writes are refused |

### Layer 3: Failures at every step

Inject each fault at each of the four calls in the sequence:

- A Fox error code, such as `44098`
- The rate limit, `40400`, which is retried
- An expired or rejected key
- A timeout, including one where **Fox applied the write but the reply was lost**
- Invalid JSON

For each, check what was left on the inverter, what's logged, what the app believes it wrote (D4), and
that Clear and Restore still work from that state.

### Layer 4: Enforcing the invariants

- **Coverage:** branch coverage of `control/schedule.py`, `control/service.py` and `control/testing.py`
  at 100%, with any exception justified in writing.
- **Property tests:** generate random plans, schedules and time zones, and assert invariants 5, 6, 7
  and 9 on each result.
- **Write allowlist:** a transport that fails any test making a write outside the expected list, run
  across the whole backend suite, which proves invariants 1 to 3.
- **Mutation testing** (optional): change the code on purpose and confirm that a test fails.

### Layer 5: Dry run against the real schedule

A command that reads the real schedule, builds tonight's proposal and prints the exact requests
without sending them: `python -m app.control.dryrun --email you@example.com`. The server setting
`SBP_INVERTER_WRITES`, `false` by default, blocks both write endpoints in `FoxEssClient` itself, so
a bug higher up can't get past it.

Run it for a week and compare each night with what you'd set by hand.

### Layer 6: Live, one call type at a time

Each step: state what should happen, run it in daylight or supervised with the Fox app open, compare
the Fox app and logged response with the golden file, and undo it. Don't move on after a surprise.

| # | Call or behaviour | Pass condition |
| --- | --- | --- |
| L1 | `scheduler/get` | Matches a screenshot of the Fox app, including switched-off periods and `properties` |
| L2 | `scheduler/enable` with the same list | Fox app unchanged, field for field |
| L3 | `set/flag` off, then on | Only the switch changes, and periods are kept |
| L4 | Add a far-off test `ForceCharge` | Appears correctly, and doesn't run |
| L5 | Remove it, keeping another period | Exactly back to L1 |
| L6 | Remove the last period (D3) | Fox's real behaviour with an empty list is known |
| L7 | Short supervised charge at low power | Charges at that power, then stops |
| L8 | Charge reaching its level mid-period | Stops there, house on the grid |
| L9 | Hold at the lowest level, from well above it | Battery flat, and import matches usage |
| L10 | Supervised `ForceDischarge`, small, level well above reserve | Stops at its level, or at the end of the period |
| L11 | Multi-stage: hold, charge, then a user's period | Each stage runs at its time |
| L12 | Restore | Identical to L1 |
| L13 | One real night by hand, then several | Levels match the plan the next morning |
| L14 | Auto-apply, last, after a week of L13 | Applies once, logged, and is watched for a week |

Keep a record of each: date, request, response, what the Fox app showed, and pass or fail.

## Questions only Fox or a real inverter can answer

The Open API document doesn't settle these:

- Whether `enable: 0` periods exist in practice, and whether they're filler or the user's (D2).
- Whether `scheduler/enable` accepts an empty list, or clears omitted groups (D3). It doesn't say
  whether a request must include every group.
- The real JSON types of the master `enable` and `maxSoc` (D9, D10).
- The units and range of `fdPwr`, which it describes only as "The maximum discharge power", including
  on a charge period.
- What `minSocOnGrid` means: it's described differently for reading and setting.
- Whether a `ForceDischarge` stops at `fdSoc`, and what it does at the end of the period.
- Whether a write is atomic on Fox's side, and how long a change takes to reach the inverter.

## Checked against Fox's document

On 1 November 2026, the V1 scheduler section of
<https://www.foxesscloud.com/public/i18n/en/OpenApiDocument.html> was compared with the code. The
page at <https://developer-eu.foxesscloud.com/apidoc> needs a browser to read, so it wasn't compared,
and may be newer.

| Item | Result |
| --- | --- |
| Paths and POST method for get, set and flag | Match |
| `deviceSN` in the body of all three | Matches |
| `enable` as 0 or 1 when setting the flag | Matches |
| Group fields, and work mode names | Match. `ForceCharge` and `ForceDischarge` are listed |
| 1,440 calls a day, one write per 2 seconds per endpoint | Match. Queries are limited to one a second per endpoint |
| Types of `enable` and `maxSoc` when read | **Don't match** (D9, D10) |
| `fdSoc` at or above `minSocOnGrid` | **Not enforced** (D6) |
| `maxGroupCount` | Not in V1, so the code's default of 8 applies. The document's V3 scheduler has it |

## Move to Scheduler V3 first

Fox's newer developer document (developer-eu.foxesscloud.com/apidoc, sections pasted by the owner on
1 November 2026) marks V1 **deprecated, "retained only for older hardware"**, and says new projects
must use V3. The app uses V1 throughout, so the golden files and live ladder should target V3, or be
written twice. The older document gives V3's fields and says a personal API key still works, using
the same `token` and signature headers.

| | V1, used today | V3 |
| --- | --- | --- |
| Write | `/op/v1/device/scheduler/enable` | `/op/v3/device/scheduler/enable` |
| Read | `/op/v1/device/scheduler/get` | `/op/v3/device/scheduler/get` |
| Period fields | Flat, with its own `enable` | No `enable`; `fdSoc`, `fdPwr`, `minSocOnGrid`, `maxSoc` and others go inside `extraParam` |
| Omitted settings | All required | **Stay at their previous values**, unless `isDefault` is true |
| Size limit | Not stated | `maxGroupCount` from the device; the newer document says up to 96 a request |
| Clearing | Not documented | An empty list wipes the schedule |

What this means for the defects:

- **D2** may go away, as V3 has no switched-off periods. Confirm by reading both versions from a
  schedule with a switched-off period.
- **D3** has a documented fix on V3: send the empty list rather than only switching off.
- **D6** still needs fixing, as nothing says V3 relaxes the rule.
- **D9** still applies: V3 also documents the master `enable` as a string.
- **D10** may go away, as V3 documents `maxSoc` as a number.

New risks from V3, each needing a test:

- **Leftover settings:** with `isDefault` false, a value we don't send keeps its old value, so always
  send every setting the stage depends on.
- **Master switch:** V3 lists no flag endpoint of its own. Find out whether its write switches the
  schedule on, or whether a separate call is still needed.
- **Whether your inverter supports V3:** confirm with a read before changing any code.

### What one real inverter returned

Read through both V1 and V3 on 1 November 2026, sending no writes:

- **V3 works** with a personal API key, reporting `maxGroupCount: 96`. V1 reports no maximum, and
  returned 8 slots.
- **The master `enable` was the integer 1** on both, and `maxSoc` was an integer on V1 and a float on
  V3, unlike both documents. So D9 and D10 didn't occur here. Parse them defensively anyway.
- **V3 hides switched-off periods.** V1 returned 8 groups, including the owner's switched-off
  `ForceDischarge`; V3 returned the 5 active ones. Moving to V3 can't preserve that period, so D2
  stays until the owner decides what to do with it.
- **`extraParam` has settings V1 doesn't show:** `importLimit`, `exportLimit`, `pvLimit` and
  `reactivePower`, with `pvLimit` differing between periods. V3 must send these back unchanged on kept
  periods, and it isn't known whether a V1 write resets them.
- **Overlapping periods are allowed.** An all-day `SelfUse` sits beside the specific ones, and the
  hold test passed with it there, so a more specific period seems to win. That order needs testing.
- **The Fox app's own periods support inclusive ends:** one ends 05:00 and the next starts 05:01.
- **Undocumented work modes** are reported: `ForceCharge(BAT)`, `ForceDischarge(BAT)`, and a
  `secondworkmode` property.
- **Ranges differ from the defaults:** `fdSoc` and `minSocOnGrid` go down to 5, and `fdPwr` up to
  12,000 W.

Run on that schedule, the current code would (offline, nothing sent):

- Remove the all-day `SelfUse` and delete the switched-off `ForceDischarge`, leaving 16:01–19:00 outside
  any period (D2, D5).
- Write holds with `fdSoc` 5 and `minSocOnGrid` 10, breaking Fox's documented rule (D6).
- Change `minSocOnGrid` from the inverter's 5 to the app's 10 on the new periods. Add a test, and show
  it plainly in the preview.

### Owner's decisions, 1 November 2026

- **Switched-off periods:** losing the switched-off summer `ForceDischarge` is acceptable, so V3 needn't
  preserve it. Still warn in the preview when a write will remove switched-off periods, as other users
  may care.
- **Minimum charge:** stays at 10% in the app, for safety, as the true value is uncertain. New periods
  write `minSocOnGrid` 10, and the hold level must be at least the minimum: `fdSoc` 10, not the
  inverter's lowest of 5. This is the fix for D6.

### Where the documents disagree or stay unclear

- **Daily repeat or 24 hours:** the newer document says a schedule runs "every day... indefinitely",
  and also that it only accepts plans "for the next 24 hours". The first matches what's been seen.
  Test which holds, as the plan's safety depends on knowing when a period stops.
- **Period ends:** its midnight example is "23:00 to 23:59" then "00:00 to 01:00". The code writes
  inclusive ends, as in 04:59 for "to 05:00". A live test should confirm whether 04:59 stops at
  04:59:00 or 05:00:00.
- **Empty list:** the older document marks `groups` as required and doesn't mention an empty list. The
  newer one says to send it. Test it.

### Error codes from the newer document

- **D11, Medium:** too-frequent requests are documented as `40404` there and `40400` in the older
  document. The client only retries `40400`, so `40404` would fail a write at once. Handle both.
- `41811` (permission denied) should count as a rejected key, alongside the existing `41807` to `41809`.
- `40257` (invalid parameter) and `41201` (system error) need clear messages. Layer 3 should inject
  both, plus `40256`, and `41201` is the only one of these Fox says to retry.

## Suggested order

1. ~~Read the real schedule with both V1 and V3 to confirm V3 support.~~ Done 1 November 2026.
2. ~~Move the client to V3 and fix D1 to D6, D9 to D11.~~ Done 5 November 2026.
3. ~~Build Layer 1, then Layers 2 and 3 alongside.~~ Mostly done; T2 to T4, E6 to E8, R1, R2, R5, A1,
   A5, A6 and the invalid-JSON fault are still to add.
4. ~~Add the write block and dry run (Layer 5), and enforce the invariants (Layer 4).~~ Done.
5. Run a week of dry runs, then the live ladder in order.

## Still open after the move to V3

- Whether a V3 write switches the schedule on by itself. The app assumes not, and calls V1's
  switch only when it must. If V1's switch is refused on newer hardware, switching on from off
  fails and is reported; Hugh's schedule is already on, so his nightly writes don't need it.
- ~~Whether leaving the all-day default out of a write keeps it (L2 answers this).~~ Answered
  5 November 2026 on the owner's inverter: any write drops it (D13). What the inverter does at
  a time no period covers is still unknown, so the app no longer leaves any.
- What `importLimit`, `exportLimit`, `pvLimit` and `reactivePower` become on a **new** period that
  doesn't send them. The app doesn't send them, so as not to set an export limit it can't know,
  and the preview can't show their values. Check them in the Fox app at L4.
