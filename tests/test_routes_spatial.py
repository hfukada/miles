import pytest

from miles import routes_spatial as rs


def _line(lat0: float, lng0: float, n: int, step_deg: float = 0.0002, lat_axis: bool = True) -> list[tuple[float, float]]:
    pts = []
    for i in range(n):
        if lat_axis:
            pts.append((lat0 + i * step_deg, lng0))
        else:
            pts.append((lat0, lng0 + i * step_deg))
    return pts


def _dists(pts: list[tuple[float, float]]) -> list[float]:
    dist = [0.0]
    for i in range(1, len(pts)):
        dist.append(dist[-1] + rs.equirect_distance_m(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1]))
    return dist


def test_equirect_distance_matches_known_latitude_delta():
    # 1 degree of latitude is ~111.19km with this module's R=6371000 sphere.
    d = rs.equirect_distance_m(40.0, -105.0, 41.0, -105.0)
    assert d == pytest.approx(111194.9, rel=0.001)


def test_equirect_distance_zero_for_same_point():
    assert rs.equirect_distance_m(40.0, -105.0, 40.0, -105.0) == 0.0


def test_grid_near_finds_close_points_and_excludes_far_ones():
    points = [(40.0, -105.0), (40.001, -105.0), (45.0, -105.0)]
    grid = rs.Grid(points, cell_size_m=50.0)
    nearby = grid.near(40.0, -105.0, radius_m=50.0)
    # Grid.near is cell-granularity (a superset); exact filtering is the caller's job.
    assert 0 in nearby
    assert 2 not in nearby


def test_find_junctions_detects_a_crossing_as_a_junction_not_an_overlap():
    # Two lines crossing at a single point, running in different directions.
    a = _line(40.0, -105.0, 20, lat_axis=True)
    b = _line(40.0019, -105.002, 20, step_deg=0.0002, lat_axis=False)
    dist_a = _dists(a)
    dist_b = _dists(b)
    result = rs.find_junctions(a, dist_a, b, dist_b, tolerance_m=30.0)
    assert len(result["junctions"]) >= 1
    assert result["overlaps"] == []


def test_find_junctions_detects_a_shared_stretch_as_an_overlap():
    # B follows the same path as A for its whole length.
    a = _line(40.0, -105.0, 30)
    b = [(lat, lng) for lat, lng in a]
    dist_a = _dists(a)
    dist_b = _dists(b)
    result = rs.find_junctions(a, dist_a, b, dist_b, tolerance_m=30.0)
    assert result["overlaps"], "identical paths should produce an overlap, not scattered junctions"
    overlap = result["overlaps"][0]
    assert overlap["a_from_m"] == pytest.approx(0.0, abs=1.0)
    assert overlap["a_to_m"] == pytest.approx(dist_a[-1], abs=1.0)


def test_find_junctions_empty_when_routes_never_come_close():
    a = _line(40.0, -105.0, 10)
    b = _line(50.0, -105.0, 10)
    result = rs.find_junctions(a, _dists(a), b, _dists(b), tolerance_m=30.0)
    assert result["junctions"] == []
    assert result["overlaps"] == []


def test_classify_shape_loop():
    # A square loop back to (approximately) the start.
    pts = [(40.0, -105.0), (40.001, -105.0), (40.001, -105.001), (40.0, -105.001), (40.00001, -105.00001)]
    dist = _dists(pts)
    assert rs.classify_shape(pts, dist) == "loop"


def test_classify_shape_out_and_back():
    out = _line(40.0, -105.0, 20)
    back = list(reversed(out[:-1]))
    pts = out + back
    dist = _dists(pts)
    assert rs.classify_shape(pts, dist) == "out_and_back"


def test_classify_shape_point_to_point():
    pts = _line(40.0, -105.0, 20)
    dist = _dists(pts)
    assert rs.classify_shape(pts, dist) == "point_to_point"


def test_routes_near_point_within_and_outside_radius():
    points = [(40.0, -105.0), (40.001, -105.0)]
    close = rs.routes_near_point(40.0, -105.0, 50.0, points)
    far = rs.routes_near_point(41.0, -105.0, 50.0, points)
    assert close is not None and close < 5.0
    assert far is None


def test_slice_by_distance_basic_subrange():
    dist = [0.0, 10.0, 20.0, 30.0, 40.0]
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    out_dist, out_vals = rs.slice_by_distance(dist, values, 5.0, 25.0)
    assert out_dist[0] == 0.0
    assert out_dist[-1] == pytest.approx(20.0)
    assert out_vals[0] == pytest.approx(0.5)  # interpolated between 0 and 1
    assert out_vals[-1] == pytest.approx(2.5)  # interpolated between 2 and 3


def test_slice_by_distance_reversed():
    dist = [0.0, 10.0, 20.0, 30.0]
    values = [0.0, 1.0, 2.0, 3.0]
    out_dist, out_vals = rs.slice_by_distance(dist, values, 30.0, 0.0)
    assert out_dist == [0.0, 10.0, 20.0, 30.0]
    assert out_vals == [3.0, 2.0, 1.0, 0.0]


def test_slice_by_distance_preserves_none_ele():
    dist = [0.0, 10.0, 20.0]
    values: list[float | None] = [1.0, None, 3.0]
    _, out_vals = rs.slice_by_distance(dist, values, 0.0, 20.0)
    assert out_vals[1] is None
