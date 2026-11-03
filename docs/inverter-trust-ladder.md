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
- [x] Known defects D1, D3, D4, D6 and D11 as strict expected failures. Each one flips to a failure
  of the suite when it's fixed, so the marker has to come off deliberately.
- [ ] The rest of the Layer 2 matrix (T2 to T4, T6, E3, E4, E6 to E8, R1 to R5, A1, A3, A5, A6).
- [ ] Layer 3: each Fox error injected at each of the four calls.

Run: `cd backend && pytest -q`. CI runs it on every pull request.

**AI review**

- [ ] For each golden file, Claude works out the expected periods from the scenario line alone,
  before reading the file, then compares them. Prompt: *"Here's a scenario and the requests a
  battery app sends to the Fox ESS v1 scheduler for it. Without trusting the app, work out by hand
  the periods you'd expect (local wall clock, ends inclusive to the minute, midnight split), then
  compare with the requests field for field. List every difference."*
- [ ] Every pull request touching the control code gets a review against the invariants and the
  rules above, and says which golden files changed and why.

**Human verification**

- [ ] Read three golden files, `S1-charge-only.json`, `S5-discharge-hold-charge.json` and
  `E2-users-periods-kept.json`. Is each what you'd set in the Fox app by hand?
- [ ] Confirm `SBP_INVERTER_WRITES` is absent or `false` in your server's `.env`.
- [ ] Open the Inverter page. It should say writes are switched off, with only step 1 and Preview
  available.

## Stage 1: Fix the known defects, move to Scheduler V3

Still offline. The test plan's suggested order: confirm V3 by reading (stage 2 does this), move
the client to V3, then fix D1 to D6 and D9 to D11, each with a failing test first.

**Automated**

- [ ] Each fixed defect's expected failure removed, and the test passing.
- [ ] Golden files regenerated for V3 (`UPDATE_GOLDEN=1 pytest tests/test_fox_requests.py`).
- [ ] New tests for V3's own risks: settings left out keep old values, the master switch, and
  `extraParam` fields such as `pvLimit` sent back unchanged.

**AI review**

- [ ] Claude compares each regenerated golden file with Fox's V3 document, section by section, and
  lists anything the document doesn't confirm.

**Human verification**

- [ ] Review the golden file diff in the pull request. Every change should be explained by V3 or a
  defect fix.

## Stage 2: Live reads only

Writes stay off. This proves the app reads your inverter correctly. It's L1 in the test plan.

**Automated**

- [ ] The write test's step 1 on the Inverter page: reads and saves the baseline.
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

## Stage 3: A week of dry runs

Writes stay off. Each evening, before the cheap window, run the dry run and compare it with what
you'd do by hand. Run it from cron if you like; it sends nothing.

**Automated**

- [ ] Seven nights of dry runs with no FAIL (exit code 0).

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
