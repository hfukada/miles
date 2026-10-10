import json
import sqlite3
import urllib.error
from datetime import date, datetime, timedelta, timezone

import pytest
from helpers import insert_activity

from miles import db, http_client, mcp_server, sync, weather


def _spec(activity_id: int, day: date, duration_s: int = 3600) -> weather.WeatherSpec:
    return {
        "activity_id": activity_id,
        "start_dt": datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc),
        "duration_s": duration_s,
    }


def _old(offset: int) -> date:
    return date(2020, 1, 1) + timedelta(days=offset)


def test_runs_within_a_fortnight_share_a_window_none_spans_more_and_recent_runs_use_the_forecast_endpoint():
    specs = [_spec(1, _old(0)), _spec(2, _old(13)), _spec(3, _old(14))]
    windows = weather.plan_windows(specs)
    assert [[s["activity_id"] for s in w.specs] for w in windows] == [[1, 2], [3]]
    assert (windows[0].start_date, windows[0].end_date) == (_old(0), _old(13))

    specs = [_spec(i, _old(i * 5)) for i in range(60)]
    windows = weather.plan_windows(specs)
    assert len(windows) == 20  # three runs (days 0, 5, 10) fit each fourteen-day window
    assert all((w.end_date - w.start_date).days + 1 <= weather.MAX_WINDOW_DAYS for w in windows)
    assert sum(len(w.specs) for w in windows) == 60

    today = date(2026, 3, 20)
    specs = [_spec(1, today - timedelta(days=30)), _spec(2, today - timedelta(days=1))]
    windows = weather.plan_windows(specs, today=today)
    assert [(w.use_archive, [s["activity_id"] for s in w.specs]) for w in windows] == [(True, [1]), (False, [2])]


class FakeOpenMeteo:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail_on: int | None = None, status: int = 429) -> None:
        self.urls: list[str] = []
        self.fail_on = fail_on
        self.status = status
        self.empty = False
        self.all_null = False
        self.fail_all = False
        monkeypatch.setattr(http_client, "request", self._request)

    def _request(self, policy: http_client.ServicePolicy, url: str) -> bytes:
        self.urls.append(url)
        if self.fail_all or (self.fail_on is not None and len(self.urls) == self.fail_on):
            raise urllib.error.HTTPError(url, self.status, "limit", {}, None)  # type: ignore[arg-type]
        if self.empty:
            return json.dumps({"hourly": {"time": []}}).encode()
        # Hourly rows over the whole requested span.
        query = dict(p.split("=") for p in url.split("?", 1)[1].split("&"))
        start = datetime.fromisoformat(query["start_date"])
        end = datetime.fromisoformat(query["end_date"]) + timedelta(days=1)
        times = []
        t = start
        while t < end:
            times.append(t.strftime("%Y-%m-%dT%H:%M"))
            t += timedelta(hours=1)
        n = len(times)
        if self.all_null:
            return json.dumps({"hourly": {
                "time": times,
                **{name: [None] * n for name in (
                    "temperature_2m", "apparent_temperature", "relative_humidity_2m", "precipitation", "windspeed_10m"
                )},
            }}).encode()
        return json.dumps({"hourly": {
            "time": times,
            "temperature_2m": [10.0] * n,
            "apparent_temperature": [9.0] * n,
            "relative_humidity_2m": [50.0] * n,
            "precipitation": [0.0] * n,
            "windspeed_10m": [5.0] * n,
        }}).encode()


def _insert_run(conn: sqlite3.Connection, activity_id: int, day: date, lat: float = 40.0, lng: float = -105.0) -> None:
    start = f"{day.isoformat()}T08:00:00Z"
    insert_activity(
        conn, activity_id, start_date=start, moving_time_s=3600, start_lat=lat, start_lng=lng,
        raw_json=json.dumps({"start_date": start}),
    )


def _stale() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=weather.NO_DATA_RETRY_DAYS + 1)).isoformat()


