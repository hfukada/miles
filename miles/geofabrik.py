"""Geofabrik's region index and the choice of which regional extract covers an
area.

Geofabrik's `index-v1.json` is a GeoJSON FeatureCollection with one feature
per downloadable region (id, parent, `urls.pbf`, boundary polygon). It is
fetched once, stored gzipped in the database, and refreshed at most every
INDEX_MAX_AGE_DAYS with a conditional GET (a stale copy keeps serving if the
refresh fails).

Region choice is the cheapest cover of the area's box: one region whose
boundary contains it, or a set of regions that together cover it for less
ground. Regions nest, so an area straddling two states is contained only by
their parent, possibly a multi-gigabyte extract. Size is compared by polygon
area in degrees squared, since the index carries no file sizes. Coverage uses
sample points of the box and a plain ray-casting test, which can miss a
boundary notch between sample points.
"""

import difflib
import gzip
import hashlib
import itertools
import json
import os
import re
import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from . import http_client, tiles

GEOFABRIK_BASE_URL = os.environ.get("MILES_GEOFABRIK_URL", "https://download.geofabrik.de")

INDEX_MAX_AGE_DAYS = 30.0

_PBF_URL_PATH = re.compile(r"^https?://[^/]+/(?P<path>.+)-latest\.osm\.pbf$")

Ring = list[tuple[float, float]]  # (lat, lng)
Polygon = list[Ring]  # outer ring first, then holes


class RegionError(Exception):
    pass


class Region(NamedTuple):
    path: str  # e.g. "continent/country/region"; the URL stem of its extract
    bbox: tuple[float, float, float, float]
    polygons: list[Polygon]


