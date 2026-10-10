"""Durable outbound-request accounting.

CLI runs and the API server write to the same database, so requests are
recorded there rather than in process memory:

- `outbound_requests`: per-day totals by service, host, endpoint kind, and
  status (HTTP code, or "error" for a connection failure); pruned after
  RETENTION_DAYS.
- `outbound_events`: a timestamped row for each request that ended on a status
  the health check must place in time (429 on the last attempt, Strava 401);
  pruned after EVENT_RETENTION_DAYS.
- a `meta` key holding the Strava allowance last seen.

Recording is opt-in: outbound.claim() calls install(), which hooks http_client
and strava_client, so exactly the processes that make outbound calls record.
"""

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import db, http_client, strava_client

_logger = logging.getLogger(__name__)

# Statuses worth a timestamped row: slow down, and refused credentials.
EVENT_STATUSES = ("429", "401")
EVENT_RETENTION_DAYS = 7
LEDGER_BUSY_TIMEOUT_MS = 1500
RETENTION_DAYS = 400
STRAVA_RATE_META = "strava_rate_remaining"

_last_pruned_day: str | None = None


def init_event_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outbound_events (
            event_id INTEGER PRIMARY KEY,
            at       TEXT NOT NULL,
            service  TEXT NOT NULL,
            status   INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_events_at ON outbound_events(at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS outbound_requests (
            day     TEXT NOT NULL,
            service TEXT NOT NULL,
            host    TEXT NOT NULL,
            kind    TEXT NOT NULL DEFAULT '',
            status  TEXT NOT NULL,
            count   INTEGER NOT NULL,
            PRIMARY KEY (day, service, host, kind, status)
        )
    """)
    conn.commit()


def _prune(conn: sqlite3.Connection, today: str, now: datetime) -> None:
    """Drop expired rows, at most once per day per process."""
    global _last_pruned_day
    if _last_pruned_day == today:
        return
    _last_pruned_day = today
    conn.execute(
        "DELETE FROM outbound_requests WHERE day < ?", ((now - timedelta(days=RETENTION_DAYS)).date().isoformat(),)
    )
    conn.execute("DELETE FROM outbound_events WHERE at < ?", ((now - timedelta(days=EVENT_RETENTION_DAYS)).isoformat(),))


@contextmanager
def _ledger_connection() -> Iterator[sqlite3.Connection]:
    """A connection for one ledger write. The write waits only briefly for a
    lock: recording a request must not hold up, or fail, the request itself,
    and a missed count costs less than a stalled caller."""
    with db.connection() as conn:
        conn.execute(f"PRAGMA busy_timeout = {LEDGER_BUSY_TIMEOUT_MS}")
        yield conn


def record_request(service: str, host: str, status: str, kind: str = "") -> None:
    """Count one outbound request. Never raises: a locked or missing database
    must not turn a successful request into a failure."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    try:
        with _ledger_connection() as conn:
            conn.execute(
                """
                INSERT INTO outbound_requests (day, service, host, kind, status, count) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(day, service, host, kind, status) DO UPDATE SET count = count + 1
                """,
                (today, service, host, kind, status),
            )
            _prune(conn, today, now)
            conn.commit()
    except Exception:
        _logger.warning("could not record outbound request %s %s %s", service, host, status, exc_info=True)


def record_outcome(service: str, status: int) -> None:
    """Leave a timestamped row for a request that ended on a status worth
    placing in time. Only the final answer counts: a 429 that a retry then got
    past is a request that succeeded. Never raises, like record_request."""
    if str(status) not in EVENT_STATUSES:
        return
    now = datetime.now(timezone.utc)
    try:
        with _ledger_connection() as conn:
            conn.execute(
                "INSERT INTO outbound_events (at, service, status) VALUES (?, ?, ?)",
                (now.isoformat(), service, status),
            )
            conn.commit()
    except Exception:
        _logger.warning("could not record outbound outcome %s %s", service, status, exc_info=True)


def record_strava_rate(daily_remaining: int, short_remaining: int) -> None:
    """Remember the allowance the latest Strava response reported, for /metrics."""
    value = json.dumps({
        "daily": daily_remaining,
        "short": short_remaining,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        with _ledger_connection() as conn:
            db.set_meta(conn, STRAVA_RATE_META, value)
            conn.commit()
    except Exception:
        _logger.warning("could not record the Strava allowance", exc_info=True)


def install() -> None:
    """Route this process's outbound requests into the database."""
    http_client.request_sink = record_request
    http_client.outcome_sink = record_outcome
    strava_client.rate_sink = record_strava_rate


def recent(conn: sqlite3.Connection, *, status: int, since: datetime, service: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT at, service, status FROM outbound_events WHERE status = ? AND at >= ?"
    params: list[object] = [status, since.isoformat()]
    if service is not None:
        sql += " AND service = ?"
        params.append(service)
    return conn.execute(sql + " ORDER BY at DESC", params).fetchall()


def daily_counts(conn: sqlite3.Connection, days: int = 30) -> list[dict[str, object]]:
    """Per-day request counts for the last `days` days (today included),
    newest first, one row per service, host, endpoint kind, and status."""
    since = (datetime.now(timezone.utc) - timedelta(days=max(days, 1) - 1)).date().isoformat()
    rows = conn.execute(
        """
        SELECT day, service, host, kind, status, count FROM outbound_requests
        WHERE day >= ? ORDER BY day DESC, service, host, kind, status
        """,
        (since,),
    ).fetchall()
    return [dict(row) for row in rows]


def totals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All-time counts (within retention) per service, host, kind, and status."""
    return conn.execute(
        """
        SELECT service, host, kind, status, SUM(count) AS count FROM outbound_requests
        GROUP BY service, host, kind, status ORDER BY service, host, kind, status
        """
    ).fetchall()
