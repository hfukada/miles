import fcntl
import os
import sqlite3

import pytest
from click.testing import CliRunner

from miles import auth, db, outbound, terrain_backfill


def _insert_importing_area(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO areas (label, center_lat, center_lng, radius_km, pinned, status, created_at, last_used_at) "
        "VALUES ('a', 1, 1, 1, 0, 'importing', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()


def test_claim_marks_jobs_left_running_as_interrupted():
    conn = db.connect()
    assert terrain_backfill.BackfillProgress(conn).try_start()
    _insert_importing_area(conn)

    outbound.claim()

    assert terrain_backfill.BackfillProgress(conn).snapshot()["status"] == "error"
    assert conn.execute("SELECT status, status_error FROM areas").fetchone()["status_error"] == outbound.IMPORT_INTERRUPTED_ERROR
    assert terrain_backfill.BackfillProgress(conn).try_start() is True


def test_a_second_process_is_refused_while_the_first_holds_the_claim():
    db.connect().close()
    other = open(outbound.lock_path(), "a+")
    fcntl.flock(other.fileno(), fcntl.LOCK_EX)
    try:
        with pytest.raises(outbound.OutboundBusyError, match="outbound calls"):
            outbound.claim()
        result = CliRunner().invoke(auth.main)  # the CLI entry points refuse too
    finally:
        other.close()

    assert result.exit_code != 0
    assert "outbound calls" in result.output

    outbound.claim()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
def test_an_unwritable_lock_file_is_reported_by_name():
    outbound.lock_path().parent.mkdir(parents=True, exist_ok=True)
    outbound.lock_path().touch(mode=0o400)

    with pytest.raises(outbound.OutboundBusyError, match="permission denied"):
        outbound.claim()


