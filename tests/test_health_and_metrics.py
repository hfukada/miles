import json
import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import FakeClock, FakeResponse, call_tool, http_error, urlopen_sequence
from mcp.server.fastmcp import FastMCP

from miles import db, events, health, health_api, http_client, metrics, outbound, outbound_mcp, strava_client, sync, terrain


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
    assert "details" not in health.check(conn)
    assert "details" in health.check(conn, verbose=True)


def test_no_sync_ever_needs_attention(conn: sqlite3.Connection):
    assert health.check(conn)["reasons"] == ["no sync has completed yet"]


def test_a_failed_last_sync_needs_attention_until_a_later_success(conn: sqlite3.Connection):
    _meta(conn, sync.META_LAST_SUCCESS, _ago(hours=5))
    _meta(conn, sync.META_LAST_FAILURE, _ago(hours=1))
    _meta(conn, sync.META_LAST_ERROR, "RuntimeError: boom")

    result = health.check(conn, verbose=True)
    assert result["reasons"] == ["last sync failed"]
    assert result["details"]["last_sync_error"] == "RuntimeError: boom"

    _meta(conn, sync.META_LAST_SUCCESS, _ago(minutes=1))
    assert health.check(conn)["status"] == "ok"


def test_a_failure_with_no_success_ever_is_one_reason_not_two(conn: sqlite3.Connection):
    _meta(conn, sync.META_LAST_FAILURE, _ago(hours=1))
    assert health.check(conn)["reasons"] == ["last sync failed"]


def test_a_stale_sync_needs_attention_and_the_limit_is_configurable(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _meta(conn, sync.META_LAST_SUCCESS, _ago(hours=40))
    assert "no successful sync in 40 h (limit 36 h)" in health.check(conn)["reasons"]

    monkeypatch.setenv(health.SYNC_MAX_AGE_ENV, "48")
    assert health.check(conn)["status"] == "ok"


def test_a_429_in_the_last_day_needs_attention_and_an_older_one_does_not(conn: sqlite3.Connection):
    _healthy(conn)
    _event(conn, "osrm", 429, _ago(hours=30))
    assert health.check(conn)["status"] == "ok"

    _event(conn, "osrm", 429, _ago(hours=3))
    _event(conn, "nominatim", 429, _ago(hours=1))
    result = health.check(conn, verbose=True)
    assert result["reasons"] == [
        "nominatim answered 429 in the last 24 h (1x)",
        "osrm answered 429 in the last 24 h (1x)",
    ]
    assert result["details"]["throttled_24h"] == {"osrm": 1, "nominatim": 1}


def test_rejected_strava_credentials_need_attention_until_a_sync_succeeds_after_them(conn: sqlite3.Connection):
    _meta(conn, sync.META_LAST_SUCCESS, _ago(hours=5))
    _event(conn, "strava", 401, _ago(hours=2))
    assert health.check(conn)["reasons"] == ["Strava rejected the credentials (401); re-authorize"]

    _meta(conn, sync.META_LAST_SUCCESS, _ago(hours=1))
    assert health.check(conn)["status"] == "ok"


def test_a_backfill_error_needs_attention_only_while_history_work_remains(conn: sqlite3.Connection):
    _healthy(conn)
    conn.execute("UPDATE terrain_backfill_status SET status = 'error', error = 'boom' WHERE id = 1")
    conn.commit()
    assert health.check(conn)["status"] == "ok"

    _queue_history(conn)
    assert health.check(conn)["reasons"] == ["terrain backfill in error"]


def test_a_run_interrupted_by_a_restart_is_not_an_alert(conn: sqlite3.Connection):
    _healthy(conn)
    _queue_history(conn)
    conn.execute(
        "UPDATE terrain_backfill_status SET status = 'error', error = ? WHERE id = 1", (outbound.BACKFILL_INTERRUPTED_ERROR,)
    )
    _area(conn, "cut off", outbound.IMPORT_INTERRUPTED_ERROR, _ago(days=1))
    assert health.check(conn)["status"] == "ok"


def test_a_failed_terrain_rebuild_needs_attention_only_while_the_model_is_behind(conn: sqlite3.Connection):
    _healthy(conn)
    _meta(conn, terrain.REBUILD_ERROR_META, "ValueError: bad")
    assert health.check(conn)["reasons"] == ["terrain model rebuild failed"]

    _meta(conn, "terrain_model_version", terrain.model_version_tag())
    assert health.check(conn)["status"] == "ok"


def test_an_area_import_error_counts_for_a_week(conn: sqlite3.Connection):
    _healthy(conn)
    _area(conn, "recent", "boom", _ago(days=2))
    _area(conn, "old", "boom", _ago(days=9))
    assert health.check(conn)["reasons"] == ["area import failed in the last 7 days (recent)"]


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


def test_a_connection_error_counts_as_an_error_attempt(clock: FakeClock, monkeypatch: pytest.MonkeyPatch, reported):
    urlopen_sequence(monkeypatch, [urllib.error.URLError("down")])
    policy = http_client.ServicePolicy("t", max_attempts=2)

    with pytest.raises(urllib.error.URLError):
        http_client.request(policy, "http://x.example/")

    assert [s[2] for s in reported] == ["error", "error"]


def test_time_spent_waiting_on_the_throttle_is_counted(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    urlopen_sequence(monkeypatch, [FakeResponse()])
    policy = http_client.ServicePolicy("slow", min_interval_s=2.0)

    http_client.request(policy, "http://x")
    http_client.request(policy, "http://x")

    assert metrics.get(metrics.HTTP_WAIT_SECONDS, service="slow") == pytest.approx(2.0)


def test_without_a_sink_nothing_is_reported_and_nothing_breaks(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(http_client, "request_sink", None)
    urlopen_sequence(monkeypatch, [FakeResponse()])
    assert http_client.request(http_client.ServicePolicy("t"), "http://x.example/") == b"ok"


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


def test_requests_are_counted_per_day_service_host_kind_and_status(ledger: sqlite3.Connection):
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


def test_only_429_and_401_outcomes_leave_a_timestamped_event(ledger: sqlite3.Connection):
    events.record_request("osrm", "h", "429")
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


def test_daily_counts_covers_only_the_requested_window(ledger: sqlite3.Connection):
    for days_ago in (0, 1, 5):
        day = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
        ledger.execute("INSERT INTO outbound_requests (day, service, host, kind, status, count) VALUES (?, 's', 'h', '', '200', ?)", (day, days_ago + 1))
    ledger.commit()

    assert [r["count"] for r in events.daily_counts(ledger, 2)] == [1, 2]
    assert [r["count"] for r in events.daily_counts(ledger, 30)] == [1, 2, 6]


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


def test_the_mcp_tool_returns_the_same_rows():
    conn = db.connect()
    day = datetime.now(timezone.utc).date().isoformat()
    conn.execute("INSERT INTO outbound_requests (day, service, host, kind, status, count) VALUES (?, 'nominatim', 'geo.example', '', '200', 2)", (day,))
    conn.commit()
    conn.close()
    server = FastMCP("test")
    outbound_mcp.register_outbound_tools(server)

    rows = call_tool(server, "get_outbound_requests", {"days": 30})

    assert rows == [{"day": day, "service": "nominatim", "host": "geo.example", "kind": "", "status": "200", "count": 2}]
