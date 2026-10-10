import sqlite3

import pytest
from helpers import activity_row as _row, insert_activity, strava_fault

from miles import db, errors, strava_budget, strava_client, streams, sync, terrain_backfill


def test_resyncing_an_activity_keeps_locally_owned_columns(conn: sqlite3.Connection) -> None:
    db.upsert_activities(conn, [_row(1)])
    conn.execute(
        "UPDATE activities SET description = 'felt good', laps_synced_at = '2026-01-06', "
        "run_type_inferred = 'long_run', race_effort = 'hard', effort_ratio = 1.1, "
        "dominant_intensity = 'tempo', workout_label = 'steady' WHERE activity_id = 1"
    )
    changed = _row(1)
    changed["name"] = "Renamed"
    changed["average_heartrate"] = 150.0
    db.upsert_activities(conn, [changed])
    got = conn.execute("SELECT * FROM activities WHERE activity_id = 1").fetchone()
    assert (got["name"], got["average_heartrate"]) == ("Renamed", 150.0)
    assert (got["description"], got["laps_synced_at"], got["run_type_inferred"]) == ("felt good", "2026-01-06", "long_run")
    assert (got["race_effort"], got["effort_ratio"], got["dominant_intensity"], got["workout_label"]) == ("hard", 1.1, "tempo", "steady")


@pytest.mark.parametrize(
    "field, value",
    [("distance_m", 9000.0), ("elapsed_time_s", 2500), ("start_date", "2026-01-03T08:00:00")],
)
def test_an_activity_changed_on_strava_has_its_laps_and_description_refetched(conn: sqlite3.Connection, field: str, value: object) -> None:
    db.upsert_activities(conn, [_row(1, workout_type=3)])
    conn.execute("INSERT INTO laps (lap_id, activity_id, lap_index) VALUES (11, 1, 0)")
    conn.execute(
        "UPDATE activities SET description = 'felt good', laps_synced_at = '2026-01-06', "
        "race_effort = 'hard', workout_label = 'steady' WHERE activity_id = 1"
    )
    conn.execute("DELETE FROM meta WHERE key = 'derive_dirty_since'")
    conn.commit()

    changed = _row(1, workout_type=3)
    changed[field] = value  # type: ignore[literal-required]
    db.upsert_activities(conn, [changed])

    got = conn.execute("SELECT * FROM activities WHERE activity_id = 1").fetchone()
    assert (got["description"], got["laps_synced_at"]) == (None, None)
    assert (got["race_effort"], got["workout_label"]) == ("hard", "steady")
    assert conn.execute("SELECT COUNT(*) FROM laps WHERE activity_id = 1").fetchone()[0] == 0


@pytest.mark.parametrize(
    "field, value",
    [("distance_m", 9000.0), ("elapsed_time_s", 2500), ("start_date", "2026-01-03T08:00:00")],
)
def test_an_activity_changed_on_strava_drops_its_terrain_and_weather_and_is_returned(conn: sqlite3.Connection, field: str, value: object) -> None:
    db.upsert_activities(conn, [_row(1), _row(2)])
    for activity_id in (1, 2):
        conn.execute(
            "INSERT INTO terrain_segment_dates (activity_id, start_date) VALUES (?, '2026-01-05')", [activity_id]
        )
        conn.execute(
            "INSERT INTO terrain_segments (activity_id, idx, dist_m, grade_dpct, is_walking, ele_source) "
            "VALUES (?, 0, 20, 10, 0, 1)",
            [activity_id],
        )
        conn.execute("INSERT INTO weather (activity_id, fetched_at) VALUES (?, 'now')", [activity_id])
    conn.commit()

    changed = _row(1)
    changed[field] = value  # type: ignore[literal-required]
    assert db.upsert_activities(conn, [changed, _row(2)]) == [1]

    for table in ("terrain_segments", "terrain_segment_dates", "weather"):
        remaining = {r[0] for r in conn.execute(f"SELECT activity_id FROM {table}")}
        assert remaining == {2}, table


@pytest.mark.parametrize(
    "field, value",
    [
        ("distance_m", 8000.0),  # not changed at all
        ("distance_m", 8000.4),
        ("elapsed_time_s", 2401),
        ("start_date", "2026-01-05T08:00:00+00:00"),
        ("start_date", "2026-01-05 08:00:00"),
    ],
)
def test_an_unchanged_activity_or_a_formatting_difference_is_not_an_edit(conn: sqlite3.Connection, field: str, value: object) -> None:
    db.upsert_activities(conn, [_row(1, workout_type=3)])
    conn.execute("UPDATE activities SET description = 'felt good', laps_synced_at = '2026-01-06' WHERE activity_id = 1")
    conn.commit()
    same = _row(1, workout_type=3)
    same[field] = value  # type: ignore[literal-required]
    db.upsert_activities(conn, [same])
    got = conn.execute("SELECT description, laps_synced_at FROM activities WHERE activity_id = 1").fetchone()
    assert (got["description"], got["laps_synced_at"]) == ("felt good", "2026-01-06")


