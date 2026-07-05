"""Shared race-distance taxonomy: one place that answers "what race distance is this?"

GPS reads long on certified courses, so bucket ranges skew above nominal distance.
"""

import sqlite3

from . import db
from .format import fmt_time

RUN_SPORT_TYPES: tuple[str, ...] = ("Run", "TrailRun", "VirtualRun")

RACE_BUCKETS: list[tuple[str, float, float]] = [
    ("5K", 4800, 5600),
    ("10K", 9700, 11200),
    ("15K", 14700, 15800),
    ("10M", 15900, 17000),
    ("half", 20700, 22300),
    ("30K", 29300, 31500),
    ("marathon", 42000, 43500),
    ("50K", 45000, 52000),
]

NOMINAL_METERS: dict[str, float] = {
    "5K": 5000.0,
    "10K": 10000.0,
    "15K": 15000.0,
    "10M": 16093.4,
    "half": 21097.5,
    "30K": 30000.0,
    "marathon": 42195.0,
    "50K": 50000.0,
}

MARATHON_MIN_M: float = 42000.0
MARATHON_MAX_M: float = 43500.0


def classify_race_distance(distance_m: float | None) -> str | None:
    """Return the category of the first RACE_BUCKETS entry containing distance_m (inclusive), else None."""
    if distance_m is None:
        return None
    for category, min_m, max_m in RACE_BUCKETS:
        if min_m <= distance_m <= max_m:
            return category
    return None


def riegel_time(t_s: float, from_m: float, to_m: float, exponent: float = 1.06) -> float:
    """Riegel formula: predicted time at to_m given a time at from_m."""
    return t_s * (to_m / from_m) ** exponent


def race_rows(
    conn: sqlite3.Connection,
    distance_category: str | None = None,
    start_date: str | None = None,
    activity_id: int | None = None,
) -> list[dict[str, object]]:
    """
    All effective-race activities, ascending by date. PR flags are computed over
    the athlete's full race history first; distance_category/start_date/activity_id
    filter the returned rows afterward, so filtering never changes an is_pr verdict.
    """
    effective_run_type = db.effective_run_type_sql("a")
    placeholders = ",".join("?" * len(RUN_SPORT_TYPES))
    where = f"WHERE a.sport_type IN ({placeholders}) AND {effective_run_type} = 'race'"

    rows = conn.execute(f"""
        SELECT
            a.activity_id,
            a.name,
            DATE(a.start_date) AS date,
            a.distance_m,
            ROUND(a.distance_m / 1609.34, 2) AS distance_miles,
            a.moving_time_s,
            CASE WHEN a.average_speed_mps > 0
                 THEN ROUND(26.8224 / a.average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile,
            ROUND(a.average_heartrate, 1) AS avg_hr,
            CASE WHEN a.workout_type = 0 AND a.run_type_inferred IS NOT NULL
                 THEN 'inferred' ELSE 'strava' END AS run_type_source,
            a.race_effort,
            a.effort_ratio,
            a.strava_url
        FROM activities a
        {where}
        ORDER BY date ASC
    """, list(RUN_SPORT_TYPES)).fetchall()

    best_finish_s: dict[str, int] = {}
    out: list[dict[str, object]] = []
    for r in rows:
        category: str = classify_race_distance(r["distance_m"]) or "other"
        finish_time_s: int | None = r["moving_time_s"]
        is_pr = False
        if category != "other" and finish_time_s is not None:
            best = best_finish_s.get(category)
            if best is None or finish_time_s < best:
                is_pr = True
                best_finish_s[category] = finish_time_s

        if distance_category is not None and category != distance_category:
            continue
        race_date: str = r["date"]
        if start_date is not None and race_date < start_date:
            continue
        if activity_id is not None and r["activity_id"] != activity_id:
            continue

        out.append({
            "activity_id": r["activity_id"],
            "name": r["name"],
            "date": race_date,
            "distance_category": category,
            "distance_miles": r["distance_miles"],
            "finish_time_s": finish_time_s,
            "finish_time": fmt_time(finish_time_s),
            "pace_min_per_mile": r["pace_min_per_mile"],
            "avg_hr": r["avg_hr"],
            "run_type_source": r["run_type_source"],
            "effort": r["race_effort"],
            "effort_ratio": r["effort_ratio"],
            "is_pr": is_pr,
            "strava_url": r["strava_url"],
        })
    return out
