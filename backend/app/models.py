"""Database models.

A user owns one system. A system has one battery, one tariff and one or more
solar arrays (e.g. east and west roofs). Imported usage is stored per half hour
and historical weather per hour, both against the system; later steps hang
forecasts off it too.
"""

import enum
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    Date,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Time,
    UniqueConstraint,
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlanningMode(str, enum.Enum):
    maximise_own_usage = "maximise_own_usage"
    maximise_export_revenue = "maximise_export_revenue"


class DataSourceKind(str, enum.Enum):
    none = "none"
    fox_ess = "fox_ess"
    csv = "csv"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=_now)

    system: Mapped["System | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class System(Base):
    __tablename__ = "systems"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    name: Mapped[str] = mapped_column(String(100), default="My system")
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/London")
    planning_mode: Mapped[PlanningMode] = mapped_column(
        Enum(PlanningMode), default=PlanningMode.maximise_own_usage
    )
    data_source: Mapped[DataSourceKind] = mapped_column(
        Enum(DataSourceKind), default=DataSourceKind.none
    )
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    user: Mapped[User] = relationship(back_populates="system")
    battery: Mapped["Battery"] = relationship(
        back_populates="system", cascade="all, delete-orphan", uselist=False
    )
    tariff: Mapped["Tariff"] = relationship(
        back_populates="system", cascade="all, delete-orphan", uselist=False
    )
    arrays: Mapped[list["SolarArray"]] = relationship(
        back_populates="system", cascade="all, delete-orphan", order_by="SolarArray.id"
    )
    fox_ess: Mapped["FoxEssConnection | None"] = relationship(
        back_populates="system", cascade="all, delete-orphan", uselist=False
    )
    usage: Mapped[list["UsageRecord"]] = relationship(
        back_populates="system", cascade="all, delete-orphan"
    )
    weather: Mapped[list["WeatherRecord"]] = relationship(
        back_populates="system", cascade="all, delete-orphan"
    )
    import_jobs: Mapped[list["ImportJob"]] = relationship(
        back_populates="system", cascade="all, delete-orphan"
    )


class Battery(Base):
    __tablename__ = "batteries"

    id: Mapped[int] = mapped_column(primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id"), unique=True)
    capacity_kwh: Mapped[float] = mapped_column(Float)
    min_soc_percent: Mapped[float] = mapped_column(Float, default=10)
    max_charge_kw: Mapped[float] = mapped_column(Float)
    max_discharge_kw: Mapped[float] = mapped_column(Float)
    round_trip_efficiency_percent: Mapped[float] = mapped_column(Float, default=90)

    system: Mapped[System] = relationship(back_populates="battery")


class SolarArray(Base):
    __tablename__ = "solar_arrays"

    id: Mapped[int] = mapped_column(primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="Array")
    kwp: Mapped[float] = mapped_column(Float)
    # Degrees from horizontal (0 = flat, 90 = vertical).
    tilt_degrees: Mapped[float] = mapped_column(Float)
    # Compass bearing the panels face (0 = north, 90 = east, 180 = south).
    azimuth_degrees: Mapped[float] = mapped_column(Float)

    system: Mapped[System] = relationship(back_populates="arrays")


class Tariff(Base):
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id"), unique=True)
    # All rates in pence per kWh.
    day_import_rate: Mapped[float] = mapped_column(Float)
    cheap_import_rate: Mapped[float] = mapped_column(Float)
    export_rate: Mapped[float] = mapped_column(Float)
    # Local time; the window may cross midnight (e.g. 23:30 to 05:30).
    cheap_start: Mapped[time] = mapped_column(Time)
    cheap_end: Mapped[time] = mapped_column(Time)

    system: Mapped[System] = relationship(back_populates="tariff")


class FoxEssConnection(Base):
    """A user's Fox ESS Cloud API key and inverter. The key is stored encrypted."""

    __tablename__ = "fox_ess_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id"), unique=True)
    api_key_encrypted: Mapped[str] = mapped_column(String(512))
    api_key_hint: Mapped[str] = mapped_column(String(8))
    device_sn: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    last_synced_at: Mapped[datetime | None] = mapped_column(default=None)

    system: Mapped[System] = relationship(back_populates="fox_ess")


class UsageSource(str, enum.Enum):
    fox_ess = "fox_ess"
    csv = "csv"


class UsageRecord(Base):
    """Energy totals for one half hour, in kWh. Times are naive UTC."""

    __tablename__ = "usage_records"
    __table_args__ = (UniqueConstraint("system_id", "start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"))
    start: Mapped[datetime]
    load_kwh: Mapped[float] = mapped_column(Float)
    solar_kwh: Mapped[float] = mapped_column(Float)
    grid_import_kwh: Mapped[float] = mapped_column(Float)
    grid_export_kwh: Mapped[float] = mapped_column(Float)
    # State of charge at the end of the interval, when the source reports it.
    battery_soc_percent: Mapped[float | None] = mapped_column(Float, default=None)
    source: Mapped[UsageSource] = mapped_column(Enum(UsageSource))

    system: Mapped[System] = relationship(back_populates="usage")


class WeatherRecord(Base):
    """Observed weather for the hour starting at `start` (naive UTC) at the system's location.

    Radiation values are the mean over the hour; temperature and cloud cover are taken at the
    start of the hour.
    """

    __tablename__ = "weather_records"
    __table_args__ = (UniqueConstraint("system_id", "start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"))
    start: Mapped[datetime]
    temperature_c: Mapped[float | None] = mapped_column(Float)
    cloud_cover_percent: Mapped[float | None] = mapped_column(Float)
    # Global horizontal irradiance and its direct/diffuse parts, W/m².
    shortwave_radiation_wm2: Mapped[float | None] = mapped_column(Float)
    direct_radiation_wm2: Mapped[float | None] = mapped_column(Float)
    diffuse_radiation_wm2: Mapped[float | None] = mapped_column(Float)

    system: Mapped[System] = relationship(back_populates="weather")


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class ImportJob(Base):
    """A background Fox ESS backfill. Dates are local days in the system's time zone."""

    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"), index=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.queued)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days_total: Mapped[int] = mapped_column(Integer)
    days_done: Mapped[int] = mapped_column(Integer, default=0)
    days_skipped: Mapped[int] = mapped_column(Integer, default=0)
    intervals_imported: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)

    system: Mapped[System] = relationship(back_populates="import_jobs")

    @classmethod
    def fail_interrupted(cls, db: Session) -> None:
        """Jobs run in-process, so any still open at startup were cut off by a restart."""
        db.execute(
            update(cls)
            .where(cls.status.in_([JobStatus.queued, JobStatus.running]))
            .values(
                status=JobStatus.failed,
                message="Interrupted by a server restart. Start the import again to resume.",
                finished_at=_now(),
            )
        )
        db.commit()
