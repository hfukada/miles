"""Seed a SCRATCH copy of the DB with synthetic activities (position + cadence
streams written straight to the raw stream store, no Strava calls) and a
synthetic route, for visually reviewing terrain.html, route.html, and
activity.html's climbs/descents table.

Usage:
    MILES_DB=/tmp/terrain_demo/a.db MILES_STREAMS_DIR=/tmp/terrain_demo/streams \\
        uv run python scripts/seed_terrain_demo.py
    MILES_DB=/tmp/terrain_demo/a.db MILES_STREAMS_DIR=/tmp/terrain_demo/streams \\
        uv run uvicorn miles.api:app --port 8010

Each activity walks a sine-wave grade profile from roughly -18% to +18%, with
cadence dropping to a walking rate above 13% grade and speed following the
Minetti cost curve elsewhere — so the derived walk-onset/descent-comfort
thresholds land near, but not exactly on, those synthetic breakpoints, and
the rarest grade bins (near +/-18%) stay low-confidence for exercising the
UI's muting of thin bins.

SAFETY: refuses to run unless MILES_DB and MILES_STREAMS_DIR are set and
point somewhere other than the repo's real data/.
"""

import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stravalib.model import Stream

_REPO = Path(__file__).resolve().parent.parent
_DB_ENV = os.environ.get("MILES_DB", "")
_STREAMS_ENV = os.environ.get("MILES_STREAMS_DIR", "")
assert _DB_ENV, "refusing to run — set MILES_DB to a scratch copy of the DB"
assert _STREAMS_ENV, "refusing to run — set MILES_STREAMS_DIR to a scratch streams cache dir"
assert Path(_DB_ENV).resolve() != (_REPO / "data" / "activities.db").resolve(), \
    "refusing to run against the real data/activities.db — use a scratch copy"
assert Path(_STREAMS_ENV).resolve() != (_REPO / "data" / "streams").resolve(), \
    "refusing to run against the real data/streams — use a scratch copy"

sys.path.insert(0, str(_REPO))

from miles import db, routes as routes_service, streams, terrain  # noqa: E402

N_ACTIVITIES = 10
STEP_M = 5.0
LOOP_LENGTH_M = 3000.0
WALK_GRADE_PCT = 13.0
# Synthetic coordinates in open ocean, far from any real trail network.
BASE_LAT, BASE_LNG = 5.0, -170.0


def grade_at(dist_m: float) -> float:
    """Sine wave from -18% to +18% over one loop."""
    return 18.0 * math.sin(2 * math.pi * dist_m / LOOP_LENGTH_M)


def speed_for_grade(grade_pct: float) -> float:
    """Constant-effort Minetti speed below the walk grade; a slow walking
    pace at or above it, independent of the energy-cost curve -- the same
    divergence between "physically efficient" and "what a person actually
    does" the real terrain model is built to capture."""
    if grade_pct >= WALK_GRADE_PCT:
        return 1.3
    return 3.0 / streams.minetti_cost_ratio(grade_pct)


def build_activity_streams(seed_offset: float) -> dict[str, Stream]:
    """Strava-shaped raw streams for one synthetic run."""
    n = int(LOOP_LENGTH_M / STEP_M)
    columns: dict[str, list] = {
        "time": [], "distance": [], "altitude": [], "velocity_smooth": [], "heartrate": [],
        "cadence": [], "grade_smooth": [], "moving": [], "latlng": [],
    }
    dist = 0.0
    alt = 200.0
    t = 0.0
    for i in range(n):
        g = grade_at(dist + seed_offset)
        speed = speed_for_grade(g)
        single_leg_cadence = 50.0 if g >= WALK_GRADE_PCT else 82.0
        columns["time"].append(t)
        columns["distance"].append(dist)
        columns["altitude"].append(alt)
        columns["velocity_smooth"].append(speed)
        columns["heartrate"].append(140.0 + g * 0.6)
        columns["cadence"].append(single_leg_cadence)
        columns["grade_smooth"].append(g)
        columns["moving"].append(True)
        columns["latlng"].append([BASE_LAT + dist * 1e-6, BASE_LNG + i * 1e-7])
        dist += STEP_M
        alt += g / 100.0 * STEP_M
        t += STEP_M / speed
    return {
        name: Stream(type=name, data=data, series_type="distance", original_size=len(data), resolution="high")
        for name, data in columns.items()
    }