def test_the_activities_pass_discards_the_raw_streams_of_a_changed_activity_after_the_commit(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.upsert_activities(conn, [_row(1), _row(2)])
    for activity_id in (1, 2):
        streams.store_raw_streams(activity_id, {})
    changed = _row(1)
    changed["distance_m"] = 9000.0
    monkeypatch.setattr(strava_client, "get_activities", lambda after_ts=None: iter([changed, _row(2)]))
    committed_when_discarded: list[bool] = []
    real_discard = streams.discard_raw_streams

    def discard(activity_id: int) -> None:
        committed_when_discarded.append(not conn.in_transaction)
        real_discard(activity_id)

    monkeypatch.setattr(streams, "discard_raw_streams", discard)

    count, _ = sync._pass_activities(conn, True, sync.SyncProgress(None), strava_budget.Gate())

    assert count == 1
    assert committed_when_discarded == [True]
    assert streams.needs_streams_fetch(1)
    assert not streams.needs_streams_fetch(2)


def test_an_incremental_sync_picks_up_an_activity_uploaded_days_late(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    db.upsert_activities(conn, [_row(1, "2026-01-20T08:00:00")])
    late = _row(2, "2026-01-12T08:00:00")  # run on the 12th, uploaded after the 20th run synced
    asked: list[str | None] = []

    def listing(after_ts: str | None = None):
        asked.append(after_ts)
        assert after_ts is not None
        for row in (_row(1, "2026-01-20T08:00:00"), late):
            if row["start_date"] > after_ts:
                yield row

    monkeypatch.setattr(strava_client, "get_activities", listing)

    count, new_ids = sync._pass_activities(conn, False, sync.SyncProgress(None), strava_budget.Gate())

    assert new_ids == {2}
    assert count == 1  # the re-listed run 1 is unchanged
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE activity_id = 2").fetchone()[0] == 1
    assert asked[0] is not None and asked[0] < "2026-01-12"


def _dirty_since(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'derive_dirty_since'").fetchone()
    return None if row is None else row[0]


def test_derived_values_are_dirtied_from_the_earliest_date_an_edited_or_moved_activity_touches(conn: sqlite3.Connection) -> None:
    db.upsert_activities(conn, [_row(1, "2026-01-05T08:00:00"), _row(2, "2026-01-06T08:00:00")])
    conn.execute("DELETE FROM meta WHERE key = 'derive_dirty_since'")
    conn.commit()

    db.upsert_activities(conn, [_row(1, "2026-01-05T08:00:00"), _row(2, "2026-01-06T08:00:00")])
    assert _dirty_since(conn) is None  # listing unchanged activities again leaves derived values clean

    renamed = _row(2, "2026-01-06T08:00:00")
    renamed["name"] = "Renamed"
    db.upsert_activities(conn, [_row(1, "2026-01-05T08:00:00"), renamed])
    assert _dirty_since(conn) == "2026-01-06"

    conn.execute("DELETE FROM meta WHERE key = 'derive_dirty_since'")
    conn.commit()
    db.upsert_activities(conn, [_row(1, "2026-01-12T08:00:00")])  # moved later: dirty from the old date
    assert _dirty_since(conn) == "2026-01-05"

    conn.execute("DELETE FROM meta WHERE key = 'derive_dirty_since'")
    conn.commit()
    db.upsert_activities(conn, [_row(1, "2026-01-20T08:00:00")])
    assert _dirty_since(conn) == "2026-01-12"


def test_an_upsert_that_flags_many_activities_logs_a_warning(conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture) -> None:
    count = db.CHANGED_ACTIVITIES_WARN_COUNT + 1
    db.upsert_activities(conn, [_row(i) for i in range(1, count + 1)])
    edited = [_row(i) for i in range(1, count + 1)]
    for row in edited:
        row["distance_m"] = 9000.0
    with caplog.at_level("WARNING", logger="miles.db"):
        db.upsert_activities(conn, edited)
    assert any("differ from what is stored" in r.message for r in caplog.records)


class _Strava:
    """Stands in for the Strava client and records what each pass asked for."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, rows: list[db.ActivityRow]) -> None:
        self.rows = rows
        self.lap_calls: list[int] = []
        self.daily_left: int | None = 1000
        self.calls = 0
        monkeypatch.setattr(strava_client, "get_activities", self._activities)
        monkeypatch.setattr(strava_client, "get_activity_detail", self._detail)
        monkeypatch.setattr(strava_client, "get_activity_laps", self._laps)
        monkeypatch.setattr(strava_client, "daily_calls_remaining", lambda: self.daily_left)
        monkeypatch.setattr(strava_client, "short_window_calls_remaining", lambda: None)

    def _spend(self) -> None:
        self.calls += 1
        if self.daily_left is not None:
            self.daily_left -= 1

    def _activities(self, after_ts: str | None = None):
        self._spend()
        yield from self.rows

    def _detail(self, activity_id: int):
        self._spend()
        self.lap_calls.append(activity_id)
        return "", []

    def _laps(self, activity_id: int):
        self._spend()
        self.lap_calls.append(activity_id)
        return []


@pytest.fixture
def phases(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    original = sync.SyncProgress.phase

    def record(self, name, **kwargs):
        seen.append(name)
        original(self, name, **kwargs)

    monkeypatch.setattr(sync.SyncProgress, "phase", record)
    return seen


@pytest.fixture(autouse=True)
def _no_side_services(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sync.terrain_backfill, "run_backfill", lambda *a, **k: 0)
    monkeypatch.setattr(sync.terrain_backfill, "refresh_backfill_queue", lambda conn: 0)
    monkeypatch.setattr(sync.terrain, "activities_to_segment", lambda conn: [])


def _old_run(conn: sqlite3.Connection, activity_id: int = 99, start: str = "2020-01-01T08:00:00") -> None:
    insert_activity(conn, activity_id, start_date=start)


def test_passes_run_in_order_and_report_their_own_phase(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, phases: list[str]):
    _Strava(monkeypatch, [_row(1, workout_type=3), _row(2)])
    _old_run(conn)  # an older run with no laps yet, for the history pass
    monkeypatch.setattr(sync.terrain_backfill, "refresh_backfill_queue", lambda c: 1)

    sync.run_sync(conn, full=False, history=True, reserve=12)

    first = lambda name: phases.index(name)  # noqa: E731
    assert first("activities") < first("derive") < first("laps") < first("backfill-laps")


def test_the_activity_pass_is_never_gated_by_the_reserve(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    strava = _Strava(monkeypatch, [_row(1)])
    strava.daily_left = 3  # already under any reserve

    assert sync.run_sync(conn, full=False, history=True, reserve=12) == 1
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1


def test_detail_laps_stop_at_the_reserve_and_the_rest_wait_for_the_next_sync(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    strava = _Strava(monkeypatch, [_row(i, f"2026-01-0{i}T08:00:00", workout_type=3) for i in range(1, 6)])
    strava.daily_left = 15  # the listing spends one: 14, then laps may spend 2 before reaching the reserve of 12

    sync.run_sync(conn, full=False, history=False, reserve=12)

    assert len(strava.lap_calls) == 2
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE laps_synced_at IS NULL").fetchone()[0] == 3


def test_history_pass_stops_at_the_reserve_and_runs_newest_first_after_long_runs(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    strava = _Strava(monkeypatch, [])
    for activity_id, start, workout_type in [(1, "2026-01-01", 0), (2, "2026-01-03", 0), (3, "2025-06-01", 2)]:
        insert_activity(conn, activity_id, start_date=start + "T08:00:00", workout_type=workout_type, run_type=db.WORKOUT_TYPE_MAP[workout_type])
    strava.daily_left = 100

    sync.run_sync(conn, full=False, history=True, reserve=12)

    assert strava.lap_calls == [3, 2, 1]


@pytest.mark.parametrize("history, backfill_running", [(False, False), (True, True)], ids=["history off", "a backfill is running"])
def test_the_history_pass_is_skipped_when_off_or_while_a_backfill_is_running(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, history: bool, backfill_running: bool
):
    strava = _Strava(monkeypatch, [])
    insert_activity(conn, 1, start_date="2026-01-01T08:00:00")
    if backfill_running:
        conn.execute("UPDATE terrain_backfill_status SET status = 'running' WHERE id = 1")
        conn.commit()

    sync.run_sync(conn, full=False, history=history, reserve=12)

    assert strava.lap_calls == []
    if backfill_running:
        assert terrain_backfill.BackfillProgress(conn).snapshot()["status"] == "running"


def test_history_env_switch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(sync.HISTORY_ENV, raising=False)
    assert sync.history_enabled() is True
    for off in ("0", "false", "NO", "off"):
        monkeypatch.setenv(sync.HISTORY_ENV, off)
        assert sync.history_enabled() is False


def test_streams_for_new_runs_are_fetched_in_the_details_pass_only_for_those_runs(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    insert_activity(conn, 50, start_date="2025-01-01T08:00:00")
    _Strava(monkeypatch, [_row(50), _row(51), _row(52, sport="Ride")])
    calls: list[dict] = []
    monkeypatch.setattr(sync.terrain_backfill, "run_backfill", lambda c, sink, **kw: calls.append(kw) or 0)

    sync.run_sync(conn, full=False, history=False, reserve=7)

    assert [kw["only_ids"] for kw in calls] == [{51}]
    assert calls[0]["gate"].reserve == 7


def test_a_running_sync_history_pass_blocks_a_standalone_backfill(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    seen: dict = {}

    def while_history_runs(c, sink, **kw):
        seen["second_start"] = terrain_backfill.BackfillProgress(conn).try_start()
        return 0

    strava = _Strava(monkeypatch, [])
    monkeypatch.setattr(sync.terrain_backfill, "refresh_backfill_queue", lambda c: 1)
    monkeypatch.setattr(sync.terrain_backfill, "run_backfill", while_history_runs)

    sync.run_sync(conn, full=False, history=True, reserve=12)

    assert seen["second_start"] is False
    assert terrain_backfill.BackfillProgress(conn).snapshot()["status"] == "done"
    assert strava.lap_calls == []


def test_a_history_failure_is_recorded_on_the_backfill_row_and_does_not_fail_the_sync(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _Strava(monkeypatch, [])
    monkeypatch.setattr(sync.terrain_backfill, "refresh_backfill_queue", lambda c: 1)

    def boom(*a, **k):
        raise terrain_backfill.BackfillAbortedError("Strava rejected the credentials (401)")

    monkeypatch.setattr(sync.terrain_backfill, "run_backfill", boom)

    sync.run_sync(conn, full=False, history=True, reserve=12)

    snapshot = terrain_backfill.BackfillProgress(conn).snapshot()
    assert snapshot["status"] == "error"
    assert "401" in (snapshot["error"] or "")
    assert conn.execute("SELECT value FROM meta WHERE key = ?", (sync.META_LAST_SUCCESS,)).fetchone() is not None


def test_a_listing_429_waits_retries_the_pass_and_is_not_recorded_as_final(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, slept: list[float], outcomes
):
    strava = _Strava(monkeypatch, [_row(1)])
    attempts = {"n": 0}
    original = strava._activities

    def flaky(after_ts=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise strava_fault(429)
        yield from original(after_ts)

    monkeypatch.setattr(strava_client, "get_activities", flaky)

    assert sync.run_sync(conn, full=False, history=False, reserve=12) == 1
    assert len(slept) == 1
    assert outcomes == []


def test_an_optional_pass_gives_up_when_a_wait_bought_nothing_and_records_the_429_as_final(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, slept: list[float], outcomes
):
    strava = _Strava(monkeypatch, [_row(1, workout_type=3)])

    def always_limited(activity_id):
        raise strava_fault(429)

    monkeypatch.setattr(strava_client, "get_activity_detail", always_limited)

    sync.run_sync(conn, full=False, history=False, reserve=12)

    assert len(slept) == 1  # one window waited out, then the cap is assumed
    assert strava.calls == 1
    assert outcomes == [("strava", 429)]


def test_the_activity_listing_fails_the_sync_when_the_daily_limit_is_spent(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, slept: list[float]):
    strava = _Strava(monkeypatch, [_row(1)])
    strava.daily_left = 0

    def limited(after_ts=None):
        raise strava_fault(429)
        yield  # pragma: no cover

    monkeypatch.setattr(strava_client, "get_activities", limited)

    with pytest.raises(errors.StravaAllowanceLowError, match="resets 00:00 UTC"):
        sync.run_sync(conn, full=False, history=False, reserve=12)

    assert slept == []
    assert "daily limit" in conn.execute("SELECT value FROM meta WHERE key = ?", (sync.META_LAST_ERROR,)).fetchone()[0]


def test_the_laps_fallback_request_respects_the_budget(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    strava = _Strava(monkeypatch, [_row(1, workout_type=3), _row(2, workout_type=3)])
    db.upsert_activities(conn, strava.rows)
    fallback: list[int] = []

    def detail(activity_id):
        strava._spend()
        return "a description", None  # the response carried no laps

    def laps(activity_id):
        fallback.append(activity_id)
        strava._spend()
        return []

    monkeypatch.setattr(strava_client, "get_activity_detail", detail)
    monkeypatch.setattr(strava_client, "get_activity_laps", laps)
    strava.daily_left = 15  # the second activity's detail call leaves exactly the reserve of 12, so its fallback must not be made

    sync._fetch_laps(conn, [1, 2], strava_budget.Gate(12), sync._PhaseSink(sync.SyncProgress()))

    assert fallback == [1]
    rows = {r["activity_id"]: r for r in conn.execute("SELECT activity_id, description, laps_synced_at FROM activities")}
    assert rows[1]["laps_synced_at"] is not None
    assert rows[2]["description"] == "a description" and rows[2]["laps_synced_at"] is None


def test_outcomes_are_recorded_for_the_health_check(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _Strava(monkeypatch, [_row(1), _row(2)])

    sync.run_sync(conn, full=False, history=False, reserve=12)

    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    assert meta[sync.META_LAST_NEW] == "2"
    assert float(meta[sync.META_LAST_DURATION]) >= 0
    assert sync.META_LAST_FAILURE not in meta


def test_a_failed_run_records_the_failure_and_still_raises(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    def broken(after_ts=None):
        raise RuntimeError("network down")
        yield  # pragma: no cover

    monkeypatch.setattr(strava_client, "get_activities", broken)

    with pytest.raises(RuntimeError):
        sync.run_sync(conn, full=False, history=False, reserve=12)

    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    assert "network down" in meta[sync.META_LAST_ERROR]
    assert sync.META_LAST_FAILURE in meta
    assert sync.META_LAST_SUCCESS not in meta


def test_laps_and_description_come_from_one_detail_request_and_no_description_is_cached(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    strava = _Strava(monkeypatch, [_row(1, workout_type=3)])

    sync.run_sync(conn, full=False, history=False, reserve=12)

    assert strava.lap_calls == [1]
    row = conn.execute("SELECT description, laps_synced_at FROM activities WHERE activity_id = 1").fetchone()
    assert row["description"] == ""
    assert row["laps_synced_at"] is not None


def test_a_detail_response_without_laps_falls_back_to_the_laps_endpoint(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    strava = _Strava(monkeypatch, [_row(1, workout_type=3)])
    lap: db.LapRow = {
        "lap_id": 7, "activity_id": 1, "lap_index": 1, "distance_m": 1000.0, "moving_time_s": 300, "elapsed_time_s": 300,
        "average_speed_mps": 3.3, "average_heartrate": None, "max_heartrate": None, "average_cadence": None,
        "total_elevation_gain_m": None, "pace_zone": None, "raw_json": "{}",
    }
    monkeypatch.setattr(strava_client, "get_activity_detail", lambda aid: ("easy hills", None))
    monkeypatch.setattr(strava_client, "get_activity_laps", lambda aid: [lap])

    sync.run_sync(conn, full=False, history=False, reserve=12)

    assert conn.execute("SELECT COUNT(*) FROM laps WHERE activity_id = 1").fetchone()[0] == 1
    assert conn.execute("SELECT description FROM activities WHERE activity_id = 1").fetchone()[0] == "easy hills"
    assert strava.lap_calls == []


def test_a_terrain_failure_neither_fails_the_sync_nor_leaves_partial_writes_for_later_passes_to_commit(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _Strava(monkeypatch, [_row(1)])

    def broken(c, *args, **kwargs):
        c.execute("INSERT INTO meta (key, value) VALUES ('partial_terrain_write', '1')")
        raise ValueError("stream file is truncated")

    monkeypatch.setattr(sync.terrain_backfill, "run_backfill", broken)

    sync.run_sync(conn, full=False, history=False, reserve=12)

    assert conn.execute("SELECT value FROM meta WHERE key = ?", (sync.META_LAST_SUCCESS,)).fetchone() is not None
    assert conn.execute("SELECT value FROM meta WHERE key = 'partial_terrain_write'").fetchone() is None


def test_a_terrain_rebuild_does_not_fail_the_sync(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _Strava(monkeypatch, [_row(1)])
    _old_run(conn)
    monkeypatch.setattr(sync.terrain_backfill, "refresh_backfill_queue", lambda c: 1)

    def rebuilding(*args, **kwargs):
        raise sync.terrain.TerrainRebuildingError("rebuild running")

    monkeypatch.setattr(sync.terrain_backfill, "run_backfill", rebuilding)

    sync.run_sync(conn, full=False, history=True, reserve=12)

    assert conn.execute("SELECT value FROM meta WHERE key = ?", (sync.META_LAST_SUCCESS,)).fetchone() is not None
    assert terrain_backfill.BackfillProgress(conn).snapshot()["status"] == "done"
