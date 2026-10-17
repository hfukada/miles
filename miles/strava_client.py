import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import requests
import urllib3
from dotenv import dotenv_values, find_dotenv, load_dotenv
from requests.adapters import HTTPAdapter
from stravalib import Client
from stravalib.exc import Fault
from stravalib.model import Lap, Stream, SummaryActivity
from stravalib.protocol import RequestMethod
from stravalib.util.limiter import RateLimiter, get_rates_from_response_headers

from . import envfile, http_client
from .db import WORKOUT_TYPE_MAP, ActivityRow, LapRow
from .errors import StravaNotConfiguredError, StravaUnavailableError

ENV_FILE_VAR = "MILES_ENV_FILE"

# Defined before the import-time environment load below, which logs.
_logger = logging.getLogger(__name__)


def _resolve_env_file() -> Path | None:
    """The dotenv file the credentials come from: MILES_ENV_FILE if set, else
    the first `.env` found walking up from this package."""
    explicit = os.environ.get(ENV_FILE_VAR, "").strip()
    if explicit:
        return Path(explicit)
    found = find_dotenv()
    return Path(found) if found else None


REFRESH_TOKEN_KEY = "STRAVA_REFRESH_TOKEN"


def _load_environment(path: Path | None) -> None:
    """Load the dotenv file into the environment. load_dotenv leaves variables
    already set alone, but the file holds the newest refresh token: a process
    restarted with the environment its container was created with would
    otherwise start from a token Strava has since replaced."""
    if path is None:
        return
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        # Only the exception type: a decode error's message quotes the bytes.
        _logger.warning("could not read the Strava env file (%s); using the environment as it is", type(e).__name__)
        return
    load_dotenv(stream=StringIO(text))
    on_disk = dotenv_values(stream=StringIO(text)).get(REFRESH_TOKEN_KEY)
    if on_disk:
        os.environ[REFRESH_TOKEN_KEY] = on_disk


# Resolved once; a rotated refresh token is written back to this same file.
ENV_FILE = _resolve_env_file()
_load_environment(ENV_FILE)


# stravalib logs every request's URL and query params at INFO. The access
# token travels in a header, so none is exposed, but a request log line per
# call is noise. Set here, not in an entry point, so the CLIs are covered too.
logging.getLogger("stravalib.protocol").setLevel(logging.WARNING)

# Seconds a Strava request may take to connect and to deliver a response.
REQUEST_TIMEOUT_S = 30.0
TOKEN_URL = "https://www.strava.com/oauth/token"
# An access token is replaced when it has less than this long to live.
REFRESH_MARGIN_S = 300.0

# Transport failures: stravalib's own Fault (an HTTP error status) is a
# RequestException too, and is handled before this tuple is consulted.
_TRANSPORT_ERRORS = (requests.RequestException, urllib3.exceptions.HTTPError)

# Length of Strava's short rate-limit window, in seconds.
WINDOW_S = 900


@dataclass(frozen=True)
class _RateObservation:
    """What the most recent rate-limit headers said, and when, alongside how
    many calls the tally had counted by then (the reading's own response
    included). Strava's windows are fixed: 15 minutes aligned to the hour, and
    the UTC day."""

    daily_remaining: int
    short_remaining: int
    at: datetime
    daily_counted: int
    short_counted: int


@dataclass
class _Tally:
    """Every response this process, or an earlier one sharing the database,
    has had from Strava in the current UTC day and 15-minute window, headers
    or not (stream responses carry none, and Strava's header usage appears
    not to include them). Each count restarts when its day or window rolls
    over. The limits are those of the latest reading."""

    day: date
    window: int
    daily: int = 0
    short: int = 0
    daily_limit: int | None = None
    short_limit: int | None = None


_observation: _RateObservation | None = None
_tally: _Tally | None = None
_seeded = False
_rate_lock = threading.Lock()

STRAVA_HOST = "www.strava.com"

