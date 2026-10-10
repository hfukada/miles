import shutil
import socket
import urllib.request
from collections import OrderedDict
from pathlib import Path

import pytest
from helpers import FakeClock, FakeOsrm, memory_db

from miles import db, drive_starts, extracts, http_client, route_search, strava_budget, strava_client, streams, terrain_backfill

# Resolved before any fixture repoints it: the one database no test may open.
_REAL_DB = db.DB_PATH.resolve()

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """Any real outbound connection from a test fails loudly; tests stub the fetch layer."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        if isinstance(address, tuple) and address and address[0] not in _LOCAL_HOSTS:
            raise NetworkBlockedError(f"test attempted a real network connection to {address!r}")
        return real_connect(self, address)

    def blocked_urlopen(url, *args, **kwargs):
        target = getattr(url, "full_url", url)
        raise NetworkBlockedError(f"test attempted a real network request to {target!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(urllib.request, "urlopen", blocked_urlopen)


@pytest.fixture(autouse=True)
def _fresh_strava_token_state(monkeypatch):
    """No test inherits another's cached access token or measured allowance."""
    monkeypatch.setattr(strava_client, "_access_token", None)
    monkeypatch.setattr(strava_client, "_access_token_expires_at", 0.0)
    monkeypatch.setattr(strava_client, "_observation", None)


@pytest.fixture(autouse=True)
def _osrm_available(monkeypatch):
    """A test that makes OSRM fail does not leave the next one estimating."""
    monkeypatch.setattr(drive_starts, "_osrm_unavailable_until", 0.0)
    monkeypatch.setattr(drive_starts, "_last_expired_at", None)


@pytest.fixture(autouse=True)
def _fresh_http_throttle_state(monkeypatch):
    """No test inherits another's throttle stamps or quiet periods."""
    monkeypatch.setattr(http_client, "_states", {})


@pytest.fixture(autouse=True)
def _no_ledger_sinks(monkeypatch):
    """events.install() hooks the request ledger into http_client and
    strava_client for the life of the process; a test that calls it must not
    leave later tests writing outbound counts into a database."""
    monkeypatch.setattr(http_client, "request_sink", None)
    monkeypatch.setattr(http_client, "outcome_sink", None)
    monkeypatch.setattr(strava_client, "rate_sink", None)


@pytest.fixture(autouse=True)
def _empty_search_graph_cache(monkeypatch):
    """Every test database numbers its first area 1; a graph cached by an
    earlier test must not answer for it."""
    monkeypatch.setattr(route_search, "_graph_cache", OrderedDict())


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("miles-template") / "activities.db"
    conn = db.connect(path)
    db.init_db(conn)
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _isolated_database(monkeypatch, tmp_path_factory, _schema_template):
    """Each test's default database is a copy of an initialized schema in a
    fresh temp directory, so request paths need no setup of their own; its
    extract directory is fresh too, and opening the developer's real
    database fails."""
    root = tmp_path_factory.mktemp("miles-data")
    shutil.copy(_schema_template, root / "activities.db")
    monkeypatch.setattr(db, "DB_PATH", root / "activities.db")
    monkeypatch.setattr(extracts, "EXTRACT_DIR", root / "extracts")
    monkeypatch.setattr(streams, "STREAMS_DIR", root / "streams")

    real_connect = db.connect

    def guarded_connect(path: Path | None = None):
        if path is not None and Path(path).resolve() == _REAL_DB:
            raise RuntimeError(f"test attempted to open the real database at {_REAL_DB}")
        return real_connect(path)

    monkeypatch.setattr(db, "connect", guarded_connect)


@pytest.fixture
def conn():
    """A fresh in-memory database with the full schema."""
    connection = memory_db()
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _no_backfill_pause(monkeypatch):
    monkeypatch.setattr(terrain_backfill, "BATCH_PAUSE_S", 0.0)


@pytest.fixture
def reported(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str]]:
    """Every outbound request the ledger would be told about, as
    (service, host, status, kind)."""
    seen: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(http_client, "request_sink", lambda *a: seen.append(a))
    return seen


@pytest.fixture
def outcomes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Every final 429 or 401 the health check would be told about, as (service, status)."""
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(http_client, "outcome_sink", lambda service, status: seen.append((service, status)))
    return seen


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """strava_budget's waits, recorded instead of slept."""
    seen: list[float] = []
    monkeypatch.setattr(strava_budget.time, "sleep", seen.append)
    return seen


@pytest.fixture
def strava_allowance(monkeypatch: pytest.MonkeyPatch):
    """Pin the allowance strava_client reports. The fixture value is a
    function taking the daily and 15-minute figures (None means unknown);
    both start unknown."""
    figures: dict[str, int | None] = {"daily": None, "short": None}
    monkeypatch.setattr(strava_client, "daily_calls_remaining", lambda: figures["daily"])
    monkeypatch.setattr(strava_client, "short_window_calls_remaining", lambda: figures["short"])

    def set_figures(daily: int | None = None, short: int | None = None) -> None:
        figures["daily"], figures["short"] = daily, short

    return set_figures


@pytest.fixture
def osrm(monkeypatch: pytest.MonkeyPatch) -> FakeOsrm:
    """drive_starts' OSRM request replaced by a configurable fake."""
    fake = FakeOsrm()
    monkeypatch.setattr(drive_starts, "_fetch_with_backoff", fake)
    return fake


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """http_client's monotonic and sleep replaced by a virtual clock."""
    return FakeClock().install(monkeypatch)
