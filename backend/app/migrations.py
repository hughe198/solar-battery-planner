"""Schema upgrades for databases created by earlier versions, run at startup before
`create_all`. Each step checks what is there, so running them twice does nothing."""

from datetime import time

from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session


def _half_hour(value) -> time:
    if isinstance(value, str):
        value = time.fromisoformat(value)
    return value.replace(minute=value.minute // 30 * 30, second=0, microsecond=0)


def _band(direction, start: time, end: time, rate: float):
    from app.models import RateDays, TariffBand

    return TariffBand(direction=direction, days=RateDays.all, start=start, end=end, rate=rate)


def upgrade_single_window_tariffs(engine: Engine) -> None:
    """Step 1 stored a day rate, one cheap window and one export rate as columns on
    `tariffs`. Rebuild those as rate bands: cheap rate inside the window, day rate outside
    it, the export rate all day. Times are rounded down to the half hour."""
    inspector = inspect(engine)
    if not inspector.has_table("tariffs"):
        return
    if "day_import_rate" not in {c["name"] for c in inspector.get_columns("tariffs")}:
        return

    from app.database import Base
    from app.models import RateDirection, Tariff, TariffBand

    with engine.begin() as conn:
        old = conn.execute(
            text(
                "SELECT system_id, day_import_rate, cheap_import_rate, export_rate, "
                "cheap_start, cheap_end FROM tariffs"
            )
        ).all()
        conn.execute(text("DROP TABLE tariffs"))
        Base.metadata.create_all(conn, tables=[Tariff.__table__, TariffBand.__table__])

        db = Session(bind=conn)
        for system_id, day, cheap, export, start, end in old:
            start, end = _half_hour(start), _half_hour(end)
            if start == end:
                bands = [_band(RateDirection.import_, time(0), time(0), day)]
            else:
                bands = [
                    _band(RateDirection.import_, start, end, cheap),
                    _band(RateDirection.import_, end, start, day),
                ]
            bands.append(_band(RateDirection.export, time(0), time(0), export))
            db.add(Tariff(system_id=system_id, name="My tariff", bands=bands))
        db.flush()


def add_battery_wear_cost(engine: Engine) -> None:
    """Battery wear cost per cycle, added after step 4; 0 (not counted) for existing systems."""
    inspector = inspect(engine)
    if not inspector.has_table("batteries"):
        return
    if "wear_pence_per_cycle" in {c["name"] for c in inspector.get_columns("batteries")}:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE batteries ADD COLUMN wear_pence_per_cycle FLOAT NOT NULL DEFAULT 0")
        )


def add_big_load_settings(engine: Engine) -> None:
    """Heat pump and electric car settings on systems; off for existing systems."""
    inspector = inspect(engine)
    if not inspector.has_table("systems"):
        return
    have = {c["name"] for c in inspector.get_columns("systems")}
    columns = {
        "has_heat_pump": "BOOLEAN NOT NULL DEFAULT FALSE",
        "has_electric_car": "BOOLEAN NOT NULL DEFAULT FALSE",
        "car_charger_kw": "FLOAT NOT NULL DEFAULT 7",
    }
    with engine.begin() as conn:
        for name, kind in columns.items():
            if name not in have:
                conn.execute(text(f"ALTER TABLE systems ADD COLUMN {name} {kind}"))


def add_whole_day_control(engine: Engine) -> None:
    """The grid charge maximum on batteries (100%, no limit, for existing systems), and the
    schedule the planner last wrote on inverter controls (unknown for existing ones)."""
    inspector = inspect(engine)
    steps = []
    if inspector.has_table("batteries"):
        if "max_grid_charge_percent" not in {c["name"] for c in inspector.get_columns("batteries")}:
            steps.append(
                "ALTER TABLE batteries ADD COLUMN max_grid_charge_percent FLOAT NOT NULL "
                "DEFAULT 100"
            )
    if inspector.has_table("inverter_controls"):
        have = {c["name"] for c in inspector.get_columns("inverter_controls")}
        if "written_schedule" not in have:
            steps.append("ALTER TABLE inverter_controls ADD COLUMN written_schedule JSON")
    if steps:
        with engine.begin() as conn:
            for step in steps:
                conn.execute(text(step))


def add_plan_lock_settings(engine: Engine) -> None:
    """When the plan locks (just before the window for existing systems), and the one-night
    "send at the last minute" request on inverter control."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, kind in (
            ("systems", "plan_lock_time", "TIME"),
            ("inverter_controls", "last_minute_day", "DATE"),
        ):
            if not inspector.has_table(table):
                continue
            if column not in {c["name"] for c in inspector.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {kind}"))


def add_plan_fixed_by(engine: Engine) -> None:
    """Why a kept plan is kept: all kept so far were kept when their lock time came."""
    inspector = inspect(engine)
    if not inspector.has_table("locked_plans"):
        return
    if "fixed_by" in {c["name"] for c in inspector.get_columns("locked_plans")}:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE locked_plans ADD COLUMN fixed_by VARCHAR(10) NOT NULL DEFAULT 'time'")
        )


def upgrade(engine: Engine) -> None:
    upgrade_single_window_tariffs(engine)
    add_battery_wear_cost(engine)
    add_big_load_settings(engine)
    add_whole_day_control(engine)
    add_plan_lock_settings(engine)
    add_plan_fixed_by(engine)
