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


def upgrade(engine: Engine) -> None:
    upgrade_single_window_tariffs(engine)
    add_battery_wear_cost(engine)
