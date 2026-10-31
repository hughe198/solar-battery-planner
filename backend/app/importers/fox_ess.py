"""Fox ESS Cloud Open API client and importer.

Docs: https://www.foxesscloud.com/public/i18n/en/OpenApiDocument.html

Each user supplies their own API key (Fox Cloud → User Profile → API Management) and the
inverter serial number. Fox limits each inverter to 1440 calls a day and each endpoint to one
call a second, and the history endpoint returns at most 24 hours per call, so a backfill makes
one call per day and paces itself.
"""

import hashlib
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.importers.base import INTERVAL, UsageInterval, interval_start

HISTORY_PATH = "/op/v0/device/history/query"
DEVICE_LIST_PATH = "/op/v0/device/list"
MIN_SOC_PATH = "/op/v0/device/battery/soc/get"
SCHEDULER_PATH = "/op/v1/device/scheduler/get"

# Fox variable name -> UsageInterval field. All are power in kW except SoC (%).
POWER_VARIABLES = {
    "loadsPower": "load_kwh",
    "pvPower": "solar_kwh",
    "gridConsumptionPower": "grid_import_kwh",
    "feedinPower": "grid_export_kwh",
}
SOC_VARIABLE = "SoC"

ERRNO_TOO_FREQUENT = 40400
# Invalid, expired or unauthorised token (as seen by community clients; not all documented).
AUTH_ERRNOS = {41807, 41808, 41809}


class FoxEssError(Exception):
    pass


class FoxEssAuthError(FoxEssError):
    pass


