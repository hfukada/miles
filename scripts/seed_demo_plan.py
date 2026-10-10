"""Seed a realistic 16-week marathon plan into a SCRATCH copy of the DB, for
visually reviewing plan.html's populated states (active plan, versions + diff,
plan_log markers, adherence bands, progression charts).

Requires a scratch copy of a DB that already holds a synced history; the
seeded plan is overlaid on those activities.

Usage:
    cp data/activities.db /tmp/plan_demo.db
    MILES_DB=/tmp/plan_demo.db uv run python scripts/seed_demo_plan.py
    MILES_DB=/tmp/plan_demo.db uv run uvicorn miles.api:app --port 8001

The plan window (WEEK1_MONDAY..RACE_DATE) starts ten weeks before this week,
so the first ten weeks overlap synced training and overlay the planned targets,
and the revision changes only weeks that have not started (a revision never
rewrites weeks already governed by the earlier version).

SAFETY: refuses to run unless MILES_DB is set and points somewhere other than
the repo's real data/activities.db.
"""

import os
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DB_ENV = os.environ.get("MILES_DB", "")
assert _DB_ENV, "refusing to run — set MILES_DB to a scratch copy of the DB"
assert Path(_DB_ENV).resolve() != (_REPO / "data" / "activities.db").resolve(), \
    "refusing to run against the real data/activities.db — use a scratch copy"

sys.path.insert(0, str(_REPO))

from miles import db, plan  # noqa: E402
from miles.derive import derive_all  # noqa: E402
from miles.periods import monday_of  # noqa: E402

N_WEEKS = 16
WEEK1_MONDAY = monday_of(date.today()) - timedelta(weeks=10)
RACE_DATE = WEEK1_MONDAY + timedelta(weeks=N_WEEKS) - timedelta(days=1)  # Sunday of week 16

TARGET_MILES = [28, 32, 35, 26, 38, 42, 45, 36, 48, 52, 50, 56, 60, 40, 26, 12]
TARGET_WORKOUTS = [1, 1, 1, 1, 1, 1, 2, 1, 2, 2, 2, 2, 2, 1, 1, 0]
TARGET_LONG_RUN = [10, 11, 12, 9, 14, 16, 16, 13, 18, 19, 19, 20, 20, 14, 10, 3]
PHASES = ["base"] * 6 + ["sharpen"] * 4 + ["peak"] * 3 + ["taper"] * 2 + ["race"]

assert len(TARGET_MILES) == len(TARGET_WORKOUTS) == len(TARGET_LONG_RUN) == len(PHASES) == N_WEEKS

WORKOUT_ZONE_BY_PHASE = {
    "base": "threshold",
    "sharpen": "interval",
    "peak": "marathon",
    "taper": "marathon",
    "race": None,
}


def week_starts() -> list[date]:
    return [WEEK1_MONDAY + timedelta(weeks=i) for i in range(N_WEEKS)]


def build_weeks(days: list[plan.DayInput], target_long_run: list[int]) -> list[plan.WeekInput]:
    """Each week targets the sum of its mileage-slot days, which commit requires."""
    weeks: list[plan.WeekInput] = []
    for i, ws in enumerate(week_starts()):
        week_end = (ws + timedelta(days=6)).isoformat()
        week_miles = sum(
            d.get("target_miles") or 0.0 for d in days
            if d["slot"] in plan.MILEAGE_SLOTS and ws.isoformat() <= d["date"] <= week_end
        )
        weeks.append(plan.WeekInput(
            week_start=ws.isoformat(),
            target_miles=round(week_miles, 1),
            target_workouts=TARGET_WORKOUTS[i],
            phase=PHASES[i],
            target_long_run_miles=float(target_long_run[i]) if target_long_run[i] else None,
            note=None,
        ))
    return weeks


