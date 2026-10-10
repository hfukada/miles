import json

import pytest

from miles import db, mcp_server


def _seed() -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO activities (activity_id, name, sport_type, start_date, run_type) "
        "VALUES (1, 'Run', 'Run', '2026-01-05T08:00:00', 'easy')"
    )
    conn.commit()
    conn.close()


def _activity_count() -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    finally:
        conn.close()


def test_a_select_returns_rows():
    _seed()
    assert json.loads(mcp_server.run_sql("SELECT name FROM activities")) == [{"name": "Run"}]


@pytest.mark.parametrize(
    "query",
    [
        "WITH x AS (SELECT 1) DELETE FROM activities",
        "WITH x AS (SELECT 1) UPDATE activities SET name = 'gone'",
        "WITH x AS (SELECT 1) INSERT INTO meta (key, value) VALUES ('k', 'v')",
        "WITH x AS (SELECT 1) REPLACE INTO meta (key, value) VALUES ('k', 'v')",
        "SELECT load_extension('/nonexistent/extension')",
        "WITH x AS (SELECT 1) SELECT load_extension('/nonexistent/extension')",
        "PRAGMA query_only = OFF",
        "ATTACH DATABASE ':memory:' AS other",
        "DELETE FROM activities",
        "SELECT 1; DELETE FROM activities",
    ],
)
def test_a_query_that_writes_is_refused_and_changes_nothing(query: str):
    _seed()
    result = json.loads(mcp_server.run_sql(query))
    assert "error" in result
    assert _activity_count() == 1
    assert json.loads(mcp_server.run_sql("SELECT name FROM activities")) == [{"name": "Run"}]
