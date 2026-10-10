import pytest
from helpers import memory_db, upload_line_route as _line_route

from miles import routes as routes_service


def test_find_route_junctions_overlap_between_two_shared_routes():
    conn = memory_db()
    a = _line_route(conn, 40.0, -105.0, 60, 0.0002, "A")
    b = _line_route(conn, 40.0, -105.0, 60, 0.0002, "B")  # identical path
    result = routes_service.find_route_junctions(conn, a, b, tolerance_m=30.0)
    assert result["overlaps"]
    assert result["route_a"] == a
    assert result["route_b"] == b


def test_find_route_junctions_unknown_route_raises():
    conn = memory_db()
    a = _line_route(conn, 40.0, -105.0, 60, 0.0002, "A")
    with pytest.raises(routes_service.RouteNotFoundError):
        routes_service.find_route_junctions(conn, a, 999)


def test_routes_near_point():
    conn = memory_db()
    close = _line_route(conn, 40.0, -105.0, 20, 0.0002, "Close")
    far = _line_route(conn, 45.0, -105.0, 20, 0.0002, "Far")
    result = routes_service.routes_near(conn, 40.0, -105.0, radius_m=500.0)
    ids = {r["route_id"] for r in result}
    assert close in ids
    assert far not in ids


def test_routes_near_route():
    conn = memory_db()
    a = _line_route(conn, 40.0, -105.0, 20, 0.0002, "A")
    b = _line_route(conn, 40.0, -105.0, 20, 0.0002, "B")
    c = _line_route(conn, 45.0, -105.0, 20, 0.0002, "C")
    result = routes_service.routes_near_route(conn, a, radius_m=500.0)
    ids = {r["route_id"] for r in result}
    assert b in ids
    assert c not in ids
    assert a not in ids  # never includes itself


def test_compare_route_to_course_both_have_elevation():
    conn = memory_db()
    a = _line_route(conn, 40.0, -105.0, 60, 0.0002, "A", ele0=100.0)
    b = _line_route(conn, 41.0, -105.0, 60, 0.0002, "B", ele0=200.0)
    result = routes_service.compare_route_to_course(conn, a, b)
    assert result["route"]["route_id"] == a
    assert result["course"]["route_id"] == b
    assert result["grade_histogram_distance"] is not None
    assert result["warnings"] == []


def test_compare_route_to_course_missing_elevation_warns():
    conn = memory_db()
    a = _line_route(conn, 40.0, -105.0, 60, 0.0002, "A", ele0=100.0)
    b = _line_route(conn, 41.0, -105.0, 60, 0.0002, "B", ele0=None)
    result = routes_service.compare_route_to_course(conn, a, b)
    assert result["grade_histogram_distance"] is None
    assert result["warnings"]
