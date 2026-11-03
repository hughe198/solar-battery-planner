from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import migrations, models  # noqa: F401  (registers tables)
from app.api import auth, control, forecast, history, imports, plan, system
from app.config import settings
from app.control import nightly
from app.database import Base, SessionLocal, engine, get_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    migrations.upgrade(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        models.ImportJob.fail_interrupted(db)
    stop = nightly.start_loop(SessionLocal) if settings.auto_apply else None
    yield
    if stop is not None:
        stop.set()


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
app.include_router(control.router)


@app.get("/api/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    """Up and able to reach the database. deploy/remote.sh rolls back a release that fails this."""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"status": "ok"}
