"""Correctness + speed check for derive.py's incremental path, run against a copy of
a real synced database — the fixtures elsewhere are too small to exercise the
fitness-checkpoint/lap-intensity scoping meaningfully. Uses data/activities.db, or
the file named by MILES_TEST_DB; skips when neither is present."""

import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from miles import db
from miles.derive import derive_all

_SOURCE_DB = Path(os.environ.get("MILES_TEST_DB", Path(__file__).parent.parent / "data" / "activities.db"))

pytestmark = pytest.mark.skipif(not _SOURCE_DB.exists(), reason=f"{_SOURCE_DB} not present")

_ACTIVITY_COLS = [
    "activity_id", "name", "sport_type", "start_date", "workout_type", "run_type",
    "distance_m", "moving_time_s", "elapsed_time_s", "total_elevation_gain_m",
    "average_speed_mps", "max_speed_mps", "average_heartrate", "max_heartrate",
    "average_cadence", "gear_id", "strava_url", "synced_at", "start_lat", "start_lng",
    "raw_json",
]
_LAP_COLS = [
    "lap_id", "activity_id", "lap_index", "distance_m", "moving_time_s", "elapsed_time_s",
    "average_speed_mps", "average_heartrate", "max_heartrate", "average_cadence",
    "total_elevation_gain_m", "pace_zone", "raw_json",
]


def _open_copy(tmp_path: Path) -> sqlite3.Connection:
    dest = tmp_path / "activities.db"
    shutil.copy(_SOURCE_DB, dest)
    conn = sqlite3.connect(dest)
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _snapshot(conn: sqlite3.Connection) -> tuple[object, ...]:
    activities = conn.execute("""
        SELECT activity_id, run_type_inferred, race_effort, effort_ratio,
               workout_label, dominant_intensity
        FROM activities ORDER BY activity_id
    """).fetchall()
    laps = conn.execute("""
        SELECT lap_id, lap_type, intensity FROM laps ORDER BY lap_id
    """).fetchall()
    checkpoints = conn.execute("""
        SELECT month, confidence, source_tier, pace_5k, pace_10k, pace_half, pace_marathon
        FROM fitness_checkpoints ORDER BY month
    """).fetchall()
    adherence = conn.execute("""
        SELECT plan_id, week_start, version_n_used, actual_miles, actual_workouts,
               actual_strength_days, long_run_done, mileage_ratio, workout_pace_delta_s,
               band, flags_json
        FROM plan_adherence ORDER BY plan_id, week_start
    """).fetchall()
    return (
        tuple(tuple(r) for r in activities),
        tuple(tuple(r) for r in laps),
        tuple(tuple(r) for r in checkpoints),
        tuple(tuple(r) for r in adherence),
    )


def _resync_activity(conn: sqlite3.Connection, activity_id: int) -> None:
    """Re-upsert one activity (and its laps, if any) through the real writer paths,
    unchanged, so db.mark_derive_dirty fires exactly as it would for a live sync
    that re-fetched this activity.

    upsert_activities is INSERT OR REPLACE over a fixed column list, so — exactly
    as it does in a real sync — it resets workout_label and laps_synced_at to NULL
    as a side effect; a real sync's later lap-fetch and label-backfill steps (not
    derive_all, which never touches either column) restore them. Restore them here
    too so this test isolates derive_all's own incremental-vs-full behavior instead
    of also re-testing that unrelated part of sync.py.
    """
    row = conn.execute(
        f"SELECT {', '.join(_ACTIVITY_COLS)}, workout_label, laps_synced_at "
        "FROM activities WHERE activity_id = ?", [activity_id]
    ).fetchone()
    activity_row = cast_activity_row(row)
    db.upsert_activities(conn, [activity_row])
    conn.execute(
        "UPDATE activities SET workout_label = ?, laps_synced_at = ? WHERE activity_id = ?",
        [row["workout_label"], row["laps_synced_at"], activity_id],
    )

    lap_rows = conn.execute(
        f"SELECT {', '.join(_LAP_COLS)} FROM laps WHERE activity_id = ?", [activity_id]
    ).fetchall()
    if lap_rows:
        db.upsert_laps(conn, [cast_lap_row(r) for r in lap_rows])


def cast_activity_row(row: sqlite3.Row) -> db.ActivityRow:
    return {col: row[col] for col in _ACTIVITY_COLS}  # type: ignore[return-value]


def cast_lap_row(row: sqlite3.Row) -> db.LapRow:
    return {col: row[col] for col in _LAP_COLS}  # type: ignore[return-value]


def test_incremental_derive_matches_full_rebuild(tmp_path: Path) -> None:
    conn = _open_copy(tmp_path)

    full_start = time.monotonic()
    derive_all(conn, full=True)
    full_elapsed = time.monotonic() - full_start
    snapshot_full = _snapshot(conn)

    newest_runs = conn.execute("""
        SELECT activity_id FROM activities
        WHERE sport_type = 'Run' ORDER BY start_date DESC LIMIT 5
    """).fetchall()
    assert len(newest_runs) == 5
    for r in newest_runs:
        _resync_activity(conn, int(r["activity_id"]))

    dirty = conn.execute("SELECT value FROM meta WHERE key = 'derive_dirty_since'").fetchone()
    assert dirty is not None and dirty["value"] is not None

    incr_start = time.monotonic()
    derive_all(conn)
    incr_elapsed = time.monotonic() - incr_start
    snapshot_incremental = _snapshot(conn)

    assert snapshot_incremental == snapshot_full
    assert incr_elapsed < 0.25 * full_elapsed, (
        f"incremental derive ({incr_elapsed:.2f}s) should be well under 25% of a full "
        f"rebuild ({full_elapsed:.2f}s)"
    )

    nothing_dirty_start = time.monotonic()
    derive_all(conn)
    nothing_dirty_elapsed = time.monotonic() - nothing_dirty_start
    assert nothing_dirty_elapsed < 1.0, (
        f"a derive_all with nothing dirty took {nothing_dirty_elapsed:.2f}s; "
        "should be well under a second"
    )
