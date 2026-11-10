# Inverter control: the trust ladder

How the app earns the right to change a real inverter's schedule, one stage at a time. Each stage
has three kinds of check, and all three must pass before the next stage starts:

- **Automated**: tests and scripts that run the same way every time, in CI or on the server.
- **AI review**: Claude reads what the automated checks produced, independently of the code that
  produced it, and says SAFE or NOT SAFE with reasons. It catches what nobody wrote a test for.
- **Human verification**: you check against the Fox app and your own judgement, and sign it off in
  the record at the end.

The AI review never replaces a human check. It's there so two different kinds of reviewer have
looked at every stage before you do.

The technical detail behind each stage, including the defect list D1 to D11 and the live steps L1
to L14, is in [the white-box test plan](inverter-api-test-plan.md).

## Rules that hold at every stage

1. **Writes are off unless a stage needs them.** `SBP_INVERTER_WRITES` is `false` by default. While
   it's off, the Fox client refuses both write calls before anything is sent, whatever the code
   above it does. Reads, the preview, the write test's step 1 and the dry run all still work.
   Turn it on only for a live session in stages 4 to 6, and back off afterwards, until stage 7.
2. **A surprise sends you back.** If anything doesn't match what was expected, stop, switch writes
   off, put the schedule right in the Fox app, and repeat the stage before. Note it in the record.
3. **A code change starts again at stage 0.** Any change to `backend/app/control/` or
   `backend/app/importers/fox_ess.py` reruns stage 0 for that change, and two nights of stage 3,
   before it's used live.
4. **The Fox app is the source of truth.** Keep it open during every live step. Restore in the app
   is always available, whatever the planner does.

## Stage 0: Offline proof

Nothing touches an inverter. This proves the app builds the requests we intend, and only those.

**Automated**

- [x] Writes blocked by default, at the Fox client and at the API (`test_fox_requests.py`,
  `test_control.py`).
- [x] The real Fox client, signing included, run against a fake Fox cloud. Every request is compared
  with a golden file in `backend/tests/golden/inverter/`, so any change shows as a diff.
