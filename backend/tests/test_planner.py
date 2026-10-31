from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pytest

from app.forecasting.base import DayForecast, IntervalForecast, Range
from app.forecasting.service import day_half_hours
from app.importers.base import INTERVAL
from app.models import (
    Battery,
    PlanningMode,
    RateDays,
    RateDirection,
    System,
    Tariff,
    TariffBand,
)
from app.planner.optimiser import best_index, candidate_targets, plan
from app.planner.simulator import BatterySpec, CyclePrices, costs, cycle_for, simulate
from app.tariffs import TariffSchedule

DAY = date(2026, 6, 2)


def band(direction, start, end, rate):
    return TariffBand(
        direction=RateDirection(direction),
        days=RateDays.all,
        start=time.fromisoformat(start),
        end=time.fromisoformat(end),
        rate=rate,
    )


def schedule(*bands, tz="UTC"):
    return TariffSchedule.from_bands(bands, tz)


def make_system(export_rate=4.0, mode=PlanningMode.maximise_own_usage, tz="UTC") -> System:
    return System(
        name="Test",
        latitude=51.5,
        longitude=0,
        timezone=tz,
        planning_mode=mode,
        battery=Battery(
            capacity_kwh=10,
            min_soc_percent=10,
            max_charge_kw=5,
            max_discharge_kw=5,
            round_trip_efficiency_percent=100,
        ),
        tariff=Tariff(
            name="Test",
            standing_charge_pence_per_day=0,
            bands=[
                band("import", "23:30", "05:30", 7.5),
                band("import", "05:30", "23:30", 30),
                band("export", "00:00", "00:00", export_rate),
            ],
        ),
        arrays=[],
    )


def make_forecast(load, solar, tz="UTC", spread=0.0) -> DayForecast:
    """`load` and `solar` give kWh for a half-hour slot number (0 = local midnight)."""
    intervals = [
        IntervalForecast(start=s, end=s + INTERVAL, load_kwh=load(k), solar_kwh=solar(k))
        for k, s in enumerate(day_half_hours(DAY, tz))
    ]
    total_load = sum(i.load_kwh for i in intervals)
    total_solar = sum(i.solar_kwh for i in intervals)
    return DayForecast(
        day=DAY,
        intervals=intervals,
        load=Range(total_load, total_load * (1 - spread), total_load * (1 + spread)),
        solar=Range(total_solar, total_solar * (1 - spread), total_solar * (1 + spread)),
        load_method="learned",
        solar_method="learned",
        history_days=60,
        array_performance=[],
        backtest=None,
    )


def evening_peak(k):
    return 0.6 if 34 <= k < 44 else 0.25


def no_sun(k):
    return 0.0


def bright(k):
    return 1.5 if 16 <= k < 36 else 0.0  # 15 kWh, 08:00 to 18:00


def test_cycle_crossing_midnight_handles_clock_change():
    tariff = schedule(
        band("import", "23:30", "05:30", 7.5),
        band("import", "05:30", "23:30", 30),
        band("export", "00:00", "00:00", 4),
        tz="Europe/London",
    )
    cycle = cycle_for(date(2026, 3, 29), tariff, "Europe/London")  # clocks go forward at 01:00
    assert cycle.night_start == datetime(2026, 3, 28, 23, 30, tzinfo=timezone.utc)
    assert cycle.night_end == datetime(2026, 3, 29, 4, 30, tzinfo=timezone.utc)
    assert len(cycle.night_starts()) == 10  # five hours, not six
    assert cycle.day_end == datetime(2026, 3, 29, 22, 30, tzinfo=timezone.utc)


def test_cycle_after_midnight():
    tariff = schedule(
        band("import", "00:30", "04:30", 8.5),
        band("import", "04:30", "00:30", 26),
        band("export", "00:00", "00:00", 4),
    )
    cycle = cycle_for(DAY, tariff, "UTC")
    assert cycle.night_start == datetime(2026, 6, 2, 0, 30, tzinfo=timezone.utc)
    assert cycle.day_end == datetime(2026, 6, 3, 0, 30, tzinfo=timezone.utc)


def test_simulation_conserves_energy_with_losses():
    battery = BatterySpec.of(
        Battery(
            capacity_kwh=10,
            min_soc_percent=10,
            max_charge_kw=3,
            max_discharge_kw=3,
            round_trip_efficiency_percent=81,
        )
    )
    load = np.array([0.5] * 10 + [0.2] * 10)
    solar = np.array([0.0] * 10 + [2.0] * 10)
    sim = simulate(battery, 1.0, np.array([1.0, 5.0, 10.0]), np.zeros(4), np.zeros(4), load, solar)
    imported = sim.day_import_kwh.sum(axis=1)
    exported = sim.day_export_kwh.sum(axis=1)
    discharged = np.clip(-np.diff(sim.stored_kwh), 0, None).sum(axis=1) * 0.9
    charged = np.clip(np.diff(sim.stored_kwh), 0, None).sum(axis=1) / 0.9
    # Supply equals demand every interval, so it does over the day.
    np.testing.assert_allclose(solar.sum() + imported + discharged, load.sum() + exported + charged)
    assert imported[0] == pytest.approx(5.0)  # empty to start: the morning comes from the grid
    assert imported[2] == 0
    # The full battery can't take the solar surplus, so more of it is exported.
    assert exported[2] > exported[0]
    assert sim.stored_kwh.max() <= 10 + 1e-9
    assert sim.stored_kwh.min() >= 1 - 1e-9
    # 3 kW for half an hour, at most.
    assert np.diff(sim.stored_kwh).max() <= 1.5 * 0.9 + 1e-9


