"""Database models.

A user owns one system. A system has one battery, one tariff and one or more
solar arrays (e.g. east and west roofs). Later steps hang imported usage,
weather and forecasts off the system.
"""

import enum
from datetime import datetime, time, timezone

from sqlalchemy import Enum, Float, ForeignKey, String, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

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
