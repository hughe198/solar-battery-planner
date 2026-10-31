# Solar Battery Planner

Recommends how much to charge a home battery overnight on a cheap tariff, based on predicted
household usage and solar generation for the next day. Two planning modes:

- **Maximise own usage** (default): charge enough to cover the house until solar takes over.
- **Maximise export revenue**: charge less and leave headroom so more solar is exported.

## Roadmap

1. **Accounts and system setup**: sign up, log in, describe your battery, solar arrays,
   location and tariff.
2. **Data import** (this step): Fox ESS cloud API and CSV upload, matched to historical weather
   (`backend/app/importers`, `backend/app/weather`). See [Data import](#data-import).
3. **Prediction model**: forecast tomorrow's load and solar per interval
   (`backend/app/forecasting`).
4. **Planner page**: turn the forecast into an overnight charge target (`backend/app/planner`).

## Running locally

Backend (FastAPI, Python 3.12+), on http://localhost:8000 with API docs at `/docs`:

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest
```

Frontend (Angular 22, Node 22+), on http://localhost:4200. The dev server proxies `/api` to
the backend:

```bash
cd frontend
npm install
npm start
npm test
```

## Configuration

Backend settings are read from environment variables (or `backend/.env`) with the `SBP_`
prefix:

| Variable | Default | Notes |
| --- | --- | --- |
| `SBP_DATABASE_URL` | `sqlite:///./solar.db` | Any SQLAlchemy URL, e.g. Postgres |
| `SBP_JWT_SECRET` | dev-only value | **Must** be set to a long random string in production |
| `SBP_ACCESS_TOKEN_MINUTES` | `10080` (7 days) | |
| `SBP_CORS_ORIGINS` | `["http://localhost:4200"]` | JSON list |
| `SBP_CREDENTIALS_SECRET` | dev-only value | Encrypts stored Fox ESS API keys. **Must** be set in production and kept stable |
| `SBP_FOX_ESS_BASE_URL` | `https://www.foxesscloud.com` | |
| `SBP_OPEN_METEO_ARCHIVE_URL` / `SBP_OPEN_METEO_RECENT_URL` | Open-Meteo public APIs | Point at a paid/self-hosted instance for commercial use |

SQLite is the default and is fine for development or a single small server. For a multi-user
deployment use Postgres (`SBP_DATABASE_URL=postgresql+psycopg://…` and `pip install psycopg`).

Tables are created on startup. Migrations (Alembic) will be added once the schema starts
changing with real data in it.

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
  sits in. Moving the system clears weather so it is refetched for the new location.
- **Deletion.** `DELETE /api/import/data` removes all imported data and the Fox key;
  `DELETE /api/auth/me` deletes the account and everything with it.

`GET /api/import/intervals?start=&end=` returns half hours joined to their weather, which is
what the prediction model in step 3 will train on.
