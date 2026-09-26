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


def _gpx(points: list[tuple[float, float, float | None]], name: str = "T") -> bytes:
    body = f"<trk><name>{name}</name><trkseg>{''.join(_trkpt(*p) for p in points)}</trkseg></trk>"
    return f"""<?xml version="1.0"?>
<gpx version="1.1" creator="Test" xmlns="http://www.topografix.com/GPX/1/1">{body}</gpx>""".encode("utf-8")


def _line_route(conn: sqlite3.Connection, lat0: float, lng0: float, n: int, step_deg: float, name: str, ele0: float | None = 100.0) -> int:
    points = [(lat0 + i * step_deg, lng0, None if ele0 is None else ele0 + i) for i in range(n)]
    return routes_service.upload_gpx(conn, _gpx(points, name), f"{name}.gpx", name=name)[0]


def test_find_route_junctions_overlap_between_two_shared_routes():
    conn = _conn()
    a = _line_route(conn, 40.0, -105.0, 60, 0.0002, "A")
    b = _line_route(conn, 40.0, -105.0, 60, 0.0002, "B")  # identical path
    result = routes_service.find_route_junctions(conn, a, b, tolerance_m=30.0)
    assert result["overlaps"]
    assert result["route_a"] == a
    assert result["route_b"] == b


def test_find_route_junctions_unknown_route_raises():
    conn = _conn()
    a = _line_route(conn, 40.0, -105.0, 60, 0.0002, "A")
    with pytest.raises(routes_service.RouteNotFoundError):
        routes_service.find_route_junctions(conn, a, 999)


def test_routes_near_point():
    conn = _conn()
    close = _line_route(conn, 40.0, -105.0, 20, 0.0002, "Close")
    far = _line_route(conn, 45.0, -105.0, 20, 0.0002, "Far")
    result = routes_service.routes_near(conn, 40.0, -105.0, radius_m=500.0)
    ids = {r["route_id"] for r in result}
    assert close in ids
    assert far not in ids


def test_routes_near_route():
    conn = _conn()
    a = _line_route(conn, 40.0, -105.0, 20, 0.0002, "A")
    b = _line_route(conn, 40.0, -105.0, 20, 0.0002, "B")
    c = _line_route(conn, 45.0, -105.0, 20, 0.0002, "C")
    result = routes_service.routes_near_route(conn, a, radius_m=500.0)
    ids = {r["route_id"] for r in result}
    assert b in ids
    assert c not in ids
    assert a not in ids  # never includes itself


def test_compare_route_to_course_both_have_elevation():
    conn = _conn()
    a = _line_route(conn, 40.0, -105.0, 60, 0.0002, "A", ele0=100.0)
    b = _line_route(conn, 41.0, -105.0, 60, 0.0002, "B", ele0=200.0)
    result = routes_service.compare_route_to_course(conn, a, b)
    assert result["route"]["route_id"] == a
    assert result["course"]["route_id"] == b
    assert result["grade_histogram_distance"] is not None
    assert result["warnings"] == []


def test_compare_route_to_course_missing_elevation_warns():
    conn = _conn()
    a = _line_route(conn, 40.0, -105.0, 60, 0.0002, "A", ele0=100.0)
    b = _line_route(conn, 41.0, -105.0, 60, 0.0002, "B", ele0=None)
    result = routes_service.compare_route_to_course(conn, a, b)
    assert result["grade_histogram_distance"] is None
    assert result["warnings"]
