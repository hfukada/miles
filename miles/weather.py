import json
import logging
import urllib.error
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing_extensions import TypedDict

from . import http_client

_logger = logging.getLogger(__name__)


class WeatherRefusedError(ValueError):
    """The service refused every request window for a location."""


# Statuses that mean this window's request can never be answered; a no-data
# marker is stored for its activities. Any other client error (credentials,
# a moved endpoint) says nothing about the window and stops the pass.
UNANSWERABLE_WINDOW_STATUSES = (400, 422)

ATTRIBUTION = "Weather data by Open-Meteo.com (https://open-meteo.com/), CC BY 4.0"

HOURLY_VARIABLES = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,windspeed_10m"
)

# Open-Meteo weighs a request by its date span (one unit per 14 days) and
# variable count (one per 10). A window never spans more than this many days,
# so every request weighs one call.
MAX_WINDOW_DAYS = 14

# An activity whose successful response held no matching hours is not asked
# about again until this long after the attempt.
NO_DATA_RETRY_DAYS = 30


class WeatherRow(TypedDict):
    activity_id: int
    fetched_at: str
    temp_c_start: float | None
    temp_c_end: float | None
    temp_c_avg: float | None
    temp_c_max: float | None
    apparent_temp_c_max: float | None
    humidity_avg: float | None
    precip_mm: float | None
    wind_kph_avg: float | None
    hourly_json: str | None
    raw_json: str


class WeatherSpec(TypedDict):
    activity_id: int
    start_dt: datetime
    duration_s: int


@dataclass(frozen=True)
class Window:
    """One request: the specs it covers and the inclusive date span asked for."""

    specs: list[WeatherSpec]
    start_date: date
    end_date: date
    use_archive: bool


def _api_url(start_date: date, end_date: date, lat: float, lng: float, use_archive: bool) -> str:
    base = (
        "https://archive-api.open-meteo.com/v1/archive"
        if use_archive
        else "https://api.open-meteo.com/v1/forecast"
    )
    params = urllib.parse.urlencode({
        "latitude": round(lat, 4),
        "longitude": round(lng, 4),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": HOURLY_VARIABLES,
        "timezone": "UTC",
        "wind_speed_unit": "kmh",
    })
    return f"{base}?{params}"


def _iter_hour_strings(start_dt: datetime, end_dt: datetime) -> Iterator[str]:
    """Yield ISO hour strings (no timezone suffix) for all hours overlapping [start_dt, end_dt)."""
    h = start_dt.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    end = end_dt.replace(tzinfo=None)
    while h < end:
        yield h.strftime("%Y-%m-%dT%H:%M")
        h += timedelta(hours=1)


def _extract_run_hours(
    time_index: dict[str, int],
    temps: list[float | None],
    apparent: list[float | None],
    humidity_list: list[float | None],
    precip_list: list[float | None],
    wind_list: list[float | None],
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict[str, object]]:
    run_hours: list[dict[str, object]] = []
    for hour_str in _iter_hour_strings(start_dt, end_dt):
        idx = time_index.get(hour_str)
        if idx is None:
            continue
        run_hours.append({
            "hour": hour_str,
            "temp_c": temps[idx] if idx < len(temps) else None,
            "apparent_temp_c": apparent[idx] if idx < len(apparent) else None,
            "humidity_pct": humidity_list[idx] if idx < len(humidity_list) else None,
            "precip_mm": precip_list[idx] if idx < len(precip_list) else None,
            "wind_kph": wind_list[idx] if idx < len(wind_list) else None,
        })
    return run_hours


def _build_weather_row(
    run_hours: list[dict[str, object]],
    activity_id: int,
    raw_json: str,
) -> WeatherRow | None:
    if not run_hours:
        return None

    def avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 1) if vals else None

    def _floats(key: str) -> list[float]:
        return [float(v) for h in run_hours if isinstance(v := h[key], (int, float))]

    run_temps = _floats("temp_c")
    run_apparent = _floats("apparent_temp_c")
    run_humid = _floats("humidity_pct")
    run_wind = _floats("wind_kph")
    run_precip = _floats("precip_mm")

    # Hours that exist but carry no value are no data and make no real row.
    if not (run_temps or run_apparent or run_humid or run_wind or run_precip):
        return None

    return {
        "activity_id": activity_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "temp_c_start": run_temps[0] if run_temps else None,
        "temp_c_end": run_temps[-1] if run_temps else None,
        "temp_c_avg": avg(run_temps),
        "temp_c_max": max(run_temps) if run_temps else None,
        "apparent_temp_c_max": max(run_apparent) if run_apparent else None,
        "humidity_avg": avg(run_humid),
        "precip_mm": round(sum(run_precip), 2) if run_precip else None,
        "wind_kph_avg": avg(run_wind),
        "hourly_json": json.dumps(run_hours),
        "raw_json": raw_json,
    }


