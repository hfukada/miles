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


def test_routes_near_a_route_or_a_point():
    conn = memory_db()
    a = _line_route(conn, 40.0, -105.0, 20, 0.0002, "A")
    b = _line_route(conn, 40.0, -105.0, 20, 0.0002, "B")
    c = _line_route(conn, 45.0, -105.0, 20, 0.0002, "C")
    result = routes_service.routes_near_route(conn, a, radius_m=500.0)
    ids = {r["route_id"] for r in result}
    assert b in ids
    assert c not in ids
    assert a not in ids  # never includes itself

    near_point = {r["route_id"] for r in routes_service.routes_near(conn, 40.0, -105.0, radius_m=500.0)}
    assert {a, b} <= near_point and c not in near_point


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
