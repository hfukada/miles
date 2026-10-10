"""The "needs attention" check behind /api/health.

Everything is read from the database, so the answer is the same whichever
process asks and whichever process did the work: the last sync's outcome, the
persisted log of throttling and credential failures (events.py), and the
status rows of the background jobs. A handful of indexed queries; cheap
enough to poll every minute from an uptime monitor.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from typing_extensions import NotRequired, TypedDict

from . import events, outbound, sync, terrain
from .db import get_meta

SYNC_MAX_AGE_ENV = "MILES_HEALTH_SYNC_MAX_HOURS"
DEFAULT_SYNC_MAX_AGE_HOURS = 36.0
THROTTLE_WINDOW = timedelta(hours=24)
AREA_ERROR_WINDOW = timedelta(days=7)


class Health(TypedDict):
    status: str  # "ok" | "attention"
    reasons: NotRequired[list[str]]
    details: NotRequired[dict[str, object]]


def sync_max_age_hours() -> float:
    try:
        value = float(os.environ.get(SYNC_MAX_AGE_ENV, ""))
    except ValueError:
        return DEFAULT_SYNC_MAX_AGE_HOURS
    return value if value > 0 else DEFAULT_SYNC_MAX_AGE_HOURS


def _sync_reasons(conn: sqlite3.Connection, now: datetime, max_age_hours: float, details: dict[str, object]) -> list[str]:
    last_success = get_meta(conn, sync.META_LAST_SUCCESS)
    last_failure = get_meta(conn, sync.META_LAST_FAILURE)
    details["last_sync_success"] = last_success
    details["last_sync_failure"] = last_failure
    reasons: list[str] = []
    if last_failure is not None and (last_success is None or last_failure > last_success):
        error = get_meta(conn, sync.META_LAST_ERROR)
        details["last_sync_error"] = error
        reasons.append("last sync failed")
    if last_success is None:
        if last_failure is None:
            reasons.append("no sync has completed yet")
    else:
        age = now - datetime.fromisoformat(last_success)
        if age > timedelta(hours=max_age_hours):
            reasons.append(f"no successful sync in {age.total_seconds() / 3600:.0f} h (limit {max_age_hours:g} h)")
    return reasons


def _throttle_reasons(conn: sqlite3.Connection, now: datetime, details: dict[str, object]) -> list[str]:
    rows = events.recent(conn, status=429, since=now - THROTTLE_WINDOW)
    if not rows:
        return []
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["service"]] = counts.get(row["service"], 0) + 1
    details["throttled_24h"] = counts
    return [f"{service} answered 429 in the last 24 h ({n}x)" for service, n in sorted(counts.items())]


def _credential_reasons(conn: sqlite3.Connection, now: datetime, details: dict[str, object]) -> list[str]:
    rows = events.recent(conn, status=401, since=now - THROTTLE_WINDOW, service="strava")
    if not rows:
        return []
    last_success = get_meta(conn, sync.META_LAST_SUCCESS)
    # A sync that completed after the refusal means the credentials work again.
    if last_success is not None and last_success > rows[0]["at"]:
        return []
    details["strava_401_at"] = rows[0]["at"]
    return ["Strava rejected the credentials (401); re-authorize"]


def _history_work_remains(conn: sqlite3.Connection) -> bool:
    """Whether any history (streams or laps) is still waiting. A backfill error
    with nothing left to fetch is stale: the next sync has no work to retry."""
    if conn.execute("SELECT 1 FROM terrain_backfill_queue LIMIT 1").fetchone() is not None:
        return True
    return bool(sync.remaining_lap_ids(conn))


def _job_reasons(conn: sqlite3.Connection, now: datetime, details: dict[str, object]) -> list[str]:
    reasons: list[str] = []
    backfill = conn.execute("SELECT status, error FROM terrain_backfill_status WHERE id = 1").fetchone()
    if (
        backfill is not None
        and backfill["status"] == "error"
        and backfill["error"] != outbound.BACKFILL_INTERRUPTED_ERROR
        and _history_work_remains(conn)
    ):
        details["terrain_backfill_error"] = backfill["error"]
        reasons.append("terrain backfill in error")

    rebuild_error = get_meta(conn, terrain.REBUILD_ERROR_META)
    if rebuild_error is not None and not terrain.model_is_current(conn):
        details["terrain_rebuild_error"] = rebuild_error
        reasons.append("terrain model rebuild failed")

    since = (now - AREA_ERROR_WINDOW).isoformat()
    failed = conn.execute(
        "SELECT label, status_error FROM areas WHERE status = 'error' AND status_error != ? AND last_used_at >= ? "
        "ORDER BY last_used_at DESC",
        (outbound.IMPORT_INTERRUPTED_ERROR, since),
    ).fetchall()
    if failed:
        details["area_import_errors"] = {row["label"]: row["status_error"] for row in failed}
        reasons.append(f"area import failed in the last 7 days ({', '.join(row['label'] for row in failed)})")
    return reasons


def check(conn: sqlite3.Connection, *, verbose: bool = False, now: datetime | None = None) -> Health:
    now = now or datetime.now(timezone.utc)
    details: dict[str, object] = {}
    reasons = [
        *_sync_reasons(conn, now, sync_max_age_hours(), details),
        *_throttle_reasons(conn, now, details),
        *_credential_reasons(conn, now, details),
        *_job_reasons(conn, now, details),
    ]
    result: Health = {"status": "attention" if reasons else "ok"}
    if reasons:
        result["reasons"] = reasons
    if verbose:
        result["details"] = details
    return result
