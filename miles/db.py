import sqlite3
from pathlib import Path
from typing import TypedDict


class ActivityRow(TypedDict):
    activity_id: int
    name: str | None
    sport_type: str
    start_date: str | None
    workout_type: int
    run_type: str
    distance_m: float | None
    moving_time_s: int | None
    elapsed_time_s: int | None
    total_elevation_gain_m: float | None
    average_speed_mps: float | None
    max_speed_mps: float | None
    average_heartrate: float | None
    max_heartrate: float | None
    average_cadence: float | None
    gear_id: str | None
    strava_url: str
    synced_at: str


DB_PATH = Path(__file__).parent.parent / "data" / "activities.db"

WORKOUT_TYPE_MAP: dict[int, str] = {
    0: "easy",
    1: "race",
    2: "long_run",
    3: "workout",
}


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            activity_id   INTEGER PRIMARY KEY,
            name          TEXT,
            sport_type    TEXT,
            start_date    TEXT,
            workout_type  INTEGER,
            run_type      TEXT,
            distance_m    REAL,
            moving_time_s INTEGER,
            elapsed_time_s INTEGER,
            total_elevation_gain_m REAL,
            average_speed_mps REAL,
            max_speed_mps     REAL,
            average_heartrate REAL,
            max_heartrate     REAL,
            average_cadence   REAL,
            gear_id       TEXT,
            strava_url    TEXT,
            synced_at     TEXT
        )
    """)
    conn.commit()


def upsert_activities(conn: sqlite3.Connection, rows: list[ActivityRow]) -> None:
    conn.executemany("""
        INSERT OR REPLACE INTO activities (
            activity_id, name, sport_type, start_date,
            workout_type, run_type,
            distance_m, moving_time_s, elapsed_time_s,
            total_elevation_gain_m,
            average_speed_mps, max_speed_mps,
            average_heartrate, max_heartrate, average_cadence,
            gear_id, strava_url, synced_at
        ) VALUES (
            :activity_id, :name, :sport_type, :start_date,
            :workout_type, :run_type,
            :distance_m, :moving_time_s, :elapsed_time_s,
            :total_elevation_gain_m,
            :average_speed_mps, :max_speed_mps,
            :average_heartrate, :max_heartrate, :average_cadence,
            :gear_id, :strava_url, :synced_at
        )
    """, rows)
    conn.commit()


def last_synced_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(start_date) FROM activities").fetchone()
    return row[0] if row and row[0] else None
