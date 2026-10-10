import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from miles import db


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
            1001,
            "Evening Run",
            "Run",
            "2026-01-05T04:00:00+00:00",
            json.dumps({"start_date": "2026-01-05T04:00:00Z", "start_date_local": "2026-01-04T22:00:00Z"}),
        ),
    )
    conn.commit()


def _start_date(conn: sqlite3.Connection, activity_id: int = 1001) -> str:
    return conn.execute("SELECT start_date FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()["start_date"]


def test_backfill_rewrites_start_date_to_local_wall_time_once_and_leaves_rows_without_raw_json(conn: sqlite3.Connection):
    _insert_dirty_row(conn)
    conn.execute("PRAGMA user_version = 0")  # an existing DB that predates the stamp

    db.init_db(conn)
    first = _start_date(conn)

    conn.execute("PRAGMA user_version = 0")
    db.init_db(conn)

    assert first == _start_date(conn) == "2026-01-04T22:00:00"

    conn.execute(
        "INSERT INTO activities (activity_id, name, sport_type, start_date, raw_json) VALUES (1, 'No raw json', 'Run', ?, NULL)",
        ("2026-01-05T04:00:00+00:00",),
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    db.init_db(conn)
    assert _start_date(conn, 1) == "2026-01-05T04:00:00+00:00"  # a row without raw json is left alone

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.execute("UPDATE activities SET start_date = '2026-01-05T04:00:00+00:00' WHERE activity_id = 1001")
    conn.commit()
    db.init_db(conn)  # stamped: the start_date rewrite does not run again
    assert _start_date(conn) == "2026-01-05T04:00:00+00:00"


# The schema a fresh database gets, by version. A change to any table or
# column definition changes the hash; the version must then be bumped, the
# upgrade for existing databases written in init_db, and the new hash recorded.
_SCHEMA_HASHES = {
    2: "6cfd39c255def5fb388bc072a342a1dfd15936c3f8ca3fc7d9e0ba41d3a278c4",
    3: "82521723265850fd348cd7fb1622d7ac3295120f6e7b8a9f5fd058d5941fad6e",
}


def _schema_hash(conn: sqlite3.Connection) -> str:
    """Comments and whitespace inside a CREATE statement are stored verbatim
    by SQLite and must not count as a schema change."""
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    normalized = [(r["type"], r["name"], " ".join(re.sub(r"--[^\n]*", "", r["sql"] or "").split())) for r in rows]
    return hashlib.sha256(json.dumps(normalized).encode()).hexdigest()


def test_the_schema_version_is_bumped_whenever_the_schema_changes(conn: sqlite3.Connection):
    recorded = _SCHEMA_HASHES.get(db.SCHEMA_VERSION)
    assert recorded is not None, f"record the schema hash for version {db.SCHEMA_VERSION}: {_schema_hash(conn)}"
    assert _schema_hash(conn) == recorded, (
        f"the schema changed without a version bump: bump SCHEMA_VERSION and add the upgrade "
        f"(then record the hash {_schema_hash(conn)} for the new version)"
    )


def test_a_database_from_before_later_columns_and_tables_is_upgraded_in_place(conn: sqlite3.Connection):
    for table, columns in {
        "activities": ["workout_label", "description", "laps_synced_at"],
        "laps": ["lap_type", "intensity"],
        "weather": ["raw_json"],
        "map_extracts": ["checked_at", "data_as_of", "etag", "last_modified"],
        "areas": ["region"],
        "plan_versions": ["committed_at"],
        "terrain_backfill_failures": ["model_version"],
    }.items():
        for column in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.execute("CREATE TABLE extract_attempts (region TEXT PRIMARY KEY, attempted_at TEXT)")
    conn.execute("INSERT INTO map_extracts (region, path, sha256, fetched_at) VALUES ('r/x', '/p', 's', '2024-01-02T00:00:00+00:00')")
    conn.execute("INSERT INTO plans (plan_id, title, race_date, distance_bucket, created_at) VALUES (1, 'P', '2026-05-30', '10K', '2026-01-01')")
    conn.execute("INSERT INTO plan_versions (version_id, plan_id, version_n, created_at, author) VALUES (1, 1, 1, '2026-01-02', 'agent')")
    _insert_dirty_row(conn)
    conn.execute("INSERT INTO meta (key, value) VALUES ('start_date_local_migrated', '1')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    db.init_db(conn)

    def columns(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    assert {"workout_label", "description", "laps_synced_at"} <= columns("activities")
    assert {"lap_type", "intensity"} <= columns("laps")
    assert "raw_json" in columns("weather")
    assert {"checked_at", "data_as_of", "etag", "last_modified"} <= columns("map_extracts")
    assert "region" in columns("areas")
    assert "model_version" in columns("terrain_backfill_failures")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'extract_attempts'").fetchone() is None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.get_meta(conn, "start_date_local_migrated") is None  # the retired marker is cleared
    # Rows survive, and a version written before drafts existed counts as committed.
    assert conn.execute("SELECT region FROM map_extracts").fetchone()["region"] == "r/x"
    assert conn.execute("SELECT committed_at FROM plan_versions").fetchone()["committed_at"] == "2026-01-02"
    assert _start_date(conn) == "2026-01-04T22:00:00"


def _layout(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    names = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name")]
    return {name: [tuple(r) for r in conn.execute(f"PRAGMA table_info({name})")] for name in names}


def test_a_version_two_database_upgrades_to_the_schema_a_fresh_one_gets(conn: sqlite3.Connection, tmp_path: Path):
    old = sqlite3.connect(tmp_path / "v2.db")
    old.row_factory = sqlite3.Row
    old.executescript((Path(__file__).parent / "schema_v2.sql").read_text())
    old.execute("PRAGMA user_version = 2")
    old.commit()

    db.init_db(old)

    assert _layout(old) == _layout(conn)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


class _RacingConnection:
    """Another process adds each column between the check and our ALTER."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if sql.startswith("ALTER TABLE"):
            self._conn.execute(sql)
        return self._conn.execute(sql, *args)


def test_a_column_added_by_a_concurrent_upgrade_is_not_an_error_but_other_failures_are(conn: sqlite3.Connection):
    added = db.add_missing_columns(_RacingConnection(conn), "weather", "extra TEXT")  # type: ignore[arg-type]

    assert added is False  # the other process added it, so the backfills that follow are its to run
    assert "extra" in {r["name"] for r in conn.execute("PRAGMA table_info(weather)")}
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.add_missing_columns(conn, "no_such_table", "x TEXT")
    assert db.add_missing_columns(conn, "weather", "extra TEXT", "another TEXT") is True


def test_connections_use_wal_and_a_backup_taken_during_a_write_is_consistent(tmp_path: Path):
    path = tmp_path / "wal.db"
    writer = db.connect(path)
    db.init_db(writer)
    assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    writer.execute("INSERT INTO meta (key, value) VALUES ('before', '1')")
    writer.commit()
    writer.execute("INSERT INTO meta (key, value) VALUES ('uncommitted', '1')")  # an open write transaction

    reader = db.connect(path)
    assert reader.execute("SELECT value FROM meta WHERE key = 'before'").fetchone()[0] == "1"  # a reader is not blocked

    copy = sqlite3.connect(tmp_path / "copy.db")
    reader.backup(copy)
    keys = {row[0] for row in copy.execute("SELECT key FROM meta")}
    assert "before" in keys and "uncommitted" not in keys
    writer.commit()
