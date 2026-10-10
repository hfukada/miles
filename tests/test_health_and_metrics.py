import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import FakeClock, FakeResponse, http_error, urlopen_sequence

from miles import db, events, health, health_api, http_client, metrics, outbound, strava_client, sync, terrain


def _meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def _ago(**kwargs: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def _healthy(conn: sqlite3.Connection) -> None:
    _meta(conn, sync.META_LAST_SUCCESS, _ago(hours=2))


def _event(conn: sqlite3.Connection, service: str, status: int, at: str) -> None:
    conn.execute("INSERT INTO outbound_events (at, service, status) VALUES (?, ?, ?)", (at, service, status))
    conn.commit()


def _queue_history(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO terrain_backfill_queue (activity_id, start_date, queued_at) VALUES (1, '2026-01-01', 'x')")
    conn.commit()


def _area(conn: sqlite3.Connection, label: str, error: str, used: str) -> None:
    conn.execute(
        "INSERT INTO areas (label, center_lat, center_lng, radius_km, status, status_error, created_at, last_used_at) "
        "VALUES (?, 0, 0, 1, 'error', ?, ?, ?)",
        (label, error, used, used),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# health.check
# ---------------------------------------------------------------------------


def test_a_recent_successful_sync_and_nothing_else_is_ok(conn: sqlite3.Connection):
    _healthy(conn)
    assert health.check(conn) == {"status": "ok"}
    assert "details" in health.check(conn, verbose=True)


def _failed_sync(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    _meta(conn, sync.META_LAST_SUCCESS, _ago(hours=5))
    _meta(conn, sync.META_LAST_FAILURE, _ago(hours=1))
    _meta(conn, sync.META_LAST_ERROR, "RuntimeError: boom")


def _backfill_error(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy(conn)
    conn.execute("UPDATE terrain_backfill_status SET status = 'error', error = 'boom' WHERE id = 1")
    conn.commit()


def _interrupted_by_a_restart(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy(conn)
    _queue_history(conn)
    conn.execute(
        "UPDATE terrain_backfill_status SET status = 'error', error = ? WHERE id = 1", (outbound.BACKFILL_INTERRUPTED_ERROR,)
    )
    _area(conn, "cut off", outbound.IMPORT_INTERRUPTED_ERROR, _ago(days=1))


def _throttled(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    _event(conn, "osrm", 429, _ago(hours=3))
    _event(conn, "nominatim", 429, _ago(hours=1))


# Each case is a sequence of (change, the reasons health.check then gives, [] meaning ok).
HEALTH_CASES = {
    "no sync ever": [(lambda c, m: None, ["no sync has completed yet"])],
    "a failed last sync until a later success": [
        (_failed_sync, ["last sync failed"]),
        (lambda c, m: _meta(c, sync.META_LAST_SUCCESS, _ago(minutes=1)), []),
    ],
    "a failure with no success ever is one reason, not two": [
        (lambda c, m: _meta(c, sync.META_LAST_FAILURE, _ago(hours=1)), ["last sync failed"]),
    ],
    "a stale sync, with a configurable limit": [
        (lambda c, m: _meta(c, sync.META_LAST_SUCCESS, _ago(hours=40)), ["no successful sync in 40 h (limit 36 h)"]),
        (lambda c, m: m.setenv(health.SYNC_MAX_AGE_ENV, "48"), []),
    ],
    "a 429 in the last day but not an older one": [
        (lambda c, m: (_healthy(c), _event(c, "osrm", 429, _ago(hours=30))), []),
        (_throttled, ["nominatim answered 429 in the last 24 h (1x)", "osrm answered 429 in the last 24 h (1x)"]),
    ],
    "rejected Strava credentials until a later sync succeeds": [
        (
            lambda c, m: (_meta(c, sync.META_LAST_SUCCESS, _ago(hours=5)), _event(c, "strava", 401, _ago(hours=2))),
            ["Strava rejected the credentials (401); re-authorize"],
        ),
        (lambda c, m: _meta(c, sync.META_LAST_SUCCESS, _ago(hours=1)), []),
    ],
    "a backfill error only while history work remains": [
        (_backfill_error, []),
        (lambda c, m: _queue_history(c), ["terrain backfill in error"]),
    ],
    "a run interrupted by a restart is not an alert": [(_interrupted_by_a_restart, [])],
    "a failed terrain rebuild only while the model is behind": [
        (lambda c, m: (_healthy(c), _meta(c, terrain.REBUILD_ERROR_META, "ValueError: bad")), ["terrain model rebuild failed"]),
        (lambda c, m: _meta(c, "terrain_model_version", terrain.model_version_tag()), []),
    ],
    "an area import error for a week": [
        (
            lambda c, m: (_healthy(c), _area(c, "recent", "boom", _ago(days=2)), _area(c, "old", "boom", _ago(days=9))),
            ["area import failed in the last 7 days (recent)"],
        ),
    ],
}


@pytest.mark.parametrize("steps", HEALTH_CASES.values(), ids=HEALTH_CASES.keys())
def test_health_needs_attention_for_each_condition_and_clears_when_it_does(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, steps):
    for change, reasons in steps:
        change(conn, monkeypatch)
        result = health.check(conn)
        assert result.get("reasons", []) == reasons
        assert result["status"] == ("attention" if reasons else "ok")


def test_a_failed_sync_reports_its_error_and_its_details_only_when_verbose(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _failed_sync(conn, monkeypatch)
    assert "details" not in health.check(conn)
    assert health.check(conn, verbose=True)["details"]["last_sync_error"] == "RuntimeError: boom"


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(health_api.router)
    return TestClient(app)


def test_health_is_200_ok_when_fine_and_503_with_reasons_otherwise(client: TestClient):
    conn = db.connect()
    _healthy(conn)
    response = client.get("/api/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})

    _event(conn, "osrm", 429, _ago(minutes=5))
    conn.close()
    response = client.get("/api/health", params={"verbose": 1})
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "attention"
    assert body["reasons"] == ["osrm answered 429 in the last 24 h (1x)"]
    assert body["details"]["throttled_24h"] == {"osrm": 1}


def test_metrics_is_404_unless_enabled(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(metrics.ENABLE_ENV, raising=False)
    assert client.get("/metrics").status_code == 404
    monkeypatch.setenv(metrics.ENABLE_ENV, "1")
    assert client.get("/metrics").status_code == 200


def test_metrics_renders_text_format_with_sync_and_backfill_series(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(metrics.ENABLE_ENV, "1")
    metrics.reset()
    conn = db.connect()
    _meta(conn, sync.META_LAST_SUCCESS, "2026-01-02T03:04:05+00:00")
    _meta(conn, sync.META_LAST_DURATION, "12.5")
    _meta(conn, sync.META_LAST_NEW, "3")
    _queue_history(conn)
    conn.execute("INSERT INTO activities (activity_id, sport_type) VALUES (1, 'Run')")
    conn.commit()
    conn.close()
    metrics.cache("geocode", "hit")

    response = client.get("/metrics")

    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = response.text
    assert "# TYPE miles_cache_requests_total counter" in text
    assert 'miles_cache_requests_total{cache="geocode",result="hit"} 1' in text
    assert "miles_sync_last_success_timestamp_seconds 1767323045" in text
    assert "miles_sync_last_duration_seconds 12.5" in text
    assert "miles_sync_last_new_activities 3" in text
    assert 'miles_backfill_remaining{kind="streams"} 1' in text
    assert 'miles_backfill_remaining{kind="laps"} 1' in text
    assert "miles_sync_last_failure_timestamp_seconds" not in text


def test_a_malformed_sync_timestamp_does_not_break_the_metrics(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(metrics.ENABLE_ENV, "1")
    conn = db.connect()
    _meta(conn, sync.META_LAST_SUCCESS, "not a timestamp")
    conn.close()

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "miles_sync_last_success_timestamp_seconds" not in response.text


# ---------------------------------------------------------------------------
# metrics registry
# ---------------------------------------------------------------------------


def test_labels_are_escaped_and_samples_sorted():
    metrics.reset()
    metrics.inc("miles_test_total", 2, name='a"b\\c')
    metrics.set_gauge("miles_test_gauge", 1.5)
    text = metrics.render()
    assert 'miles_test_total{name="a\\"b\\\\c"} 2' in text
    assert "miles_test_gauge 1.5" in text


# ---------------------------------------------------------------------------
# http_client instrumentation and the persisted event log
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset()


def test_every_attempt_is_reported_with_its_host_and_retries_are_counted(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, reported: list[tuple[str, str, str, str]]
):
    urlopen_sequence(monkeypatch, [http_error(429), http_error(503), FakeResponse()])
    policy = http_client.ServicePolicy("t", retry_statuses=(429, 503), max_attempts=4, backoff_base_s=1.0)

    assert http_client.request(policy, "http://mirror.example:8080/x?y=1") == b"ok"

    assert reported == [
        ("t", "mirror.example", "429", ""),
        ("t", "mirror.example", "503", ""),
        ("t", "mirror.example", "200", ""),
    ]
    assert metrics.get(metrics.HTTP_RETRIES, service="t") == 2


def test_time_spent_waiting_on_the_throttle_is_counted(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    urlopen_sequence(monkeypatch, [FakeResponse()])
    policy = http_client.ServicePolicy("slow", min_interval_s=2.0)

    http_client.request(policy, "http://x")
    http_client.request(policy, "http://x")

    assert metrics.get(metrics.HTTP_WAIT_SECONDS, service="slow") == pytest.approx(2.0)


def test_install_hooks_the_ledger_into_both_clients():
    events.install()

    assert http_client.request_sink is events.record_request
    assert http_client.outcome_sink is events.record_outcome
    assert strava_client.rate_sink is events.record_strava_rate


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch):
    """The default (isolated) database, with the once-a-day prune reset."""
    monkeypatch.setattr(events, "_last_pruned_day", None)
    connection = db.connect()
    yield connection
    connection.close()


def test_requests_are_counted_per_day_service_host_kind_and_status_and_only_429_and_401_leave_an_event(ledger: sqlite3.Connection):
    events.record_request("osrm", "maps.example", "200")
    events.record_request("osrm", "maps.example", "200")
    events.record_request("osrm", "mirror.example", "200")
    events.record_request("strava", "www.strava.com", "200", "laps")
    events.record_request("strava", "www.strava.com", "200", "streams")

    rows = {(r["service"], r["host"], r["kind"], r["status"]): r["count"] for r in events.daily_counts(ledger, 1)}

    assert rows == {
        ("osrm", "maps.example", "", "200"): 2,
        ("osrm", "mirror.example", "", "200"): 1,
        ("strava", "www.strava.com", "laps", "200"): 1,
        ("strava", "www.strava.com", "streams", "200"): 1,
    }
    assert {(r["service"], r["host"], r["count"]) for r in events.totals(ledger) if r["kind"] == ""} == {
        ("osrm", "maps.example", 2),
        ("osrm", "mirror.example", 1),
    }

    events.record_outcome("osrm", 200)
    events.record_outcome("osrm", 429)
    events.record_outcome("strava", 401)
    events.record_outcome("osrm", 500)
    assert [(r["service"], r["status"]) for r in ledger.execute("SELECT service, status FROM outbound_events ORDER BY event_id")] == [
        ("osrm", 429),
        ("strava", 401),
    ]


def test_old_rows_are_pruned(ledger: sqlite3.Connection):
    old_day = (datetime.now(timezone.utc) - timedelta(days=events.RETENTION_DAYS + 1)).date().isoformat()
    ledger.execute("INSERT INTO outbound_requests (day, service, host, kind, status, count) VALUES (?, 'x', 'h', '', '200', 5)", (old_day,))
    ledger.execute("INSERT INTO outbound_events (at, service, status) VALUES (?, 'old', 429)", (_ago(days=events.EVENT_RETENTION_DAYS + 1),))
    ledger.commit()

    events.record_request("osrm", "h", "200")

    assert ledger.execute("SELECT COUNT(*) FROM outbound_requests WHERE service = 'x'").fetchone()[0] == 0
    assert ledger.execute("SELECT COUNT(*) FROM outbound_events WHERE service = 'old'").fetchone()[0] == 0


def test_the_strava_allowance_is_remembered(ledger: sqlite3.Connection):
    events.record_strava_rate(640, 85)
    events.record_strava_rate(639, 84)

    value = ledger.execute("SELECT value FROM meta WHERE key = ?", (events.STRAVA_RATE_META,)).fetchone()["value"]
    assert json.loads(value)["daily"] == 639


def test_recording_never_raises_when_the_database_is_unusable(monkeypatch: pytest.MonkeyPatch):
    def broken():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(events.db, "connect", broken)
    events.record_request("osrm", "h", "429")
    events.record_strava_rate(1, 1)


def test_the_outbound_endpoint_and_metrics_expose_the_ledger(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(metrics.ENABLE_ENV, "1")
    conn = db.connect()
    day = datetime.now(timezone.utc).date().isoformat()
    conn.execute("INSERT INTO outbound_requests (day, service, host, kind, status, count) VALUES (?, 'osrm', 'router.example', '', '200', 7)", (day,))
    conn.execute("INSERT INTO outbound_requests (day, service, host, kind, status, count) VALUES ('2020-01-01', 'osrm', 'router.example', '', '200', 3)")
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (events.STRAVA_RATE_META, json.dumps({"daily": 600, "short": 90, "at": "x"})))
    conn.commit()
    conn.close()

    assert client.get("/api/outbound", params={"days": 7}).json() == [
        {"day": day, "service": "osrm", "host": "router.example", "kind": "", "status": "200", "count": 7}
    ]
    text = client.get("/metrics").text
    assert 'miles_outbound_requests_total{host="router.example",kind="",service="osrm",status="200"} 10' in text
    assert "miles_strava_daily_calls_remaining 600" in text
    assert "miles_strava_short_window_calls_remaining 90" in text


