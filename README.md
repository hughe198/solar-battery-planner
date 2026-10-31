# Solar Battery Planner

Recommends how much to charge a home battery overnight on a cheap tariff, based on predicted
household usage and solar generation for the next day. Two planning modes:

- **Maximise own usage** (default): charge enough to cover the house until solar takes over.
- **Maximise export revenue**: charge less and leave headroom so more solar is exported.

## Roadmap

1. **Accounts and system setup** (this step): sign up, log in, describe your battery, solar
   arrays, location and tariff.
2. **Data import**: Fox ESS cloud API and CSV upload, matched to historical weather
   (`backend/app/importers`).
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

Tables are created on startup. Migrations (Alembic) will be added once the schema starts
changing with real data in it.
