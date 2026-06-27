import json
import sqlite3
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

from . import db

mcp = FastMCP("miles")

RUN_TYPES = ("Run", "TrailRun", "VirtualRun")


def _conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def _run_type_filter(sport_types: tuple[str, ...] = RUN_TYPES) -> tuple[str, list[str]]:
    placeholders = ",".join("?" * len(sport_types))
    return f"sport_type IN ({placeholders})", list(sport_types)


@mcp.tool()
def get_weekly_mileage(start_date: str | None = None, end_date: str | None = None) -> str:
    """
    Weekly running mileage grouped by ISO week (YYYY-Www).
    Optionally filter by start_date / end_date (YYYY-MM-DD).
    Returns list of {week, miles, runs}.
    """
    conn = _conn()
    type_clause, params = _run_type_filter()
    where = f"WHERE {type_clause}"
    if start_date:
        where += " AND start_date >= ?"
        params.append(start_date)
    if end_date:
        where += " AND start_date <= ?"
        params.append(end_date)

    rows = conn.execute(f"""
        SELECT
            strftime('%Y-W%W', start_date) AS week,
            ROUND(SUM(distance_m) / 1609.34, 2)  AS miles,
            COUNT(*) AS runs
        FROM activities
        {where}
        GROUP BY week
        ORDER BY week
    """, params).fetchall()
    return json.dumps([dict(r) for r in rows])


