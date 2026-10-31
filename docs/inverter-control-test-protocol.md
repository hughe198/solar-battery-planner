# Inverter control: test protocol

A step-by-step way to build trust in the planner's writes to a Fox ESS inverter before letting
it set anything that matters. Each stage does a little more than the one before. Don't move on
until the current stage has passed and you've checked the result in the Fox app yourself.

None of this has been run against a real inverter yet, so your first run is the first live
test. The write calls follow Fox's Open API documentation and community clients, not a tested
integration.

## What the planner can and can't change

- It only writes to the inverter's **mode schedule** (Fox Open API v1 scheduler): the list of
  timed periods you see under the scheduler / time periods in the Fox app.
- It adds **ForceCharge** periods with a charge-to level (`fdSoc`), a charge power (`fdPwr`, W)
  and a minimum charge (`minSocOnGrid`). Your own periods that don't overlap are written back
  exactly as Fox returned them.
- It never changes the inverter's work mode, export limits, battery settings, firmware or
  anything outside the schedule.
- Fox schedules **repeat every day**. A period stays until it's changed or removed.
- Fox limits the API to 1,440 calls per inverter per day (shared with other apps) and one write
  every 2 seconds per endpoint. A full apply uses about four calls.

## Before you start

1. Take a screenshot of the schedule in the Fox app. This is your manual fallback.
2. Do this in daylight, at home, with the battery comfortably above its minimum, not just before
   the cheap rate starts.
3. Have the Fox app open on your phone so you can check each stage and undo by hand.
4. Know the manual undo: in the Fox app, delete the period or switch the scheduler off. That
   works whatever state the planner leaves things in.

**Stop at any stage if** a step reports "Failed" or "read back differently", the Fox app shows
something different from the planner's log, or the inverter does anything you didn't expect.
Use "Restore original schedule" (or the Fox app) and send me the log entry: open "Sent and read
back" and copy it.

## Stage 1: read only (no writes)

On the **Inverter** page, tick the consent box and turn inverter control on. This writes
nothing. Run **step 1**.

- Pass: the log shows your schedule, and it matches your screenshot (periods, times, modes, on or
  off).
- This also saves the "original schedule" used by Restore.

## Stage 2: write with no change

Run **step 2**. It writes your schedule back exactly as read, then reads it again.

- Pass: "Done", and the Fox app shows the schedule unchanged.
- This proves the key can write, the request is signed correctly and Fox accepts the format.

## Stage 3: add a harmless test period

Run **step 3**. It adds a 10-minute force charge at 500 W, placed at least 3 hours from now in a
free gap, so it won't run before you remove it.

- Pass: "Done", and the Fox app shows the new period at the time in the log.
- Worst case, if left in place: 10 minutes at 500 W each day, about 0.08 kWh.
- If your schedule is switched off but has periods in it, this step refuses, because turning the
  schedule on for the test would also turn your periods on.

## Stage 4: remove it and compare

Run **step 4**. It removes the test period and checks the schedule matches stage 1 exactly.

- Pass: "Done", and the Fox app matches your screenshot again.
- Passing unlocks **Apply**.

## Stage 5 (recommended): a live charge you can watch

Run **step 5**. It adds a 10-minute force charge at 500 W starting about 5 minutes from now.

- Watch the Fox app: the battery should start charging from the grid at about 500 W at the
  start time and stop 10 minutes later. Note what the inverter does before and after the period
  too.
- Then run **step 6** to remove it. Pass: the schedule matches stage 1 again.
- This is the only test that shows the inverter acting on a period, not just storing it.

## Stage 6: apply a real plan by hand, and check it overnight

1. Press **Preview**. Check the plan (target %, start and stop times, power) and the table of
   what will be added, kept and removed. "Exact request sent to Fox ESS" shows the request body.
2. Press **Apply**. It's refused if the plan or your schedule changed since the preview.
3. Check the Fox app shows the force charge period.
4. Next morning, compare the battery level at the end of the cheap window with the target, and
   check the Charge plan page's review of the night.
5. Repeat for a few nights before trusting it.

## Stage 7: automatic nightly apply

This unlocks after the write test and one successful manual Apply. Each evening, from 2 hours
before the cheap window opens, the server plans and applies once. If anything fails, it's logged
and the inverter keeps the previous night's charge, because schedules repeat.

The server only runs this with `SBP_AUTO_APPLY=true` on one server process, or with
`python -m app.control.nightly` run from cron every few minutes.

## Undoing

- **Remove planner periods**: takes out the planner's charge and test periods and keeps yours.
- **Restore original schedule**: writes back the schedule saved at stage 1 (or before the first
  write).
- **Turn inverter control off**: stops all writes but leaves the inverter as it is. Remove or
  restore first if you want the planner's periods gone.
- Disconnecting Fox ESS or deleting your data also turns control off.

## Known unknowns

- Whether your inverter model and firmware accept the v1 scheduler write. Some community reports
  mention `44098 Write Failed` on ForceCharge for some models, and a history of v0 scheduler
  writes upsetting the cloud's copy of the schedule. The planner only uses v1.
- What the inverter does outside the scheduled periods when the scheduler is on. Fox normally
  falls back to the inverter's own work mode. Stage 5 lets you see this.
- What a ForceCharge period does when the battery is already above `fdSoc`. The planner relies on
  this to hold the battery, since Fox has no grid-only mode: it writes a ForceCharge to the lowest
  accepted level. An owner has seen a manual ForceCharge to 90% stop there and run the house from
  the grid for the rest of the period, which is the same state, but no one has watched one start
  from above its level. The write test doesn't cover this, as its periods charge to 100%. To
  check it, set one by hand in the Fox app with the battery above the level, and confirm the
  battery neither charges nor discharges.
- The minimum charge range. Fox documents 10–100%. If your system's minimum is lower (for
  example 5%), periods are written with the inverter's lowest accepted value and the preview
  says so.