# Called with (daily_remaining, short_window_remaining, counts) after each
# response once a limit is known, where counts is what rate_source returns;
# outbound.claim() points it at the database.
rate_sink: Callable[[int, int, dict[str, object]], None] | None = None
# Returns the counts rate_sink last stored, so a new process resumes the
# tally of the window and day it starts in. Read once per process.
rate_source: Callable[[], dict[str, object] | None] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fault_status(e: Fault) -> int | None:
    """The HTTP status a Fault carries, None when it has no response."""
    return e.response.status_code if e.response is not None else None


def _report(kind: str, status: int | str) -> None:
    http_client.report("strava", STRAVA_HOST, status, kind)


def _kind_of(path: str) -> str:
    """The endpoint kind of a Strava API path, for the request ledger."""
    parts = path.rstrip("/").split("/")
    if parts[-1] in ("activities", "laps", "streams"):
        return parts[-1]
    if len(parts) > 1 and parts[-2] == "activities":
        return "detail"
    return "other"


@contextmanager
def _transport_errors(kind: str) -> Iterator[None]:
    """Turn a failure to get a response into StravaUnavailableError. The
    library's message for it carries the request URL; none of it is kept."""
    try:
        yield
    except Fault:
        raise
    except _TRANSPORT_ERRORS as e:
        if not isinstance(e, requests.exceptions.JSONDecodeError):
            # A body that would not parse arrived with a status of its own,
            # already counted by the response hook; only a failure to get a
            # response is an error.
            _report(kind, "error")
        raise StravaUnavailableError(f"Strava {kind} request failed ({type(e).__name__})") from None


def _on_response(response: requests.Response, **_: object) -> None:
    """Runs for every response from the Strava API, before its status is
    checked: counts it in the request ledger by endpoint kind and status,
    tallies it against the allowance, records the rate-limit headers, and
    drops the access token on a 401."""
    global _observation
    status = response.status_code
    _report(_kind_of(urlsplit(response.url).path), status)
    rate = get_rates_from_response_headers(
        dict(response.headers), cast(RequestMethod, (response.request.method or "GET").upper())
    )
    now = _now()
    with _rate_lock:
        _seed()
        tally = _count_call(now)
        if rate is not None:
            _observation = _RateObservation(
                rate.long_limit - rate.long_usage, rate.short_limit - rate.short_usage, now, tally.daily, tally.short
            )
            tally.daily_limit, tally.short_limit = rate.long_limit, rate.short_limit
        left = _remaining(now)
        counts = _persisted(tally)
    if rate_sink is not None and left is not None:
        rate_sink(left[0], left[1], counts)
    if status == 401:
        _forget_access_token()
        http_client.report_outcome("strava", 401)


