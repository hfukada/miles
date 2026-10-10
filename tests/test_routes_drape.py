"""Terrain-model elevation draping for routes: the auto/file/dem
preference, the cache-only guarantee on every derive path, and the
explicit redrape that's allowed to hit the network."""

import pytest
from helpers import line_points, make_gpx, memory_db

from miles import routes as routes_service


class FakeSampler:
    """A deterministic stand-in for elevation.ElevationSampler: elevation
    rises with latitude so a draped route always has positive gain, and
    never touches a tile cache or the network."""

    def __init__(self, coverage: bool = True) -> None:
        self.coverage = coverage
        self.calls: list[list[tuple[float, float]]] = []

    def sample(self, points):
        self.calls.append(list(points))
        if not self.coverage:
            return [None for _ in points]
        return [100.0 + (lat - 40.0) * 10000.0 for lat, lng in points]


def _patch_sampler_for(monkeypatch: pytest.MonkeyPatch, sampler: FakeSampler) -> None:
    monkeypatch.setattr(routes_service.elevation, "sampler_for", lambda conn: sampler)


def _patch_ensure_tiles(monkeypatch: pytest.MonkeyPatch) -> list[list[tuple[float, float]]]:
    """Record the point sets routes_service asks the elevation cache to be
    filled for, instead of downloading."""
    ensured: list[list[tuple[float, float]]] = []
    monkeypatch.setattr(
        routes_service.elevation, "ensure_tiles", lambda conn, *, points, **kwargs: ensured.append(list(points))
    )
    return ensured


def _gpx(ele0: float | None = 100.0) -> bytes:
    return make_gpx(line_points(40, ele0=ele0))


def test_a_new_route_defaults_to_auto_and_uses_file_elevation_without_consulting_the_sampler(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    sampler = FakeSampler()
    _patch_sampler_for(monkeypatch, sampler)
    route_ids = routes_service.upload_gpx(conn, _gpx(ele0=100.0), "a.gpx")
    detail = routes_service.get_route(conn, route_ids[0])
    assert detail["elevation_source"] == "auto"
    assert detail["has_elevation"] is True
    assert detail["ele_source"] == "file"
    assert detail["elevation_pending"] is False
    assert sampler.calls == []


def test_derive_with_no_network_leaves_route_pending_and_does_not_crash():
    """No elevation_tiles cached and no monkeypatch: the real cache-only
    sampler finds nothing, and derive must not raise or hang."""
    conn = memory_db()
    route_ids = routes_service.upload_gpx(conn, _gpx(ele0=None), "flat.gpx")
    detail = routes_service.get_route(conn, route_ids[0])
    assert detail["has_elevation"] is False
    assert detail["ele_source"] is None
    assert detail["elevation_pending"] is True
    assert detail["gain_m"] is None


def test_auto_preference_drapes_from_dem_when_file_has_no_elevation(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    sampler = FakeSampler()
    _patch_sampler_for(monkeypatch, sampler)
    route_ids = routes_service.upload_gpx(conn, _gpx(ele0=None), "flat.gpx")
    detail = routes_service.get_route(conn, route_ids[0])
    assert detail["has_elevation"] is True
    assert detail["ele_source"] == "dem"
    assert detail["elevation_pending"] is False
    assert detail["gain_m"] is not None and detail["gain_m"] > 0
    assert len(sampler.calls) == 1  # one drape attempt, not one per point


def test_redrape_route_fetches_and_clears_pending(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    route_ids = routes_service.upload_gpx(conn, _gpx(ele0=None), "flat.gpx")
    assert routes_service.get_route(conn, route_ids[0])["elevation_pending"] is True

    sampler = FakeSampler()
    _patch_sampler_for(monkeypatch, sampler)
    ensured = _patch_ensure_tiles(monkeypatch)
    detail = routes_service.redrape_route(conn, route_ids[0])
    assert len(ensured) == 1 and len(ensured[0]) == 40  # the track's points, fetched before the drape
    assert detail["ele_source"] == "dem"
    assert detail["elevation_pending"] is False
    assert detail["gain_m"] is not None and detail["gain_m"] > 0


def test_redrape_route_still_pending_when_tiles_remain_uncovered(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    route_ids = routes_service.upload_gpx(conn, _gpx(ele0=None), "flat.gpx")
    sampler = FakeSampler(coverage=False)
    _patch_sampler_for(monkeypatch, sampler)
    _patch_ensure_tiles(monkeypatch)
    detail = routes_service.redrape_route(conn, route_ids[0])
    assert detail["elevation_pending"] is True
    assert detail["has_elevation"] is False


def test_file_preference_never_drapes(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    sampler = FakeSampler()
    _patch_sampler_for(monkeypatch, sampler)
    route_ids = routes_service.upload_gpx(conn, _gpx(ele0=None), "flat.gpx", name="Flat")
    sampler.calls.clear()  # the initial auto-preference upload already drapes once
    routes_service.update_route(conn, route_ids[0], elevation_source="file")
    detail = routes_service.get_route(conn, route_ids[0])
    assert detail["elevation_source"] == "file"
    assert detail["has_elevation"] is False
    assert detail["elevation_pending"] is False
    assert sampler.calls == []


def test_dem_preference_ignores_file_elevation_pends_without_coverage_drapes_when_available_and_auto_returns_to_the_file(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    route_id = routes_service.upload_gpx(conn, _gpx(ele0=100.0), "a.gpx")[0]
    before = routes_service.get_route(conn, route_id)
    assert before["ele_source"] == "file"

    routes_service.update_route(conn, route_id, elevation_source="dem")
    after = routes_service.get_route(conn, route_id)
    assert after["elevation_source"] == "dem"
    assert after["ele_source"] is None
    assert after["has_elevation"] is False
    assert after["elevation_pending"] is True
    assert after["gain_m"] is None

    assert routes_service.update_route(conn, route_id, elevation_source="auto")["ele_source"] == "file"

    _patch_sampler_for(monkeypatch, FakeSampler())
    draped = routes_service.update_route(conn, route_id, elevation_source="dem")
    assert draped["ele_source"] == "dem"
    assert draped["gain_m"] is not None and draped["gain_m"] != before["gain_m"]

    with pytest.raises(ValueError):
        routes_service.update_route(conn, route_id, elevation_source="satellite")


def test_derive_all_routes_stays_cache_only_and_pending(monkeypatch: pytest.MonkeyPatch):
    """A full rebuild (ROUTE_DERIVE_VERSION bump path) must behave exactly
    like upload: cache-only, never crashing, leaving an uncovered route
    pending."""
    conn = memory_db()
    routes_service.upload_gpx(conn, _gpx(ele0=None), "flat.gpx")
    routes_service.derive_all_routes(conn)
    routes = routes_service.list_routes(conn)
    assert len(routes) == 1
    assert routes[0]["elevation_pending"] is True
