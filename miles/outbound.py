"""The one process that makes outbound calls for a database.

Rate-limit readings, per-service throttles, and job locks are per-process, so
two processes spending against one database would each assume they own the
limits. A process that downloads, imports, or spends Strava calls first takes
an exclusive lock on a file beside the database; the OS releases it on any
exit, so a crash leaves no stale claim.

The claimant is the only process that runs jobs, so any job row still marked
running was cut short and is marked interrupted.
"""

import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import click

from . import db

# Recorded on a job found running at startup; the health check does not flag these.
IMPORT_INTERRUPTED_ERROR = "import interrupted; run it again"
BACKFILL_INTERRUPTED_ERROR = "interrupted; run it again"

_held: dict[Path, TextIO] = {}


class OutboundBusyError(click.ClickException):
    """Another process already owns this database's outbound calls."""


def lock_path() -> Path:
    return db.DB_PATH.with_name(db.DB_PATH.name + ".outbound.lock")


def claim() -> None:
    """Take this database's outbound lock for the life of the process and mark
    interrupted jobs. Idempotent within a process. Raises OutboundBusyError
    when another process holds the lock."""
    path = lock_path()
    if path in _held:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(path, "a+")
    except PermissionError:
        raise OutboundBusyError(
            f"cannot open {path} to claim the database's outbound calls (permission denied); "
            "run as the user that owns the data directory"
        ) from None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.seek(0)
        holder = handle.read().strip()
        handle.close()
        owner = f"process {holder}" if holder else "another process"
        raise OutboundBusyError(
            f"{owner} already makes this database's outbound calls (a running miles-api, for one); "
            "stop it first, or use its HTTP API (its MCP endpoint is /mcp)"
        ) from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _held[path] = handle
    with db.connection() as conn:
        db.init_db(conn)
        conn.execute(
            "UPDATE areas SET status = 'error', status_error = ? WHERE status = 'importing'",
            (IMPORT_INTERRUPTED_ERROR,),
        )
        conn.execute(
            """
            UPDATE terrain_backfill_status SET status = 'error', phase = 'error', error = ?, finished_at = ?
            WHERE id = 1 AND status = 'running'
            """,
            (BACKFILL_INTERRUPTED_ERROR, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
