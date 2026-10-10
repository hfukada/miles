import pytest
from fastapi.testclient import TestClient

from miles import api, elevation, routes_api


def _clear_tile_env(monkeypatch):
    for name in ("MILES_TILE_URL", "MILES_TILE_ATTRIBUTION", "MILES_TILE_MAX_ZOOM"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, {"tile_url": None, "tile_attribution": None, "tile_max_zoom": None}),
        (
            {"MILES_TILE_URL": "https://tiles.example/{z}/{x}/{y}.png", "MILES_TILE_ATTRIBUTION": "Tiles by Example", "MILES_TILE_MAX_ZOOM": "16"},
            {"tile_url": "https://tiles.example/{z}/{x}/{y}.png", "tile_attribution": "Tiles by Example", "tile_max_zoom": 16},
        ),
        # An empty URL or a non-numeric zoom counts as unset.
        ({"MILES_TILE_URL": "", "MILES_TILE_MAX_ZOOM": "deep"}, {"tile_url": None, "tile_attribution": None, "tile_max_zoom": None}),
    ],
    ids=["no override", "configured layer", "empty or invalid"],
)
def test_config_serves_the_configured_tile_layer(monkeypatch, env: dict, expected: dict):
    _clear_tile_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert TestClient(api.app).get("/api/config").json() == expected


def test_redrape_reports_a_failed_tile_fetch_as_a_gateway_error(monkeypatch):
    def failing(conn, route_id):
        raise elevation.ElevationFetchError("tile server unreachable")

    monkeypatch.setattr(routes_api.routes_service, "connect", lambda: None)
    monkeypatch.setattr(routes_api.routes_service, "redrape_route", failing)
    response = TestClient(api.app).post("/api/routes/1/redrape")
    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]


def _capture_sync_history(monkeypatch) -> list[bool]:
    seen: list[bool] = []

    def fake_run_sync(conn, *, full, history, progress):
        seen.append(history)
        return 0

    monkeypatch.setattr(api.sync, "run_sync", fake_run_sync)
    monkeypatch.setattr(api, "_sync_progress", api.sync.SyncProgress())
    return seen


def test_ui_sync_skips_the_history_pass_even_when_history_is_enabled(monkeypatch):
    monkeypatch.setenv("MILES_SYNC_HISTORY", "1")
    seen = _capture_sync_history(monkeypatch)
    api._run_sync_in_background(False)
    assert seen == [False]


def test_sync_endpoint_runs_history_only_when_asked(monkeypatch):
    started: list[tuple] = []

    class FakeThread:
        def __init__(self, target, args, daemon):
            started.append(args)

        def start(self):
            pass

    monkeypatch.setattr(api.threading, "Thread", FakeThread)
    client = TestClient(api.app)

    monkeypatch.setattr(api, "_sync_progress", api.sync.SyncProgress())
    assert client.post("/api/sync").json() == {"status": "started"}
    monkeypatch.setattr(api, "_sync_progress", api.sync.SyncProgress())
    assert client.post("/api/sync?history=true").json() == {"status": "started"}
    assert started == [(False,), (True,)]


def test_a_custom_tile_layer_without_attribution_is_credited_to_openstreetmap(monkeypatch, caplog):
    _clear_tile_env(monkeypatch)
    monkeypatch.setattr(api, "_warned_tile_attribution", [])
    monkeypatch.setenv("MILES_TILE_URL", "https://tiles.example/{z}/{x}/{y}.png")
    client = TestClient(api.app)
    with caplog.at_level("WARNING", logger="miles.api"):
        body = client.get("/api/config").json()
        client.get("/api/config")
    assert "OpenStreetMap" in body["tile_attribution"]
    assert [r for r in caplog.records if "MILES_TILE_ATTRIBUTION" in r.getMessage()].__len__() == 1
