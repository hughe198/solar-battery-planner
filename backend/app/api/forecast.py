from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.imports import get_weather_client
from app.database import get_db
from app.forecasting.base import DayForecast
from app.forecasting.service import HistoryForecaster, backtest, load_history
from app.models import System, User
from app.schemas import AccuracyOut, DayForecastOut
from app.security import current_user
from app.weather.open_meteo import OpenMeteoClient, WeatherError

router = APIRouter(prefix="/api/forecast", tags=["forecast"])

# Open-Meteo forecasts 16 days, but cloud forecasts beyond a few days are little use here.
MAX_DAYS_AHEAD = 7


def _system(user: User) -> System:
    if user.system is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Set up your system first")
    return user.system


@router.get("", response_model=DayForecastOut)
def forecast_day(
    day: date | None = Query(default=None, description="Local day; defaults to tomorrow"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    weather_client: OpenMeteoClient = Depends(get_weather_client),
) -> DayForecast:
    """Predicted half-hourly load and solar for a day, with ranges and recent accuracy."""
    system = _system(user)
    today = datetime.now(ZoneInfo(system.timezone)).date()
    day = day or today + timedelta(days=1)
    if not today <= day <= today + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Forecasts are available from today to {MAX_DAYS_AHEAD} days ahead",
        )
    try:
        return HistoryForecaster(db, weather_client).forecast(system, day)
    except WeatherError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))


@router.get("/accuracy", response_model=AccuracyOut)
def accuracy(user: User = Depends(current_user), db: Session = Depends(get_db)) -> AccuracyOut:
    """How well the model predicted the most recent imported days. Needs no weather fetch."""
    system = _system(user)
    history = load_history(db, system)
    checked = backtest(system, history)
    return AccuracyOut.model_validate(
        {
            "history_days": len(set(history.calendar.local_day)),
            "backtest": checked[0] if checked else None,
        },
        from_attributes=True,
    )
