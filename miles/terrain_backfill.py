"""Resumable backfill of run streams for the terrain model.

Fetches streams for every run activity with no raw stream file yet
(streams.has_cached_streams), newest first, respecting Strava's read rate
limits, and segments every activity whose streams are on disk but which has
not been segmented (streams stored by some other path, an activity-page view
for instance) or was segmented without fetching DEM tiles. Segmenting is
never done on a request path, so this job is the only place tiles are fetched
for history. The queue table (terrain_backfill_queue) is the resume point -- a
restart just re-reads it rather than losing place -- and a status row
(terrain_backfill_status) is polled for progress the way sync.SyncProgress
is. Startable from the CLI (`miles-terrain-backfill`) or the web API
(/api/terrain/backfill).
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Collection
from datetime import date, datetime, timezone
from typing import Protocol
import click
from stravalib.exc import Fault
from typing_extensions import TypedDict

from . import db, elevation, events, outbound, strava_budget, strava_client, streams, terrain
from .errors import StravaAllowanceLowError, describe_error

_logger = logging.getLogger(__name__)


# Requests between pauses, and the pause itself -- gives an interactive
# request (an activity-detail page fetching its own streams) room to
# interleave rather than queuing behind a long unattended run.
BATCH_SIZE = 10
BATCH_PAUSE_S = 2.0

# Strava statuses that describe one activity (deleted, private, unprocessable)
# rather than the service or the credentials. Only these are held against the
# activity being fetched.
ACTIVITY_ERROR_CODES = (403, 404, 422)
# Consecutive non-activity failures (5xx, no status) tolerated before the run
# stops; the cause is the service, and every further call would fail the same.
MAX_CONSECUTIVE_SERVICE_FAILURES = 3


class BackfillAbortedError(Exception):
    """The run cannot continue for a reason that is not any one activity's fault."""


class ProgressSink(Protocol):
    def start(self, total: int) -> None: ...

    def phase(self, phase: str, *, done: int, total: int) -> None: ...


class BackfillStatus(TypedDict):
    status: str  # "idle" | "running" | "done" | "error"
    phase: str | None
    done: int | None
    total: int | None
    started_at: str | None
    finished_at: str | None
    error: str | None