def no_data_row(activity_id: int) -> WeatherRow:
    """A weather row with no values: the service answered but had no hours for
    this activity. Distinguished from real rows by a NULL `hourly_json`."""
    return {
        "activity_id": activity_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "temp_c_start": None,
        "temp_c_end": None,
        "temp_c_avg": None,
        "temp_c_max": None,
        "apparent_temp_c_max": None,
        "humidity_avg": None,
        "precip_mm": None,
        "wind_kph_avg": None,
        "hourly_json": None,
        "raw_json": json.dumps({"no_data": True}),
    }


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def plan_windows(specs: list[WeatherSpec], today: date | None = None) -> list[Window]:
    """Group specs (all at one location) into request windows. Recent runs go
    to the forecast endpoint, older ones to the archive; within each, a new
    window starts when the window would pass MAX_WINDOW_DAYS."""
    today = today or date.today()
    archive_cutoff = today - timedelta(days=5)
    spans = sorted(
        (
            (
                _utc(s["start_dt"]).date(),
                (_utc(s["start_dt"]) + timedelta(seconds=s["duration_s"])).date(),
                s,
            )
            for s in specs
        ),
        key=lambda t: t[0],
    )
    windows: list[Window] = []
    for use_archive in (True, False):
        subset = [t for t in spans if (t[0] <= archive_cutoff) == use_archive]
        current: list[WeatherSpec] = []
        first = last = date.min
        for start, end, spec in subset:
            if current and (max(last, end) - first).days + 1 > MAX_WINDOW_DAYS:
                windows.append(Window(current, first, last, use_archive))
                current = []
            if not current:
                first, last = start, end
            else:
                last = max(last, end)
            current.append(spec)
        if current:
            windows.append(Window(current, first, last, use_archive))
    return windows


def fetch_weather_windows(
    specs: list[WeatherSpec],
    lat: float,
    lng: float,
) -> Iterator[tuple[list[WeatherRow], list[WeatherRow]]]:
    """Fetch weather for activities at one location, one request per window.

    Yields (rows, no_data_rows) per window as it completes, so a caller can
    store progress before a later request fails. A request refused with one of
    UNANSWERABLE_WINDOW_STATUSES yields no-data rows for its specs and the
    next window is tried; if every window is refused that way, the
    refusals are no longer about the windows and WeatherRefusedError is
    raised once their markers are yielded. Any other failure raises (after
    http_client's retries) and nothing past it is attempted.
    raw_json stores compact metadata (not the full response) to avoid per-activity bloat.
    """
    windows = plan_windows(specs)
    refused = 0
    for window in windows:
        url = _api_url(window.start_date, window.end_date, lat, lng, window.use_archive)
        try:
            data = json.loads(http_client.request(http_client.OPEN_METEO, url))
        except urllib.error.HTTPError as e:
            if e.code in UNANSWERABLE_WINDOW_STATUSES:
                _logger.warning(
                    "weather window %s to %s refused with status %s",
                    window.start_date, window.end_date, e.code,
                )
                refused += 1
                yield [], [no_data_row(spec["activity_id"]) for spec in window.specs]
                continue
            raise

        hourly = data.get("hourly", {})
        times: list[str] = hourly.get("time", [])
        temps: list[float | None] = hourly.get("temperature_2m", [])
        apparent: list[float | None] = hourly.get("apparent_temperature", [])
        humidity_list: list[float | None] = hourly.get("relative_humidity_2m", [])
        precip_list: list[float | None] = hourly.get("precipitation", [])
        wind_list: list[float | None] = hourly.get("windspeed_10m", [])

        time_index: dict[str, int] = {t: i for i, t in enumerate(times)}
        bulk_meta = json.dumps({
            "bulk_fetch": True,
            "lat": lat,
            "lng": lng,
            "start_date": window.start_date.isoformat(),
            "end_date": window.end_date.isoformat(),
        })

        rows: list[WeatherRow] = []
        empty: list[WeatherRow] = []
        for spec in window.specs:
            start_dt = _utc(spec["start_dt"])
            end_dt = start_dt + timedelta(seconds=spec["duration_s"])
            run_hours = _extract_run_hours(
                time_index, temps, apparent, humidity_list, precip_list, wind_list,
                start_dt, end_dt,
            )
            row = _build_weather_row(run_hours, spec["activity_id"], bulk_meta)
            if row is None:
                empty.append(no_data_row(spec["activity_id"]))
            else:
                rows.append(row)
        yield rows, empty

    if windows and refused == len(windows):
        raise WeatherRefusedError(f"every weather window at {lat:.1f},{lng:.1f} was refused")
