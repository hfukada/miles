import sqlite3
from datetime import date, timedelta

import pytest
from helpers import insert_activity, strava_fault
from stravalib.model import Stream

from miles import db, elevation, strava_budget, strava_client, streams, terrain, terrain_backfill


@pytest.fixture(autouse=True)
def _no_dem_fetch(monkeypatch):
    monkeypatch.setattr(elevation, "ensure_tiles", lambda conn, **kwargs: None)
    monkeypatch.setattr(elevation, "sampler_for", lambda conn: _NoDemSampler())


class _NoDemSampler:
    def sample(self, points):
        return [None] * len(points)


def _run(conn: sqlite3.Connection, days_ago: int, activity_id: int, sport_type: str = "Run") -> None:
    insert_activity(conn, activity_id, start_date=(date.today() - timedelta(days=days_ago)).isoformat() + "T08:00:00", sport_type=sport_type)


def _stream(data: list, stream_type: str) -> Stream:
    return Stream(type=stream_type, data=data, series_type="distance", original_size=len(data), resolution="high")


def _fake_raw_streams(n: int = 30) -> dict:
    return {
        "time": _stream([i * 4 for i in range(n)], "time"),
        "distance": _stream([i * 10.0 for i in range(n)], "distance"),
        "altitude": _stream([i * 1.0 for i in range(n)], "altitude"),
        "velocity_smooth": _stream([2.5] * n, "velocity_smooth"),
        "heartrate": _stream([140] * n, "heartrate"),
        "cadence": _stream([80] * n, "cadence"),
        "moving": _stream([True] * n, "moving"),
        "latlng": _stream([[40.0, -105.0]] * n, "latlng"),
    }


def _model_is_current(conn: sqlite3.Connection) -> None:
    """Skip the full-rebuild pass a stale terrain model would trigger."""
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('terrain_model_version', ?)", [terrain.model_version_tag()])
    conn.commit()


def _backfill(conn: sqlite3.Connection, **kwargs) -> int:
    return terrain_backfill.run_backfill(conn, terrain_backfill.BackfillProgress(conn), **kwargs)


