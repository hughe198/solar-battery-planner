from datetime import date

from sqlalchemy import select

from app.database import get_db
from app.forecasting.look_back import (
    ChargeHindsight,
    DayActual,
    DayForecastKept,
    daylight_cloud,
    look_back,
)
from app.main import app
from app.models import ForecastSnapshot
from tests.test_forecast_api import headers, tomorrow, upload_days, weather  # noqa: F401

DAY = date(2026, 11, 8)


def actual(solar=11.2, load=7.1, cloud=30.0, temp=11.0, export=2.1, complete=True, panels=True):
    return DayActual(
        day=DAY,
        complete=complete,
        load_kwh=load,
        solar_kwh=solar,
        import_kwh=2.0,
        export_kwh=export,
        import_cost_pence=95.0,
        export_revenue_pence=31.0,
        grid_only_pence=184.0,
        cloud_percent=cloud,
        temperature_c=temp,
        has_panels=panels,
    )


KEPT = DayForecastKept(
    load_kwh=7.4, solar_kwh=7.5, cloud_percent=70.0, temperature_c=10.0, target_soc_percent=70
)


def test_sunnier_than_forecast_with_a_charge_that_was_too_high():
    story = look_back(actual(), KEPT, 7.0, ChargeHindsight(70, 55, 18))
    assert story.basis == "forecast"
    assert story.headline == "Sunnier than forecast"
    assert story.detail == "30% cloud, not 70%"
    assert story.weather_headline == "Sunny spells, 11°C"
    assert story.summary == (
        "It was sunnier than forecast, so solar made 11.2 kWh against 7.5, "
        "and a lower charge would have saved about 18p."
    )
    assert story.points[0] == (
        "Forecast the night before: 7.5 kWh of solar with 70% cloud and 10°C. "
        "It turned out 11.2 kWh, with 30% cloud and 11°C."
    )
    assert "2.1 kWh went to export." in story.points
    assert "Usage was 7.1 kWh against 7.4 forecast." in story.points
    assert story.points[-1] == "It cost 64p instead of £1.84 on grid import alone, saving £1.20."
    assert story.saved_pence == 120


def test_as_forecast_and_about_right():
    story = look_back(actual(solar=7.8, cloud=65), KEPT, None, ChargeHindsight(70, 68, 2))
    assert story.headline == "As forecast"
    assert story.detail == "65% cloud"
    assert story.summary.startswith("The sun was about as forecast: solar made 7.8 kWh")
    assert story.summary.endswith(", and the charge was about right.")


def test_duller_and_colder_than_forecast():
    dull = actual(solar=3.0, cloud=95, temp=6.0)
    story = look_back(dull, KEPT, None, ChargeHindsight(70, 90, 22))
    assert story.headline == "Duller than forecast"
    assert "a higher charge would have saved about 22p" in story.summary
    assert "Usage was 7.1 kWh against 7.4 forecast; it was colder than forecast." in story.points


def test_without_a_kept_forecast_it_compares_with_recent_days():
    story = look_back(actual(), None, 7.0, None)
    assert story.basis == "recent"
    assert story.headline == "Sunnier than your recent days"
    assert story.summary == (
        "It was sunnier than your recent days, so solar made 11.2 kWh against a recent 7.0."
    )
    assert story.points[0].startswith("There's no forecast saved for that day")


def test_without_anything_to_compare_or_panels():
    story = look_back(actual(panels=False, export=0), None, None, None)
    assert story.basis == "none"
    assert story.summary == "You used 7.1 kWh."


def test_partial_day_says_so():
    story = look_back(actual(complete=False), KEPT, None, None)
    assert story.points[-1].startswith("Some readings for the day are still to come")


def test_daylight_cloud_uses_the_sunny_hours():
    assert daylight_cloud([90, 30, 40, 90], [0, 300, 200, 5]) == 35
    assert daylight_cloud([90, 70], [0, 0]) == 80
    assert daylight_cloud([None], [None]) is None


def _db():
    return next(app.dependency_overrides[get_db]())


def test_look_back_api(client, headers):  # noqa: F811
    upload_days(client, headers, date(2024, 5, 1), 10)
    url = "/api/history/look-back?day=2024-05-10"
    body = client.get(url, headers=headers).json()
    assert body["complete"] is True
    assert body["basis"] == "recent"
    assert body["headline"] == "A typical day"
    assert body["solar_kwh"] == 9.6
    assert body["weather_headline"] == "Cloudy, 12°C"
    assert body["forecast_solar_kwh"] is None

    db = _db()
    db.add(
        ForecastSnapshot(
            system_id=1,
            day=date(2024, 5, 10),
            load_kwh=9.0,
            load_low_kwh=8.0,
            load_high_kwh=10.0,
            solar_kwh=6.0,
            solar_low_kwh=4.0,
            solar_high_kwh=8.0,
            cloud_percent=95.0,
            temperature_c=12.0,
        )
    )
    db.commit()
    body = client.get(url, headers=headers).json()
    assert body["basis"] == "forecast"
    assert body["headline"] == "Sunnier than forecast"
    assert body["detail"] == "80% cloud, not 95%"
    assert body["forecast_solar_kwh"] == 6.0

    assert client.get("/api/history/look-back?day=2024-04-01", headers=headers).status_code == 404
    future = tomorrow().isoformat()
    assert client.get(f"/api/history/look-back?day={future}", headers=headers).status_code == 422


def test_making_a_plan_keeps_the_forecast(client, headers):  # noqa: F811
    resp = client.get("/api/plan", headers=headers)
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    snap = _db().scalar(select(ForecastSnapshot))
    assert snap is not None
    assert snap.day.isoformat() == plan["day"]
    assert snap.target_soc_percent == plan["recommendations"][0]["target_soc_percent"]
    assert snap.weather_headline
    # A level typed in to try something out doesn't replace the saved target.
    client.get(f"/api/plan?start_soc_percent=95&day={plan['day']}", headers=headers)
    db = _db()
    assert db.scalar(select(ForecastSnapshot)).target_soc_percent == snap.target_soc_percent
