from datetime import datetime, timedelta, timezone

import pytest

from app.models import Battery, UsageRecord, UsageSource
from app.planner.charging import (
    ChargeRate,
    charge_hours,
    discharge,
    learn_rate,
    reachable_kwh,
    schedule,
)
from app.planner.simulator import BatterySpec

BATTERY = BatterySpec.of(
    Battery(
        capacity_kwh=10,
        min_soc_percent=10,
        max_charge_kw=5,
        max_discharge_kw=5,
        round_trip_efficiency_percent=100,
    )
)
START = datetime(2024, 6, 1, 23, 30)
UTC_START = START.replace(tzinfo=timezone.utc)


def record(k, soc, grid_import, load=0.2):
    return UsageRecord(
        start=START + timedelta(minutes=30 * k),
        load_kwh=load,
        solar_kwh=0.0,
        grid_import_kwh=grid_import,
        grid_export_kwh=0.0,
        battery_soc_percent=soc,
        source=UsageSource.csv,
    )


def test_learns_bulk_and_taper_rates_from_grid_charging():
    # 3 kW into a 10 kWh battery is 15 points a half hour, slowing to 0.5 kW above 90%.
    socs = [20, 35, 50, 65, 80, 90, 92.5, 95, 97.5, 99]
    records = [record(0, 20, 0.2)]
    for k, (before, after) in enumerate(zip(socs, socs[1:]), start=1):
        records.append(record(k, after, 0.2 + (after - before) / 10))
    # A gap in the data and a half hour with no grid charge are ignored.
    records.append(record(20, 50, 0.2))
    rate = learn_rate(BATTERY, records)
    assert rate.bulk_kw == pytest.approx(3.0)
    assert rate.taper_kw == pytest.approx(0.5)
    assert rate.samples == 8  # the half hour reaching 99% may have stopped part way


def test_falls_back_to_the_battery_settings():
    rate = learn_rate(BATTERY, [record(0, 50, 0.2), record(1, 50, 0.2)])
    assert rate == ChargeRate(5.0, 5.0, 0)


def test_charge_hours_slow_down_near_full():
    rate = ChargeRate(4.0, 1.0, 10)
    assert charge_hours(rate, BATTERY, 1.0, 9.0) == pytest.approx(2.0)
    assert charge_hours(rate, BATTERY, 1.0, 10.0) == pytest.approx(2.0 + 1.0)
    assert charge_hours(rate, BATTERY, 9.5, 10.0) == pytest.approx(0.5)
    assert reachable_kwh(rate, BATTERY, 1.0, timedelta(hours=2.5)) == pytest.approx(9.5)


def test_schedule_holds_then_finishes_as_the_window_closes():
    rate = ChargeRate(4.0, 4.0, 10)
    end = UTC_START + timedelta(hours=6)
    plan = schedule(rate, BATTERY, 5.0, 9.0, UTC_START, end)
    assert plan.stop == end
    assert plan.start == end - timedelta(hours=1)
    assert plan.minutes == 60
    assert plan.reaches_target
    # The battery is held, with the house on the grid, until the charge starts.
    assert plan.hold_start == UTC_START
    assert plan.hold_stop == plan.start
    assert plan.hold_minutes == 300

    # Starts are rounded to five minutes earlier.
    odd = schedule(rate, BATTERY, 5.0, 8.9, UTC_START, end)
    assert odd.start.minute % 5 == 0
    assert odd.start < end - timedelta(hours=0.975)


def test_schedule_when_the_target_needs_the_whole_window():
    rate = ChargeRate(1.0, 1.0, 10)
    end = UTC_START + timedelta(hours=2)
    plan = schedule(rate, BATTERY, 1.0, 9.0, UTC_START, end)
    assert plan.start == UTC_START
    assert plan.stop == end
    assert plan.hold_minutes == 0
    assert not plan.reaches_target


def test_no_charge_holds_the_whole_window():
    end = UTC_START + timedelta(hours=6)
    plan = schedule(ChargeRate(4, 4, 0), BATTERY, 5.0, 5.0, UTC_START, end)
    assert plan.minutes == 0
    assert (plan.hold_start, plan.hold_stop) == (UTC_START, end)


def test_no_window_no_schedule():
    plan = schedule(ChargeRate(4, 4, 0), BATTERY, 5.0, 9.0, UTC_START, UTC_START)
    assert plan.minutes == 0 and plan.hold_minutes == 0


class FlatExport:
    def __init__(self, pence):
        self.pence = pence

    def rate_at(self, when, direction):
        return self.pence if direction == "export" else 0.0


def test_discharge_is_capped_and_ends_as_the_window_opens():
    # 9 kWh above the reserve at 5 kW would take 1 h 48 min; capped at 90 minutes.
    drain = discharge(BATTERY, 10.0, UTC_START, FlatExport(10))
    assert drain.stop == UTC_START
    assert drain.minutes == 90
    assert drain.export_kwh == pytest.approx(7.5)
    assert drain.revenue_pence == pytest.approx(75.0)


def test_no_discharge_when_export_pays_nothing_or_battery_is_low():
    assert discharge(BATTERY, 10.0, UTC_START, FlatExport(0)) is None
    assert discharge(BATTERY, 1.05, UTC_START, FlatExport(10)) is None