class BackfillProgress:
    """In-process progress for one backfill run, polled by /api/terrain/backfill/status
    and mirrored into terrain_backfill_status so a CLI run and the API agree
    on the last-known state across restarts."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def _write(
        self,
        status: str,
        *,
        phase: str | None = None,
        done: int | None = None,
        total: int | None = None,
        error: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE terrain_backfill_status SET
                    status = ?, phase = ?, done = ?, total = ?, error = ?,
                    started_at = COALESCE(?, started_at),
                    finished_at = ?
                WHERE id = 1
                """,
                [status, phase, done, total, error, started_at, finished_at],
            )
            self._conn.commit()

    def try_start(self) -> bool:
        """Atomically switch to running unless a run is in progress. Returns
        whether this call won the race and should launch the job."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE terrain_backfill_status SET status = 'running' WHERE id = 1 AND status != 'running'"
            )
            self._conn.commit()
            return cur.rowcount > 0

    def start(self, total: int) -> None:
        self._write("running", phase="streams", done=0, total=total,
                     started_at=datetime.now(timezone.utc).isoformat(), finished_at=None, error=None)

    def phase(self, phase: str, *, done: int, total: int) -> None:
        self._write("running", phase=phase, done=done, total=total)

    def finish(self) -> None:
        self._write("done", phase="done", finished_at=datetime.now(timezone.utc).isoformat())

    def fail(self, error: str) -> None:
        self._write("error", phase="error", error=error, finished_at=datetime.now(timezone.utc).isoformat())

    def snapshot(self) -> BackfillStatus:
        row = self._conn.execute(
            "SELECT status, phase, done, total, started_at, finished_at, error FROM terrain_backfill_status WHERE id = 1"
        ).fetchone()
        if row is None:
            return {"status": "idle", "phase": None, "done": None, "total": None,
                     "started_at": None, "finished_at": None, "error": None}
        return dict(row)  # type: ignore[return-value]


def refresh_backfill_queue(conn: sqlite3.Connection) -> int:
    """Sync terrain_backfill_queue with reality: drop activities that already
    have a current stream cache (fetched some other way, e.g. an interactive
    activity-detail view), and queue any run activity that still needs one.
    Returns the queue size after refreshing."""
    placeholders = ",".join("?" * len(terrain.TERRAIN_SPORT_TYPES))
    codes = ",".join("?" * len(ACTIVITY_ERROR_CODES))
    rows = conn.execute(
        f"""
        SELECT activity_id, start_date FROM activities
        WHERE sport_type IN ({placeholders})
          AND activity_id NOT IN (SELECT activity_id FROM terrain_backfill_failures WHERE status_code IN ({codes}))
        """,
        [*terrain.TERRAIN_SPORT_TYPES, *ACTIVITY_ERROR_CODES],
    ).fetchall()

    needed_ids: list[int] = []
    start_dates: dict[int, str | None] = {}
    for row in rows:
        activity_id = row["activity_id"]
        if not streams.has_cached_streams(activity_id):
            needed_ids.append(activity_id)
            start_dates[activity_id] = row["start_date"]

    needed_set = set(needed_ids)
    queued_ids = {r["activity_id"] for r in conn.execute("SELECT activity_id FROM terrain_backfill_queue").fetchall()}

    to_drop = queued_ids - needed_set
    if to_drop:
        conn.executemany("DELETE FROM terrain_backfill_queue WHERE activity_id = ?", [(i,) for i in to_drop])

    to_add = needed_set - queued_ids
    if to_add:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT OR IGNORE INTO terrain_backfill_queue (activity_id, start_date, queued_at) VALUES (?, ?, ?)",
            [(i, start_dates[i], now) for i in to_add],
        )
    conn.commit()
    return len(needed_set)


def run_backfill(
    conn: sqlite3.Connection,
    progress: ProgressSink,
    *,
    limit: int | None = None,
    gate: strava_budget.Gate | None = None,
    only_ids: Collection[int] | None = None,
) -> int:
    """Segment activities whose streams are already cached, then fetch,
    cache, and segment streams for queued activities, newest first. Stops
    when the gate refuses a request (the default gate keeps
    strava_budget.default_reserve() daily calls). only_ids restricts the run
    to those activities and skips the cached-but-unsegmented sweep, for a
    caller that wants just what a sync found.
    Resumable: the queue and each activity's raw stream file are the only
    state, both persisted before this call returns control (a crash
    mid-batch just leaves the next call to pick up where the queue says
    to). An activity that fails for its own reasons (a 404 on a deleted
    activity, unparseable streams) is recorded in terrain_backfill_failures
    and skipped rather than blocking the queue. Returns the number fetched
    this call.
    """
    if gate is None:
        gate = strava_budget.Gate()
    terrain.ensure_terrain_derived(conn)
    total_remaining = refresh_backfill_queue(conn)
    cached_unsegmented = terrain.activities_to_segment(conn) if only_ids is None else []
    if total_remaining == 0 and not cached_unsegmented:
        return 0

    todo_ids = [
        r["activity_id"]
        for r in conn.execute("SELECT activity_id FROM terrain_backfill_queue ORDER BY start_date DESC").fetchall()
        if only_ids is None or r["activity_id"] in only_ids
    ]
    if limit is not None:
        todo_ids = todo_ids[:limit]
    batch_total = len(todo_ids)

    progress.start(batch_total)
    fetched = 0
    segmented_dates: list[date] = []
    service_failures = 0
    # This job (not a request path) is where tile fetching for terrain-model
    # draping is allowed to happen. A DEM fetch failure propagates and ends
    # the run (the streams already stored stay put and are segmented on the
    # next run) rather than spending Strava calls on activities that cannot
    # be segmented right now.
    def segment(activity_id: int) -> None:
        if terrain.segment_cached_activity(conn, activity_id, fetch_dem=True):
            row = conn.execute("SELECT start_date FROM terrain_segment_dates WHERE activity_id = ?", [activity_id]).fetchone()
            if row is not None:
                segmented_dates.append(date.fromisoformat(row["start_date"]))

    def try_segment(activity_id: int, raw: object = None) -> bool:
        """Store raw streams just fetched (if any) and segment one activity;
        False after recording why that could not be done."""
        try:
            if raw is not None:
                streams.cache_streams(activity_id, raw)  # type: ignore[arg-type]
            segment(activity_id)
        except elevation.ElevationFetchError:
            raise
        except Exception as e:
            _logger.warning("terrain segmentation failed for activity %s", activity_id, exc_info=True)
            terrain.record_segmentation_failure(conn, activity_id, e)
            return False
        return True

    for i, activity_id in enumerate(cached_unsegmented, 1):
        try_segment(activity_id)
        progress.phase("segments", done=i, total=len(cached_unsegmented))

    progress.phase("streams", done=0, total=batch_total)
    try:
        for position, activity_id in enumerate(todo_ids):
            if position and position % BATCH_SIZE == 0:
                time.sleep(BATCH_PAUSE_S)
            raw = None
            while raw is None:
                try:
                    raw = gate.call("streams", lambda: strava_client.get_activity_streams(activity_id))
                except Fault as e:
                    code = strava_client.fault_status(e)
                    if code == 401:
                        raise BackfillAbortedError("Strava rejected the credentials (401); re-authorize and run again") from e
                    if code in ACTIVITY_ERROR_CODES:
                        _logger.warning("stream fetch failed for activity %s: %s", activity_id, e)
                        terrain.record_failure(conn, activity_id, code, describe_error(e, None))
                        break
                    service_failures += 1
                    if service_failures >= MAX_CONSECUTIVE_SERVICE_FAILURES:
                        raise BackfillAbortedError(
                            f"Strava failed {service_failures} requests in a row (last status {code})"
                        ) from e
                    _logger.warning("stream fetch hit a service error (status %s): %s", code, e)
                    time.sleep(BATCH_PAUSE_S)
            if raw is None:
                continue
            service_failures = 0
            if not try_segment(activity_id, raw):
                continue
            conn.execute("DELETE FROM terrain_backfill_queue WHERE activity_id = ?", [activity_id])
            conn.commit()
            fetched += 1
            progress.phase("streams", done=fetched, total=batch_total)
    except StravaAllowanceLowError as e:
        _logger.info("stream fetch stopped: %s", e)

    if segmented_dates:
        terrain.backfill_snapshots(conn, terrain.ROLLING_WINDOW_DAYS, since=min(segmented_dates))
    return fetched


@click.command()
@click.option("--limit", type=int, default=None, help="Max activities to fetch this invocation.")
@click.option("--short-reserve", type=int, default=strava_budget.SHORT_WINDOW_RESERVE, help="15-min-window calls to leave unused.")
@click.option(
    "--reserve", "--daily-reserve", "daily_reserve", type=int, default=None,
    help=f"Daily Strava calls to leave unused (default: ${strava_budget.RESERVE_ENV} or {strava_budget.DEFAULT_RESERVE}).",
)
def main(limit: int | None, short_reserve: int, daily_reserve: int | None) -> None:
    outbound.claim()
    events.install()
    conn = db.connect()
    gate = strava_budget.Gate(daily_reserve, short_reserve)
    progress = BackfillProgress(conn)
    if not progress.try_start():
        raise click.ClickException("A terrain backfill is already running.")
    try:
        fetched = run_backfill(conn, progress, limit=limit, gate=gate)
        progress.finish()
        print(f"Backfilled streams for {fetched} activities.")
    except Exception as e:
        progress.fail(describe_error(e))
        raise


if __name__ == "__main__":
    main()
