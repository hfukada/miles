import sqlite3

import pytest
from click.testing import CliRunner

from miles import area_refresh, areas, extracts, geocode


def test_an_extract_registry_from_before_conditional_downloads_still_reads():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE map_extracts (region TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, "
        "fetched_at TEXT NOT NULL, bounds_south REAL, bounds_west REAL, bounds_north REAL, bounds_east REAL)"
    )
    conn.execute(
        "INSERT INTO map_extracts VALUES ('r/x', '/p', 's', '2024-01-01T00:00:00+00:00', 0, 0, 1, 1)"
    )
    extracts.init_extract_schema(conn)

    record = extracts.get_extract(conn, "r/x")

    assert record is not None
    assert record["checked_at"] == record["data_as_of"] == "2024-01-01T00:00:00+00:00"
    assert record["etag"] is None


def test_refresh_area_runs_the_import_with_refresh_and_reports_it(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    conn.execute(
        "INSERT INTO areas (label, center_lat, center_lng, radius_km, created_at, last_used_at, status) "
        "VALUES ('home', 1, 2, 3, 'n', 'n', 'ready')"
    )
    conn.commit()
    jobs_run: list[tuple[int, bool]] = []
    monkeypatch.setattr(areas, "_run_import_job", lambda area_id, refresh=False, force_extract=False: jobs_run.append((area_id, refresh)))

    status = areas.refresh_area(conn, "home")

    assert jobs_run == [(1, True)]
    assert status["label"] == "home"


def test_refresh_area_rejects_an_unknown_label(conn: sqlite3.Connection):
    with pytest.raises(areas.AreaNotFoundError):
        areas.refresh_area(conn, "nowhere")


def test_cli_geocode_refresh_updates_the_cache(monkeypatch: pytest.MonkeyPatch):
    asked: list[bool] = []
    found = {"lat": 1.5, "lng": 2.5, "display_name": "Somewhere, Region", "attribution": geocode.ATTRIBUTION}
    monkeypatch.setattr(geocode, "lookup", lambda c, q, refresh=False: asked.append(refresh) or found)

    result = CliRunner().invoke(area_refresh.main, ["--geocode", "Somewhere"])

    assert result.exit_code == 0, result.output
    assert asked == [True]
    assert "1.50000, 2.50000" in result.output


def test_cli_needs_something_to_refresh():
    result = CliRunner().invoke(area_refresh.main, [])
    assert result.exit_code != 0

