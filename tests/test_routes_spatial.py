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


def test_nearest_index_returns_the_closest_key_offset_and_distance_within_the_radius():
    index = rs.NearestIndex(cell_size_m=50.0)
    index.add("a", [(40.0, -105.0, 0.0), (40.0002, -105.0, 22.0)])
    index.add("b", [(40.001, -105.0, 0.0), (40.0012, -105.0, 22.0)])

    key, offset, dist = index.nearest(40.00021, -105.0, 100.0) or (None, None, None)
    assert (key, offset) == ("a", 22.0)
    assert dist == pytest.approx(rs.equirect_distance_m(40.00021, -105.0, 40.0002, -105.0))
    assert index.nearest(40.00021, -105.0, 1.0) is None
    assert rs.NearestIndex(cell_size_m=50.0).nearest(40.0, -105.0, 100.0) is None


def test_nearest_index_follows_removed_and_added_keys():
    index = rs.NearestIndex(cell_size_m=50.0)
    index.add(1, [(40.0, -105.0, 0.0)])
    index.remove(1)
    assert index.nearest(40.0, -105.0, 100.0) is None
    index.add(2, [(40.0, -105.0, 7.0)])
    assert index.nearest(40.0, -105.0, 100.0) == (2, 7.0, 0.0)

    index.remove(2)
    assert index.nearest(40.0, -105.0, 100.0) is None


def test_nearest_index_finds_every_point_in_range_far_from_the_first_points_latitude():
    index = rs.NearestIndex(cell_size_m=50.0)
    index.add("equator", [(0.0, 0.0, 0.0)])
    for lat, lng in ((60.0, 10.0), (-60.0, 10.0)):
        east = lng + 80.0 / (111_320.0 * 0.5)  # 80 m east at this latitude
        index.add(lat, [(lat, east, 3.0)])
        hit = index.nearest(lat, lng, 100.0)
        assert hit is not None and hit[:2] == (lat, 3.0)
        assert hit[2] == pytest.approx(80.0, rel=0.02)
        assert index.nearest(lat, lng, 70.0) is None


def test_find_junctions_detects_a_crossing_as_a_junction_not_an_overlap():
    # Two lines crossing at a single point, running in different directions.
    a = _line(40.0, -105.0, 20, lat_axis=True)
    b = _line(40.0019, -105.002, 20, step_deg=0.0002, lat_axis=False)
    dist_a = _dists(a)
    dist_b = _dists(b)
    result = rs.find_junctions(a, dist_a, b, dist_b, tolerance_m=30.0)
    assert len(result["junctions"]) >= 1
    assert result["overlaps"] == []


@pytest.mark.parametrize("reverse_b", [False, True], ids=["same direction", "opposite direction"])
def test_find_junctions_detects_a_shared_stretch_as_one_overlap_in_either_direction(reverse_b: bool):
    a = _line(40.0, -105.0, 30)
    b = list(reversed(a)) if reverse_b else list(a)
    dist_a = _dists(a)
    dist_b = _dists(b)
    result = rs.find_junctions(a, dist_a, b, dist_b, tolerance_m=30.0)
    assert len(result["overlaps"]) == 1, "identical paths should produce one overlap, not scattered junctions"
    overlap = result["overlaps"][0]
    assert overlap["a_from_m"] == pytest.approx(0.0, abs=1.0)
    assert overlap["a_to_m"] == pytest.approx(dist_a[-1], abs=1.0)
    assert overlap["b_from_m"] == pytest.approx(0.0, abs=1.0)
    assert overlap["b_to_m"] == pytest.approx(dist_b[-1], abs=1.0)


def test_find_junctions_splits_overlap_across_a_loop_wrap():
    # A shares a stretch with B at both ends of B's own point sequence --
    # the shape a loop takes when its outbound and return legs both pass a
    # common trailhead stretch that a second, smaller loop also uses once.
    # Matching every point of A into one A-contiguous run (as before the
    # fix) reported this as a single overlap spanning the whole of B, from
    # its first cluster to its last. It must instead split into two
    # overlaps, each mapped to the matching half of B.
    shared_low = _line(40.0, -105.0, 15)    # B's start; A's first half
    shared_high = _line(40.05, -105.0, 15)  # B's end; A's second half
    filler = _line(41.0, -105.0, 15)        # far from A -- the rest of B's loop

    b = shared_low + filler + shared_high
    a = shared_low + shared_high  # one A-contiguous run, matching both ends of B

    dist_a = _dists(a)
    dist_b = _dists(b)
    result = rs.find_junctions(a, dist_a, b, dist_b, tolerance_m=30.0)

    assert len(result["overlaps"]) == 2
    first, second = sorted(result["overlaps"], key=lambda o: o["a_from_m"])
    assert first["a_from_m"] == pytest.approx(0.0, abs=1.0)
    assert first["b_from_m"] == pytest.approx(0.0, abs=1.0)
    assert first["b_to_m"] < dist_b[len(shared_low) + len(filler)]
    assert second["a_to_m"] == pytest.approx(dist_a[-1], abs=1.0)
    assert second["b_from_m"] > dist_b[len(shared_low) + len(filler) - 1]
    assert second["b_to_m"] == pytest.approx(dist_b[-1], abs=1.0)


def test_find_junctions_empty_when_routes_never_come_close():
    a = _line(40.0, -105.0, 10)
    b = _line(50.0, -105.0, 10)
    result = rs.find_junctions(a, _dists(a), b, _dists(b), tolerance_m=30.0)
    assert result["junctions"] == []
    assert result["overlaps"] == []


@pytest.mark.parametrize(
    "pts, shape",
    [
        # A square loop back to (approximately) the start.
        ([(10.0, 20.0), (10.001, 20.0), (10.001, 20.001), (10.0, 20.001), (10.00001, 20.00001)], "loop"),
        (_line(40.0, -105.0, 20) + list(reversed(_line(40.0, -105.0, 20)[:-1])), "out_and_back"),
        (_line(40.0, -105.0, 20), "point_to_point"),
    ],
)
def test_classify_shape(pts, shape: str):
    assert rs.classify_shape(pts, _dists(pts)) == shape


def test_routes_near_point_within_and_outside_radius():
    points = [(40.0, -105.0), (40.001, -105.0)]
    close = rs.routes_near_point(40.0, -105.0, 50.0, points)
    far = rs.routes_near_point(41.0, -105.0, 50.0, points)
    assert close is not None and close < 5.0
    assert far is None


def test_slice_by_distance_interpolates_a_subrange_runs_backwards_and_keeps_missing_values():
    dist = [0.0, 10.0, 20.0, 30.0, 40.0]
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    out_dist, out_vals = rs.slice_by_distance(dist, values, 5.0, 25.0)
    assert out_dist[0] == 0.0
    assert out_dist[-1] == pytest.approx(20.0)
    assert out_vals[0] == pytest.approx(0.5)  # interpolated between 0 and 1
    assert out_vals[-1] == pytest.approx(2.5)  # interpolated between 2 and 3

    out_dist, out_vals = rs.slice_by_distance(dist[:4], values[:4], 30.0, 0.0)
    assert out_dist == [0.0, 10.0, 20.0, 30.0]
    assert out_vals == [3.0, 2.0, 1.0, 0.0]

    with_gap: list[float | None] = [1.0, None, 3.0]
    assert rs.slice_by_distance([0.0, 10.0, 20.0], with_gap, 0.0, 20.0)[1][1] is None


