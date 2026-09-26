import sqlite3

import pytest

from miles import db, routes as routes_service


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _trkpt(lat: float, lng: float, ele: float | None) -> str:
    if ele is None:
        return f'<trkpt lat="{lat}" lon="{lng}"/>'
    return f'<trkpt lat="{lat}" lon="{lng}"><ele>{ele}</ele></trkpt>'


def _gpx_single_track(n: int = 40, lat0: float = 40.0, step_deg: float = 0.0002, ele0: float | None = 100.0, name: str = "Test Track", creator: str = "Test") -> bytes:
    pts = []
    for i in range(n):
        ele = None if ele0 is None else ele0 + 2.0 * i
        pts.append(_trkpt(lat0 + i * step_deg, -105.0, ele))
    body = f"<trk><name>{name}</name><trkseg>{''.join(pts)}</trkseg></trk>"
    return f"""<?xml version="1.0"?>
<gpx version="1.1" creator="{creator}" xmlns="http://www.topografix.com/GPX/1/1">{body}</gpx>""".encode("utf-8")


def _gpx_multi_track(names: list[str]) -> bytes:
    tracks = []
    for i, name in enumerate(names):
        pts = "".join(_trkpt(40.0 + i + j * 0.0002, -105.0, None) for j in range(10))
        tracks.append(f"<trk><name>{name}</name><trkseg>{pts}</trkseg></trk>")
    return f"""<?xml version="1.0"?>
<gpx version="1.1" creator="Test" xmlns="http://www.topografix.com/GPX/1/1">{''.join(tracks)}</gpx>""".encode("utf-8")


def test_upload_and_derive_single_track():
    conn = _conn()
    route_ids = routes_service.upload_gpx(conn, _gpx_single_track(), "track.gpx", name="Ridge Loop", source="alltrails", tags=["hike"])
    assert len(route_ids) == 1
    detail = routes_service.get_route(conn, route_ids[0])
    assert detail["name"] == "Ridge Loop"
    assert detail["source"] == "alltrails"
    assert detail["tags"] == ["hike"]
    assert detail["has_elevation"] is True
    assert detail["gain_m"] is not None and detail["gain_m"] > 0
    assert detail["distance_m"] is not None and detail["distance_m"] > 0
    assert detail["shape"] in ("loop", "out_and_back", "point_to_point")


def test_upload_no_elevation_route_has_null_gain():
    conn = _conn()
    route_ids = routes_service.upload_gpx(conn, _gpx_single_track(ele0=None), "flat.gpx", name="No Ele")
    detail = routes_service.get_route(conn, route_ids[0])
    assert detail["has_elevation"] is False
    assert detail["gain_m"] is None
    assert detail["loss_m"] is None
    assert detail["ft_per_mi"] is None
    assert detail["grade_bands"] is None
    assert detail["climbs"] == []


def test_upload_multi_track_names_each_route():
    conn = _conn()
    route_ids = routes_service.upload_gpx(
        conn, _gpx_multi_track(["Leg 1", "Leg 2"]), "course.gpx", name="Race", source="caltopo"
    )
    assert len(route_ids) == 2
    names = {routes_service.get_route(conn, rid)["name"] for rid in route_ids}
    assert names == {"Race — Leg 1", "Race — Leg 2"}


def test_upload_dedupes_identical_bytes_by_sha256():
    conn = _conn()
    data = _gpx_single_track()
    ids1 = routes_service.upload_gpx(conn, data, "a.gpx", name="First")
    ids2 = routes_service.upload_gpx(conn, data, "a.gpx", name="Second")
    assert ids1 == ids2
    file_count = conn.execute("SELECT COUNT(*) FROM gpx_files").fetchone()[0]
    assert file_count == 1
    # Metadata from the first upload wins; a dedup reupload doesn't overwrite it.
    assert routes_service.get_route(conn, ids1[0])["name"] == "First"