class FoxEssClient:
    """Thin signed client. Waits so that calls are at least `min_gap` seconds apart."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        http: httpx.Client | None = None,
        min_gap: float = 1.1,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key
        self.http = http or httpx.Client(base_url=base_url or settings.fox_ess_base_url, timeout=30)
        self.min_gap = min_gap
        self.sleep = sleep
        self.clock = clock
        self.max_retries = max_retries
        self._last_call: float | None = None

    def _headers(self, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        # Fox signs the literal characters "\r\n", not CR LF, as its own samples do.
        signature = hashlib.md5(rf"{path}\r\n{self.api_key}\r\n{timestamp}".encode()).hexdigest()
        return {
            "token": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "lang": "en",
            "Content-Type": "application/json",
        }

    def _pace(self) -> None:
        if self._last_call is not None:
            wait = self.min_gap - (self.clock() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = self.clock()

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("POST", path, json=body)

    def get(self, path: str, params: dict[str, Any]) -> Any:
        return self._call("GET", path, params=params)

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                resp = self.http.request(method, path, headers=self._headers(path), **kwargs)
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise FoxEssError(f"Fox ESS request failed: {exc}") from exc
            errno = payload.get("errno")
            if errno == 0:
                return payload.get("result")
            if errno == ERRNO_TOO_FREQUENT and attempt < self.max_retries:
                self.sleep(5 * 2**attempt)
                continue
            msg = payload.get("msg") or "unknown error"
            if errno in AUTH_ERRNOS:
                raise FoxEssAuthError(f"Fox ESS rejected the API key ({errno}: {msg})")
            raise FoxEssError(f"Fox ESS error {errno}: {msg}")
        raise FoxEssError("Fox ESS kept rate limiting the request")  # pragma: no cover

    def device_serials(self) -> list[str]:
        result = self.post(DEVICE_LIST_PATH, {"currentPage": 1, "pageSize": 100})
        return [d["deviceSN"] for d in (result or {}).get("data", [])]

    def battery_min_soc(self, sn: str) -> float | None:
        """The inverter's minimum state of charge while on grid (the floor it discharges to in
        everyday use), falling back to the off-grid minimum. Read-only."""
        result = self.get(MIN_SOC_PATH, {"sn": sn}) or {}
        for key in ("minSocOnGrid", "minSoc"):
            value = result.get(key)
            if isinstance(value, (int, float)) and 0 <= value < 100:
                return float(value)
        return None

    def force_charge_power_kw(self, sn: str) -> float | None:
        """The power set for forced charging in the inverter's schedule, if it has a forced
        charge period (the highest, if several). Read-only."""
        result = self.post(SCHEDULER_PATH, {"deviceSN": sn}) or {}
        powers = [
            g.get("fdPwr")
            for g in result.get("groups") or []
            if g.get("workMode") == "ForceCharge" and g.get("enable", 1)
        ]
        watts = [p for p in powers if isinstance(p, (int, float)) and p > 0]
        return max(watts) / 1000 if watts else None

    def history(self, sn: str, begin: datetime, end: datetime) -> list[dict[str, Any]]:
        """Raw history series for up to 24 hours: [{variable, unit, data: [{time, value}]}]."""
        body = {
            "sn": sn,
            "variables": [*POWER_VARIABLES, SOC_VARIABLE],
            "begin": int(begin.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
        }
        result = self.post(HISTORY_PATH, body) or []
        return result[0].get("datas", []) if result else []


def parse_fox_time(value: str) -> datetime:
    """Fox history times look like "2024-06-01 12:05:13 BST+0100" or end in "Z"/an offset."""
    value = value.strip()
    head, _, tail = value.rpartition(" ")
    # Drop a zone name before the offset, e.g. "BST+0100" -> "+0100".
    for i, ch in enumerate(tail):
        if ch in "+-":
            tail = tail[i:]
            break
    candidate = f"{head} {tail}" if head and tail[:1] in "+-" else value
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(candidate.replace("Z", ""), fmt)
            break
        except ValueError:
            continue
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # Fox documents history times as UTC
    return parsed.astimezone(timezone.utc)


def aggregate_history(series: list[dict[str, Any]], min_samples: int = 2) -> list[UsageInterval]:
    """Turn ~5 minute power samples into half-hour energy totals.

    Energy is mean power over the half hour times half an hour, which copes with uneven
    sampling. Half hours with fewer than `min_samples` load samples are dropped as gaps.
    SoC is the last reading in the half hour.
    """
    power: dict[str, dict[datetime, list[float]]] = defaultdict(lambda: defaultdict(list))
    soc: dict[datetime, tuple[datetime, float]] = {}
    for s in series:
        variable = s.get("variable")
        for point in s.get("data", []):
            if point.get("value") is None:
                continue
            moment = parse_fox_time(point["time"])
            bucket = interval_start(moment)
            value = float(point["value"])
            if variable in POWER_VARIABLES:
                power[variable][bucket].append(value)
            elif variable == SOC_VARIABLE and (bucket not in soc or moment >= soc[bucket][0]):
                soc[bucket] = (moment, value)

    hours = INTERVAL.total_seconds() / 3600
    intervals = []
    for bucket in sorted(power["loadsPower"]):
        if len(power["loadsPower"][bucket]) < min_samples:
            continue
        energy = {}
        for variable, field in POWER_VARIABLES.items():
            samples = power[variable].get(bucket, [])
            # Fox sometimes reports small negative PV or grid values at night.
            mean = max(0.0, sum(samples) / len(samples)) if samples else 0.0
            energy[field] = round(mean * hours, 4)
        intervals.append(
            UsageInterval(
                start=bucket,
                end=bucket + INTERVAL,
                battery_soc_percent=soc[bucket][1] if bucket in soc else None,
                **energy,
            )
        )
    return intervals


def local_day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    start = datetime.combine(day, datetime.min.time(), zone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


class FoxEssImporter:
    def __init__(self, client: FoxEssClient, device_sn: str) -> None:
        self.client = client
        self.device_sn = device_sn

    def fetch(self, start: datetime, end: datetime) -> Iterable[UsageInterval]:
        """Intervals in [start, end), one API call per 24 hours."""
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(hours=24), end)
            series = self.client.history(self.device_sn, cursor, chunk_end)
            for interval in aggregate_history(series):
                if cursor <= interval.start < chunk_end:
                    yield interval
            cursor = chunk_end