def _segment_count(conn: sqlite3.Connection, activity_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM terrain_segments WHERE activity_id = ?", [activity_id]).fetchone()[0]


def _queued(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM terrain_backfill_queue").fetchone()[0]


def _failures(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM terrain_backfill_failures").fetchone()[0]


def _strava_streams(monkeypatch: pytest.MonkeyPatch, fetch=None) -> list[int]:
    """Answer stream requests with a short synthetic run (or with `fetch`);
    returns the activity ids asked for."""
    asked: list[int] = []

    def get(activity_id: int):
        asked.append(activity_id)
        return fetch(activity_id) if fetch else _fake_raw_streams()

    monkeypatch.setattr(strava_client, "get_activity_streams", get)
    return asked


def _no_strava_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strava_client, "get_activity_streams", lambda aid: pytest.fail("no Strava fetch needed"))


# ---------------------------------------------------------------------------
# refresh_backfill_queue
# ---------------------------------------------------------------------------


def test_the_queue_holds_runs_that_need_streams_and_drops_them_once_fetched_elsewhere(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _run(conn, 8, 2)
    _run(conn, 7, 3, sport_type="VirtualRun")  # never queued
    needing = {1, 3}
    monkeypatch.setattr(streams, "needs_streams_fetch", lambda aid: aid in needing)

    assert terrain_backfill.refresh_backfill_queue(conn) == 1
    assert {r["activity_id"] for r in conn.execute("SELECT activity_id FROM terrain_backfill_queue")} == {1}

    needing.clear()  # an interactive fetch elsewhere cached it in the meantime
    assert terrain_backfill.refresh_backfill_queue(conn) == 0
    assert _queued(conn) == 0


# ---------------------------------------------------------------------------
# run_backfill
# ---------------------------------------------------------------------------


def test_run_backfill_fetches_caches_and_segments(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _run(conn, 8, 2)
    _strava_streams(monkeypatch)

    progress = terrain_backfill.BackfillProgress(conn)
    assert progress.try_start()
    fetched = terrain_backfill.run_backfill(conn, progress)

    assert fetched == 2
    assert _queued(conn) == 0
    for activity_id in (1, 2):
        assert streams.needs_streams_fetch(activity_id) is False
        assert _segment_count(conn, activity_id) > 0

    snap = progress.snapshot()
    assert snap["status"] == "running"  # run_backfill doesn't call finish() -- the caller does
    assert snap["done"] == 2
    assert snap["total"] == 2


def test_run_backfill_only_ids_fetches_just_those_activities(conn: sqlite3.Connection, monkeypatch):
    for activity_id in (1, 2, 3):
        _run(conn, 10 - activity_id, activity_id)
    asked = _strava_streams(monkeypatch)

    assert _backfill(conn, only_ids={2}) == 1
    assert asked == [2]
    assert _queued(conn) == 2


def test_run_backfill_with_an_empty_queue_does_nothing(conn: sqlite3.Connection, monkeypatch):
    monkeypatch.setattr(streams, "needs_streams_fetch", lambda aid: False)
    assert _backfill(conn) == 0


@pytest.mark.parametrize(
    "daily_left, reserve_from",
    [(5, "gate"), (40, "environment")],
)
def test_the_reserve_stops_the_run_with_the_queue_intact(conn: sqlite3.Connection, monkeypatch, strava_allowance, daily_left: int, reserve_from: str):
    _run(conn, 9, 1)
    strava_allowance(daily=daily_left)
    _no_strava_fetch(monkeypatch)
    gate = None
    if reserve_from == "gate":
        gate = strava_budget.Gate(10)
    else:
        monkeypatch.setenv(strava_budget.RESERVE_ENV, "50")

    assert _backfill(conn, **({"gate": gate} if gate else {})) == 0
    assert _queued(conn) == 1  # nothing fetched, but queued for next time


def test_the_reserve_is_checked_before_every_request(conn: sqlite3.Connection, monkeypatch):
    for activity_id in range(1, 6):
        _run(conn, activity_id, activity_id)
    left = {"daily": 14}
    monkeypatch.setattr(strava_client, "daily_calls_remaining", lambda: left["daily"])

    def fetch(aid):
        left["daily"] -= 1
        return _fake_raw_streams()

    asked = _strava_streams(monkeypatch, fetch)

    assert _backfill(conn, gate=strava_budget.Gate(12)) == 2
    assert len(asked) == 2


def test_a_nearly_spent_window_is_waited_out_before_the_next_request(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    readings = iter([3, None, None])
    monkeypatch.setattr(strava_client, "short_window_calls_remaining", lambda: next(readings, None))
    waits: list[int] = []
    monkeypatch.setattr(strava_budget, "wait_for_next_window", lambda on_wait=None: waits.append(1))
    _strava_streams(monkeypatch)

    assert _backfill(conn) == 1
    assert waits == [1]


def test_run_backfill_resumes_after_a_429(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    monkeypatch.setattr(strava_budget, "wait_for_next_window", lambda on_wait=None: None)

    def fetch(aid):
        if len(asked) == 1:
            raise strava_fault(429)
        return _fake_raw_streams()

    asked = _strava_streams(monkeypatch, fetch)

    assert _backfill(conn) == 1
    assert len(asked) == 2
    assert _queued(conn) == 0


def test_a_daily_limit_hit_with_a_429_ends_the_run_quietly_with_the_queue_intact(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _model_is_current(conn)
    monkeypatch.setattr(strava_budget, "wait_for_next_window", lambda on_wait=None: None)

    def limited(aid):
        raise strava_fault(429)

    _strava_streams(monkeypatch, limited)

    assert _backfill(conn) == 0
    assert _queued(conn) == 1


@pytest.mark.parametrize("status", [404, 403])
def test_a_missing_or_private_activity_is_recorded_against_it_and_skipped(conn: sqlite3.Connection, monkeypatch, status: int):
    _run(conn, 7, 1)  # newest: first in the queue
    _run(conn, 8, 2)
    _run(conn, 9, 3)
    _model_is_current(conn)

    def fetch(aid):
        if aid == 1:
            raise strava_fault(status)
        return _fake_raw_streams()

    _strava_streams(monkeypatch, fetch)

    assert _backfill(conn) == 2
    assert _segment_count(conn, 2) > 0 and _segment_count(conn, 3) > 0
    assert conn.execute("SELECT status_code FROM terrain_backfill_failures WHERE activity_id = 1").fetchone()[0] == status
    assert terrain_backfill.refresh_backfill_queue(conn) == 0  # not queued again


def test_an_expired_token_stops_the_run_without_blaming_the_activity(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 8, 1)
    _run(conn, 9, 2)
    _model_is_current(conn)

    def fetch(aid):
        raise strava_fault(401)

    asked = _strava_streams(monkeypatch, fetch)
    with pytest.raises(terrain_backfill.BackfillAbortedError, match="401"):
        _backfill(conn)

    assert len(asked) == 1
    assert _failures(conn) == 0
    assert terrain_backfill.refresh_backfill_queue(conn) == 2


def test_a_transient_server_error_is_retried_and_not_held_against_the_activity(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 8, 1)
    _model_is_current(conn)

    def fetch(aid):
        if len(asked) == 1:
            raise strava_fault(500)
        return _fake_raw_streams()

    asked = _strava_streams(monkeypatch, fetch)

    assert _backfill(conn) == 1
    assert _failures(conn) == 0


def test_persistent_server_errors_stop_the_run(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 8, 1)
    _run(conn, 9, 2)
    _model_is_current(conn)

    def fetch(aid):
        raise strava_fault(502)

    asked = _strava_streams(monkeypatch, fetch)
    with pytest.raises(terrain_backfill.BackfillAbortedError):
        _backfill(conn)

    assert len(asked) == terrain_backfill.MAX_CONSECUTIVE_SERVICE_FAILURES
    assert _failures(conn) == 0


# ---------------------------------------------------------------------------
# BackfillProgress persistence
# ---------------------------------------------------------------------------


def test_backfill_progress_persists_across_connections():
    progress1 = terrain_backfill.BackfillProgress(db.connect())
    assert progress1.try_start() is True
    progress1.phase("streams", done=3, total=10)

    progress2 = terrain_backfill.BackfillProgress(db.connect())
    snap = progress2.snapshot()
    assert snap["status"] == "running"
    assert snap["done"] == 3
    assert snap["total"] == 10
    assert progress2.try_start() is False  # already running, per the persisted status


# ---------------------------------------------------------------------------
# Segmenting what is cached
# ---------------------------------------------------------------------------


def test_run_whose_streams_were_stored_by_an_activity_view_still_gets_segmented(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _model_is_current(conn)
    streams.cache_streams(1, _fake_raw_streams())  # stored outside the backfill
    _no_strava_fetch(monkeypatch)

    assert _segment_count(conn, 1) == 0
    _backfill(conn)

    assert _segment_count(conn, 1) > 0
    assert conn.execute("SELECT COUNT(*) FROM terrain_segment_dates WHERE activity_id = 1").fetchone()[0] == 1


def test_a_run_that_fails_to_segment_is_not_retried_by_the_next_backfill(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _model_is_current(conn)
    streams.cache_streams(1, _fake_raw_streams())
    _no_strava_fetch(monkeypatch)
    attempts: list[int] = []

    def failing(conn, activity_id, *args, **kwargs):
        attempts.append(activity_id)
        raise ValueError("bad segment")

    monkeypatch.setattr(terrain, "segment_and_store_activity", failing)

    _backfill(conn)
    _backfill(conn)

    assert attempts == [1]
    assert streams.has_cached_streams(1)


def test_a_cached_run_with_an_unreadable_stream_file_is_discarded_and_queued_for_refetch(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _model_is_current(conn)
    streams.cache_streams(1, _fake_raw_streams())
    streams._raw_path(1).write_bytes(b"not gzip")
    _no_strava_fetch(monkeypatch)

    _backfill(conn)

    assert streams.needs_streams_fetch(1)
    assert terrain_backfill.refresh_backfill_queue(conn) == 1


def test_a_run_with_no_segments_is_processed_once_not_on_every_backfill(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _model_is_current(conn)
    raw = _fake_raw_streams()
    raw["altitude"] = _stream([None] * 30, "altitude")  # treadmill: no altitude
    streams.cache_streams(1, raw)
    _no_strava_fetch(monkeypatch)

    assert terrain.activities_to_segment(conn) == [1]
    _backfill(conn)

    assert _segment_count(conn, 1) == 0
    assert conn.execute("SELECT segment_count FROM terrain_segment_dates WHERE activity_id = 1").fetchone()["segment_count"] == 0
    assert terrain.activities_to_segment(conn) == []


def test_a_run_segmented_without_tile_fetching_is_redraped_by_the_backfill(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _model_is_current(conn)
    df = streams.cache_streams(1, _fake_raw_streams())
    terrain.segment_cached_activity(conn, 1, df)  # rebuild-style: no fetching, recorded altitude only
    assert terrain.activities_to_segment(conn) == [1]
    _no_strava_fetch(monkeypatch)

    _backfill(conn)

    assert terrain.activities_to_segment(conn) == []


def test_backfill_rebuilds_instead_of_just_stamping_a_stale_model_version(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _run(conn, 8, 2)
    streams.cache_streams(1, _fake_raw_streams())
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('terrain_model_version', '0.0')")
    # A segment row left over from an older model for an activity whose
    # streams are cached: only a real rebuild replaces it.
    conn.execute(
        "INSERT INTO terrain_segments (activity_id, idx, dist_m, grade_dpct, is_walking, ele_source) "
        "VALUES (1, 0, 0.0, 999, 0, 0)"
    )
    conn.execute("INSERT INTO terrain_segment_dates (activity_id, start_date) VALUES (1, ?)", [date.today().isoformat()])
    conn.commit()
    _strava_streams(monkeypatch)

    _backfill(conn)

    grades = {r["grade_dpct"] for r in conn.execute("SELECT grade_dpct FROM terrain_segments WHERE activity_id = 1")}
    assert 999 not in grades and grades
    version = conn.execute("SELECT value FROM meta WHERE key = 'terrain_model_version'").fetchone()["value"]
    assert version == terrain.model_version_tag()


def test_backfill_writes_snapshots_for_what_it_segmented(conn: sqlite3.Connection, monkeypatch):
    _run(conn, 9, 1)
    _model_is_current(conn)
    _strava_streams(monkeypatch)

    assert conn.execute("SELECT COUNT(*) FROM terrain_snapshots").fetchone()[0] == 0
    _backfill(conn)
    assert conn.execute("SELECT COUNT(*) FROM terrain_snapshots").fetchone()[0] > 0
