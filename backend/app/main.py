from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import migrations, models  # noqa: F401  (registers tables)
from app.api import auth, forecast, history, imports, plan, system
from app.config import settings
from app.database import Base, SessionLocal, engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    migrations.upgrade(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        models.ImportJob.fail_interrupted(db)
    yield


app = FastAPI(title="Solar Battery Planner", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(system.router)
app.include_router(imports.router)
app.include_router(forecast.router)
app.include_router(plan.router)
app.include_router(history.router)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
