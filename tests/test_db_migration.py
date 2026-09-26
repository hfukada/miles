import json
import sqlite3

from miles import db


def _fresh_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _insert_dirty_row(conn: sqlite3.Connection) -> None:
    """Insert a row shaped like one synced before the local-wall-time fix: start_date
    holds Strava's UTC value, and raw_json carries the true start_date_local (with its
    fake trailing 'Z') the migration should read from instead."""
    conn.execute(
        """
        INSERT INTO activities (activity_id, name, sport_type, start_date, raw_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            19439498791,
            "Evening Run",
            "Run",
            "2026-07-24T00:09:03+00:00",
            json.dumps({"start_date": "2026-07-24T00:09:03Z", "start_date_local": "2026-07-23T18:09:03Z"}),
        ),
    )
    conn.commit()


def test_backfill_rewrites_start_date_to_local_wall_time():
    conn = _fresh_conn()
    # init_db already ran the guarded migration once (on an empty table, a no-op);
    # clear its stamp to simulate an existing DB that predates this migration.
    conn.execute("DELETE FROM meta WHERE key = 'start_date_local_migrated'")
    _insert_dirty_row(conn)

    db.init_db(conn)

    row = conn.execute(
        "SELECT start_date FROM activities WHERE activity_id = 19439498791"
    ).fetchone()
    assert row["start_date"] == "2026-07-23T18:09:03"


def test_backfill_is_idempotent_on_rerun():
    conn = _fresh_conn()
    conn.execute("DELETE FROM meta WHERE key = 'start_date_local_migrated'")
    _insert_dirty_row(conn)

    db.init_db(conn)
    first = conn.execute(
        "SELECT start_date FROM activities WHERE activity_id = 19439498791"
    ).fetchone()["start_date"]

    db.init_db(conn)
    second = conn.execute(
        "SELECT start_date FROM activities WHERE activity_id = 19439498791"
    ).fetchone()["start_date"]

    assert first == second == "2026-07-23T18:09:03"


def test_backfill_leaves_rows_without_raw_json_untouched():
    conn = _fresh_conn()
    conn.execute("DELETE FROM meta WHERE key = 'start_date_local_migrated'")
    conn.execute(
        """
        INSERT INTO activities (activity_id, name, sport_type, start_date, raw_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (1, "No raw json", "Run", "2026-07-24T00:09:03+00:00", None),
    )
    conn.commit()

    db.init_db(conn)

    row = conn.execute("SELECT start_date FROM activities WHERE activity_id = 1").fetchone()
    assert row["start_date"] == "2026-07-24T00:09:03+00:00"


def test_backfill_is_a_noop_on_fresh_empty_db():
    # init_db on a brand-new DB must not error with zero activity rows present.
    conn = _fresh_conn()
    count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    assert count == 0
    stamp = conn.execute(
        "SELECT value FROM meta WHERE key = 'start_date_local_migrated'"
    ).fetchone()
    assert stamp is not None


def test_init_db_creates_route_tables():
    conn = _fresh_conn()
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {
        "gpx_files",
        "routes",
        "route_points",
        "route_waypoints",
        "route_climbs",
        "route_grade_bands",
        "route_legs",
    } <= tables


def test_init_db_is_idempotent_for_route_tables():
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO gpx_files (sha256, filename, creator, data, uploaded_at) VALUES (?, ?, ?, ?, ?)",
        ["abc123", "test.gpx", "Test", b"<gpx></gpx>", "2026-01-01T00:00:00+00:00"],
    )
    conn.commit()
    db.init_db(conn)  # must not drop/recreate and lose the row
    row = conn.execute("SELECT filename FROM gpx_files WHERE sha256 = 'abc123'").fetchone()
    assert row["filename"] == "test.gpx"