def _window(when: datetime) -> int:
    return int(when.timestamp() // WINDOW_S)


def _count_call(now: datetime) -> _Tally:
    """Count one response. Caller holds _rate_lock."""
    global _tally
    tally = _roll(now)
    tally.daily += 1
    tally.short += 1
    return tally


def _roll(now: datetime) -> _Tally:
    """The tally, started or restarted for the day and window `now` is in.
    Caller holds _rate_lock."""
    global _tally
    tally = _tally
    if tally is None:
        tally = _tally = _Tally(now.date(), _window(now))
    if tally.day != now.date():
        tally.day, tally.daily = now.date(), 0
    if tally.window != _window(now):
        tally.window, tally.short = _window(now), 0
    return tally


def _persisted(tally: _Tally) -> dict[str, object]:
    return {
        "day": tally.day.isoformat(),
        "daily": tally.daily,
        "daily_limit": tally.daily_limit,
        "window": tally.window,
        "short": tally.short,
        "short_limit": tally.short_limit,
    }


def _seed() -> None:
    """Once per process, resume the counts an earlier process stored. Counts
    from another day or window are dropped; the limits are kept. Caller holds
    _rate_lock."""
    global _tally, _seeded
    if _seeded:
        return
    _seeded = True
    if rate_source is None:
        return
    try:
        stored = rate_source()
        if not stored:
            return
        now = _now()
        tally = _Tally(
            now.date(),
            _window(now),
            daily_limit=_optional_int(stored.get("daily_limit")),
            short_limit=_optional_int(stored.get("short_limit")),
        )
        if stored.get("day") == tally.day.isoformat():
            tally.daily = max(0, int(cast(int, stored["daily"])))
        if stored.get("window") == tally.window:
            tally.short = max(0, int(cast(int, stored["short"])))
    except (KeyError, TypeError, ValueError):
        _logger.warning("ignoring a malformed stored Strava tally")
        return
    _tally = tally


def _optional_int(value: object) -> int | None:
    return None if value is None else int(cast(int, value))


def _remaining(now: datetime) -> tuple[int, int] | None:
    """(daily, short-window) calls left, None while no limit is known. Each is
    the smaller of the limit less the calls tallied in the current day or
    window, and the latest reading's figure less the calls tallied since it
    (a reading from an earlier day or window no longer applies). A reading
    therefore never lifts the figure above what the tally says remains.
    Caller holds _rate_lock."""
    tally = _tally
    if tally is None or tally.daily_limit is None or tally.short_limit is None:
        return None
    daily_limit, short_limit = tally.daily_limit, tally.short_limit
    tally = _roll(now)
    daily = daily_limit - tally.daily
    short = short_limit - tally.short
    seen = _observation
    if seen is not None:
        if seen.at.date() == now.date():
            daily = min(daily, seen.daily_remaining - (tally.daily - seen.daily_counted))
        if _window(seen.at) == tally.window:
            short = min(short, seen.short_remaining - (tally.short - seen.short_counted))
    return max(0, daily), max(0, short)


def daily_calls_remaining() -> int | None:
    """Daily Strava API calls left: the smaller of the daily limit less the
    calls made today and the latest response headers' figure less the calls
    since. None until a limit is known (from a response, or an earlier
    process's stored tally)."""
    with _rate_lock:
        _seed()
        left = _remaining(_now())
        return None if left is None else left[0]


def short_window_calls_remaining() -> int | None:
    """Calls left in the current 15-minute window, by the same rule as
    daily_calls_remaining. None until a limit is known."""
    with _rate_lock:
        _seed()
        left = _remaining(_now())
        return None if left is None else left[1]


class _TimeoutAdapter(HTTPAdapter):
    """Applies REQUEST_TIMEOUT_S to requests that name no timeout, which is
    every request stravalib makes."""

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = REQUEST_TIMEOUT_S
        return super().send(request, **kwargs)


_session_lock = threading.Lock()
_session: requests.Session | None = None
# One adapter for every session, so connections are pooled across the
# per-token sessions api_session() hands out.
_adapter = _TimeoutAdapter()


def _http_session() -> requests.Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
            _session.mount("https://", _adapter)
        return _session


def _api_session(access_token: str) -> requests.Session:
    """A session that sends the access token as a bearer header. stravalib
    would otherwise put it in every request's query string, where request-
    library errors and debug logs repeat it."""
    session = requests.Session()
    session.mount("https://", _adapter)
    session.headers["Authorization"] = f"Bearer {access_token}"
    session.hooks["response"].append(_on_response)
    return session


def oauth_token_request(form: dict[str, str]) -> dict[str, object]:
    """POST to Strava's token endpoint with the credentials in the form body
    (stravalib puts them in the URL query, which request-library exceptions
    then repeat). Raises StravaUnavailableError when no response arrives and
    Fault on an error status. The Fault carries a bare response with only the
    status: the real one holds the request, and its body holds the secret."""
    try:
        response = _http_session().post(TOKEN_URL, data=form, timeout=REQUEST_TIMEOUT_S)
    except _TRANSPORT_ERRORS as e:
        _report("token", "error")
        raise StravaUnavailableError(f"Strava token request failed ({type(e).__name__})") from None
    if not response.ok:
        status = 401 if response.status_code in (400, 401) else response.status_code
        _report("token", status)
        # A 429 is the gate's to handle: it waits and retries, and records the
        # outcome when it gives up. miles-auth's exchange runs without a gate,
        # so a 429 there is never recorded.
        if status != 429:
            http_client.report_outcome("strava", status)
        rejected = requests.Response()
        rejected.status_code = response.status_code
        raise Fault(f"Strava token request rejected ({response.status_code})", response=rejected)
    _report("token", 200)
    try:
        payload = response.json()
    except ValueError:
        raise StravaUnavailableError("Strava token response was not JSON") from None
    if not isinstance(payload, dict):
        raise StravaUnavailableError("Strava token response was not a JSON object")
    return payload


def _exchange_refresh_token() -> dict[str, object]:
    """Trade the refresh token for an access token. Raises
    StravaNotConfiguredError when a credential is missing or empty."""
    client_id = os.environ.get("STRAVA_CLIENT_ID")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET")
    refresh_token = os.environ.get(REFRESH_TOKEN_KEY)
    if not (client_id and client_secret and refresh_token):
        raise StravaNotConfiguredError
    return oauth_token_request({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })


def _persist_refresh_token(token: str) -> None:
    """Keep a rotated refresh token: in this process's environment always, and
    on disk in the dotenv file the credentials were loaded from. Strava
    invalidates the old refresh token when it issues a new one, so a token
    that is not saved is lost at the next restart."""
    os.environ[REFRESH_TOKEN_KEY] = token
    if ENV_FILE is None:
        _logger.warning(
            "Strava issued a new refresh token but no dotenv file was loaded, so it is not saved; "
            "set %s or copy STRAVA_REFRESH_TOKEN from the environment before restarting",
            ENV_FILE_VAR,
        )
        return
    try:
        envfile.upsert_key(ENV_FILE, REFRESH_TOKEN_KEY, token)
    except OSError as e:
        _logger.warning(
            "Strava issued a new refresh token but %s could not be updated (%s); "
            "put the new STRAVA_REFRESH_TOKEN there by hand before restarting",
            ENV_FILE,
            type(e).__name__,
        )


_token_lock = threading.Lock()
_access_token: str | None = None
_access_token_expires_at = 0.0


def _forget_access_token() -> None:
    global _access_token
    with _token_lock:
        _access_token = None


def _current_access_token() -> tuple[str, float]:
    """The access token and its expiry, refreshed when it is missing or within
    REFRESH_MARGIN_S of expiring. Held in memory only: the database is backed
    up and readable by queries, so no token goes there."""
    global _access_token, _access_token_expires_at
    with _token_lock:
        if _access_token is None or _access_token_expires_at - time.time() <= REFRESH_MARGIN_S:
            result = _exchange_refresh_token()
            try:
                token = str(result["access_token"])
                expires_at = float(result["expires_at"])  # type: ignore[arg-type]
                new_refresh = str(result["refresh_token"])
            except (KeyError, TypeError, ValueError):
                raise StravaUnavailableError("Strava token response was missing fields") from None
            _access_token = token
            _access_token_expires_at = expires_at
            if new_refresh != os.environ[REFRESH_TOKEN_KEY]:
                _persist_refresh_token(new_refresh)
        return _access_token, _access_token_expires_at


def _secs(td: int | timedelta | None) -> int | None:
    if td is None:
        return None
    return int(td.total_seconds()) if isinstance(td, timedelta) else int(td)


def _flt(q: float | None) -> float | None:
    return float(q) if q is not None else None


def _lap_row(lap: Lap, activity_id: int) -> LapRow:
    assert lap.id is not None, "Lap must have an id"
    assert lap.lap_index is not None, "Lap must have a lap_index"
    return {
        "lap_id": lap.id,
        "activity_id": activity_id,
        "lap_index": lap.lap_index,
        "distance_m": _flt(lap.distance),
        "moving_time_s": _secs(lap.moving_time),
        "elapsed_time_s": _secs(lap.elapsed_time),
        "average_speed_mps": _flt(lap.average_speed),
        "average_heartrate": lap.average_heartrate,
        "max_heartrate": lap.max_heartrate,
        "average_cadence": lap.average_cadence,
        "total_elevation_gain_m": _flt(lap.total_elevation_gain),
        "pace_zone": lap.pace_zone,
        "raw_json": lap.model_dump_json(),
    }


def _get_client() -> Client:
    access_token, expires_at = _current_access_token()
    # No refresh token or app credentials are handed to stravalib: its own
    # refresh would replace the token inside a long-lived client, and a
    # rotated refresh token would never reach the dotenv file. The access
    # token is not handed over either; the session sends it as a header.
    # stravalib's default limiter can sleep a request until the next UTC
    # midnight; this one has no rules, the session's response hook records the
    # allowance, and waiting is the callers' decision (strava_budget).
    client = Client(
        token_expires=int(expires_at),
        requests_session=_api_session(access_token),
        rate_limiter=RateLimiter(),
    )
    client.protocol.client_id = None
    client.protocol.client_secret = None
    return client


def get_activity_laps(activity_id: int) -> list[LapRow]:
    client = _get_client()
    with _transport_errors("laps"):
        return [_lap_row(lap, activity_id) for lap in client.get_activity_laps(activity_id)]


def get_activity_detail(activity_id: int) -> tuple[str, list[LapRow] | None]:
    """(description, laps) from one DetailedActivity request, which carries
    both. The description is "" when the activity has none, so "has none" can
    be stored and told apart from "not fetched". laps is None when the response
    did not include any."""
    client = _get_client()
    with _transport_errors("detail"):
        detail = client.get_activity(activity_id)
    laps = None if detail.laps is None else [_lap_row(lap, activity_id) for lap in detail.laps]
    return detail.description or "", laps


def get_activities(after_ts: str | None = None) -> Iterator[ActivityRow]:
    client = _get_client()
    with _transport_errors("activities"):
        for activity in client.get_activities(after=after_ts):
            yield row_from_activity(activity)


def row_from_activity(activity: SummaryActivity) -> ActivityRow:
    wt = int(activity.workout_type or 0)
    sport = str(activity.sport_type.root) if activity.sport_type is not None else ""

    latlng = activity.start_latlng
    start_lat: float | None = latlng.lat if latlng is not None else None
    start_lng: float | None = latlng.lon if latlng is not None else None

    assert activity.id is not None, "Activity must have an id"
    # Store athlete-local wall time, naive (no offset/Z suffix) — see ActivityRow.start_date
    # in db.py. stravalib parses start_date_local as tz-aware with a UTC offset because
    # Strava tags it with a (fake) trailing 'Z', but the wall-clock value it carries is
    # already local; stripping tzinfo (not converting) recovers it.
    local_start = activity.start_date_local
    start_date = local_start.replace(tzinfo=None).isoformat() if local_start else None
    return {
        "activity_id": activity.id,
        "name": activity.name,
        "sport_type": sport,
        "start_date": start_date,
        "workout_type": wt,
        "run_type": WORKOUT_TYPE_MAP.get(wt, "easy"),
        "distance_m": _flt(activity.distance),
        "moving_time_s": _secs(activity.moving_time),
        "elapsed_time_s": _secs(activity.elapsed_time),
        "total_elevation_gain_m": _flt(activity.total_elevation_gain),
        "average_speed_mps": _flt(activity.average_speed),
        "max_speed_mps": _flt(activity.max_speed),
        "average_heartrate": activity.average_heartrate,
        "max_heartrate": activity.max_heartrate,
        "average_cadence": activity.average_cadence,
        "gear_id": activity.gear_id,
        "strava_url": f"https://www.strava.com/activities/{activity.id}",
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "start_lat": start_lat,
        "start_lng": start_lng,
        "raw_json": activity.model_dump_json(),
    }


STREAM_TYPES = [
    "time", "distance", "latlng", "altitude", "velocity_smooth",
    "heartrate", "cadence", "watts", "temp", "moving", "grade_smooth",
]


def get_activity_streams(activity_id: int) -> dict[str, Stream]:
    client = _get_client()
    with _transport_errors("streams"):
        streams = client.get_activity_streams(activity_id, types=STREAM_TYPES)
    return {str(k): v for k, v in streams.items()}
