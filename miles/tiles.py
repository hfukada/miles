"""Fixed lat/lng grid for map tiles, so overlapping areas share cached data.
Pure math, no I/O.
"""

import math
from typing import Iterable, Sequence

TILE_DEG = 0.05  # ~5.5 km at the equator

EARTH_RADIUS_M = 6371000.0


def tile_index(lat: float, lng: float) -> tuple[int, int]:
    """The (gx, gy) grid cell containing a point; flooring keeps cells gap-free
    across zero."""
    return (math.floor(lng / TILE_DEG), math.floor(lat / TILE_DEG))


def tile_bounds(gx: int, gy: int) -> tuple[float, float, float, float]:
    """(south, west, north, east) bounds of one grid cell."""
    west = gx * TILE_DEG
    south = gy * TILE_DEG
    return (south, west, south + TILE_DEG, west + TILE_DEG)


def bbox_overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """Whether two (south, west, north, east) boxes share any area."""
    a_south, a_west, a_north, a_east = a
    b_south, b_west, b_north, b_east = b
    return a_south <= b_north and a_north >= b_south and a_west <= b_east and a_east >= b_west


def bbox_within(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> bool:
    """Whether the `inner` (south, west, north, east) box lies inside `outer`."""
    i_south, i_west, i_north, i_east = inner
    o_south, o_west, o_north, o_east = outer
    return o_south <= i_south and o_west <= i_west and o_north >= i_north and o_east >= i_east


def bbox_for_center_radius(lat: float, lng: float, radius_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) bounding square of a circle, longitude scaled
    for convergence at this latitude. Callers needing the exact circle filter
    further."""
    lat_delta = math.degrees(radius_km * 1000.0 / EARTH_RADIUS_M)
    lng_scale = max(math.cos(math.radians(lat)), 0.01)  # avoid blowup near the poles
    lng_delta = lat_delta / lng_scale
    return (lat - lat_delta, lng - lng_delta, lat + lat_delta, lng + lng_delta)


def tiles_in_bbox(south: float, west: float, north: float, east: float) -> list[tuple[int, int]]:
    """Every grid cell intersecting a bbox, in row-major (gy then gx) order."""
    gx0, gy0 = tile_index(south, west)
    gx1, gy1 = tile_index(north, east)
    return [(gx, gy) for gy in range(gy0, gy1 + 1) for gx in range(gx0, gx1 + 1)]


def assign_elements(elements: Sequence[dict], tile_ids: Iterable[tuple[int, int]]) -> dict[tuple[int, int], list[dict]]:
    """Bucket elements into the tile_ids they intersect: a way into every tile
    its geometry's bbox overlaps, a node into the one containing it."""
    tile_id_list = list(tile_ids)
    bounds_by_tile = {t: tile_bounds(*t) for t in tile_id_list}
    buckets: dict[tuple[int, int], list[dict]] = {t: [] for t in tile_id_list}
    if not bounds_by_tile:
        return buckets
    overall_south = min(b[0] for b in bounds_by_tile.values())
    overall_west = min(b[1] for b in bounds_by_tile.values())
    overall_north = max(b[2] for b in bounds_by_tile.values())
    overall_east = max(b[3] for b in bounds_by_tile.values())

    for el in elements:
        el_type = el.get("type")
        if el_type == "way":
            geom = el.get("geometry") or []
            lats = [p["lat"] for p in geom if p is not None]
            lons = [p["lon"] for p in geom if p is not None]
            if not lats:
                continue
            wsouth, wnorth, wwest, weast = min(lats), max(lats), min(lons), max(lons)
        elif el_type == "node":
            lat, lon = el.get("lat"), el.get("lon")
            if lat is None or lon is None:
                continue
            wsouth = wnorth = lat
            wwest = weast = lon
        else:
            continue

        if wsouth > overall_north or wnorth < overall_south or wwest > overall_east or weast < overall_west:
            continue
        for t, (south, west, north, east) in bounds_by_tile.items():
            if wsouth <= north and wnorth >= south and wwest <= east and weast >= west:
                buckets[t].append(el)
    return buckets
