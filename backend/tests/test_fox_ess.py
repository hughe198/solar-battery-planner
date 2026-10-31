import hashlib
from datetime import datetime, timezone

import httpx
import pytest

from app.importers.fox_ess import (
    FoxEssAuthError,
    FoxEssClient,
    FoxEssImporter,
    aggregate_history,
    parse_fox_time,
)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def series(variable, points):
    return {
        "variable": variable,
        "unit": "kW",
        "data": [{"time": t, "value": v} for t, v in points],
    }


def test_parse_fox_time_formats():
    assert parse_fox_time("2024-06-01 12:05:13 BST+0100") == utc(2024, 6, 1, 11, 5, 13)
    assert parse_fox_time("2024-01-01 09:26:14 GMT+0000") == utc(2024, 1, 1, 9, 26, 14)
    assert parse_fox_time("2024-01-01 09:26:14") == utc(2024, 1, 1, 9, 26, 14)
    assert parse_fox_time("2024-01-01T09:26:14Z") == utc(2024, 1, 1, 9, 26, 14)


def test_aggregate_history_to_half_hours():
    times = [f"2024-06-01 10:{m:02d}:00 GMT+0000" for m in range(0, 60, 5)]
    data = [
        series("loadsPower", [(t, 1.0) for t in times]),
        series("pvPower", [(t, 3.0 if i < 6 else -0.01) for i, t in enumerate(times)]),
        series("gridConsumptionPower", [(t, 0.0) for t in times]),
        series("feedinPower", [(t, 2.0) for t in times[:6]]),
        series("SoC", [(t, 50 + i) for i, t in enumerate(times)]),
    ]
    first, second = aggregate_history(data)
    assert first.start == utc(2024, 6, 1, 10, 0)
    assert first.load_kwh == 0.5
    assert first.solar_kwh == 1.5
    assert first.grid_export_kwh == 1.0
    assert first.battery_soc_percent == 55
    # Negative PV noise is clamped and missing feed-in counts as zero.
    assert second.solar_kwh == 0
    assert second.grid_export_kwh == 0
    assert second.battery_soc_percent == 61


def test_aggregate_drops_sparse_half_hours():
    data = [series("loadsPower", [("2024-06-01 10:00:00 GMT+0000", 1.0)])]
    assert aggregate_history(data) == []


def fox_transport(responses, seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=responses.pop(0))

    return httpx.MockTransport(handler)


def make_client(responses, seen, sleeps):
    http = httpx.Client(base_url="https://fox.test", transport=fox_transport(responses, seen))
    return FoxEssClient("secret-key", http=http, sleep=sleeps.append, clock=lambda: 0.0)


def test_client_signs_requests_and_paces():
    seen, sleeps = [], []
    client = make_client(
        [{"errno": 0, "result": {"data": [{"deviceSN": "ABC123"}]}}] * 2, seen, sleeps
    )
    assert client.device_serials() == ["ABC123"]
    client.device_serials()

    headers = seen[0].headers
    expected = hashlib.md5(
        f"/op/v0/device/list\\r\\nsecret-key\\r\\n{headers['timestamp']}".encode()
    ).hexdigest()
    assert headers["token"] == "secret-key"
    assert headers["signature"] == expected
    # The fake clock never moves, so the second call waits the full gap.
    assert sleeps == [pytest.approx(1.1)]


def test_client_backs_off_when_too_frequent():
    seen, sleeps = [], []
    client = make_client([{"errno": 40400}, {"errno": 0, "result": {"data": []}}], seen, sleeps)
    assert client.device_serials() == []
    assert len(seen) == 2
    assert 5 in sleeps


def test_client_reads_battery_min_soc():
    seen = []
    client = make_client(
        [
            {"errno": 0, "result": {"minSoc": 10, "minSocOnGrid": 5}},
            {"errno": 0, "result": {"minSoc": 15}},
            {"errno": 0, "result": {}},
        ],
        seen,
        [],
    )
    assert client.battery_min_soc("ABC123") == 5
    assert client.battery_min_soc("ABC123") == 15
    assert client.battery_min_soc("ABC123") is None
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/op/v0/device/battery/soc/get"
    assert seen[0].url.params["sn"] == "ABC123"


def test_client_auth_error():
    client = make_client([{"errno": 41808, "msg": "token expired"}], [], [])
    with pytest.raises(FoxEssAuthError):
        client.device_serials()


def test_importer_chunks_by_24_hours():
    calls = []

    class FakeClient:
        def history(self, sn, begin, end):
            calls.append((begin, end))
            stamp = begin.strftime("%Y-%m-%d %H:%M:%S GMT+0000")
            return [series("loadsPower", [(stamp, 1.0), (stamp, 1.0)])]

    intervals = list(FoxEssImporter(FakeClient(), "SN").fetch(utc(2024, 6, 1), utc(2024, 6, 3, 12)))
    assert calls == [
        (utc(2024, 6, 1), utc(2024, 6, 2)),
        (utc(2024, 6, 2), utc(2024, 6, 3)),
        (utc(2024, 6, 3), utc(2024, 6, 3, 12)),
    ]
    assert [i.start for i in intervals] == [utc(2024, 6, 1), utc(2024, 6, 2), utc(2024, 6, 3)]