- [x] What the dry run predicts is checked against what Apply actually sends, scenario by scenario.
- [x] The invariants (values in range, no overlaps, your periods sent back exactly, forced periods
  only inside the plan's times, stop level at or above the minimum) checked on 150 generated plans,
  schedules and time zones.
- [x] Each of the defects D1 to D11 has a test named after it, which failed before its fix.
- [x] More of the Layer 2 matrix: S2, T6, E3, E4, E9 (all-day default), R3, R4, A3.
- [ ] The rest of the Layer 2 matrix (T2 to T4, E6 to E8, R1, R2, R5, A1, A5, A6).
- [x] Layer 3: Fox errors (`40257`, `41201`, `41808`) injected at each call, a lost reply on the
  write, and a refused write. Each leaves a known state that Remove can undo.
- [ ] Layer 3: invalid JSON at each call.

Run: `cd backend && pytest -q`. CI runs it on every pull request.

**AI review**

- [ ] For each golden file, Claude works out the expected periods from the scenario line alone,
  before reading the file, then compares them. Prompt: *"Here's a scenario and the requests a
  battery app sends to the Fox ESS Scheduler V3 for it. Without trusting the app, work out by hand
  the periods you'd expect (local wall clock, ends inclusive to the minute, midnight split), then
  compare with the requests field for field. List every difference."*
- [ ] Every pull request touching the control code gets a review against the invariants and the
  rules above, and says which golden files changed and why.

**Human verification**

- [ ] Read three golden files, `S1-charge-only.json`, `S5-discharge-hold-charge.json` and
  `E2-users-periods-kept.json`. Is each what you'd set in the Fox app by hand?
- [ ] Confirm `SBP_INVERTER_WRITES` is absent or `false` in your server's `.env`.
- [ ] Open the Automation page. It should say writes are switched off, with only step 1 and Preview
  available.

## Stage 1: Fix the known defects, move to Scheduler V3

Still offline. The test plan's suggested order: confirm V3 by reading (stage 2 does this), move
the client to V3, then fix D1 to D6 and D9 to D11, each with a failing test first.

**Automated**

- [x] Each fixed defect's expected failure removed, and the test passing.
- [x] Golden files regenerated for V3 (`UPDATE_GOLDEN=1 pytest tests/test_fox_requests.py`).
- [x] New tests for V3's own risks: every setting a stage depends on is sent, the master switch is
  called only when it must change, `extraParam` fields such as `pvLimit` go back unchanged, and
  the all-day default is never sent. The fake Fox cloud refuses V1 fields, overlaps, the all-day
  period and `fdSoc` below `minSocOnGrid`, as Fox's documents say it should.
- [x] The calls listed for authorising on the Automation page are checked to be exactly the calls
  sent, in every scenario.

**AI review**

- [ ] Claude compares each regenerated golden file with Fox's V3 document, section by section, and
  lists anything the document doesn't confirm.

**Human verification**

- [ ] Review the golden file diff in the pull request. Every change should be explained by V3 or a
  defect fix.

## Stage 2: Live reads only

Writes stay off. This proves the app reads your inverter correctly. It's L1 in the test plan.

**Automated**

- [ ] The write test's step 1 on the Automation page: reads and saves the baseline.
- [ ] A dry run, which reads once and prints the review pack:
  `cd backend && python -m app.control.dryrun --email you@example.com --out dryrun.md`.
  It exits 1 if any automated check fails.

**AI review**

- [ ] Give Claude the review pack and a screenshot of the Fox app's schedule. Prompt: *"Does
  'Schedule now' in this file match the screenshot exactly, period by period and value by value?
  List any differences."*

**Human verification**

- [ ] "Schedule now" matches the Fox app, including whether it's on.
- [ ] Switched-off periods are listed as a NOTE if you have any.

## Stage 3: Three nights of dry runs

Writes stay off. Each evening, before the cheap window, run the dry run and compare it with what
you'd do by hand: "Run tonight's dry run" on the Automation page (section 2), or the command
above for the full review pack. It sends nothing.

Shortened from a week to three nights by the owner on 5 November 2026, after stages 4 and 5 had
already been run live. Ideally one bright and one dull night, so the peak rule and the dull day
are seen working. The owner applies by hand in the Fox app meanwhile.

**Automated**

- [ ] Three nights of dry runs with no FAIL, ticked off on the Automation page.

**AI review**

- [ ] Each night, paste the review pack into Claude with the prompt at the end of the pack. It
  answers six questions and finishes with SAFE or NOT SAFE.

**Human verification**, each night, in the record:

- [ ] Would you set this schedule by hand tonight?
- [ ] Is each NOTE acceptable?
- [ ] Next morning: did the plan's reasoning hold up against what actually happened?

## Stage 4: First supervised writes, with no effect

Writes on, for this session only. The in-app write test steps 2 to 4, which are L2 to L6. Do it in
daylight, with the Fox app open, well away from any cheap window.

**Automated**

- [ ] Each step reads back and compares, and is logged. Steps can't be skipped.

**AI review**

- [ ] After each step, give Claude the log entry (sent and read back) and a Fox app screenshot.
  Prompt: *"Did the inverter end up exactly as the app intended? Is anything in the screenshot not
  in what was sent?"*

**Human verification**

- [ ] Step 2: the Fox app is unchanged.
- [ ] Step 3: a 10-minute test period appears hours ahead, at 500 W.
- [ ] Step 4: it's gone, and the schedule is exactly as before.
- [ ] Writes switched off again afterwards.

## Stage 5: Supervised live behaviour

Writes on, for this session only. L7 to L10: a short low-power charge, a charge reaching its level
mid-period, a hold from well above the level, and a small discharge well above the reserve. One
per session, watched from start to finish.

**AI review**

- [ ] Claude compares the logged requests with the golden file for the same stage type, and the
  readings during the period with what the stage should do.

**Human verification**

- [ ] Each behaves as stated in the test plan's pass condition, seen in the Fox app.
- [ ] Restore puts the schedule back exactly (L12).

## Stage 6: Real nights, applied by hand

Writes on in the evening for Apply, off again after. L11 and L13: preview, compare with that
evening's dry run, apply, and check the battery level next morning against the plan.

**AI review**

- [ ] The evening's review pack, as in stage 3, before you press Apply.

**Human verification**

- [ ] One night, then several, with the morning level matching the plan.
- [ ] Seven good nights (green on the Automation page's "last 7 nights" list: sent and read
  back as written, and the verdict about right) before stage 7. Amber nights are looked into;
  any red night restarts the count.

**Whole-day control (from 9 November 2026).** Apply now writes the whole day, replacing the
owner's own periods (see docs/inverter-api-test-plan.md). Before relying on it nightly:

- [x] One supervised apply (9 November 2026): the preview lists your periods as "Yours, replaced" and the day as
  self-use around the planner's charge, at a 10% minimum and up to your grid charge maximum.
  After sending, the Fox app shows exactly that, every minute covered.
- [ ] Restore puts back your own schedule and turns off the nightly apply.

## Stage 7: Automatic apply

Last, after at least a week of stage 6 with no surprises. Writes stay on, and
`SBP_AUTO_APPLY=true` on one server process. Watch the log every morning for a week (L14).

## Record

Add a row for every stage attempt, pass or fail, with a link to any evidence such as the review
pack, the AI's answer or a screenshot.

| Date | Stage | Check | Who | Result | Notes |
| --- | --- | --- | --- | --- | --- |
| | 0 | Automated suite, CI | CI | | |
| | 0 | Golden files reviewed | Claude | | |
| | 0 | Three golden files, writes off on server | Hugh | | |
| 2026-11-05 | 0 | Golden files S1, S5 and E2 reviewed | Claude | SAFE | Matched hand-derived requests. S1's switch-on uses V1's `scheduler/set/flag` alongside V3 |
| 2026-11-05 | 2 | Write test step 1, read the schedule | Hugh | Pass | Matched the Fox app. Labels show inclusive ends plus a minute, e.g. `05:01` |
| 2026-11-05 | 2 | Preview for the night of 6 November | Hugh, Claude | NOT SAFE | Defect D12: a one-minute ForceCharge 05:00–05:01 to 67% left from Hugh's own 02:45–05:00 period. Fixed on `claude/fix-d12-force-leftover`; run Preview again before any write |
| 2026-11-05 | 4 | Write test step 2, schedule written back | Hugh, Claude | Mismatch | Four periods read back exactly, but Fox dropped its all-day SelfUse default and the switched-off ForceDischarge 16:00–19:00 (D2, accepted). Left 02:00–02:45 and 16:01–19:01 with no period: defect D13, fixed on the same branch. Writes switched off again; step 2 to be rerun with the fix |
| 2026-11-05 | 4 | Write test step 2 rerun with the D13 fix | Hugh, Claude | Pass | Six periods, all 1,440 minutes covered, matching the Fox app. Fox changed `pvLimit` on three periods (20,000/24,000 W swapped), unchecked by read-back: D14, harmless with 10 kWp of panels |
| 2026-11-05 | 4 | Write test step 3, test period added | Hugh, Claude | Pass | ForceCharge 18:40–18:49 at 500 W to 100%, inside the filled 16:01–19:01, seen in the Fox app. Fox gave the new period import 12,000, export 12,000, PV 24,000 W, reactive 0 |
| 2026-11-05 | 4 | Write test step 4, test period removed | Hugh, Claude | Pass | Back to the six periods, 16:01–19:01 rejoined, every setting as sent, including `pvLimit` this time. Test level 4 |
| 2026-11-05 | 4 | Writes switched off again | Hugh, Claude | Pass | `SBP_INVERTER_WRITES=false`, server restarted and confirmed |
| 2026-11-05 | 5 | Write test step 5, live 500 W charge 16:05–16:15 | Hugh, Claude | Mismatch, then seen working | The read-back straight after the write showed the old schedule; a read two minutes later showed it exactly as sent, and the charge ran at 16:05 as seen in the Fox app. Defect D15: read-back doesn't allow for Fox's delay |
| 2026-11-05 | 5 | Step 5 rerun, live charge 16:30–16:40 | Hugh, Claude | Pass | Read back as written; charged as expected |
| 2026-11-05 | 5 | Owner removed the 16:05 test period in the Fox app | Hugh, Claude | Note | The Fox app re-added its all-day default and copied the test's fdSoc 100 and fdPwr 500 into the neighbouring SelfUse periods, and set every pvLimit to 24,000 W. Let the app's own steps remove its periods |
| 2026-11-05 | 5 | Write test step 6, remove the live test | Hugh, Claude | Mismatch, then seen working | Same delay as step 5 (D15): a read straight after showed the test charge gone and the gaps filled |
| 2026-11-05 | 5 | Restore | Hugh, Claude | Pass | The six periods from step 2, every minute covered, copied settings gone, confirmed by a separate read |
| 2026-11-05 | 5 | Writes switched off again | Hugh, Claude | Pass | `SBP_INVERTER_WRITES=false`, server restarted and confirmed |
| 2026-11-09 | 6 | First real night applied from the app: hold 02:00–03:00, charge to 89% 03:00–05:00 | Hugh, Claude | Pass | Read back as written; the only difference was `pvLimit` (D14), harmless as the panels peak near 3.2 kW. The preview showed the owner's own periods kept beside the charge, which led to whole-day control |
| 2026-11-09 | 6 | First whole-day apply: self-use 00:00–02:00, hold 02:00–03:00, charge to 89% 03:00–05:00, self-use 05:00–24:00 | Hugh, Claude | Pass | Four periods, read back as written, 10% minimum and 100% maximum, under the 95% grid charge maximum. Self-use kept the inverter's 20 kW solar limit; Fox set 24 kW on the forced periods (D14, harmless). The saved manual schedule is still 5 November's |
