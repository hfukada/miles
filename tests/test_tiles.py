from miles import tiles


def test_tile_index_floors_toward_negative_infinity():
    assert tiles.tile_index(0.0, 0.0) == (0, 0)
    assert tiles.tile_index(0.049, 0.049) == (0, 0)
    assert tiles.tile_index(0.05, 0.05) == (1, 1)
    assert tiles.tile_index(-0.001, -0.001) == (-1, -1)


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

    unplaceable = [
        {"type": "node", "id": 5, "lat": 89.0, "lon": 179.0, "tags": {}},  # outside every requested tile
        {"type": "way", "id": 1, "tags": {}, "geometry": [None, None]},  # no usable geometry
        {"type": "node", "id": 2, "tags": {}},  # no coordinates
    ]
    assert tiles.assign_elements(unplaceable, [(0, 0)])[(0, 0)] == []


def test_assign_elements_places_areas_and_relations_by_their_bounds():
    (lat0, lng0), (lat1, lng1) = _middle_of(0, 0), _middle_of(1, 0)
    across = {"type": "area", "id": 7, "tags": {"leisure": "park"}, "rings": [], "bounds": [lat0, lng0, lat1, lng1]}
    within = {"type": "relation", "id": 8, "tags": {}, "members": [1], "bounds": [lat0, lng0, lat0 + 0.001, lng0 + 0.001]}
    boundless = {"type": "area", "id": 9, "tags": {}, "rings": []}

    buckets = tiles.assign_elements([across, within, boundless], [(0, 0), (1, 0)])

    assert buckets[(0, 0)] == [across, within]
    assert buckets[(1, 0)] == [across]