def test_waypoints_are_snapped_along_the_route():
    conn = _conn()
    data = _gpx_single_track(ele0=None)
    # Insert a waypoint near the 5th point manually by re-uploading a file with a wpt.
    body = f"""<?xml version="1.0"?>
<gpx version="1.1" creator="Test" xmlns="http://www.topografix.com/GPX/1/1">
<wpt lat="40.0008" lon="-105.0"><name>Aid</name><desc>near start</desc></wpt>
<trk><name>T</name><trkseg>{''.join(_trkpt(40.0 + i * 0.0002, -105.0, None) for i in range(40))}</trkseg></trk>
</gpx>""".encode("utf-8")
    route_ids = routes_service.upload_gpx(conn, body, "wpt.gpx")
    detail = routes_service.get_route(conn, route_ids[0])
    assert len(detail["waypoints"]) == 1
    wp = detail["waypoints"][0]
    assert wp["name"] == "Aid"
    assert wp["dist_m"] > 0
    assert wp["offset_m"] < 50


def test_update_route_name_notes_tags():
    conn = _conn()
    route_ids = routes_service.upload_gpx(conn, _gpx_single_track(), "a.gpx", name="Old")
    updated = routes_service.update_route(conn, route_ids[0], name="New", tags=["x", "y"])
    assert updated["name"] == "New"
    assert updated["tags"] == ["x", "y"]
    # Omitted fields (notes here) stay unchanged.
    assert updated["notes"] is None


def test_update_route_missing_raises():
    conn = _conn()
    with pytest.raises(routes_service.RouteNotFoundError):
        routes_service.update_route(conn, 999, name="x")


def test_delete_route():
    conn = _conn()
    route_ids = routes_service.upload_gpx(conn, _gpx_single_track(), "a.gpx")
    routes_service.delete_route(conn, route_ids[0])
    with pytest.raises(routes_service.RouteNotFoundError):
        routes_service.get_route(conn, route_ids[0])


def test_list_routes_filters_by_distance_and_tag():
    conn = _conn()
    short_ids = routes_service.upload_gpx(conn, _gpx_single_track(n=10), "short.gpx", name="Short", tags=["a"])
    long_ids = routes_service.upload_gpx(conn, _gpx_single_track(n=200), "long.gpx", name="Long", tags=["b"])
    all_routes = routes_service.list_routes(conn)
    assert {r["route_id"] for r in all_routes} == {short_ids[0], long_ids[0]}

    tagged = routes_service.list_routes(conn, tag="a")
    assert {r["route_id"] for r in tagged} == {short_ids[0]}

    short_detail = routes_service.get_route(conn, short_ids[0])
    narrow = routes_service.list_routes(conn, max_distance_m=short_detail["distance_m"] + 1)
    assert {r["route_id"] for r in narrow} == {short_ids[0]}


def test_derive_all_routes_is_idempotent():
    conn = _conn()
    route_ids = routes_service.upload_gpx(conn, _gpx_single_track(), "a.gpx", name="A")
    before = routes_service.get_route(conn, route_ids[0])
    routes_service.derive_all_routes(conn)
    after = routes_service.get_route(conn, route_ids[0])
    assert before["distance_m"] == after["distance_m"]
    assert before["gain_m"] == after["gain_m"]
    assert before["name"] == after["name"]  # user metadata untouched by re-derive


def test_ensure_routes_derived_reruns_on_version_bump(monkeypatch: pytest.MonkeyPatch):
    conn = _conn()
    routes_service.upload_gpx(conn, _gpx_single_track(), "a.gpx")
    conn.execute("UPDATE meta SET value = '0' WHERE key = 'route_derive_version'")
    conn.commit()
    routes_service.ensure_routes_derived(conn)
    row = conn.execute("SELECT value FROM meta WHERE key = 'route_derive_version'").fetchone()
    assert row["value"] == str(routes_service.ROUTE_DERIVE_VERSION)
