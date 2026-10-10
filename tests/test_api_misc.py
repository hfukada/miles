import pytest
from fastapi.testclient import TestClient

from miles import api


def _clear_tile_env(monkeypatch):
    for name in ("MILES_TILE_URL", "MILES_TILE_ATTRIBUTION", "MILES_TILE_MAX_ZOOM"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "env, expected",
    [
        (
            {"MILES_TILE_URL": "https://tiles.example/{z}/{x}/{y}.png", "MILES_TILE_ATTRIBUTION": "Tiles by Example", "MILES_TILE_MAX_ZOOM": "16"},
            {"tile_url": "https://tiles.example/{z}/{x}/{y}.png", "tile_attribution": "Tiles by Example", "tile_max_zoom": 16},
        ),
        # A custom layer without a credit is credited to OpenStreetMap, whose data any tile server built on it carries.
        ({"MILES_TILE_URL": "https://tiles.example/{z}/{x}/{y}.png"}, {"tile_url": "https://tiles.example/{z}/{x}/{y}.png", "tile_attribution": api._DEFAULT_TILE_ATTRIBUTION, "tile_max_zoom": None}),
        # An empty URL or a non-numeric zoom counts as unset.
        ({"MILES_TILE_URL": "", "MILES_TILE_MAX_ZOOM": "deep"}, {"tile_url": None, "tile_attribution": None, "tile_max_zoom": None}),
    ],
    ids=["configured layer", "no credit given", "empty or invalid"],
)
def test_config_serves_the_configured_tile_layer(monkeypatch, env: dict, expected: dict):
    _clear_tile_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert TestClient(api.app).get("/api/config").json() == expected


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