def test_candidates_run_from_start_to_what_the_window_can_reach():
    battery = BatterySpec.of(make_system().battery)
    targets = candidate_targets(battery, 1.25, 7.0)
    assert targets[0] == pytest.approx(1.25)
    assert targets[-1] == pytest.approx(7.0)
    assert np.all(np.diff(targets) > 0)


def test_lean_under_picks_lowest_within_tolerance():
    assert best_index(np.array([10.0, 5.0, 4.0, 3.5, 3.6])) == 1
    assert best_index(np.array([10.0, 5.0, 4.0, 3.5, 3.6]), tolerance=0) == 3


def test_dull_day_charges_to_cover_the_house():
    result = plan(make_system(), make_forecast(evening_peak, no_sun), start_soc_percent=10)
    own = result.recommendations[0]
    assert own.mode is PlanningMode.maximise_own_usage
    # Six hours of charging at 5 kW fills the battery; with no sun it should be used in full.
    assert result.max_soc_percent == 100
    assert own.target_soc_percent == 100
    assert own.charge_kwh == pytest.approx(9.0)
    assert own.expected.reserve_at is not None
    assert own.reasons[0].startswith("Charge to 100%")
    # 9 kWh at 5 kW: 1 h 50 min (rounded to five minutes), finishing at 05:30.
    assert own.force_charge_stop == datetime(2026, 6, 2, 5, 30, tzinfo=timezone.utc)
    assert own.force_charge_minutes == 110
    assert own.force_charge_reaches_target
    assert own.reasons[1].startswith("Force charge from 03:40 to 05:30")
    # Held from the window opening (the evening before) until the charge starts.
    assert own.hold_start == datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
    assert own.hold_stop == own.force_charge_start
    assert "battery is held so the house runs on the cheap grid" in own.reasons[1]


def test_over_charging_on_a_sunny_day_is_penalised():
    """Low export rate: charging fills the battery that solar would have filled for free."""
    result = plan(make_system(export_rate=4), make_forecast(evening_peak, bright), 10)
    own, export = result.recommendations
    # 05:30 to 08:00 at 0.25 kWh a half hour is 1.25 kWh to cover before the sun is up.
    assert 20 <= own.target_soc_percent <= 30
    assert export.target_soc_percent == own.target_soc_percent
    assert own.expected.full_at is not None
    assert any("solar takes over" in r for r in own.reasons)
    full = next(c for c in result.candidates if c.target_soc_percent == 100)
    best = next(c for c in result.candidates if c.target_soc_percent == own.target_soc_percent)
    assert full.own_usage_pence > best.own_usage_pence + 20


def test_export_mode_fills_up_when_export_pays_more_than_charging():
    system = make_system(export_rate=15, mode=PlanningMode.maximise_export_revenue)
    result = plan(system, make_forecast(evening_peak, bright), 10)
    export, own = result.recommendations
    assert export.mode is PlanningMode.maximise_export_revenue
    assert export.target_soc_percent >= 95
    assert own.target_soc_percent < 40
    assert export.expected.export_revenue_pence > own.expected.export_revenue_pence
    assert any("Export pays 15p" in r for r in export.reasons)


def test_uncertainty_is_explained_and_timeline_follows_the_plan():
    result = plan(make_system(), make_forecast(evening_peak, bright, spread=0.4), 10)
    own = result.recommendations[0]
    assert own.dull.solar_kwh < own.expected.solar_kwh < own.bright.solar_kwh
    assert own.dull.net_cost_pence > own.bright.net_cost_pence
    assert any("dull" in r for r in own.reasons)
    cheap = [p for p in result.timeline if p.cheap]
    assert len(cheap) == 11  # 00:00 to 05:30; the 23:30 slot is on the evening before
    assert cheap[-1].soc_percent == pytest.approx(own.target_soc_percent, abs=0.5)
    assert cheap[0].soc_percent <= 10  # self-use until the forced charge starts
    assert cheap[-1].import_kwh > cheap[-1].load_kwh  # charging from the grid
    assert result.timeline[-1].start.hour == 23 and result.timeline[-1].start.minute == 0


def test_already_charged_battery_needs_nothing():
    result = plan(make_system(), make_forecast(evening_peak, bright), start_soc_percent=60)
    own = result.recommendations[0]
    assert own.target_soc_percent == 60
    assert own.charge_kwh == 0
    assert own.reasons[0].startswith("No overnight charge")


