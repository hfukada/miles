import pytest

from miles import tiles


def test_tile_index_floors_toward_negative_infinity():
    assert tiles.tile_index(0.0, 0.0) == (0, 0)
    assert tiles.tile_index(0.049, 0.049) == (0, 0)
    assert tiles.tile_index(0.05, 0.05) == (1, 1)
    assert tiles.tile_index(-0.001, -0.001) == (-1, -1)


def test_tile_bounds_round_trip_into_tile_index():
    gx, gy = 3, -2
    south, west, north, east = tiles.tile_bounds(gx, gy)
    assert north - south == pytest.approx(tiles.TILE_DEG)
    assert east - west == pytest.approx(tiles.TILE_DEG)
    # A point just inside the tile's own bounds indexes back to the same tile.
    assert tiles.tile_index(south + 0.001, west + 0.001) == (gx, gy)


def test_bbox_for_center_radius_grows_with_radius():
    small = tiles.bbox_for_center_radius(5.0, 10.0, 5.0)
    large = tiles.bbox_for_center_radius(5.0, 10.0, 50.0)
    small_span = small[2] - small[0]
    large_span = large[2] - large[0]
    assert large_span > small_span
    assert large_span == pytest.approx(small_span * 10, rel=0.01)


def test_bbox_for_center_radius_widens_longitude_near_poles():
    equator = tiles.bbox_for_center_radius(0.0, 0.0, 100.0)
    high_lat = tiles.bbox_for_center_radius(80.0, 0.0, 100.0)
    equator_lng_span = equator[3] - equator[1]
    high_lat_lng_span = high_lat[3] - high_lat[1]
    assert high_lat_lng_span > equator_lng_span


def _middle_of(gx: int, gy: int) -> tuple[float, float]:
    south, west, north, east = tiles.tile_bounds(gx, gy)
    return (south + north) / 2.0, (west + east) / 2.0


def test_assign_elements_puts_a_way_in_every_tile_it_crosses_and_a_node_in_its_own():
    (lat0, lng0), (lat1, lng1) = _middle_of(0, 0), _middle_of(1, 0)
    inside_one = {
        "type": "way", "id": 1, "tags": {"highway": "path"}, "nodes": [10, 11],
        "geometry": [{"lat": lat0, "lon": lng0}, {"lat": lat0, "lon": lng0 + 0.001}],
    }
    across_both = {
        "type": "way", "id": 2, "tags": {"highway": "path"}, "nodes": [12, 13],
        "geometry": [{"lat": lat0, "lon": lng0}, {"lat": lat1, "lon": lng1}],
    }
    parking = {"type": "node", "id": 5, "lat": lat1, "lon": lng1, "tags": {"amenity": "parking"}}

    buckets = tiles.assign_elements([inside_one, across_both, parking], [(0, 0), (1, 0)])

    assert buckets[(0, 0)] == [inside_one, across_both]
    assert buckets[(1, 0)] == [across_both, parking]


@pytest.mark.parametrize(
    "element",
    [
        {"type": "node", "id": 5, "lat": 89.0, "lon": 179.0, "tags": {}},
        {"type": "way", "id": 1, "tags": {}, "geometry": [None, None]},
        {"type": "node", "id": 2, "tags": {}},
    ],
    ids=["outside every requested tile", "way without usable geometry", "node without coordinates"],
)
def test_assign_elements_skips_elements_that_belong_to_no_requested_tile(element: dict):
    assert tiles.assign_elements([element], [(0, 0)])[(0, 0)] == []
