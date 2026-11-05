# Inverter control: test protocol

A step-by-step way to build trust in the planner's writes to a Fox ESS inverter before letting
it set anything that matters. Each stage does a little more than the one before. Don't move on
until the current stage has passed and you've checked the result in the Fox app yourself.

None of this has been run against a real inverter yet, so your first run is the first live
test. The write calls follow Fox's Open API documentation and community clients, not a tested
integration.

The app now writes through Fox's **Scheduler V3**, which Fox says new projects must use. The
first version of this protocol was written for V1, which Fox has deprecated, so don't use any
results from it. Run the trust ladder's stages 0 to 3 again on this version before stage 1 here.

## What the planner can and can't change

- It only writes to the inverter's **mode schedule** (Fox Open API Scheduler V3): the list of
  timed periods you see under the scheduler / time periods in the Fox app.
- It adds **ForceCharge** periods, and before a free window a **ForceDischarge**, each with a
  stop level (`fdSoc`), a power (`fdPwr`, W), a minimum charge (`minSocOnGrid`, never below 10%)
  and, if your inverter has one, a maximum charge (`maxSoc`, at its top). V3 keeps a setting
  that isn't sent at its old value, so every setting a period depends on is sent.
- Your own periods are written back with every setting as Fox returned them, including ones
  the app doesn't use, such as `pvLimit` and `exportLimit`. If one of yours is partly
  overlapped, only the overlapping part is replaced, and the rest is kept as its own period.
- An **all-day** period (00:00 to 23:59) is Fox's default for times no other period covers. It's
  never sent, as the API can't set it and community reports say sending it can stop the other
  periods working. Each write checks it's still there afterwards.
- **Switched-off periods** aren't shown by V3, and the write replaces the whole list, so any you
  have may be lost. Note them in your screenshot before you start.
- The write is one V3 call. V3 has no on/off switch of its own, so the old V1 switch call is used
  only when the schedule is off and must go on, or is to be off.
- It never changes the inverter's work mode, export limits, battery settings, firmware or
  anything outside the schedule.
- Fox schedules **repeat every day**. A period stays until it's changed or removed.
- Fox limits the API to 1,440 calls per inverter per day (shared with other apps) and one write
  every 2 seconds per endpoint. A full apply uses three or four calls, all listed before you
  authorise them.

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

On the **Automation** page, tick the consent box and turn inverter control on. This writes
nothing. Run **step 1**.

- Pass: the log shows your schedule, and it matches your screenshot (periods, times, modes, on or
  off). An all-day period, if you have one, is listed but never written.
- This also saves the "original schedule" used by Restore.

## Stage 2: write with no change

Run **step 2**. It writes your schedule back exactly as read, then reads it again.

- Pass: "Done", and the Fox app shows the schedule unchanged, including any all-day period and
  each period's export and PV limits.
- This proves the key can write, the request is signed correctly and Fox accepts V3's format.
- If you had switched-off periods, check whether they're still in the Fox app, and tell me. That
  answers one of the open questions below.

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

This is also how to use the planner without automatic apply: each evening, review and authorise
that night's schedule yourself.

1. Press **Preview**. Check the plan (target %, start and stop times, power) and the table of
   what will be added, kept and removed.
2. Read **Calls to Fox ESS**. It lists every call, in order, saying what each does and whether
   it changes the inverter. Open any of them to see the exact body sent.
3. Tick "I've checked these calls and authorise them", then press **Authorise and send**. It's
   refused if the plan or your schedule changed since the preview, and nothing is written.
4. Check the Fox app shows the periods.
5. Next morning, compare the battery level at the end of the cheap window with the target, and
   check the History page's review of the night.
6. Repeat for a few nights before trusting it.

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

- Whether your inverter accepts the V3 write. One owner's inverter reads through V3 with a
  personal API key, reporting up to 96 periods, but no V3 write has been made. Some community
  reports mention `44098 Write Failed` on ForceCharge for some models, and one says an invalid
  V3 upload returned success but locked the Fox app's scheduler until every period was deleted
  in the Fox web client. That's the manual recovery if it happens.
- Whether the V3 write switches the schedule on by itself. The app only calls V1's switch when
  it must, and the read-back reports if the schedule ends up on or off unexpectedly.
- Whether the all-day default period survives a write that doesn't include it. Stage 2 shows
  this, and the read-back reports it.
- Whether switched-off periods survive a V3 write (stage 2).
- What the inverter does outside the scheduled periods when the scheduler is on. Fox normally
  falls back to the inverter's own work mode. Stage 5 lets you see this.
- Whether the hold works on your inverter. The planner relies on it, since Fox has no grid-only
  mode: it writes a ForceCharge to the lowest accepted level. On one owner's inverter, a manual
  ForceCharge to 50% from 02:00 to 05:00 on 1 November 2026, with the battery at 65%, kept it at
  65% and ran the house from the grid, with import matching usage. That hold was set by hand 15
  points below the battery, not written by the planner at the lowest level, and the write test
  doesn't cover it, as its periods charge to 100%. To check yours, set one by hand in the Fox app
  with the battery above the level, and confirm the battery neither charges nor discharges.
- The minimum charge range. Fox documents 10–100%, and one inverter reports 5–100%. The app
  writes at least 10% whatever the inverter accepts, and no forced period stops below the minimum
  written with it, as Fox requires. The preview says when your minimum is lower.