@mcp.tool()
def get_activities(
    run_type: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> str:
    """
    List individual runs with key stats.
    run_type: 'easy' | 'workout' | 'long_run' | 'race' | None for all.
    Dates are YYYY-MM-DD. Returns up to `limit` rows, newest first.
    """
    conn = _conn()
    type_clause, params = _run_type_filter()
    where = f"WHERE {type_clause}"
    if run_type:
        where += " AND run_type = ?"
        params.append(run_type)
    if start_date:
        where += " AND start_date >= ?"
        params.append(start_date)
    if end_date:
        where += " AND start_date <= ?"
        params.append(end_date)

    rows = conn.execute(f"""
        SELECT
            activity_id,
            name,
            start_date,
            run_type,
            ROUND(distance_m / 1609.34, 2) AS miles,
            moving_time_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile,
            average_heartrate,
            total_elevation_gain_m,
            strava_url
        FROM activities
        {where}
        ORDER BY start_date DESC
        LIMIT ?
    """, params + [limit]).fetchall()
    return json.dumps([dict(r) for r in rows])


@mcp.tool()
def get_training_block(start_date: str, end_date: str) -> str:
    """
    Aggregate stats for a training block (date range), broken down by run_type.
    Dates are YYYY-MM-DD. Good for comparing base vs. build phases.
    """
    conn = _conn()
    type_clause, base_params = _run_type_filter()
    date_clause = "AND start_date >= ? AND start_date <= ?"
    date_params = [start_date, end_date]

    by_type = conn.execute(f"""
        SELECT
            run_type,
            COUNT(*) AS runs,
            ROUND(SUM(distance_m) / 1609.34, 2)  AS total_miles,
            ROUND(AVG(distance_m) / 1609.34, 2)  AS avg_miles,
            ROUND(AVG(average_heartrate), 1)       AS avg_hr,
            CASE WHEN AVG(average_speed_mps) > 0
                 THEN ROUND(26.8224 / AVG(average_speed_mps), 2)
                 ELSE NULL END                     AS avg_pace_min_per_mile,
            ROUND(SUM(total_elevation_gain_m) * 3.28084, 0) AS total_elevation_ft
        FROM activities
        WHERE {type_clause} {date_clause}
        GROUP BY run_type
        ORDER BY run_type
    """, base_params + date_params).fetchall()

    totals = conn.execute(f"""
        SELECT
            COUNT(*) AS runs,
            ROUND(SUM(distance_m) / 1609.34, 2) AS total_miles,
            ROUND(SUM(total_elevation_gain_m) * 3.28084, 0) AS total_elevation_ft
        FROM activities
        WHERE {type_clause} {date_clause}
    """, base_params + date_params).fetchone()

    return json.dumps({
        "period": {"start": start_date, "end": end_date},
        "total": dict(totals),
        "by_type": [dict(r) for r in by_type],
    })


MARATHON_MIN_M = 42000.0
MARATHON_MAX_M = 43500.0


@mcp.tool()
def get_marathon_comparison(build_weeks: int = 12) -> str:
    """
    For every tagged marathon race, returns the result alongside stats for
    the build_weeks-week training block that preceded it.
    Sorted by date ascending. build_weeks defaults to 12.

    Each entry has:
      name, date, finish_time_s, distance_miles, pace_min_per_mile,
      build: { start, end, weeks, total_miles, avg_mpw,
               by_type: { easy, workout, long_run, race? } }
    """
    conn = _conn()
    type_clause, type_params = _run_type_filter()

    races = conn.execute("""
        SELECT
            name,
            DATE(start_date) AS race_date,
            ROUND(distance_m / 1609.34, 2) AS distance_miles,
            moving_time_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_per_mile
        FROM activities
        WHERE run_type = 'race'
          AND distance_m BETWEEN ? AND ?
        ORDER BY race_date
    """, [MARATHON_MIN_M, MARATHON_MAX_M]).fetchall()

    out = []
    for race in races:
        race_date: str = race["race_date"]
        build_start: str = conn.execute(
            "SELECT DATE(?, ?)", (race_date, f"-{build_weeks * 7} days")
        ).fetchone()[0]

        by_type = conn.execute(f"""
            SELECT
                run_type,
                COUNT(*) AS runs,
                ROUND(SUM(distance_m) / 1609.34, 2)         AS total_miles,
                ROUND(AVG(distance_m) / 1609.34, 2)         AS avg_miles,
                ROUND(AVG(average_heartrate), 1)             AS avg_hr,
                CASE WHEN AVG(average_speed_mps) > 0
                     THEN ROUND(26.8224 / AVG(average_speed_mps), 2)
                     ELSE NULL END                           AS avg_pace_min_per_mile
            FROM activities
            WHERE {type_clause}
              AND DATE(start_date) >= ?
              AND DATE(start_date) < ?
            GROUP BY run_type
            ORDER BY run_type
        """, type_params + [build_start, race_date]).fetchall()

        totals = conn.execute(f"""
            SELECT
                COUNT(*) AS runs,
                ROUND(SUM(distance_m) / 1609.34, 2) AS total_miles
            FROM activities
            WHERE {type_clause}
              AND DATE(start_date) >= ?
              AND DATE(start_date) < ?
        """, type_params + [build_start, race_date]).fetchone()

        total_miles: float = totals["total_miles"] or 0.0

        out.append({
            "name": race["name"],
            "date": race_date,
            "finish_time_s": race["moving_time_s"],
            "distance_miles": race["distance_miles"],
            "pace_min_per_mile": race["pace_min_per_mile"],
            "build": {
                "start": build_start,
                "end": race_date,
                "weeks": build_weeks,
                "total_miles": total_miles,
                "avg_mpw": round(total_miles / build_weeks, 1),
                "by_type": {
                    row["run_type"]: {
                        "runs": row["runs"],
                        "total_miles": row["total_miles"],
                        "avg_miles": row["avg_miles"],
                        "avg_hr": row["avg_hr"],
                        "avg_pace_min_per_mile": row["avg_pace_min_per_mile"],
                    }
                    for row in by_type
                    if row["run_type"] is not None
                },
            },
        })

    return json.dumps(out)


@mcp.tool()
def get_workout_laps(
    workout_label: str | None = None,
    name_contains: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> str:
    """
    Workout sessions with per-lap breakdown. Useful for cross-build quality comparisons.
    workout_label: classifier label e.g. 'LT', 'MP Flux', 'Tempo', 'Strides'.
    name_contains: substring match on activity name (fallback if no label set).
    Returns newest-first up to `limit` sessions. Each session includes:
      activity_id, name, date, workout_label,
      laps: [{lap_index, distance_miles, pace_min_per_mile, avg_hr, max_hr}]
    """
    conn = _conn()
    type_clause, params = _run_type_filter()
    where = f"WHERE {type_clause} AND run_type = 'workout'"
    if workout_label:
        where += " AND workout_label = ?"
        params.append(workout_label)
    if name_contains:
        where += " AND name LIKE ?"
        params.append(f"%{name_contains}%")
    if start_date:
        where += " AND start_date >= ?"
        params.append(start_date)
    if end_date:
        where += " AND start_date <= ?"
        params.append(end_date)

    activities = conn.execute(f"""
        SELECT activity_id, name, DATE(start_date) AS date, workout_label
        FROM activities
        {where}
        ORDER BY start_date DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    out = []
    for act in activities:
        laps = conn.execute("""
            SELECT
                lap_index,
                ROUND(distance_m / 1609.34, 3) AS distance_miles,
                CASE WHEN average_speed_mps > 0
                     THEN ROUND(26.8224 / average_speed_mps, 2)
                     ELSE NULL END AS pace_min_per_mile,
                average_heartrate AS avg_hr,
                max_heartrate AS max_hr
            FROM laps
            WHERE activity_id = ?
            ORDER BY lap_index
        """, [act["activity_id"]]).fetchall()
        out.append({
            "activity_id": act["activity_id"],
            "name": act["name"],
            "date": act["date"],
            "workout_label": act["workout_label"],
            "laps": [dict(lap) for lap in laps],
        })

    return json.dumps(out)


@mcp.tool()
def get_build_snapshot(race_date: str | None = None, build_weeks: int = 12) -> str:
    """
    Week-by-week breakdown of a marathon build.
    race_date: YYYY-MM-DD of the target race. If omitted, uses the most recent marathon in the DB.
    Returns: race info, weeks_to_race (negative = past race), week-by-week mileage with
    workout/long-run counts, all workout sessions with rep stats, and long run list.
    Use this to orient at the start of any build-specific conversation.
    """
    conn = _conn()
    type_clause, type_params = _run_type_filter()

    if race_date:
        race_date_str = race_date
        race_row = conn.execute("""
            SELECT name FROM activities
            WHERE run_type = 'race' AND distance_m BETWEEN ? AND ?
              AND DATE(start_date) = ?
        """, [MARATHON_MIN_M, MARATHON_MAX_M, race_date]).fetchone()
        race_name: str | None = race_row["name"] if race_row else None
    else:
        race_row = conn.execute("""
            SELECT name, DATE(start_date) AS race_date FROM activities
            WHERE run_type = 'race' AND distance_m BETWEEN ? AND ?
            ORDER BY start_date DESC LIMIT 1
        """, [MARATHON_MIN_M, MARATHON_MAX_M]).fetchone()
        if not race_row:
            return json.dumps({"error": "No marathon found in the database."})
        race_date_str = race_row["race_date"]
        race_name = race_row["name"]

    race_dt = date.fromisoformat(race_date_str)
    race_week_monday = race_dt - timedelta(days=race_dt.weekday())
    build_start = (race_week_monday - timedelta(weeks=build_weeks)).isoformat()
    weeks_to_race = (race_dt - date.today()).days // 7

    weeks = conn.execute(f"""
        SELECT
            CAST((julianday(DATE(start_date)) - julianday(?)) / 7.0 AS INTEGER) - ? AS week_offset,
            ROUND(SUM(distance_m) / 1609.34, 1) AS miles,
            COUNT(*) AS runs,
            SUM(CASE WHEN run_type = 'workout' THEN 1 ELSE 0 END) AS workouts,
            SUM(CASE WHEN run_type = 'long_run' THEN 1 ELSE 0 END) AS long_runs
        FROM activities
        WHERE {type_clause}
          AND DATE(start_date) >= ? AND DATE(start_date) <= ?
        GROUP BY week_offset
        ORDER BY week_offset
    """, [build_start, build_weeks] + type_params + [build_start, race_date_str]).fetchall()

    workouts = conn.execute("""
        SELECT
            a.activity_id,
            a.name,
            DATE(a.start_date) AS date,
            a.workout_label,
            COUNT(l.lap_id) AS rep_count,
            ROUND(AVG(26.8224 / l.average_speed_mps), 2) AS avg_rep_pace,
            ROUND(AVG(l.average_heartrate), 1) AS avg_rep_hr
        FROM activities a
        LEFT JOIN laps l ON l.activity_id = a.activity_id
            AND l.distance_m >= 200 AND l.moving_time_s >= 45
            AND l.average_speed_mps IS NOT NULL AND l.average_speed_mps > 0
        WHERE a.run_type = 'workout'
          AND DATE(a.start_date) >= ? AND DATE(a.start_date) < ?
        GROUP BY a.activity_id
        ORDER BY a.start_date
    """, [build_start, race_date_str]).fetchall()

    long_runs = conn.execute(f"""
        SELECT
            DATE(start_date) AS date,
            ROUND(distance_m / 1609.34, 1) AS miles,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS avg_pace,
            ROUND(average_heartrate) AS avg_hr
        FROM activities
        WHERE {type_clause} AND run_type = 'long_run'
          AND DATE(start_date) >= ? AND DATE(start_date) < ?
        ORDER BY start_date
    """, type_params + [build_start, race_date_str]).fetchall()

    return json.dumps({
        "race": race_name,
        "race_date": race_date_str,
        "build_start": build_start,
        "weeks_to_race": weeks_to_race,
        "weeks": [dict(w) for w in weeks],
        "workouts": [dict(w) for w in workouts],
        "long_runs": [dict(lr) for lr in long_runs],
    })


@mcp.tool()
def get_workout_session(activity_id: int) -> str:
    """
    Detailed view of a single workout: reps only, in sequence.
    Non-rep laps (< 200m or < 45s) are filtered out.
    Each rep: rep_num, distance_miles, duration_s, pace_min_mi, avg_hr, max_hr.
    Use this to inspect within-session structure — whether reps held even, drifted,
    or fell apart — rather than relying solely on session averages.
    activity_id comes from get_build_snapshot, compare_workouts_by_build, or get_activities.
    """
    conn = _conn()

    activity = conn.execute("""
        SELECT activity_id, name, DATE(start_date) AS date, workout_label,
               ROUND(distance_m / 1609.34, 2) AS total_miles,
               moving_time_s AS total_time_s, strava_url
        FROM activities WHERE activity_id = ?
    """, [activity_id]).fetchone()

    if not activity:
        return json.dumps({"error": f"Activity {activity_id} not found."})

    reps = conn.execute("""
        SELECT
            lap_index,
            ROUND(distance_m / 1609.34, 3) AS distance_miles,
            moving_time_s AS duration_s,
            CASE WHEN average_speed_mps > 0
                 THEN ROUND(26.8224 / average_speed_mps, 2)
                 ELSE NULL END AS pace_min_mi,
            ROUND(average_heartrate) AS avg_hr,
            ROUND(max_heartrate) AS max_hr
        FROM laps
        WHERE activity_id = ?
          AND distance_m >= 200 AND moving_time_s >= 45
          AND average_speed_mps IS NOT NULL AND average_speed_mps > 0
        ORDER BY lap_index
    """, [activity_id]).fetchall()

    return json.dumps({
        **dict(activity),
        "reps": [{"rep_num": i + 1, **dict(r)} for i, r in enumerate(reps)],
    })


@mcp.tool()
def get_easy_hr_trend(months: int = 36) -> str:
    """
    Monthly average HR and pace for easy runs — the primary long-term aerobic fitness signal.
    A declining HR trend at stable or faster paces indicates improving aerobic efficiency
    accumulated across builds, not attributable to any single cycle.
    Returns months with avg_hr, avg_pace_min_mi, run_count. Filtered to easy-tagged runs only.
    """
    conn = _conn()
    type_clause, type_params = _run_type_filter()

    cutoff_dt = date.today() - timedelta(days=months * 30)
    cutoff = cutoff_dt.isoformat()

    rows = conn.execute(f"""
        SELECT
            strftime('%Y-%m', start_date) AS month,
            COUNT(*) AS runs,
            ROUND(AVG(average_heartrate), 1) AS avg_hr,
            CASE WHEN AVG(average_speed_mps) > 0
                 THEN ROUND(26.8224 / AVG(average_speed_mps), 2)
                 ELSE NULL END AS avg_pace_min_mi
        FROM activities
        WHERE {type_clause}
          AND run_type = 'easy'
          AND average_heartrate IS NOT NULL
          AND start_date >= ?
        GROUP BY month
        ORDER BY month
    """, type_params + [cutoff]).fetchall()

    return json.dumps([dict(r) for r in rows])


@mcp.tool()
def compare_workouts_by_build(
    workout_label: str,
    build_weeks: int = 12,
) -> str:
    """
    Compare workout sessions (by label) across marathon builds.
    Non-rep laps are filtered out (distance < 200m or duration < 45s), so stats
    reflect actual work intervals only — not warmup, recovery jogs, or GPS artifacts.
    Returns builds chronologically, each with per-session:
      date, name, rep_count, avg_rep_pace_min_mi, avg_rep_hr, best_rep_pace_min_mi
    Use this for cross-build quality questions: "Did my LT pace drop at lower HR over time?"
    """
    conn = _conn()

    races = conn.execute("""
        SELECT name, DATE(start_date) AS race_date
        FROM activities
        WHERE run_type = 'race' AND distance_m BETWEEN ? AND ?
        ORDER BY race_date
    """, [MARATHON_MIN_M, MARATHON_MAX_M]).fetchall()

    sessions = conn.execute("""
        SELECT
            a.activity_id,
            a.name,
            DATE(a.start_date) AS date,
            COUNT(l.lap_id) AS rep_count,
            ROUND(AVG(26.8224 / l.average_speed_mps), 2) AS avg_rep_pace,
            ROUND(AVG(l.average_heartrate), 1) AS avg_rep_hr,
            ROUND(MIN(26.8224 / l.average_speed_mps), 2) AS best_rep_pace
        FROM activities a
        JOIN laps l USING (activity_id)
        WHERE a.workout_label = ?
          AND a.run_type = 'workout'
          AND l.distance_m >= 200
          AND l.moving_time_s >= 45
          AND l.average_speed_mps IS NOT NULL
          AND l.average_speed_mps > 0
        GROUP BY a.activity_id
        ORDER BY a.start_date
    """, [workout_label]).fetchall()

    builds: list[dict[str, object]] = []
    for race in races:
        race_date_str: str = race["race_date"]
        race_dt = date.fromisoformat(race_date_str)
        race_week_monday = race_dt - timedelta(days=race_dt.weekday())
        build_start = (race_week_monday - timedelta(weeks=build_weeks)).isoformat()

        build_sessions = [
            dict(s) for s in sessions
            if build_start <= s["date"] < race_date_str
        ]
        if build_sessions:
            builds.append({
                "race": race["name"],
                "race_date": race_date_str,
                "sessions": build_sessions,
            })

    return json.dumps(builds)


@mcp.tool()
def run_sql(query: str) -> str:
    """
    Run a read-only SQL SELECT against the database.
    Use this for ad-hoc questions the other tools don't cover.

    Table: activities
      activity_id, name, sport_type, start_date, workout_type, run_type, workout_label,
      distance_m, moving_time_s, elapsed_time_s, total_elevation_gain_m,
      average_speed_mps, max_speed_mps, average_heartrate, max_heartrate,
      average_cadence, gear_id, strava_url, synced_at

    Table: laps  (one row per lap; only workout activities are synced)
      lap_id, activity_id, lap_index, distance_m, moving_time_s, average_speed_mps,
      average_heartrate, max_heartrate, average_cadence, total_elevation_gain_m, pace_zone
    """
    stripped = query.strip().upper().lstrip("(")
    if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
        return json.dumps({"error": "Only SELECT / WITH queries are permitted."})
    conn = _conn()
    try:
        rows = conn.execute(query).fetchall()
        return json.dumps([dict(r) for r in rows])
    except Exception as e:
        return json.dumps({"error": str(e)})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