PEAK = schedule(
    band("import", "23:00", "05:00", 7.0),
    band("import", "05:00", "12:00", 25.0),
    band("import", "12:00", "16:00", 22.0),
    band("import", "16:00", "19:00", 40.0),
    band("import", "19:00", "23:00", 22.0),
    band("export", "16:00", "19:00", 25.0),
    band("export", "19:00", "16:00", 5.0),
)


def test_costs_follow_a_multi_rate_price_book():
    cycle = cycle_for(DAY, PEAK, "UTC")
    assert cycle.night_start == datetime(2026, 6, 1, 23, 0, tzinfo=timezone.utc)
    prices = CyclePrices.of(PEAK, cycle.night_starts(), cycle.day_starts())
    assert prices.charge == 7.0
    battery = BatterySpec.of(make_system().battery)
    day = len(cycle.day_starts())
    load = np.full(day, 0.5)
    sim = simulate(
        battery, 1.0, np.array([1.0]), np.full(12, 0.2), np.zeros(12), load, np.zeros(day)
    )
    c = costs(sim, battery, prices, 1.0)
    expected_day = sum(0.5 * PEAK.rate_at(s, "import") for s in cycle.day_starts())
    assert c.import_cost[0] == pytest.approx(12 * 0.2 * 7.0 + expected_day)


def test_charge_window_follows_the_rates():
    # Cosy-style: two cheap overnight runs; the longer one, nearer morning, is used.
    cosy = schedule(
        band("import", "04:00", "07:00", 13.5),
        band("import", "07:00", "13:00", 27),
        band("import", "13:00", "16:00", 13.5),
        band("import", "16:00", "19:00", 40),
        band("import", "19:00", "22:00", 27),
        band("import", "22:00", "00:00", 13.5),
        band("import", "00:00", "04:00", 27),
        band("export", "00:00", "00:00", 15),
    )
    cycle = cycle_for(DAY, cosy, "UTC")
    assert cycle.night_start == datetime(2026, 6, 2, 4, 0, tzinfo=timezone.utc)
    assert cycle.night_end == datetime(2026, 6, 2, 7, 0, tzinfo=timezone.utc)
    assert cycle.day_end == datetime(2026, 6, 3, 4, 0, tzinfo=timezone.utc)


def test_single_rate_tariff_has_no_charge_window():
    flat = schedule(band("import", "00:00", "00:00", 25), band("export", "00:00", "00:00", 15))
    cycle = cycle_for(DAY, flat, "UTC")
    assert cycle.window == timedelta(0)
    assert len(cycle.day_starts()) == 48


def free_window_system(export_rate=15.0):
    system = make_system(export_rate=export_rate)
    system.tariff.bands[0] = band("import", "23:30", "05:30", 0.0)
    return system


def test_battery_is_sold_before_a_free_window():
    result = plan(free_window_system(), make_forecast(evening_peak, bright), start_soc_percent=60)
    own = result.recommendations[0]
    # 5 kWh above the 10% reserve at 5 kW: an hour, ending as the free window opens.
    assert own.discharge_start == datetime(2026, 6, 1, 22, 30, tzinfo=timezone.utc)
    assert own.discharge_stop == datetime(2026, 6, 1, 23, 30, tzinfo=timezone.utc)
    assert own.discharge_kwh == pytest.approx(5.0)
    assert own.discharge_revenue_pence == pytest.approx(75.0)
    assert own.discharge_to_percent == pytest.approx(10.0)
    assert own.reasons[0].startswith("Before the free window, force discharge from 22:30")
    # The user's level is kept; the charge is planned from the drained battery.
    assert result.start_soc_percent == 60
    assert own.stored_kwh == pytest.approx(own.target_soc_percent / 10 - 1.0, abs=0.01)
    assert result.timeline[0].soc_percent <= 10.1


def test_no_discharge_when_the_window_costs_or_export_pays_nothing():
    forecast = make_forecast(evening_peak, bright)
    paid = plan(make_system(export_rate=15), forecast, start_soc_percent=60)
    unpaid = plan(free_window_system(export_rate=0), forecast, start_soc_percent=60)
    for result in (paid, unpaid):
        assert all(r.discharge_start is None for r in result.recommendations)
        assert result.recommendations[0].discharge_kwh == 0


def test_downsides_explain_wear_and_risk():
    free = plan(free_window_system(), make_forecast(evening_peak, bright), start_soc_percent=60)
    own, export = free.recommendations
    assert own.downsides[0].startswith("Battery wear: the discharge and refill put an extra 0.50")
    assert "per full cycle" in own.downsides[0]
    assert any("free window doesn't happen" in d for d in own.downsides)
    # Export mode fills up to sell solar: more cycling, and time near full.
    assert export.downsides[0].startswith("Charges")
    assert any("above 90%" in d for d in export.downsides)

    dull = plan(make_system(), make_forecast(evening_peak, bright, spread=0.4), 10)
    assert any(d.startswith("If the day turns out dull") for d in dull.recommendations[0].downsides)
