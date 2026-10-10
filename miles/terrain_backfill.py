"""Resumable backfill of run streams for the terrain model, run by sync.

Fetches streams for every run activity with no raw stream file yet, newest
first, within Strava's rate limits, and segments every activity whose streams
are on disk but not yet segmented (or segmented without elevation tiles).
This is the only place tiles are fetched for history. The queue table
(terrain_backfill_queue) is the resume point.
"""

import logging
import sqlite3
import time
from collections.abc import Callable, Collection, Mapping
from datetime import date, datetime, timezone

from stravalib.exc import Fault

from . import strava_budget, strava_client, streams, terrain
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


# Called with (phase, done, total) as the run moves through "segments" and "streams".
PhaseReporter = Callable[[str, int, int], None]


def refresh_backfill_queue(conn: sqlite3.Connection) -> int:
    """Sync terrain_backfill_queue with reality: drop activities that already
    have a current stream cache (fetched some other way, e.g. an interactive
    activity-detail view), and queue any run activity that still needs one.
    Returns the queue size after refreshing."""
    sport_sql, sport_params = terrain.sport_filter()
    codes = ",".join("?" * len(ACTIVITY_ERROR_CODES))
    rows = conn.execute(
        f"""
        SELECT activity_id, start_date FROM activities
        WHERE {sport_sql}
          AND activity_id NOT IN (SELECT activity_id FROM terrain_backfill_failures WHERE status_code IN ({codes}))
        """,
        [*sport_params, *ACTIVITY_ERROR_CODES],
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
    report: PhaseReporter,
    *,
    limit: int | None = None,
    gate: strava_budget.Gate | None = None,
    only_ids: Collection[int] | None = None,
) -> int:
    """Segment activities whose streams are already cached, then fetch,
    cache, and segment streams for queued activities, newest first, until
    the gate refuses a request. only_ids restricts the run to those
    activities and skips the cached-but-unsegmented sweep. An activity that
    fails for its own reasons (a 404, unparseable streams) is recorded in
    terrain_backfill_failures and skipped. The queue and each raw stream
    file are persisted as the run goes, so a crash loses nothing. Returns the
    number fetched.
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

    fetched = 0
    segmented_dates: list[date] = []
    service_failures = 0
    # This job (not a request path) is where tile fetching for terrain-model
    # draping is allowed to happen. A DEM fetch failure propagates and ends
    # the run (the streams already stored stay put and are segmented on the
    # next run) rather than spending Strava calls on activities that cannot
    # be segmented right now.
    def segment(activity_id: int, raw: Mapping[str, object] | None = None) -> bool:
        """Store raw streams just fetched (if any) and segment one activity;
        False after recording why that could not be done."""
        if terrain.segment_or_record_failure(conn, activity_id, raw, fetch_dem=True) is None:
            return False
        row = conn.execute("SELECT start_date FROM terrain_segment_dates WHERE activity_id = ?", [activity_id]).fetchone()
        if row is not None:
            segmented_dates.append(date.fromisoformat(row["start_date"]))
        return True

    for i, activity_id in enumerate(cached_unsegmented, 1):
        segment(activity_id)
        report("segments", i, len(cached_unsegmented))

    report("streams", 0, batch_total)
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
            if not segment(activity_id, raw):
                continue
            conn.execute("DELETE FROM terrain_backfill_queue WHERE activity_id = ?", [activity_id])
            conn.commit()
            fetched += 1
            report("streams", fetched, batch_total)
    except StravaAllowanceLowError as e:
        _logger.info("stream fetch stopped: %s", e)

    if segmented_dates:
        terrain.backfill_snapshots(conn, terrain.ROLLING_WINDOW_DAYS, since=min(segmented_dates))
    return fetched
