# Solar Battery Planner

Recommends how much to charge a home battery overnight on a cheap tariff, based on predicted
household usage and solar generation for the next day. Two planning modes:

- **Maximise own usage** (default): charge enough to cover the house until solar takes over.
- **Maximise export revenue**: count export income too, so when export pays more than
  charging costs it fills up from the grid and sells more solar.

## Roadmap

1. **Accounts and system setup**: sign up, log in, describe your battery, solar arrays,
   location and tariff.
2. **Data import**: Fox ESS cloud API and CSV upload, matched to historical weather
   (`backend/app/importers`, `backend/app/weather`). See [Data import](#data-import).
3. **Prediction model**: forecast tomorrow's load and solar per half hour, with
   a likely range and recent accuracy (`backend/app/forecasting`). See
   [Prediction model](#prediction-model).
4. **Planner** (this step): turn the forecast into an overnight charge target, review past
   charges and chart the last week (`backend/app/planner`). See [Planner](#planner).

## Running locally

Backend (FastAPI, Python 3.12+), on http://localhost:8000 with API docs at `/docs`:

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python setup_env.py   # writes backend/.env, see Configuration
uvicorn app.main:app --reload
pytest
```

Frontend (Angular 22; Node 22.22.3+, 24.15+ or 26+), on http://localhost:4200. The dev server
proxies `/api` to the backend:

```bash
cd frontend
npm install
npm start
npm test
```

## Configuration

Backend settings are read from environment variables (or `backend/.env`) with the `SBP_`
prefix.

### Generating `backend/.env`

`backend/setup_env.py` walks through every setting below, explains it, suggests a value and
generates the two secrets with Python's `secrets` module. It uses only the standard library, so
it runs before `pip install`:

```bash
cd backend
python setup_env.py              # ask about each setting (Enter accepts the suggestion)
python setup_env.py --advanced   # also ask about the token algorithm and API addresses
python setup_env.py --defaults   # accept every suggestion, just generate fresh secrets
python setup_env.py --print      # show the result instead of writing it
```

If `backend/.env` already exists it asks before overwriting (`--force` skips the question) and
offers the current values as defaults, so re-running keeps your secrets. Typing `new` at a
secret generates a fresh one; replacing `SBP_CREDENTIALS_SECRET` asks again, because saved
Fox ESS keys can't be decrypted afterwards. The file is written readable by its owner only.
The backend refuses to start if `.env` contains keys it doesn't know, so any such lines are
carried over commented out. `tests/test_setup_env.py` fails if a setting is added to
`app/config.py` without adding it to the script.

### Settings

| Variable | Default | Notes |
| --- | --- | --- |
| `SBP_DATABASE_URL` | `sqlite:///./solar.db` | Any SQLAlchemy URL, e.g. Postgres |
| `SBP_JWT_SECRET` | dev-only value | **Must** be set to a long random string in production |
| `SBP_JWT_ALGORITHM` | `HS256` | |
| `SBP_ACCESS_TOKEN_MINUTES` | `10080` (7 days) | |
| `SBP_CORS_ORIGINS` | `["http://localhost:4200"]` | JSON list |
| `SBP_CREDENTIALS_SECRET` | dev-only value | Encrypts stored Fox ESS API keys. **Must** be set in production and kept stable |
| `SBP_FOX_ESS_BASE_URL` | `https://www.foxesscloud.com` | |
| `SBP_AUTO_APPLY` | `false` | Run the nightly automatic apply of inverter control in this process. Set on one process only, or run `python -m app.control.nightly` from cron instead |
| `SBP_OPEN_METEO_ARCHIVE_URL` / `SBP_OPEN_METEO_RECENT_URL` / `SBP_OPEN_METEO_FORECAST_URL` | Open-Meteo public APIs | Point at a paid/self-hosted instance for commercial use |

SQLite is the default and is fine for development or a single small server. For a multi-user
deployment use Postgres (`SBP_DATABASE_URL=postgresql+psycopg://…` and `pip install psycopg`).

Tables are created on startup. Migrations (Alembic) will be added once the schema starts
changing with real data in it.

## Deploying on Ubuntu

A single-server setup: uvicorn behind nginx under systemd, Postgres for the database, and nginx
serving the built frontend with `/api` passed to the backend, so browser and API share one
origin. Ubuntu 24.04 ships Python 3.12, which is enough; the paths and the `solar` user below are
examples.

1. **User, code and packages.**

   ```bash
   sudo apt install python3-venv postgresql nginx
   sudo useradd --system --create-home --home-dir /opt/solar-battery-planner solar
   sudo -u solar git clone https://github.com/hughe198/solar-battery-planner /opt/solar-battery-planner/app
   cd /opt/solar-battery-planner/app/backend
   sudo -u solar python3 -m venv .venv
   sudo -u solar .venv/bin/pip install -r requirements.txt "psycopg[binary]"
   ```

2. **Database.**

   ```bash
   sudo -u postgres createuser solar
   sudo -u postgres createdb -O solar solar
   ```

   With the app running as the `solar` Unix user, Postgres's default peer login needs no
   password: the URL is `postgresql+psycopg://solar@/solar`.

3. **Settings.** Run the setup script as the service user so it owns `backend/.env` (mode 600):

   ```bash
   sudo -u solar python3 setup_env.py
   ```

   Use the Postgres URL above, set the allowed origins to `https://your-domain`, and leave
   auto-apply off (see step 5). Copy `SBP_CREDENTIALS_SECRET` into a password manager: without
   it, saved Fox ESS keys can't be decrypted after a rebuild. The backend reads `.env` from the
   directory it is started in, which is why the unit below sets `WorkingDirectory`. Settings
   given as real environment variables (for example a systemd `EnvironmentFile=`) win over
   `.env`.

4. **Backend service.** `/etc/systemd/system/solar-battery-planner.service`:

   ```ini
   [Unit]
   Description=Solar Battery Planner API
   After=network-online.target postgresql.service

   [Service]
   User=solar
   WorkingDirectory=/opt/solar-battery-planner/app/backend
   ExecStart=/opt/solar-battery-planner/app/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 2 --proxy-headers
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```

   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now solar-battery-planner
   journalctl -u solar-battery-planner -f
   ```

5. **Nightly auto-apply.** With more than one worker, keep `SBP_AUTO_APPLY=false`: each worker
   would run its own loop. Run the pass from cron instead, every five minutes like the built-in
   loop (`sudo crontab -u solar -e`):

   ```cron
   */5 * * * * cd /opt/solar-battery-planner/app/backend && .venv/bin/python -m app.control.nightly
   ```

6. **Frontend.** Build it (Node 22.22.3+, 24.15+ or 26+; on another machine is fine) and copy
   `dist/frontend/browser` to the server:

   ```bash
   cd frontend && npm ci && npx ng build
   rsync -a dist/frontend/browser/ server:/var/www/solar-battery-planner/
   ```

7. **nginx.** `/etc/nginx/sites-available/solar-battery-planner`, then link it into
   `sites-enabled`, `sudo nginx -t && sudo systemctl reload nginx`, and add HTTPS with
   `sudo certbot --nginx -d your-domain` (package `python3-certbot-nginx`):

   ```nginx
   server {
       listen 80;
       server_name your-domain;
       root /var/www/solar-battery-planner;

       location /api/ {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }

       # Angular routes such as /plan are handled in the browser.
       location / {
           try_files $uri $uri/ /index.html;
       }
   }
   ```

   Leave `/docs` unproxied as above unless you want the API docs public.

**Updating:** `git pull`, then `.venv/bin/pip install -r requirements.txt` and
`sudo systemctl restart solar-battery-planner`, then rebuild and copy the frontend. Tables are
created on startup. Back up the database (`sudo -u solar pg_dump solar > backup.sql`) and
`backend/.env` together.

## Tariffs

A tariff is a list of rate bands for import and another for export, each with a start and end
in local clock time (on the hour or half hour), a rate in p/kWh and whether it applies every day,
Monday to Friday or at weekends. A band can cross midnight, and start = end means all day. For
each direction and each kind of day the bands must cover all 24 hours exactly once; the API
rejects gaps and overlaps with a plain message (e.g. "Import has no rate for 02:00-05:00"), and
the setup form shows the same check and a 24-hour strip of the rates as you type. There is also
a standing charge in p/day. Presets (single rate, Economy 7, Octopus Go, Intelligent Go, Flux,
Cosy) come from `GET /api/system/tariff-presets` and fill in typical times and example prices
for the user to correct. Dynamic tariffs such as Agile, and bank-holiday rates, aren't modelled.

Code that prices energy should use `app.tariffs.TariffSchedule` rather than the bands:

```python
from app.tariffs import TariffSchedule

schedule = TariffSchedule.from_system(system)
schedule.rate_at(interval.start, "import")   # p/kWh; naive datetimes are UTC
schedule.day_rates(day, "export")            # [(utc_start, p/kWh)] for each half hour of a local day
schedule.standing_charge_pence_per_day
```

Changing the rates keeps the old tariff in `past_tariffs`, with the day the new rates began (today
unless given; `GET /api/system/tariff-history` lists them). Plans use the current tariff.
Anything about past days uses `TariffTimeline`, which has the same `rate_at`, so they're priced at
what they really cost. Choosing a day before the current tariff began replaces whatever applied
from then on, which is how to correct a mistake. Past tariffs can't be recovered for changes made
before this existed, so those days are priced at the tariff that was current when tracking began.

Databases from step 1 (one cheap window) are converted to bands on startup
(`app/migrations.py`).

## Data import

Usage is stored per half hour (UTC) and weather per hour, both against the user's system.
The Data page (`/data`) and `/api/import/*` endpoints cover:

- **Fox ESS Open API.** Each user creates an API key in Fox ESS Cloud (User Profile → API
  Management) and enters it with their inverter serial. The key is checked against the device
  list, then stored encrypted. Imports run in the background, one history call per local day
  (5-minute power samples summed into half-hour kWh, plus end-of-interval SoC), newest day
  first, paced at about one call a second. Fox allows 1440 calls per inverter per day, shared
  with any other app the user runs, so days already stored are skipped and one import is
  capped at 366 days. Only read endpoints are called.
- **CSV upload** with `start, load_kwh, solar_kwh, grid_import_kwh, grid_export_kwh` and optional
  `battery_soc_percent`. Naive times are read in the system's time zone.
- **Weather** from Open-Meteo at the system's coordinates: the ERA5 archive for older days and
  the historical-forecast API for the last week (the archive lags by several days). Radiation is
  re-keyed to the start of the hour it averages over, so each half hour joins to the hour it
  sits in. Moving the system replaces its weather with the new location's straight away, in the
  background (the next import does it if that fails). Fox ESS isn't called: usage was measured at
  the inverter, so it doesn't change.
- **Deletion.** `DELETE /api/import/data` removes all imported data and the Fox key;
  `DELETE /api/auth/me` deletes the account and everything with it.

`GET /api/import/intervals?start=&end=` returns half hours joined to their weather, which is
what the prediction model in step 3 will train on.

## Prediction model

Plain statistics with numpy, no machine learning: each fit is a few small least-squares solves,
so the model is retrained on every request and always reflects the latest import. It predicts
household **usage** and **solar generation**, not grid import/export, because those depend on
how the battery was charged; the planner (step 4) simulates the battery on top of the forecast.

- **Solar.** A physics estimate per array: sun position (NOAA approximations), Open-Meteo's
  global and diffuse irradiance transposed onto the panel's tilt and azimuth (isotropic sky),
  and a cell-temperature derating. Hourly radiation is split into half hours by sun height. A
  ridge regression then learns one multiplier per array from the user's history (shading,
  inverter losses, soiling, wrong kWp), shrunk towards a standard 85% performance ratio.
- **Usage.** A weighted regression with one value per local half hour for weekdays and another
  for weekends, plus heating-degree terms (below 15.5 °C daily mean) for four blocks of the day.
  Recent days weigh more (60-day half-life), and it is shrunk towards a typical UK home
  (Ofgem 2,700 kWh/year, Elexon profile shape) scaled to the user's average.
- **Thin history.** Both priors are worth three days of data, so with no history the forecast is
  the typical home and panel physics, and the user's own data takes over within a couple of
  weeks. The response says which (`learned`, `blended`, `typical`, `physics`).
- **Accuracy and ranges.** With three weeks of complete days, the last quarter (7–28 days) is
  held out, predicted from a model trained on the earlier days with the weather that actually
  happened, and compared with what was measured; physics-only solar is shown alongside. The
  10th–90th percentile errors give the ranges: per four-hour block for usage, as ratios for
  solar. Solar ranges are then widened (×1.5, with a floor) because tomorrow's weather is
  itself a forecast. Logging forecasts to calibrate that allowance is a later improvement.

`GET /api/forecast?day=YYYY-MM-DD` (today to 7 days ahead, default tomorrow) returns the
half-hourly forecast, daily totals with ranges, the method used, the learned array multipliers
and the backtest. `GET /api/forecast/accuracy` returns just the backtest and needs no weather
fetch. The Forecast page (`/forecast`) shows both.

## Planner

The planner (`backend/app/planner`) picks the overnight charge by simulation, not a formula.

- **Cycle.** The charge window that ends on the planned day, then the day until the next window
  opens. The window is read from the tariff: the longest run of the cheapest import rate between
  18:00 and noon (none on a single-rate tariff, so no grid charge). Overnight the battery charges to the target and holds while the house runs on cheap
  grid power; by day the inverter is in self-use (solar feeds the house, surplus fills the
  battery then exports, shortfalls come from the battery down to its minimum charge, then the
  grid). Charge and discharge rates, minimum charge and round-trip losses come from the system.
- **Forecast span.** The forecast behind a plan covers the whole cycle, including window hours
  before midnight and usage after the next midnight, so a 02:00 window plans through to 02:00.
- **Starting level.** Unless one is entered, the plan starts from the latest imported battery
  level, simulated in self-use up to the window opening (`app/planner/lead_in.py`). A reading from
  before the previous charge window closed, or after this one opened, isn't used, and the plan
  then assumes the battery's minimum charge. The response says which (`start_soc_source`), and
  nightly auto-apply uses the same rule, so import recent data for it to apply.
- **Candidates and scenarios.** Every whole-percent target from tonight's starting level to the
  most the window can reach is simulated half-hourly, all at once as one numpy array, on the
  expected forecast, a dull day (usage high, solar low, from the forecast ranges) and a bright
  day. The score is the weighted cost (50/25/25), crediting energy left in the battery at the
  price of recharging it.
- **Modes.** *Own usage* scores import cost only, so it never buys grid energy just to push your
  own solar out. *Export revenue* subtracts export income as well.
- **Over-charging** is caught by the simulation: grid energy bought overnight that leaves the
  battery full by mid-morning forces later solar out at the export rate (or for nothing, in own
  usage mode). The planner takes the lowest target within 2p of the best average, so it leans
  under when extra charge only pays off on a dull day, and explains its choice in plain words.
- **Prices** for every half hour come from `TariffSchedule.rate_at`, so any number of import
  and export bands, weekday or weekend, are priced as they are.
- **Force charge timing.** An inverter in forced charge keeps drawing from the grid until its
  schedule ends, so each recommendation comes with a start and stop time. The charge is timed to
  finish as the charge window closes. The time it takes uses the charge rate learned from recent
  grid-charging half hours, with a slower rate above 90% if history shows one. Without that
  history it uses the battery's charge rate, which is read from the Fox ESS schedule on connect
  when one is set. From the window opening until the charge starts the battery is held, so the
  house runs on the cheap (or free) grid instead of spending stored energy that would only be
  bought back with losses. With no charge needed, the whole window is a hold. Fox ESS has no
  grid-only mode, so the hold is written as a forced charge to the lowest level the inverter
  accepts: a forced charge stops at its level and the rest of the period runs the house from the
  grid. An owner has seen this on a charge to 90%; a hold from above the level hasn't been watched
  yet. It can be set by hand in the Fox app, and the Forecast page says how.
- **Discharge before a free window.** When the charge window is free (import at 0p or less)
  and export pays, whatever is above the battery's reserve is force discharged (exported) in up
  to 90 minutes ending as the window opens, since the window refills it for nothing. The charge
  is then planned from the reserve. On Fox ESS this is a ForceDischarge period stopping at the
  minimum charge, or the lowest level the inverter accepts.
- **Battery wear.** An optional wear cost per full cycle (battery price ÷ rated cycles) is
  charged on every kWh stored, overnight or from solar, so grid charging and the pre-window
  discharge only happen when they pay after wear. Each recommendation also lists its downsides:
  cycles, hours near full, and the cost if the day turns out dull.
- **Replay.** Past days with imported battery level are replayed from the charge that was
  actually made. Simulated import, export and battery level are compared with the meter to check
  the simulator, and every other target is simulated on the same day to find the best charge
  with hindsight and what the actual decision cost. The latest day becomes a plain-language
  review.

`GET /api/plan?day=&start_soc_percent=` returns a recommendation per mode (the system's mode
first) with outcomes for each scenario, reasons, the cost of every candidate and the expected
half-hourly timeline. `GET /api/plan/replay?days=30` returns the replay and review,
`GET /api/plan/replay/YYYY-MM-DD` one day in full for the calendar, and
`GET /api/history?days=7` the metered half hours and daily totals for the home page charts.
The Charge plan page (`/plan`) and home page show them.

## Inverter control (opt-in)

Users can let the planner write the next overnight charge to their Fox ESS inverter. It is off
by default and needs explicit consent on the **Inverter** page (`/inverter`, API `/api/control`).

- Writes go to the inverter's mode schedule (Open API v1 scheduler: `/op/v1/device/scheduler/get`,
  `/enable` and `/set/flag`) as a `ForceCharge` period from the recommendation's force charge
  start to stop, with `fdSoc` = target %, `fdPwr` = the battery's charge power and
  `minSocOnGrid` = its minimum charge, clamped to the ranges the inverter reports. Periods that
  cross midnight are split; the user's other periods are kept as Fox returned them.
- Preview shows exactly what would be sent. Apply recomputes it and refuses if it differs from
  the preview. Every write is read back and compared, the schedule from before the first write is
  saved for **Restore**, and every action (with what was sent and read back) goes in an audit log.
- A six-step write test must reach step 4 before Apply unlocks, and auto-apply also needs one good
  manual apply. See [the test protocol](docs/inverter-control-test-protocol.md).
- Auto-apply (`app/control/nightly.py`) runs each system once a night, from two hours before the
  charge window opens.

None of this has been tested against a live inverter yet.
