import sqlite3
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import click
from stravalib.exc import Fault
from typing_extensions import TypedDict

from . import db, plan, strava_client, weather as weather_module
from .classifier import classify_workout
from .derive import derive_all


class SyncStatus(TypedDict):
    status: str  # "idle" | "running" | "done" | "error"
    phase: str | None
    done: int | None
    total: int | None
    until: str | None  # "HH:MM" local, set only during a "waiting" phase
    started_at: str | None
    finished_at: str | None
    error: str | None
    new_activities: int | None


class SyncProgress:
    """Thread-safe status for one sync run, polled by /api/sync/status and
    (optionally) echoed to the terminal by the CLI. Holds only the current phase —
    there is no history, just "what's happening right now." One instance is reused
    across an entire run_with_retry call, including any rate-limit retries."""

    def __init__(self, printer: Callable[[SyncStatus], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._printer = printer
        self._reset_locked()

    def _reset_locked(self) -> None:
        self._status = "idle"
        self._phase: str | None = None
        self._done: int | None = None
        self._total: int | None = None
        self._until: str | None = None
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._error: str | None = None
        self._new_activities: int | None = None

    def _snapshot_locked(self) -> SyncStatus:
        return {
            "status": self._status,
            "phase": self._phase,
            "done": self._done,
            "total": self._total,
            "until": self._until,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
            "error": self._error,
            "new_activities": self._new_activities,
        }

    def _notify_locked(self) -> None:
        if self._printer is not None:
            self._printer(self._snapshot_locked())

    def start(self) -> None:
        with self._lock:
            self._reset_locked()
            self._status = "running"
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._notify_locked()

    def try_start(self) -> bool:
        """Atomically switch to running unless a run is already in progress.
        Returns whether this call won the race and should launch the sync."""
        with self._lock:
            if self._status == "running":
                return False
            self._reset_locked()
            self._status = "running"
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._notify_locked()
            return True

    def phase(
        self, name: str, *, done: int | None = None, total: int | None = None, until: str | None = None
    ) -> None:
        with self._lock:
            self._phase = name
            self._done = done
            self._total = total
            self._until = until
            self._notify_locked()

    def finish(self, new_activities: int) -> None:
        with self._lock:
            self._status = "done"
            self._phase = "done"
            self._finished_at = datetime.now(timezone.utc).isoformat()
            self._new_activities = new_activities
            self._notify_locked()

    def fail(self, error: str) -> None:
        with self._lock:
            self._status = "error"
            self._phase = "error"
            self._finished_at = datetime.now(timezone.utc).isoformat()
            self._error = error
            self._notify_locked()

    def snapshot(self) -> SyncStatus:
        with self._lock:
            return self._snapshot_locked()

    @property
    def status(self) -> str:
        with self._lock:
            return self._status


def _print_progress(snapshot: SyncStatus) -> None:
    """miles-sync's connection to a SyncProgress: turns each phase update back into
    the terminal text a live run has always printed. The API's background sync
    passes no printer, so this never runs there — its progress is exposed only via
    GET /api/sync/status."""
    phase = snapshot["phase"]
    if phase == "activities":
        print(f"  {snapshot['done']} activities fetched...")
    elif phase == "waiting":
        print(f"\nRate limit hit — waiting until {snapshot['until']}...")
    elif phase in ("laps", "weather", "backfill") and snapshot["total"] is not None:
        print(f"  {phase} {snapshot['done']}/{snapshot['total']}...")
    elif phase == "done":
        n = snapshot["new_activities"] or 0
        print(f"Sync complete. {n} new/updated activities.")


def _wait_for_rate_limit(progress: SyncProgress) -> None:
    now = datetime.now()
    seconds_into_window = (now.minute % 15) * 60 + now.second
    wait = (15 * 60) - seconds_into_window + 5  # +5s buffer past window boundary
    until = (now + timedelta(seconds=wait)).strftime("%H:%M")
    progress.phase("waiting", until=until)
    time.sleep(wait)


def _extra_lap_backfill(conn: sqlite3.Connection, extra_limit: int, reserve: int, progress: SyncProgress) -> None:
    """Backfill laps for all remaining runs, long runs first then newest-first.
    Resumable: each activity is stamped and committed individually, and a fresh
    generator is built for the remaining ids after a rate-limit interruption.

    The remaining-rows check is a local DB query (free); it runs before anything
    that spends a Strava call, so a no-op --extra (nothing left to backfill, the
    common case once history is fully backfilled) never touches the network,
    prints nothing, and reports no progress phase.
    """
    effective_run_type = db.effective_run_type_sql()
    remaining_ids = [
        row["activity_id"]
        for row in conn.execute(f"""
            SELECT activity_id FROM activities
            WHERE sport_type IN ('Run', 'TrailRun', 'VirtualRun')
              AND laps_synced_at IS NULL
            ORDER BY CASE WHEN {effective_run_type} = 'long_run' THEN 0 ELSE 1 END,
                     start_date DESC
        """).fetchall()
    ]
    total_remaining = len(remaining_ids)
    if total_remaining == 0:
        return

    # The incremental sync earlier in _run has already hit the API, so the
    # recorded daily usage is current — skip without spending another call
    # when the reserve is already gone (e.g. a second --extra run today).
    remaining_calls = strava_client.daily_calls_remaining()
    if remaining_calls is not None and remaining_calls <= reserve:
        return

    todo_ids = remaining_ids[:extra_limit]
    batch_size = len(todo_ids)
    fetched = 0
    lap_total = 0
    # Nonzero start: the first 429 always waits out the 15-min window; only a
    # 429 after a fruitless full-window wait means the daily cap.
    successes_since_429 = 1

    progress.phase("backfill", done=0, total=batch_size)
    stopped_for_reserve = False
    while todo_ids and not stopped_for_reserve:
        processed_in_attempt = 0
        try:
            for activity_id, laps in strava_client.get_activity_laps_batch(todo_ids):
                if laps:
                    db.upsert_laps(conn, laps)
                    lap_total += len(laps)
                conn.execute(
                    "UPDATE activities SET laps_synced_at = datetime('now') WHERE activity_id = ?",
                    (activity_id,),
                )
                conn.commit()
                fetched += 1
                successes_since_429 += 1
                processed_in_attempt += 1
                if fetched % 25 == 0:
                    progress.phase("backfill", done=fetched, total=batch_size)
                remaining_calls = strava_client.daily_calls_remaining()
                if remaining_calls is not None and remaining_calls <= reserve:
                    stopped_for_reserve = True
                    break
            else:
                todo_ids = []
        except Fault as e:
            if e.response is not None and e.response.status_code == 429:
                todo_ids = todo_ids[processed_in_attempt:]
                if successes_since_429 == 0:
                    break
                _wait_for_rate_limit(progress)
                successes_since_429 = 0
            else:
                raise

    progress.phase("backfill", done=fetched, total=batch_size)


def _run(
    conn: sqlite3.Connection,
    full: bool,
    extra: bool,
    extra_limit: int,
    extra_reserve: int,
    progress: SyncProgress,
) -> int:
    """Run one sync pass. Returns the number of new/updated activities fetched."""
    after = None if full else db.last_synced_date(conn)
    if after:
        # activities.start_date is athlete-local wall time (db.py), but Strava's
        # `after` filter cuts on true UTC start_date. Local time can sit hours
        # either side of UTC depending on the athlete's timezone/DST — for an
        # athlete east of UTC, using the local timestamp directly would produce
        # a cursor LATER than the real UTC cutoff and could permanently skip
        # activities in that gap. Subtract generous slack; re-fetching a couple
        # extra days is harmless since upserts are idempotent.
        after = (datetime.fromisoformat(after) - timedelta(hours=48)).isoformat()
        print(f"Incremental sync: fetching activities after {after}")
    else:
        print("Full sync: fetching all activities (may take a few minutes)...")

    rows = []
    progress.phase("activities", done=0)
    for i, row in enumerate(strava_client.get_activities(after_ts=after)):
        rows.append(row)
        if (i + 1) % 50 == 0:
            progress.phase("activities", done=i + 1)
    progress.phase("activities", done=len(rows))

    if rows:
        db.upsert_activities(conn, rows)

    total = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    print(f"Done. {len(rows)} new/updated. {total} total in DB.")

    # Run derive here (not just at the end) so newly synced rows have run_type_inferred
    # populated before the lap fetch below queries effective type — otherwise
    # freshly-inferred races/workouts would be skipped for another sync cycle.
    progress.phase("derive")
    counts = derive_all(conn)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
    print(f"Derive done. {summary}")

    # Auto-complete the active plan when a synced effective-race activity
    # now matches its race_date (+/-1 day) and distance_bucket (case-
    # insensitive) — placed after the derive_all above so a race that was
    # only just inferred (run_type_inferred) this same sync cycle is already
    # visible to the match, not just on the next sync. The only mutation is
    # the status flip; plans/plan_versions/plan_weeks/plan_days stay
    # untouched and append-only. No-ops cleanly with no active plan or no
    # matching race (see plan.auto_complete_plan).
    completed_plan_id = plan.auto_complete_plan(conn)
    if completed_plan_id is not None:
        print(f"Plan {completed_plan_id} auto-completed (matching race synced).")

    # Lazy lap sync: fetch laps for workout/race activities that have none yet.
    # Backstamp anything already fetched by a prior sync so it's never refetched.
    conn.execute("""
        UPDATE activities SET laps_synced_at = datetime('now')
        WHERE laps_synced_at IS NULL
          AND activity_id IN (SELECT DISTINCT activity_id FROM laps)
    """)
    conn.commit()

    effective_run_type = db.effective_run_type_sql()
    unsynced = conn.execute(f"""
        SELECT activity_id, name FROM activities
        WHERE {effective_run_type} IN ('workout', 'race')
          AND laps_synced_at IS NULL
        ORDER BY start_date
    """).fetchall()

    if unsynced:
        total_workouts = len(unsynced)
        print(f"Fetching laps for {total_workouts} workout(s)...")
        ids = [a["activity_id"] for a in unsynced]
        names = {a["activity_id"]: a["name"] for a in unsynced}
        lap_total = 0
        progress.phase("laps", done=0, total=total_workouts)
        for i, (activity_id, laps) in enumerate(strava_client.get_activity_laps_batch(ids), 1):
            if laps:
                db.upsert_laps(conn, laps)
                lap_total += len(laps)
            conn.execute(
                "UPDATE activities SET laps_synced_at = datetime('now') WHERE activity_id = ?",
                (activity_id,),
            )
            conn.commit()
            name: str | None = names.get(activity_id)
            if name:
                label = classify_workout(name)
                if label:
                    conn.execute(
                        "UPDATE activities SET workout_label = ? WHERE activity_id = ? AND workout_label IS NULL",
                        (label, activity_id),
                    )
                    conn.commit()
            if i % 5 == 0 or i == total_workouts:
                progress.phase("laps", done=i, total=total_workouts)
        print(f"Laps done. {lap_total} total.")

    # Backfill labels for any workout activities that have laps but no label yet.
    unlabeled = conn.execute("""
        SELECT activity_id, name FROM activities
        WHERE run_type = 'workout' AND workout_label IS NULL
          AND activity_id IN (SELECT DISTINCT activity_id FROM laps)
    """).fetchall()
    for activity in unlabeled:
        unlabeled_name: str | None = activity["name"]
        if unlabeled_name:
            label = classify_workout(unlabeled_name)
            if label:
                conn.execute(
                    "UPDATE activities SET workout_label = ? WHERE activity_id = ?",
                    (label, activity["activity_id"]),
                )
    if unlabeled:
        conn.commit()

    # Weather sync: fetch for any activity with location but no weather yet.
    # Open-Meteo's hourly data is requested in UTC (weather.py's `timezone: UTC`
    # param), so this needs Strava's real UTC start_date — recovered from
    # raw_json, since activities.start_date now stores athlete-local wall time.
    needs_weather = conn.execute("""
        SELECT a.activity_id, a.start_lat, a.start_lng, a.moving_time_s,
               json_extract(a.raw_json, '$.start_date') AS utc_start_date
        FROM activities a
        LEFT JOIN weather w ON w.activity_id = a.activity_id
        WHERE a.start_lat IS NOT NULL AND a.start_lng IS NOT NULL
          AND a.moving_time_s IS NOT NULL
          AND json_extract(a.raw_json, '$.start_date') IS NOT NULL
          AND w.activity_id IS NULL
        ORDER BY a.start_date DESC
    """).fetchall()

    if needs_weather:
        total_w = len(needs_weather)
        print(f"Fetching weather for {total_w} activities...")

        # Group by rounded location (~11km grid) so each group needs at most 2 API calls.
        groups: defaultdict[tuple[float, float], list[weather_module.WeatherSpec]] = defaultdict(list)
        for act in needs_weather:
            key = (round(float(act["start_lat"]), 1), round(float(act["start_lng"]), 1))
            start_dt = datetime.fromisoformat(str(act["utc_start_date"]).replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            groups[key].append({
                "activity_id": act["activity_id"],
                "start_dt": start_dt,
                "duration_s": int(act["moving_time_s"]),
            })

        total_groups = len(groups)
        print(f"  {total_groups} location group(s) — at most {total_groups * 2} API calls total.")
        fetched_w = 0
        progress.phase("weather", done=0, total=total_groups)
        for g_idx, ((lat, lng), specs) in enumerate(groups.items(), 1):
            rows = weather_module.fetch_weather_bulk(specs, lat, lng)
            if rows:
                db.upsert_weather(conn, rows)
                fetched_w += len(rows)
            print(f"  Group {g_idx}/{total_groups} ({lat:.1f},{lng:.1f}): {len(rows)}/{len(specs)} fetched. Total: {fetched_w}/{total_w}")
            progress.phase("weather", done=g_idx, total=total_groups)

        print(f"Weather done. {fetched_w} new records.")

    if extra:
        _extra_lap_backfill(conn, extra_limit, extra_reserve, progress)

    # Recompute all derived values (inferred run types, lap types, ...) from raw synced rows.
    progress.phase("derive")
    counts = derive_all(conn)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
    print(f"Derive done. {summary}")

    # Stamp the moment this sync finished so readers (adherence, plan tools)
    # can tell how fresh the synced data is without guessing from activities.
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('last_sync_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [datetime.now(timezone.utc).isoformat()],
    )
    conn.commit()

    return len(rows)


def run_with_retry(
    conn: sqlite3.Connection,
    full: bool,
    extra: bool,
    extra_limit: int = 900,
    extra_reserve: int = 10,
    progress: SyncProgress | None = None,
) -> int:
    """Run _run, retrying once per 15-minute window on a Strava 429 until it
    succeeds. Shared by miles-sync (CLI) and /api/sync (web UI, which runs this on
    a background thread); returns the number of new/updated activities. Callers own
    the running/done/error transitions on `progress` (start/try_start, finish/fail)
    — this only reports phase updates during the run itself.
    """
    prog = progress if progress is not None else SyncProgress()
    while True:
        try:
            return _run(conn, full, extra, extra_limit, extra_reserve, prog)
        except Fault as e:
            if e.response is not None and e.response.status_code == 429:
                _wait_for_rate_limit(prog)
            else:
                raise


@click.command()
@click.option("--full", is_flag=True, help="Ignore last sync date and fetch everything.")
@click.option("--extra", is_flag=True, help="Backfill laps for all remaining runs, most important first (resumable; rerun daily until complete).")
@click.option("--extra-limit", type=int, default=900, help="Max lap fetches per --extra invocation.")
@click.option("--extra-reserve", type=int, default=10, help="Daily API calls to leave unused when --extra backfills (so later syncs today can still fetch new activities).")
@click.option("--max-hr", type=int, default=None, help="Set max heart rate and exit (no Strava calls).")
@click.option("--long-run-floor", type=float, default=None, help="Set long-run distance floor in miles and exit (no Strava calls).")
def main(
    full: bool,
    extra: bool,
    extra_limit: int,
    extra_reserve: int,
    max_hr: int | None,
    long_run_floor: float | None,
) -> None:
    conn = db.connect()
    db.init_db(conn)

    if max_hr is not None or long_run_floor is not None:
        existing = db.get_athlete(conn)
        merged_max_hr = max_hr if max_hr is not None else (existing["max_hr"] if existing else None)
        merged_floor = (
            long_run_floor if long_run_floor is not None
            else (existing["long_run_floor_miles"] if existing else None)
        )
        db.upsert_athlete(conn, max_hr=merged_max_hr, long_run_floor_miles=merged_floor)
        # A changed long_run_floor_miles is a global classifier input (inference.py),
        # not a raw-data change any writer marks dirty — needs the same full rebuild
        # as a DERIVE_VERSION bump to take effect on existing activities.
        counts = derive_all(conn, full=True)
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no changes"
        print(f"Athlete profile updated. Derive done. {summary}")
        return

    if db.get_athlete(conn) is None and sys.stdin.isatty():
        raw = click.prompt(
            "Max heart rate for HR-based analysis (Enter to skip)",
            default="", show_default=False,
        )
        prompted_max_hr: int | None
        try:
            prompted_max_hr = int(raw) if raw.strip() else None
        except ValueError:
            prompted_max_hr = None
        db.upsert_athlete(conn, max_hr=prompted_max_hr, long_run_floor_miles=None)

    progress = SyncProgress(printer=_print_progress)
    progress.start()
    new_count = run_with_retry(conn, full, extra, extra_limit, extra_reserve, progress)
    progress.finish(new_count)


if __name__ == "__main__":
    main()