def build_days(target_miles: list[int], target_long_run: list[int]) -> list[plan.DayInput]:
    days: list[plan.DayInput] = []
    for i, ws in enumerate(week_starts()):
        miles = target_miles[i]
        workouts = TARGET_WORKOUTS[i]
        long_run = target_long_run[i]
        phase = PHASES[i]
        zone = WORKOUT_ZONE_BY_PHASE[phase]

        if phase == "race":
            for d in range(6):
                the_date = ws + timedelta(days=d)
                days.append(plan.DayInput(
                    date=the_date.isoformat(), slot="rest" if d == 4 else "easy",
                    title="Shakeout" if d != 4 else None,
                    target_miles=None if d == 4 else round(miles * 0.15, 1),
                ))
            race_day = ws + timedelta(days=6)
            days.append(plan.DayInput(
                date=race_day.isoformat(), slot="race", title="Goal race",
                target_miles=26.2,
            ))
            continue

        mon, tue, wed, thu, fri, sat, sun = (ws + timedelta(days=d) for d in range(7))

        days.append(plan.DayInput(
            date=mon.isoformat(), slot="easy", target_miles=round(miles * 0.12, 1),
            target=plan.DayTarget(zone_name="easy"),
        ))

        if workouts >= 1:
            reps: plan.DayTarget = {"zone_name": zone} if zone else {}
            if zone == "interval":
                reps = {"zone_name": "interval", "reps": 6, "rep_distance_m": 1000.0}
            elif zone == "threshold":
                reps = {"zone_name": "threshold", "reps": 1, "rep_distance_m": 6000.0}
            elif zone == "marathon":
                reps = {"zone_name": "marathon", "reps": 3, "rep_distance_m": 3218.0}
            title = {"interval": "6x1000m", "threshold": "Tempo", "marathon": "MP intervals"}.get(zone, "Workout")
            days.append(plan.DayInput(
                date=tue.isoformat(), slot="workout", title=title,
                target_miles=round(miles * 0.18, 1), target=reps,
            ))
        else:
            days.append(plan.DayInput(
                date=tue.isoformat(), slot="easy", target_miles=round(miles * 0.12, 1),
                target=plan.DayTarget(zone_name="easy"),
            ))

        days.append(plan.DayInput(
            date=wed.isoformat(), slot="easy", target_miles=round(miles * 0.14, 1),
            target=plan.DayTarget(zone_name="easy"),
        ))

        if workouts >= 2:
            days.append(plan.DayInput(
                date=thu.isoformat(), slot="workout", title="Threshold",
                target_miles=round(miles * 0.16, 1),
                target={"zone_name": "threshold", "reps": 1, "rep_distance_m": 4800.0},
            ))
        else:
            days.append(plan.DayInput(
                date=thu.isoformat(), slot="easy", target_miles=round(miles * 0.12, 1),
                target=plan.DayTarget(zone_name="easy"),
            ))

        days.append(plan.DayInput(date=fri.isoformat(), slot="rest"))

        days.append(plan.DayInput(
            date=sat.isoformat(), slot="easy", target_miles=round(miles * 0.10, 1),
            target=plan.DayTarget(zone_name="easy"),
        ))

        days.append(plan.DayInput(
            date=sun.isoformat(), slot="long", title="Long run",
            target_miles=float(long_run), target=plan.DayTarget(zone_name="easy"),
        ))

    return days


def seed(conn: sqlite3.Connection) -> None:
    db.init_db(conn)

    plan_id, v1_id = plan.start_plan_draft(
        conn, title="Demo Marathon", race_date=RACE_DATE.isoformat(),
        distance_bucket="marathon", goal_time_s=3 * 3600 + 15 * 60,
    )
    days = build_days(TARGET_MILES, TARGET_LONG_RUN)
    plan.upsert_draft_days(conn, v1_id, days)
    plan.upsert_draft_weeks(conn, v1_id, build_weeks(days, TARGET_LONG_RUN))
    plan.commit_plan(conn, plan_id, note="Initial 16-week build for the demo marathon.")
    # Version 1 governs from week 1 whatever its committed_at, so this date
    # only makes the history read as authored before the plan began.
    authored_at = (datetime.combine(WEEK1_MONDAY, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE plan_versions SET created_at = ?, committed_at = ? WHERE version_id = ?", [authored_at, authored_at, v1_id])
    conn.commit()
    print(f"created plan_id={plan_id} v1 version_id={v1_id}")

    # Mid-plan revision so version history, the diff view, and contemporaneous
    # scoring have something to show. A later version governs the weeks that
    # begin after its committed_at, so backdating it to week 8 makes
    # version_n_used flip 1 -> 2 from week 9 on.
    v2_miles = list(TARGET_MILES)
    v2_miles[12], v2_miles[13], v2_miles[14] = 52, 36, 24
    v2_long_run = list(TARGET_LONG_RUN)
    v2_long_run[12] = 18
    v2_id = plan.start_revision_draft(conn, plan_id)
    days = build_days(v2_miles, v2_long_run)
    plan.upsert_draft_days(conn, v2_id, days)
    plan.upsert_draft_weeks(conn, v2_id, build_weeks(days, v2_long_run))
    plan.commit_plan(conn, plan_id, note="Cutting the true peak week after two weeks of elevated soreness.")
    revised_at = (datetime.combine(WEEK1_MONDAY + timedelta(weeks=7), datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)).isoformat()
    conn.execute("UPDATE plan_versions SET committed_at = ? WHERE version_id = ?", [revised_at, v2_id])
    conn.commit()
    print(f"created v2 version_id={v2_id}")

    log1 = plan.add_log_entry(
        conn, plan_id, log_date=(WEEK1_MONDAY + timedelta(weeks=5, days=1)).isoformat(), action="skipped",
        reason="sore right calf, skipped the Tuesday tempo",
    )
    print(f"created log entry: {log1}")

    counts = derive_all(conn)
    print(f"derive_all counts: {counts}")


def main() -> None:
    with closing(db.connect()) as conn:
        seed(conn)


if __name__ == "__main__":
    main()
