import sqlite3
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import FlatSampler, ImmediateThread, write_pbf

from miles import area_import, areas_api, db


def _write_sample_pbf(path: Path) -> None:
    write_pbf(path, nodes=[(1, 0.010, 0.010, {}), (2, 0.020, 0.020, {})], ways=[(100, [1, 2], {"highway": "path"})])


@pytest.fixture()
def app_conn(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setattr(areas_api.areas_service.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(areas_api.areas_service.elevation, "ensure_tiles", lambda conn, **kwargs: None)
    monkeypatch.setattr(
        areas_api.areas_service.elevation, "sampler_for", lambda conn, provider=None, **kwargs: FlatSampler()
    )
    return db.connect()


class _RequestsAdapter:
    """Enough of requests.Response's surface for area_import.main to check,
    backed by a TestClient response instead of a real HTTP round trip."""

    def __init__(self, response):
        self._response = response

    @property
    def ok(self) -> bool:
        return self._response.is_success

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def text(self) -> str:
        return self._response.text

    def json(self):
        return self._response.json()


def test_import_tiles_endpoint_creates_area_and_builds_network(app_conn: sqlite3.Connection):
    client = TestClient(_make_app())
    resp = client.post(
        "/api/areas/import-tiles",
        json={
            "label": "endpoint-test",
            "center_lat": 0.015, "center_lng": 0.015, "radius_km": 1.0,
            "tiles": [{
                "gx": 0, "gy": 0,
                "elements": [{
                    "type": "way", "id": 100, "tags": {"highway": "path"}, "nodes": [1, 2],
                    "geometry": [{"lat": 0.010, "lon": 0.010}, {"lat": 0.020, "lon": 0.020}],
                }],
            }],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["label"] == "endpoint-test"

    detail = areas_api.areas_service.get_area(app_conn, body["area_id"])
    assert detail["tile_count"] == 1


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(areas_api.router)
    return app


def test_cli_uploads_cut_tiles_through_the_endpoint(
    app_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    pbf = tmp_path / "sample.osm.pbf"
    _write_sample_pbf(pbf)
    client = TestClient(_make_app())

    def fake_post(url, json=None, timeout=None):
        return _RequestsAdapter(client.post("/api/areas/import-tiles", json=json))

    monkeypatch.setattr(area_import.requests, "post", fake_post)

    runner = CliRunner()
    result = runner.invoke(
        area_import.main,
        ["--pbf", str(pbf), "--center", "0.015,0.015", "--radius-km", "1", "--label", "cli-area", "--url", "http://test"],
    )
    assert result.exit_code == 0, result.output
    assert "cli-area" in result.output
    assert "tiles uploaded" in result.output

    areas = areas_api.areas_service.list_areas(app_conn)
    assert any(a["label"] == "cli-area" for a in areas)


def test_cli_requires_center_and_radius_or_bbox(tmp_path: Path):
    pbf = tmp_path / "sample.osm.pbf"
    _write_sample_pbf(pbf)
    runner = CliRunner()
    result = runner.invoke(area_import.main, ["--pbf", str(pbf)])
    assert result.exit_code != 0


def test_resolve_bbox_and_center_from_center_radius():
    bbox, lat, lng, radius = area_import._resolve_bbox_and_center("10.0,20.0", 5.0, None)
    assert (lat, lng, radius) == (10.0, 20.0, 5.0)
    assert bbox[0] < 10.0 < bbox[2]


def test_resolve_bbox_and_center_from_bbox():
    bbox, lat, lng, radius = area_import._resolve_bbox_and_center(None, None, "0.0,0.0,1.0,1.0")
    assert bbox == (0.0, 0.0, 1.0, 1.0)
    assert lat == pytest.approx(0.5)
    assert lng == pytest.approx(0.5)
    assert radius > 0


def test_resolve_bbox_and_center_rejects_mixing_modes():
    with pytest.raises(click.UsageError):
        area_import._resolve_bbox_and_center("1,1", 5.0, "0,0,1,1")