def climb_route_gpx() -> bytes:
    """A separate synthetic out-and-back with a sustained climb, a flat
    shoulder, and a steep descent -- distinct terrain from the activities
    above so score_route/route_point_classes has real run/grind/hike mix."""
    lat0, lng0 = BASE_LAT + 0.2, BASE_LNG + 0.2
    step_deg = 0.00018  # ~20m/point at this latitude
    profile = (
        [(i, 3.0) for i in range(20)]       # gradual climb (run)
        + [(i, 16.0) for i in range(25)]    # steep climb, above walk-onset (hike)
        + [(i, 0.0) for i in range(15)]     # flat shoulder (run)
        + [(i, -22.0) for i in range(20)]   # steep descent, past descent-comfort (grind)
        + [(i, -6.0) for i in range(20)]    # comfortable descent (run)
        + [(i, 0.5) for i in range(20)]     # easy return (run)
    )
    pts = []
    ele = 300.0
    for i, grade in enumerate(profile):
        _, g = grade
        ele += g / 100.0 * 20.0
        pts.append(f'<trkpt lat="{lat0 + i * step_deg}" lon="{lng0}"><ele>{ele:.1f}</ele></trkpt>')
    body = f"<trk><name>Synthetic Ridge Loop</name><trkseg>{''.join(pts)}</trkseg></trk>"
    return f"""<?xml version="1.0"?>
<gpx version="1.1" creator="seed_terrain_demo" xmlns="http://www.topografix.com/GPX/1/1">{body}</gpx>""".encode("utf-8")


def main() -> None:
    Path(_STREAMS_ENV).mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    db.init_db(conn)

    now = datetime.now(timezone.utc)
    activity_ids = []
    for i in range(N_ACTIVITIES):
        activity_id = 900_000 + i
        streams.store_raw_streams(activity_id, build_activity_streams(seed_offset=i * 137.0))
        start = now - timedelta(days=(N_ACTIVITIES - i) * 8)
        conn.execute(
            """
            INSERT OR REPLACE INTO activities (
                activity_id, name, sport_type, start_date, workout_type, run_type,
                distance_m, moving_time_s, elapsed_time_s, total_elevation_gain_m,
                average_speed_mps, max_speed_mps, average_heartrate, max_heartrate,
                average_cadence, gear_id, strava_url, synced_at, start_lat, start_lng, raw_json,
                laps_synced_at
            ) VALUES (?, ?, 'Run', ?, 0, 'easy', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, '{}', ?)
            """,
            [
                activity_id, f"Synthetic terrain run {i}", start.strftime("%Y-%m-%dT%H:%M:%S"),
                LOOP_LENGTH_M, int(LOOP_LENGTH_M / 3.0), int(LOOP_LENGTH_M / 3.0), 20.0,
                3.0, 4.0, 150.0, 170.0, 170.0,
                f"https://www.strava.com/activities/{activity_id}", now.isoformat(),
                BASE_LAT, BASE_LNG,
                # laps_synced_at set so /api/activity-streams skips its live
                # Strava laps fetch -- there's no lap data to seed here, only
                # the position/cadence streams the terrain model reads.
                now.isoformat(),
            ],
        )
        activity_ids.append(activity_id)
    conn.commit()

    result = terrain.rebuild_terrain(conn, full=True)
    print(f"Segmented {result['activities_segmented']} activities, {result['snapshots']} snapshots.")

    route_ids = routes_service.upload_gpx(conn, climb_route_gpx(), "synthetic_ridge_loop.gpx", name="Synthetic Ridge Loop")
    print(f"Seeded activities: {activity_ids[0]}..{activity_ids[-1]}")
    print(f"Seeded route id: {route_ids[0]}")
    print(f"View: /activity.html?id={activity_ids[0]}  /route.html?id={route_ids[0]}  /terrain.html")


if __name__ == "__main__":
    main()
