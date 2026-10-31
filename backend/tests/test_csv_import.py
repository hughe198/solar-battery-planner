from datetime import datetime, timezone

import pytest

from app.importers.csv_import import CsvImportError, parse_csv

HEADER = "start,load_kwh,solar_kwh,grid_import_kwh,grid_export_kwh,battery_soc_percent\n"


def test_sums_quarter_hours_into_half_hours_in_local_time():
    text = HEADER + (
        "2024-06-01T10:00,0.2,0.5,0,0.1,40\n"
        "2024-06-01T10:15,0.3,0.5,0,0.2,45\n"
        "2024-06-01T10:30,0.25,0.4,0.05,0,\n"
    )
    first, second = parse_csv(text, "Europe/London")
    # 10:00 BST is 09:00 UTC.
    assert first.start == datetime(2024, 6, 1, 9, 0, tzinfo=timezone.utc)
    assert first.load_kwh == 0.5
    assert first.solar_kwh == 1.0
    assert first.grid_export_kwh == pytest.approx(0.3)
    assert first.battery_soc_percent == 45
    assert second.battery_soc_percent is None


def test_offsets_are_respected_and_soc_optional():
    text = "Start,Load_kWh,solar_kwh,grid_import_kwh,grid_export_kwh\n2024-01-01T00:00Z,1,0,1,0\n"
    (row,) = parse_csv(text, "Australia/Sydney")
    assert row.start == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_reports_missing_columns_and_bad_rows():
    with pytest.raises(CsvImportError, match="missing column"):
        parse_csv("start,load_kwh\n", "UTC")
    with pytest.raises(CsvImportError) as err:
        parse_csv(HEADER + "nope,1,1,1,1,\n2024-01-01T00:00,-1,0,0,0,\n", "UTC")
    assert len(err.value.errors) == 2
    assert err.value.errors[0].startswith("line 2")
    with pytest.raises(CsvImportError, match="no data"):
        parse_csv(HEADER, "UTC")
