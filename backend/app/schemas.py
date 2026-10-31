from datetime import time
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models import DataSourceKind, PlanningMode


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    has_system: bool


class BatteryIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    capacity_kwh: float = Field(gt=0, le=200)
    min_soc_percent: float = Field(default=10, ge=0, lt=100)
    max_charge_kw: float = Field(gt=0, le=50)
    max_discharge_kw: float = Field(gt=0, le=50)
    round_trip_efficiency_percent: float = Field(default=90, gt=50, le=100)


class SolarArrayIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(default="Array", max_length=100)
    kwp: float = Field(gt=0, le=100)
    tilt_degrees: float = Field(ge=0, le=90)
    azimuth_degrees: float = Field(ge=0, lt=360)


class TariffIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day_import_rate: float = Field(ge=0, le=200)
    cheap_import_rate: float = Field(ge=0, le=200)
    export_rate: float = Field(ge=0, le=200)
    cheap_start: time
    cheap_end: time

    @model_validator(mode="after")
    def window_not_empty(self) -> "TariffIn":
        if self.cheap_start == self.cheap_end:
            raise ValueError("cheap window start and end must differ")
        return self


class SystemIn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(default="My system", max_length=100)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = "Europe/London"
    planning_mode: PlanningMode = PlanningMode.maximise_own_usage
    data_source: DataSourceKind = DataSourceKind.none
    battery: BatteryIn
    tariff: TariffIn
    arrays: list[SolarArrayIn] = Field(min_length=1, max_length=6)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        if value not in available_timezones():
            raise ValueError("unknown timezone")
        return value


class SystemOut(SystemIn):
    id: int
