"""CSV upload fallback for users without Fox ESS (or with gaps in it).

Expected columns (header row required, extra columns ignored):

    start, load_kwh, solar_kwh, grid_import_kwh, grid_export_kwh[, battery_soc_percent]

`start` is the start of each interval as ISO 8601. Times without an offset are read in the
system's time zone. Intervals of up to 30 minutes are summed into half hours; the SoC of the
last row in each half hour is kept.
"""

import csv
import io
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.importers.base import INTERVAL, UsageInterval, interval_start

ENERGY_COLUMNS = ("load_kwh", "solar_kwh", "grid_import_kwh", "grid_export_kwh")
REQUIRED_COLUMNS = ("start", *ENERGY_COLUMNS)
MAX_ERRORS = 5


class CsvImportError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _parse_start(value: str, zone: ZoneInfo) -> datetime:
    moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(timezone.utc)


def _number(value: str | None, column: str, *, required: bool = True) -> float | None:
    if value is None or not value.strip():
        if required:
            raise ValueError(f"{column} is empty")
        return None
    number = float(value)
    if number < 0 or number != number:
        raise ValueError(f"{column} must be zero or more")
    return number


def parse_csv(text: str, tz: str) -> list[UsageInterval]:
    zone = ZoneInfo(tz)
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    columns = {(c or "").strip().lower(): c for c in reader.fieldnames or []}
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise CsvImportError([f"missing column(s): {', '.join(missing)}"])

    totals: dict[datetime, dict[str, float]] = defaultdict(
        lambda: dict.fromkeys(ENERGY_COLUMNS, 0.0)
    )
    soc: dict[datetime, tuple[datetime, float]] = {}
    errors: list[str] = []
    for line, row in enumerate(reader, start=2):
        try:
            start = _parse_start(row[columns["start"]] or "", zone)
            bucket = interval_start(start)
            values = {c: _number(row[columns[c]], c) for c in ENERGY_COLUMNS}
            soc_value = None
            if "battery_soc_percent" in columns:
                soc_value = _number(
                    row[columns["battery_soc_percent"]], "battery_soc_percent", required=False
                )
                if soc_value is not None and soc_value > 100:
                    raise ValueError("battery_soc_percent must be 0 to 100")
        except (ValueError, TypeError) as exc:
            errors.append(f"line {line}: {exc}")
            if len(errors) >= MAX_ERRORS:
                break
            continue
        for column, value in values.items():
            totals[bucket][column] += value
        if soc_value is not None and (bucket not in soc or start >= soc[bucket][0]):
            soc[bucket] = (start, soc_value)

    if errors:
        raise CsvImportError(errors)
    if not totals:
        raise CsvImportError(["the file has no data rows"])
    return [
        UsageInterval(
            start=bucket,
            end=bucket + INTERVAL,
            battery_soc_percent=soc[bucket][1] if bucket in soc else None,
            **{c: round(v, 4) for c, v in totals[bucket].items()},
        )
        for bucket, _ in sorted(totals.items())
    ]