def init_index_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geofabrik_index (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            fetched_at    TEXT NOT NULL,
            checked_at    TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            etag          TEXT,
            last_modified TEXT,
            data          BLOB NOT NULL
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Parsing and geometry
# ---------------------------------------------------------------------------


def _ring(coords: list) -> Ring:
    return [(float(lat), float(lng)) for lng, lat, *_ in coords]


def _polygons(geometry: dict | None) -> list[Polygon]:
    if not geometry:
        return []
    if geometry.get("type") == "Polygon":
        return [[_ring(r) for r in geometry["coordinates"]]]
    if geometry.get("type") == "MultiPolygon":
        return [[_ring(r) for r in polygon] for polygon in geometry["coordinates"]]
    return []


def parse_index(raw: bytes) -> list[Region]:
    """The index's regions that have an extract and a boundary."""
    regions: list[Region] = []
    for feature in json.loads(raw).get("features", []):
        pbf = ((feature.get("properties") or {}).get("urls") or {}).get("pbf")
        match = _PBF_URL_PATH.match(pbf or "")
        polygons = _polygons(feature.get("geometry"))
        if match is None or not polygons:
            continue
        points = [p for polygon in polygons for ring in polygon[:1] for p in ring]
        lats, lngs = [p[0] for p in points], [p[1] for p in points]
        regions.append(Region(match["path"], (min(lats), min(lngs), max(lats), max(lngs)), polygons))
    return regions


def _in_ring(lat: float, lng: float, ring: Ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i, (lat_i, lng_i) in enumerate(ring):
        lat_j, lng_j = ring[j]
        if (lat_i > lat) != (lat_j > lat) and lng < (lng_j - lng_i) * (lat - lat_i) / (lat_j - lat_i) + lng_i:
            inside = not inside
        j = i
    return inside


def _in_polygon(lat: float, lng: float, polygon: Polygon) -> bool:
    return _in_ring(lat, lng, polygon[0]) and not any(_in_ring(lat, lng, hole) for hole in polygon[1:])


def _ring_area(ring: Ring) -> float:
    total = 0.0
    j = len(ring) - 1
    for i, (lat_i, lng_i) in enumerate(ring):
        lat_j, lng_j = ring[j]
        total += lng_j * lat_i - lng_i * lat_j
        j = i
    return abs(total) / 2.0


def _area(region: Region) -> float:
    return sum(_ring_area(p[0]) - sum(_ring_area(h) for h in p[1:]) for p in region.polygons)


def _grid_points(bbox: tuple[float, float, float, float], n: int) -> list[tuple[float, float]]:
    """An n-by-n lattice over bbox, corners and edges included."""
    south, west, north, east = bbox
    steps = n - 1
    return [
        (south + (north - south) * i / steps, west + (east - west) * j / steps)
        for i in range(n)
        for j in range(n)
    ]


def _covered(region: Region, points: list[tuple[float, float]]) -> frozenset[int]:
    south, west, north, east = region.bbox
    return frozenset(
        i
        for i, (lat, lng) in enumerate(points)
        if south <= lat <= north and west <= lng <= east and any(_in_polygon(lat, lng, p) for p in region.polygons)
    )


def _contains(region: Region, points: list[tuple[float, float]]) -> bool:
    return len(_covered(region, points)) == len(points)


def _segment_meets_box(a: tuple[float, float], b: tuple[float, float], box: tuple[float, float, float, float]) -> bool:
    """Whether the segment a-b has a point in the closed box (Liang-Barsky
    clipping)."""
    south, west, north, east = box
    if max(a[0], b[0]) < south or min(a[0], b[0]) > north or max(a[1], b[1]) < west or min(a[1], b[1]) > east:
        return False
    d_lat, d_lng = b[0] - a[0], b[1] - a[1]
    t_in, t_out = 0.0, 1.0
    for p, q in ((-d_lat, a[0] - south), (d_lat, north - a[0]), (-d_lng, a[1] - west), (d_lng, east - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            t_in = max(t_in, r)
        else:
            t_out = min(t_out, r)
        if t_in > t_out:
            return False
    return True


def intersects(region: Region, bbox: tuple[float, float, float, float]) -> bool:
    """Whether region's polygon and bbox share any point, border included: a
    way crossing a border can be fuller in the neighbour's file, so a border
    tile needs both. True when a polygon edge meets the box or its center is
    inside the polygon."""
    south, west, north, east = bbox
    if not tiles.bbox_overlaps(region.bbox, bbox):
        return False
    center = ((south + north) / 2.0, (west + east) / 2.0)
    for polygon in region.polygons:
        if _in_polygon(center[0], center[1], polygon):
            return True
        for ring in polygon:
            if any(_segment_meets_box(ring[i - 1], ring[i], bbox) for i in range(len(ring))):
                return True
    return False


def contains_bbox(region: Region, bbox: tuple[float, float, float, float]) -> bool:
    """Whether region's polygon contains bbox's corners, edge midpoints, and centre."""
    return _contains(region, _grid_points(bbox, _CONTAIN_GRID))


# contains_bbox tests a coarse lattice of the box; a covering set is searched
# over a finer one.
_CONTAIN_GRID = 3
_COVER_GRID = 5
MAX_COVER_REGIONS = 4
_MAX_COVER_CANDIDATES = 24


def covering_set(regions: list[Region], bbox: tuple[float, float, float, float]) -> list[Region] | None:
    """At most MAX_COVER_REGIONS regions whose polygons together cover every
    sample point of bbox with the least total area (fewest regions on a tie),
    or None. A region covering only part of a larger-or-equal region's points
    is dropped before the search, and the search keeps the _MAX_COVER_CANDIDATES
    smallest of the rest plus the cheapest single region covering everything."""
    points = _grid_points(bbox, _COVER_GRID)
    everything = frozenset(range(len(points)))
    best_by_cover: dict[frozenset[int], tuple[float, Region]] = {}
    for region in regions:
        cover = _covered(region, points)
        if not cover:
            continue
        area = _area(region)
        if cover not in best_by_cover or area < best_by_cover[cover][0]:
            best_by_cover[cover] = (area, region)
    candidates = sorted(
        (
            (area, cover, region)
            for cover, (area, region) in best_by_cover.items()
            if not any(cover < other and other_area <= area for other, (other_area, _) in best_by_cover.items())
        ),
        key=lambda c: c[0],
    )
    kept = candidates[:_MAX_COVER_CANDIDATES]
    whole = next((c for c in candidates if c[1] == everything), None)
    if whole is not None and whole not in kept:
        kept.append(whole)
    best: tuple[float, int, tuple[Region, ...]] | None = None
    for size in range(1, MAX_COVER_REGIONS + 1):
        for combo in itertools.combinations(kept, size):
            if frozenset().union(*(cover for _, cover, _ in combo)) != everything:
                continue
            key = (sum(area for area, _, _ in combo), size, tuple(region for _, _, region in combo))
            if best is None or key[:2] < best[:2]:
                best = key
    return list(best[2]) if best is not None else None


def covering_children(regions: list[Region], parent: str, bbox: tuple[float, float, float, float]) -> list[Region] | None:
    """The regions nested under parent's path that cover bbox, or None."""
    prefix = parent.rstrip("/") + "/"
    return covering_set([r for r in regions if r.path.startswith(prefix)], bbox)


# ---------------------------------------------------------------------------
# The cached index
# ---------------------------------------------------------------------------

_parsed: tuple[str, list[Region]] | None = None


def _load(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM geofabrik_index WHERE id = 1").fetchone()


def _regions_of(row: sqlite3.Row) -> list[Region]:
    global _parsed
    if _parsed is None or _parsed[0] != row["sha256"]:
        _parsed = (row["sha256"], parse_index(gzip.decompress(row["data"])))
    return _parsed[1]


def _is_fresh(row: sqlite3.Row) -> bool:
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["checked_at"])
    return age < timedelta(days=INDEX_MAX_AGE_DAYS)


def _refresh(conn: sqlite3.Connection, row: sqlite3.Row | None) -> sqlite3.Row:
    headers = http_client.conditional_headers(row["etag"], row["last_modified"]) if row is not None else {}
    url = f"{GEOFABRIK_BASE_URL.rstrip('/')}/index-v1.json"
    now = datetime.now(timezone.utc).isoformat()

    def read(resp: object) -> tuple[bytes, str | None, str | None]:
        get = getattr(getattr(resp, "headers", None), "get", lambda _k: None)
        return resp.read(), get("ETag"), get("Last-Modified")  # type: ignore[attr-defined]

    try:
        raw, etag, last_modified = http_client.request(http_client.GEOFABRIK, url, headers=headers, reader=read)
    except urllib.error.HTTPError as e:
        if e.code == 304 and row is not None:
            conn.execute("UPDATE geofabrik_index SET checked_at = ? WHERE id = 1", (now,))
            conn.commit()
            return _load(conn)  # type: ignore[return-value]
        raise RegionError(f"could not fetch Geofabrik's region index: {e}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise RegionError(f"could not fetch Geofabrik's region index: {e}") from e
    try:
        parse_index(raw)
    except (ValueError, KeyError, TypeError) as e:
        raise RegionError(f"Geofabrik's region index was not valid GeoJSON: {e}") from e
    conn.execute(
        """
        INSERT INTO geofabrik_index (id, fetched_at, checked_at, sha256, etag, last_modified, data)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            fetched_at = excluded.fetched_at, checked_at = excluded.checked_at, sha256 = excluded.sha256,
            etag = excluded.etag, last_modified = excluded.last_modified, data = excluded.data
        """,
        (now, now, hashlib.sha256(raw).hexdigest(), etag, last_modified, gzip.compress(raw)),
    )
    conn.commit()
    return _load(conn)  # type: ignore[return-value]


def regions(conn: sqlite3.Connection) -> list[Region]:
    """Every region in the index, fetched or refreshed when missing or older
    than INDEX_MAX_AGE_DAYS; a failed refresh keeps the stale copy."""
    row = _load(conn)
    if row is None or not _is_fresh(row):
        try:
            row = _refresh(conn, row)
        except RegionError:
            if row is None:
                raise
    return _regions_of(row)


def cached_regions(conn: sqlite3.Connection) -> list[Region] | None:
    """The stored index of any age, or None if never fetched. No network."""
    row = _load(conn)
    return _regions_of(row) if row is not None else None


def _suggestions(known: list[Region], region: str) -> str:
    paths = [r.path for r in known]
    suffix = region.strip("/")
    near = [p for p in paths if p.endswith("/" + suffix)]
    near += [p for p in difflib.get_close_matches(suffix, paths, n=5, cutoff=0.8) if p not in near]
    return f"; did you mean {', '.join(sorted(near)[:5])}?" if near else ""


def check_region(known: list[Region], region: str) -> None:
    if not any(r.path == region for r in known):
        raise RegionError(
            f"unknown Geofabrik region {region!r}; use a path from "
            f"{GEOFABRIK_BASE_URL.rstrip('/')}/index-v1.json, such as continent/country{_suggestions(known, region)}"
        )


def validate_region(conn: sqlite3.Connection, region: str) -> None:
    """Refuse a region path the index does not list. With no index available the
    path is accepted and a bad one surfaces as the download's 404."""
    try:
        known = regions(conn)
    except RegionError:
        return
    check_region(known, region)


def _cover_paths(known: list[Region], bbox: tuple[float, float, float, float]) -> list[str] | None:
    found = covering_set(known, bbox)
    return [r.path for r in found] if found is not None else None


def choose_regions(conn: sqlite3.Connection, bbox: tuple[float, float, float, float]) -> list[str]:
    """The region paths to extract bbox from (see the module docstring)."""
    found = _cover_paths(regions(conn), bbox)
    if found is None:
        raise RegionError(
            "no Geofabrik region contains this whole area; import it from a .osm.pbf with miles-area-import --pbf"
        )
    return found


def choose_cached_regions(conn: sqlite3.Connection, bbox: tuple[float, float, float, float]) -> list[str] | None:
    """choose_regions from the stored index alone; None without one or a cover."""
    known = cached_regions(conn)
    return _cover_paths(known, bbox) if known is not None else None
