import gzip
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest
from helpers import ImmediateThread, fake_extract_record, wait_until

from miles import areas as areas_service
from miles import db, elevation, extracts, geocode, network, tilecache, tiles

LAT, LNG = 5.0, 10.0

_REAL_THREAD = threading.Thread
_REAL_SAMPLER_FOR = elevation.sampler_for


class _FakeSampler:
    def __init__(self, conn=None, provider=None):
        self.calls: list = []

    def sample(self, points):
        self.calls.append(list(points))
        return [100.0 for _ in points]


@pytest.fixture
def conn():
    """The default (file-backed) database: the import job opens its own
    connection to it, as it does in production."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _synchronous_import(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(areas_service.threading, "Thread", ImmediateThread)


@pytest.fixture(autouse=True)
def _no_dem_download(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(elevation, "ensure_tiles", lambda conn, **kwargs: None)
    monkeypatch.setattr(elevation, "sampler_for", lambda conn, provider=None, **kwargs: _FakeSampler())


def _fake_fetch_and_cache_tiles(calls: list) -> callable:
    def fake(conn, tile_ids):
        for gx, gy in tile_ids:
            calls.append((gx, gy))
            south, west, north, east = tiles.tile_bounds(gx, gy)
            payload = json.dumps({"elements": [
                {"type": "way", "id": gx * 1000 + gy},
                {"type": "node", "id": 1, "lat": south, "lon": west},
                {"type": "node", "id": 2, "lat": north, "lon": east},
            ]}).encode()
            conn.execute(
                """
                INSERT OR REPLACE INTO map_tiles (gx, gy, query_version, fetched_at, sha256, data, source)
                VALUES (?, ?, 1, ?, 'deadbeef', ?, 'extract:test/region')
                """,
                (gx, gy, datetime.now(timezone.utc).isoformat(), gzip.compress(payload)),
            )
        conn.commit()

    return fake


def _use_tile_source(monkeypatch: pytest.MonkeyPatch, fetch=None) -> list:
    """Route the import's tile phase through fetch(conn, tile_ids), which fills
    map_tiles the way an extract cut would. Returns the tiles it was asked for
    when `fetch` is left as the default."""
    calls: list = []
    fetch = fetch or _fake_fetch_and_cache_tiles(calls)
    record = fake_extract_record()
    monkeypatch.setattr(extracts, "select_extracts", lambda *a, **kw: [record])
    monkeypatch.setattr(extracts, "tiles_cut_from", lambda *a: False)

    def cut(conn, extract, bbox):
        tile_ids = tiles.tiles_in_bbox(*bbox)
        fetch(conn, tile_ids)
        return tile_ids

    monkeypatch.setattr(extracts, "cut_area", cut)
    return calls


def _insert_area(
    conn: sqlite3.Connection, label: str, *, status: str = "ready", pinned: int = 0, region: str | None = None, used_days_ago: float = 0
) -> int:
    created = (datetime.now(timezone.utc) - timedelta(days=used_days_ago)).isoformat()
    cur = conn.execute(
        "INSERT INTO areas (label, center_lat, center_lng, radius_km, pinned, status, region, created_at, last_used_at) "
        "VALUES (?, ?, ?, 2.0, ?, ?, ?, ?, ?)",
        (label, LAT, LNG, pinned, status, region, created, created),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


def test_import_area_by_lat_lng_and_radius(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    tile_calls = _use_tile_source(monkeypatch)

    status = areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=2.0, label="test-area")
    assert status["status"] == "ready"
    assert status["label"] == "test-area"

    detail = areas_service.get_area(conn, status["area_id"])
    assert (detail["center_lat"], detail["center_lng"], detail["radius_km"]) == (LAT, LNG, 2.0)
    assert detail["tile_count"] == len(tile_calls) > 0
    assert detail["pinned"] is False

    payloads = list(areas_service.area_tile_payloads(conn, status["area_id"]))
    assert len(payloads) == len(tile_calls)
    assert all("elements" in p for p in payloads)


def test_import_area_by_place_geocodes(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _use_tile_source(monkeypatch)
    found = {"lat": 12.5, "lng": 34.5, "display_name": "Somewhere, Region", "attribution": geocode.ATTRIBUTION}
    monkeypatch.setattr(geocode, "lookup", lambda conn, query, **kwargs: found)

    status = areas_service.import_area(conn, place="Somewhere", radius_km=1.0, label="geocoded")
    detail = areas_service.get_area(conn, status["area_id"])
    assert (detail["center_lat"], detail["center_lng"]) == (12.5, 34.5)
    assert status["geocode"] == found
    assert "geocode" not in areas_service.import_area(conn, lat=1.0, lng=1.0, radius_km=1.0, label="by-coordinates")


def test_import_area_drive_minutes_heuristic():
    assert areas_service._resolve_radius_km(None, 10.0) == 10.0
    assert areas_service._resolve_radius_km(None, 500.0) == areas_service.MAX_RADIUS_KM == 60.0
    assert areas_service._resolve_radius_km(5.0, 30.0) == 5.0  # explicit radius wins


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"lat": 1.0, "lng": 1.0, "radius_km": 0.0, "label": "far"}, "radius_km"),
        ({"lat": 1.0, "lng": 1.0, "radius_km": -1.0, "label": "far"}, "radius_km"),
        ({"lat": 1.0, "lng": 1.0, "radius_km": 60.5, "label": "far"}, "radius_km"),
        ({"radius_km": 1.0, "label": "no-center"}, "place, or lat and lng"),
        ({"lat": 1.0, "lng": 1.0, "label": "no-size"}, "radius_km or drive_minutes"),
        ({"lat": 1.0, "lng": 1.0, "radius_km": 1.0, "label": ""}, "needs a label"),
    ],
    ids=["zero radius", "negative radius", "radius over the cap", "no center", "no size", "no label"],
)
def test_an_import_missing_its_center_size_or_label_or_beyond_the_radius_cap_is_refused(conn: sqlite3.Connection, arguments: dict, message: str):
    with pytest.raises(ValueError, match=message):
        areas_service.import_area(conn, **arguments)


def test_a_region_the_index_does_not_list_is_refused_up_front(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(areas_service.geofabrik, "cached_regions", lambda conn: [])
    with pytest.raises(ValueError, match="unknown Geofabrik region"):
        areas_service.import_area(conn, lat=1.0, lng=1.0, radius_km=1.0, label="x", region="nope/nowhere")


def test_the_estimate_names_the_pending_region_from_the_cached_index_without_the_network(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    bbox = tiles.bbox_for_center_radius(LAT, LNG, 2.0)
    assert areas_service._estimate_import(conn, bbox, None)["source"] == "pending-extract"
    monkeypatch.setattr(areas_service.geofabrik, "choose_cached_regions", lambda conn, bbox: ["example/region-a", "example/region-b"])
    estimate = areas_service._estimate_import(conn, bbox, None)
    assert estimate["source"] == "pending-extract:example/region-a,example/region-b"
    assert estimate["estimated_seconds"] is None and "query_count" not in estimate
    assert areas_service._estimate_import(conn, bbox, "example/region-c")["source"] == "pending-extract:example/region-c"


def test_import_area_reuses_existing_label(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _use_tile_source(monkeypatch)

    first = areas_service.import_area(conn, lat=1.0, lng=1.0, radius_km=1.0, label="home")
    second = areas_service.import_area(conn, lat=2.0, lng=2.0, radius_km=1.0, label="home")

    assert first["area_id"] == second["area_id"]
    assert len(areas_service.list_areas(conn)) == 1
    assert areas_service.get_area(conn, second["area_id"])["center_lat"] == 2.0  # re-import updates the center


def test_a_reimport_keeps_pinned_and_region_unless_told_otherwise(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _use_tile_source(monkeypatch)
    monkeypatch.setattr(extracts, "select_extracts", lambda *a, **kw: [])
    area = {"lat": 1.0, "lng": 1.0, "radius_km": 1.0, "label": "home"}

    first = areas_service.import_area(conn, **area, pinned=True, region=["example/region-a", "example/region-b"])
    detail = areas_service.get_area(conn, first["area_id"])
    assert detail["region"] == "example/region-a,example/region-b"  # several regions share one comma-separated value

    areas_service.import_area(conn, **area)
    detail = areas_service.get_area(conn, first["area_id"])
    assert (detail["pinned"], detail["region"]) == (True, "example/region-a,example/region-b")

    areas_service.import_area(conn, **area, pinned=False)
    assert areas_service.get_area(conn, first["area_id"])["pinned"] is False


def test_try_start_import_is_atomic_against_a_running_import(conn: sqlite3.Connection):
    area_id = _insert_area(conn, "busy", status="importing")
    assert areas_service._try_start_import(conn, area_id) is False


def test_pin_and_unpin_area(conn: sqlite3.Connection):
    area_id = _insert_area(conn, "home")

    assert areas_service.pin_area(conn, area_id)["pinned"] is True
    assert areas_service.unpin_area(conn, area_id)["pinned"] is False


def test_unknown_area_ids_raise(conn: sqlite3.Connection):
    for function in (areas_service.get_area, areas_service.get_area_status, areas_service.pin_area):
        with pytest.raises(areas_service.AreaNotFoundError):
            function(conn, 999)


def test_list_areas_orders_by_last_used_desc(conn: sqlite3.Connection):
    _insert_area(conn, "older", used_days_ago=5)
    _insert_area(conn, "newer")
    assert [a["label"] for a in areas_service.list_areas(conn)] == ["newer", "older"]


def test_gc_removes_stale_unpinned_areas_their_networks_and_orphaned_tiles(conn: sqlite3.Connection):
    stale_id = _insert_area(conn, "stale", used_days_ago=areas_service.UNPINNED_GC_DAYS + 1)
    kept_id = _insert_area(conn, "kept", pinned=1, used_days_ago=areas_service.UNPINNED_GC_DAYS + 1)  # pinned: exempt however old
    fresh = datetime.now(timezone.utc).isoformat()
    for gx, gy, sha, owner in ((0, 0, "x", stale_id), (1, 1, "y", kept_id)):
        conn.execute(
            "INSERT INTO map_tiles (gx, gy, query_version, fetched_at, sha256, data) VALUES (?, ?, 1, ?, ?, ?)",
            (gx, gy, fresh, sha, gzip.compress(b"{}")),
        )
        conn.execute("INSERT INTO area_tiles (area_id, gx, gy) VALUES (?, ?, ?)", (owner, gx, gy))
    conn.execute(
        "INSERT INTO net_graphs (area_id, derive_version, built_at, node_count, edge_count, excluded_way_count) "
        "VALUES (?, 1, 'now', 2, 1, 0)",
        (stale_id,),
    )
    for node_id, osm_id in ((1, 10), (2, 11)):
        conn.execute(
            "INSERT INTO net_nodes (node_id, area_id, osm_node_id, kind, lat, lng) VALUES (?, ?, ?, 'junction', 1, 1)",
            (node_id, stale_id, osm_id),
        )
    conn.execute(
        "INSERT INTO net_edges (area_id, way_id, highway, surface_class, from_node_id, to_node_id, length_m, "
        "has_elevation, technicality_json, points_blob) VALUES (?, 5, 'path', 'dirt', 1, 2, 10, 0, '{}', x'00')",
        (stale_id,),
    )
    conn.commit()

    areas_service.gc_areas_and_tiles(conn)

    assert {a["area_id"] for a in areas_service.list_areas(conn)} == {kept_id}
    assert {(r["gx"], r["gy"]) for r in conn.execute("SELECT gx, gy FROM map_tiles")} == {(1, 1)}
    for table in ("net_graphs", "net_nodes", "net_edges"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_an_import_reports_queued_until_it_holds_the_import_lock(conn: sqlite3.Connection):
    area_id = _insert_area(conn, "q", status="importing")
    phases: list = []

    def phase() -> str:
        return conn.execute("SELECT status_phase FROM areas WHERE area_id = ?", (area_id,)).fetchone()[0]

    def body(c, aid):
        phases.append(c.execute("SELECT status_phase FROM areas WHERE area_id = ?", (aid,)).fetchone()[0])

    with areas_service._import_lock:
        runner = _REAL_THREAD(target=areas_service._run_job, args=(area_id, body))
        runner.start()
        assert wait_until(lambda: phase() == "queued")
        assert runner.is_alive()
    runner.join(timeout=5)
    assert phases == ["queued"]


def test_work_before_the_lock_does_not_hold_other_imports_up(conn: sqlite3.Connection):
    area_id = _insert_area(conn, "q", status="importing")
    entered = threading.Event()
    release = threading.Event()

    def slow_prepare(c, aid):
        entered.set()
        release.wait(5)

    runner = _REAL_THREAD(target=areas_service._run_job, args=(area_id, lambda c, aid: None, slow_prepare))
    runner.start()
    assert entered.wait(5)
    assert areas_service._import_lock.acquire(timeout=1), "waiting before the lock must not hold it"
    areas_service._import_lock.release()
    release.set()
    runner.join(timeout=5)
    assert not runner.is_alive()


def test_imports_run_one_at_a_time_process_wide(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(areas_service.threading, "Thread", _REAL_THREAD)
    active = 0
    max_active = 0
    guard = threading.Lock()

    def slow_body(conn, area_id):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        threading.Event().wait(0.05)
        with guard:
            active -= 1

    threads = [threading.Thread(target=areas_service._run_job, args=(i, slow_body)) for i in (1, 2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max_active == 1


def test_tile_membership_is_recorded_before_tiles_are_fetched(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    """A concurrent import's GC must see this area's tiles as referenced
    while they are still being fetched."""
    seen: list[int] = []

    def fetch(conn, tile_ids):
        seen.append(conn.execute("SELECT COUNT(*) FROM area_tiles").fetchone()[0])
        _fake_fetch_and_cache_tiles([])(conn, tile_ids)

    _use_tile_source(monkeypatch, fetch)
    areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=2.0, label="a")
    assert seen and seen[0] > 0


@pytest.mark.parametrize(
    "failure, expected",
    [("tile fetch", "network is down"), ("missing tiles", "missing"), ("elevation fetch", "unreachable")],
)
def test_an_import_that_fails_reaches_error_status_with_the_reason(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, failure: str, expected: str
):
    def boom(conn, tile_ids):
        raise RuntimeError("network is down")

    _use_tile_source(monkeypatch, {"tile fetch": boom, "missing tiles": lambda conn, tile_ids: None}.get(failure))
    if failure == "elevation fetch":
        def failing_ensure_tiles(conn, **kwargs):
            raise elevation.ElevationFetchError("tile server unreachable")

        monkeypatch.setattr(elevation, "ensure_tiles", failing_ensure_tiles)

    status = areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=2.0, label="broken")

    assert status["status"] == "error"
    assert expected in (status["error"] or "")


def test_a_failed_rebuild_keeps_the_previous_graph_and_records_the_error(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _use_tile_source(monkeypatch)
    real_build = network.build_network

    def seeded_build(conn, area_id, payloads, sampler):
        payload = {
            "elements": [
                {"type": "node", "id": 1, "lat": LAT, "lon": LNG},
                {"type": "node", "id": 2, "lat": LAT + 0.001, "lon": LNG},
                {"type": "way", "id": 10, "nodes": [1, 2], "tags": {"highway": "path"}},
            ]
        }
        return real_build(conn, area_id, [payload], sampler)

    monkeypatch.setattr(areas_service.network, "build_network", seeded_build)
    first = areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=2.0, label="rebuilt")
    assert first["status"] == "ready"
    area_id = first["area_id"]
    edges_before = conn.execute("SELECT COUNT(*) FROM net_edges WHERE area_id = ?", (area_id,)).fetchone()[0]
    assert edges_before > 0

    def dying_build(conn, area_id, payloads, sampler):
        conn.execute("DELETE FROM net_edges WHERE area_id = ?", (area_id,))
        conn.execute("DELETE FROM net_nodes WHERE area_id = ?", (area_id,))
        raise RuntimeError("died mid-build")

    monkeypatch.setattr(areas_service.network, "build_network", dying_build)
    second = areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=2.0, label="rebuilt", refresh=True)
    assert second["status"] == "error"
    assert "died mid-build" in (second["error"] or "")
    assert conn.execute("SELECT COUNT(*) FROM net_edges WHERE area_id = ?", (area_id,)).fetchone()[0] == edges_before


def test_the_network_build_reads_the_elevation_cache_and_every_dem_tile_is_fetched_first(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _use_tile_source(monkeypatch)
    ensured: list[tuple[float, float, float, float]] = []

    def recording_ensure_tiles(conn, *, bbox, **kwargs):
        ensured.append(bbox)

    monkeypatch.setattr(elevation, "ensure_tiles", recording_ensure_tiles)
    monkeypatch.setattr(elevation, "sampler_for", _REAL_SAMPLER_FOR)

    def recording_build(conn, area_id, payloads, sampler):
        assert ensured, "the DEM tiles were fetched before the build started"
        assert isinstance(sampler, elevation.TerrariumSampler)

    monkeypatch.setattr(areas_service.network, "build_network", recording_build)
    status = areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=6.0, label="dem")
    assert status["status"] == "ready"

    area_tiles = conn.execute("SELECT gx, gy FROM area_tiles WHERE area_id = ?", (status["area_id"],)).fetchall()
    bounds = [tiles.tile_bounds(r["gx"], r["gy"]) for r in area_tiles]
    assert ensured == [(
        min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds)
    )]


def test_import_skips_the_cut_when_tiles_are_already_cut_from_the_current_extract(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    record = fake_extract_record()
    monkeypatch.setattr(extracts, "select_extracts", lambda *a, **kw: [record])
    cuts: list = []

    def fake_cut(conn, extract, bbox):
        cuts.append(bbox)
        for gx, gy in tiles.tiles_in_bbox(*bbox):
            tilecache.store_tile_elements(
                conn, gx, gy, [], source=extracts.tile_source(extract),
                inputs={record["region"]: record["fetched_at"]},
            )
        return []

    monkeypatch.setattr(extracts, "cut_area", fake_cut)
    record["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=2.0, label="x")
    assert len(cuts) == 1  # nothing cached yet
    areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=2.0, label="x")
    assert len(cuts) == 1  # tiles are newer than the extract: no second pass over the file

    record["fetched_at"] = datetime.now(timezone.utc).isoformat()  # extract refreshed since
    areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=2.0, label="x")
    assert len(cuts) == 2


def test_an_area_reports_the_download_it_performs_with_bytes_received(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    area_id = _insert_area(conn, "dl", status="importing")
    phases: list[tuple[str, int, int]] = []

    def select(conn, bbox, preferred_region=None, *, refresh, force, on_wait, on_progress):
        on_wait()
        on_progress(0, 2000)
        on_progress(500, 2000)
        on_progress(7, None)
        return []

    monkeypatch.setattr(extracts, "select_extracts", select)
    monkeypatch.setattr(
        areas_service, "_set_progress", lambda c, a, *, phase, done, total: phases.append((phase, done, total))
    )
    areas_service.select_area_extracts(conn, area_id)

    assert phases == [
        ("waiting-for-download", 0, 1),
        ("downloading", 0, 2000),
        ("downloading", 500, 2000),
        ("downloading", 7, None),
    ]


def test_a_label_only_refresh_reuses_the_stored_center_and_radius_and_reaches_extract_selection(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    seen: list = []
    record = fake_extract_record()

    def select(conn, bbox, preferred_region=None, *, refresh, force, on_wait, on_progress):
        seen.append({"refresh": refresh, "force": force})
        return [record]

    monkeypatch.setattr(extracts, "select_extracts", select)
    monkeypatch.setattr(extracts, "cut_area", lambda conn, extract, bbox: tiles.tiles_in_bbox(*bbox))
    monkeypatch.setattr(extracts, "tiles_cut_from", lambda *a: True)
    monkeypatch.setattr(areas_service, "missing_tiles", lambda *a: [])
    monkeypatch.setattr(areas_service.network, "build_network", lambda *a, **kw: None)

    first = areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=2.0, label="x", pinned=False)
    areas_service.import_area(conn, label="x", refresh=True)

    assert seen == [{"refresh": False, "force": False}, {"refresh": True, "force": False}]
    detail = areas_service.get_area(conn, first["area_id"])
    assert (detail["center_lat"], detail["center_lng"], detail["radius_km"]) == (LAT, LNG, 2.0)


def test_import_area_refresh_geocode_reaches_the_geocoder(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _use_tile_source(monkeypatch)
    asked: list = []
    found = {"lat": 1.0, "lng": 2.0, "display_name": "x", "attribution": geocode.ATTRIBUTION}
    monkeypatch.setattr(geocode, "lookup", lambda conn, query, refresh=False: asked.append(refresh) or found)

    areas_service.import_area(conn, place="Somewhere", radius_km=1.0, label="g")
    areas_service.import_area(conn, place="Somewhere", radius_km=1.0, label="g", refresh_geocode=True)

    assert asked == [False, True]


def _tile_row(conn: sqlite3.Connection, area_id: int, fetched_at: str, source: str = "overpass") -> None:
    conn.execute("INSERT OR IGNORE INTO area_tiles (area_id, gx, gy) VALUES (?, 0, 0)", (area_id,))
    conn.execute(
        "INSERT OR REPLACE INTO map_tiles (gx, gy, query_version, fetched_at, sha256, data, source) "
        "VALUES (0, 0, 1, ?, 'x', X'00', ?)",
        (fetched_at, source),
    )
    conn.commit()


def test_a_fresh_area_is_not_flagged_for_refresh_and_an_old_one_is(conn: sqlite3.Connection):
    fresh_id = _insert_area(conn, "fresh")
    now = datetime.now(timezone.utc).isoformat()
    _tile_row(conn, fresh_id, now)
    assert areas_service.area_freshness(conn, fresh_id) == (now, False)

    old_id = _insert_area(conn, "old")
    old = (datetime.now(timezone.utc) - timedelta(days=areas_service.REFRESH_SUGGESTED_AFTER_DAYS + 1)).isoformat()
    conn.execute("DELETE FROM area_tiles")
    _tile_row(conn, old_id, old)
    status = areas_service.get_area_status(conn, old_id)
    assert status["data_as_of"] == old
    assert status["refresh_suggested"] is True
    assert areas_service.get_area(conn, old_id)["refresh_suggested"] is True


def test_an_extract_cut_tile_is_as_of_the_extracts_own_date_not_the_cut_time(conn: sqlite3.Connection):
    area_id = _insert_area(conn, "cut")
    extract_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    conn.execute(
        "INSERT INTO map_extracts (region, path, sha256, fetched_at, data_as_of) VALUES ('r/x', '/p', 's', ?, ?)",
        (datetime.now(timezone.utc).isoformat(), extract_date),
    )
    _tile_row(conn, area_id, datetime.now(timezone.utc).isoformat(), source="extract:r/x")
    assert areas_service.area_freshness(conn, area_id) == (extract_date, True)


def test_a_tile_merged_from_several_extracts_is_as_of_the_oldest_extract(conn: sqlite3.Connection):
    area_id = _insert_area(conn, "merged")
    now = datetime.now(timezone.utc)
    older, newer = (now - timedelta(days=200)).isoformat(), (now - timedelta(days=5)).isoformat()
    for region, as_of in (("r/x", newer), ("r/y", older)):
        conn.execute(
            "INSERT INTO map_extracts (region, path, sha256, fetched_at, data_as_of) VALUES (?, '/p', 's', ?, ?)",
            (region, now.isoformat(), as_of),
        )
    _tile_row(conn, area_id, now.isoformat(), source="extract:r/x,r/y")
    assert areas_service.area_freshness(conn, area_id) == (older, True)


def test_an_area_with_no_tiles_has_no_data_date(conn: sqlite3.Connection):
    area_id = _insert_area(conn, "empty", status="new")
    assert areas_service.area_freshness(conn, area_id) == (None, False)


def _insert_graph(conn: sqlite3.Connection, area_id: int, derive_version: int) -> None:
    conn.execute(
        "INSERT INTO net_graphs (area_id, derive_version, built_at, node_count, edge_count, excluded_way_count) "
        "VALUES (?, ?, ?, 0, 0, 0)",
        (area_id, derive_version, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def test_status_rebuilds_a_network_built_by_an_older_version_from_cached_tiles(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    area_id = _insert_area(conn, "old")
    _insert_graph(conn, area_id, network.NETWORK_DERIVE_VERSION - 1)
    started: list[int] = []
    monkeypatch.setattr(areas_service, "_run_finish_only_job", lambda aid: started.append(aid))

    assert areas_service.get_area_status(conn, area_id)["status"] == "importing"
    assert areas_service.get_area_status(conn, area_id)["status"] == "importing"
    assert started == [area_id]  # the second poll sees a live rebuild and does not start another


def test_status_leaves_a_current_network_alone(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    area_id = _insert_area(conn, "new")
    _insert_graph(conn, area_id, network.NETWORK_DERIVE_VERSION)
    monkeypatch.setattr(areas_service, "_run_finish_only_job", lambda aid: pytest.fail("no rebuild needed"))

    assert areas_service.get_area_status(conn, area_id)["status"] == "ready"


class _CachedOnlySampler:
    """Samples None for any point whose DEM tile was not fetched first."""

    def __init__(self, fetched: set) -> None:
        self._fetched = fetched

    def sample(self, points):
        out = []
        for lat, lng in points:
            gx, gy = elevation._lonlat_to_terrarium_pixel(lat, lng, elevation.TERRARIUM_ZOOM)
            tile = (int(gx) // elevation.TERRARIUM_TILE_PX, int(gy) // elevation.TERRARIUM_TILE_PX)
            out.append(100.0 if tile in self._fetched else None)
        return out


def test_a_way_running_past_the_area_keeps_its_elevation(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    way = {
        "type": "way", "id": 7, "nodes": [1, 2], "tags": {"highway": "path"},
        "geometry": [{"lat": LAT, "lon": LNG}, {"lat": LAT, "lon": LNG + 0.2}],
    }

    def fetch(conn, tile_ids):
        for gx, gy in tile_ids:
            conn.execute(
                "INSERT OR REPLACE INTO map_tiles (gx, gy, query_version, fetched_at, sha256, data, source) "
                "VALUES (?, ?, 1, ?, 'deadbeef', ?, 'extract:test/region')",
                (gx, gy, datetime.now(timezone.utc).isoformat(), gzip.compress(json.dumps({"elements": [way]}).encode())),
            )
        conn.commit()

    _use_tile_source(monkeypatch, fetch)
    fetched: set = set()
    monkeypatch.setattr(
        elevation, "ensure_tiles", lambda conn, *, bbox, **kwargs: fetched.update(elevation.terrarium_tiles_covering(*bbox))
    )
    monkeypatch.setattr(elevation, "sampler_for", lambda conn, **kwargs: _CachedOnlySampler(fetched))

    status = areas_service.import_area(conn, lat=LAT, lng=LNG, radius_km=1.0, label="long-way")

    rows = conn.execute("SELECT has_elevation FROM net_edges WHERE area_id = ?", (status["area_id"],)).fetchall()
    assert rows and all(r["has_elevation"] for r in rows)