def _weather_ids(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT activity_id FROM weather")}


@pytest.mark.parametrize("status", [429, 500, 404])
def test_a_refusal_that_says_stop_ends_the_pass_and_keeps_what_was_stored(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, status: int):
    for i, offset in enumerate((0, 100, 200), 1):  # three windows at one location
        _insert_run(conn, i, _old(offset))
    fake = FakeOpenMeteo(monkeypatch, fail_on=2, status=status)

    sync._pass_weather(conn, sync.SyncProgress())

    assert len(fake.urls) == 2  # the third window was never attempted
    assert _weather_ids(conn) == {1}  # windows run oldest first; the failing request stored nothing

    fake.fail_on = None
    sync._pass_weather(conn, sync.SyncProgress())
    assert _weather_ids(conn) == {1, 2, 3}


@pytest.mark.parametrize("status", [400, 422])
def test_a_window_the_service_cannot_answer_leaves_a_marker_and_does_not_block_later_ones(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status: int
):
    for i, offset in enumerate((0, 100, 200), 1):
        _insert_run(conn, i, _old(offset))
    fake = FakeOpenMeteo(monkeypatch, fail_on=2, status=status)

    with caplog.at_level("WARNING", logger="miles.weather"):
        sync._pass_weather(conn, sync.SyncProgress())

    assert len(fake.urls) == 3
    rows = {r["activity_id"]: r for r in conn.execute("SELECT * FROM weather")}
    assert set(rows) == {1, 2, 3}
    assert rows[2]["hourly_json"] is None  # a no-data marker, retried only after the retry age
    assert rows[1]["hourly_json"] is not None and rows[3]["hourly_json"] is not None
    assert any(str(status) in r.getMessage() and _old(100).isoformat() in r.getMessage() for r in caplog.records)


def test_a_location_whose_every_window_is_refused_stops_the_pass(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _insert_run(conn, 1, _old(0))
    _insert_run(conn, 2, _old(100), lat=41.0)
    fake = FakeOpenMeteo(monkeypatch, status=400)
    fake.fail_all = True

    sync._pass_weather(conn, sync.SyncProgress())

    assert len(fake.urls) == 1  # the second location is never reached
    assert _weather_ids(conn) == {2}


@pytest.mark.parametrize("mode", ["all_null", "empty"])
def test_hours_with_no_values_leave_a_marker_that_is_not_requested_again_until_the_retry_age(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, mode: str
):
    _insert_run(conn, 1, _old(0))
    fake = FakeOpenMeteo(monkeypatch)
    setattr(fake, mode, True)

    sync._pass_weather(conn, sync.SyncProgress())
    row = conn.execute("SELECT * FROM weather WHERE activity_id = 1").fetchone()
    assert row is not None and row["hourly_json"] is None and row["temp_c_avg"] is None
    assert len(fake.urls) == 1

    sync._pass_weather(conn, sync.SyncProgress())
    assert len(fake.urls) == 1

    conn.execute("UPDATE weather SET fetched_at = ?", [_stale()])
    conn.commit()
    setattr(fake, mode, False)
    sync._pass_weather(conn, sync.SyncProgress())
    assert len(fake.urls) == 2
    assert conn.execute("SELECT hourly_json FROM weather WHERE activity_id = 1").fetchone()[0] is not None

    # A stored row whose hours hold no values is asked about again after the retry age too.
    _insert_run(conn, 2, _old(100))
    conn.execute(
        "INSERT INTO weather (activity_id, fetched_at, hourly_json, raw_json) VALUES (2, ?, '[{\"temp_c\": null}]', '{}')",
        [_stale()],
    )
    conn.commit()
    sync._pass_weather(conn, sync.SyncProgress())
    assert len(fake.urls) == 3
    assert conn.execute("SELECT temp_c_avg FROM weather WHERE activity_id = 2").fetchone()[0] == 10.0


def test_a_long_history_at_one_location_costs_one_request_per_window(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    for i in range(12):  # a run every 30 days: each its own window, never one multi-year request
        _insert_run(conn, i + 1, _old(i * 30))
    fake = FakeOpenMeteo(monkeypatch)

    sync._pass_weather(conn, sync.SyncProgress())

    assert len(fake.urls) == 12
    assert conn.execute("SELECT COUNT(*) FROM weather").fetchone()[0] == 12


def test_the_tools_credit_only_rows_that_have_weather(monkeypatch: pytest.MonkeyPatch):
    conn = db.connect()
    _insert_run(conn, 1, _old(0))
    _insert_run(conn, 2, _old(5))
    FakeOpenMeteo(monkeypatch)
    for rows, _no_data in weather.fetch_weather_windows([_spec(1, _old(0))], 40.0, -105.0):
        db.upsert_weather(conn, rows)

    out = {r["activity_id"]: r for r in json.loads(mcp_server.get_activities())}
    assert out[1]["weather_attribution"] == weather.ATTRIBUTION
    assert "weather_attribution" not in out[2]

    detail = json.loads(mcp_server.get_activity_weather(1))
    assert detail["attribution"] == weather.ATTRIBUTION
    assert detail["hourly"] and detail["temp_c_avg"] == 10.0
    assert json.loads(mcp_server.get_activity_weather(2))["hourly"] is None
    assert "error" in json.loads(mcp_server.get_activity_weather(99))
