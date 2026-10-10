"""Drive-to trailhead starts: trailhead and trail-adjacent parking nodes in
an imported area, ranked by drive time from a point and by how much trail
they put within reach.

Candidates come straight out of network.py's net_nodes (built by
build_network, which already excludes private parking and keeps each node's
name). Drive time comes from one OSRM table call per search: candidates whose
fastest possible drive (straight line at MAX_DRIVE_KM_PER_MINUTE) already
exceeds the budget are dropped, the rest are ranked by the best score that
time could still give them, and only the most promising uncached ones are
sent. Answers are
cached by origin cell and destination node, so a repeated search from "the
same place" never re-hits OSRM. When OSRM cannot be reached, drive times are
estimated from straight-line distance, flagged as estimates, never cached,
and OSRM is left alone for OSRM_RETRY_COOLDOWN_S (kept in process memory:
one long-running API process owns the outbound calls, so a restart is the
only thing it does not survive).
"""

import http.client
import json
import math
import os
import sqlite3
import threading
import time
import urllib.error
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import cast
from typing_extensions import TypedDict

from . import areas, gpx_parse, http_client, metrics, network, routes_spatial
from .fitness import MILE_M

OSRM_BASE = os.environ.get("MILES_OSRM_URL", "https://router.project-osrm.org")

# OSRM's public table endpoint accepts roughly 100 coordinates per request;
# one of those is the origin, so a chunk holds at most this many destinations.
OSRM_MAX_DESTINATIONS_PER_CALL = 99

# Longest drive a search will consider: past this the prefilter
# admits most of the area and the result is not "a nearby trailhead" anymore.
MAX_DRIVE_MINUTES = 90.0

DRIVE_TIME_CACHE_TTL_DAYS = 180
# An unreachable answer (OSRM's null) may be a gap in the road data or a bad
# snap, so it expires sooner than a measured time.
UNREACHABLE_CACHE_TTL_DAYS = 7
ORIGIN_CACHE_GRID_M = 100.0

# Candidates sent to OSRM per search: a multiple of the number wanted back,
# so a search is one request, never more than one table call's worth.
OSRM_CANDIDATES_PER_RESULT = 3

# After a failed OSRM request, drive times are estimated for this long
# instead of trying again.
OSRM_RETRY_COOLDOWN_S = 600.0
# The estimate used when OSRM is down: straight-line distance, lengthened by
# this factor, driven at areas.DRIVE_KM_PER_MINUTE.
STRAIGHT_LINE_DETOUR = 1.4
# The prefilter and ranking bound: no road is driven faster than this on
# average, so a straight line at this speed never overestimates the real time
# and a reachable trailhead is not dropped before OSRM has been asked.
MAX_DRIVE_KM_PER_MINUTE = 2.0

DEDUPE_RADIUS_M = 300.0
PARKING_TRAIL_PROXIMITY_M = 150.0
TRAIL_NEARBY_RADIUS_M = 1000.0

# Edges are resampled to this spacing (network.py), so counting a trail
# edge's sample points within TRAIL_NEARBY_RADIUS_M and multiplying by this
# constant approximates the length of trail actually inside the radius --
# unlike crediting an edge's full length whenever any one of its points
# qualifies, which lets a parking lot near just the tip of a long trail
# outscore a lot genuinely surrounded by short ones.
_TRAIL_SAMPLE_SPACING_M = gpx_parse.RESAMPLE_SPACING_M

# Ranking blend: one minute of extra drive time costs as much as losing this
# many meters of nearby trail. Arbitrary but fixed, so results are stable.
DRIVE_MINUTE_TRAIL_TRADEOFF_M = 200.0

# A mapped trailhead is a curated, verified access point; an unlabeled
# parking lot only qualifies by proximity to trail-class edges, which can
# include a lot that's adjacent to a trail in name only (a short connector
# spur, a service road). Worth this much nearby trail in the ranking so a
# real trailhead isn't edged out by a lot with a marginally larger
# (but less trustworthy) proximity count.
TRAILHEAD_BONUS_M = 400.0

_METERS_PER_DEG_LAT = 111320.0


class DriveStart(TypedDict):
    lat: float
    lng: float
    label: str
    drive_s: float
    kind: str
    osm_node_id: int | None
    trail_m_nearby: float
    # True when drive_s is a straight-line estimate because OSRM was
    # unavailable.
    drive_time_estimated: bool


def init_drive_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drive_time_cache (
            origin_lat_r REAL NOT NULL,
            origin_lng_r REAL NOT NULL,
            dest_node_id INTEGER NOT NULL,
            drive_s      REAL,
            fetched_at   TEXT NOT NULL,
            PRIMARY KEY (origin_lat_r, origin_lng_r, dest_node_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_drive_time_cache_fetched ON drive_time_cache(fetched_at)")


# ---------------------------------------------------------------------------
# Candidate gathering
# ---------------------------------------------------------------------------


def _load_trail_edges(
    conn: sqlite3.Connection, area_id: int
) -> tuple[list[tuple[str | None, float]], routes_spatial.NearestIndex]:
    """Every trail-class edge in an area as (way_name, length_m), plus an
    index (keyed by position in that list) over its 20m samples (already
    stored on the edge) for straight-line proximity and nearby-trail-distance
    queries -- cheaper than a graph walk and accurate enough at the
    ~150m/~1km radii those queries use."""
    rows = conn.execute(
        "SELECT edge_id, way_id, way_name, highway, surface_class, length_m, points_blob FROM net_edges WHERE area_id = ?",
        [area_id],
    ).fetchall()

    trail_edges: list[tuple[str | None, float]] = []
    index = routes_spatial.NearestIndex(cell_size_m=TRAIL_NEARBY_RADIUS_M)
    for row in rows:
        if not network.is_trail_class(row["highway"], row["surface_class"]):
            continue
        if row["way_id"] < 0:
            # A synthetic access spur (network.py splices one in when an
            # amenity sits off the trail it snapped to) -- real trail
            # mileage and names for a candidate's own label shouldn't come
            # from the short unsurveyed line connecting it to the network.
            continue
        trail_edges.append((row["way_name"], row["length_m"]))
        network.index_edge(index, len(trail_edges) - 1, row["points_blob"])

    return trail_edges, index


def _nearby_trail_info(
    trail_edges: list[tuple[str | None, float]], index: routes_spatial.NearestIndex, lat: float, lng: float
) -> tuple[float | None, str | None, float]:
    """(distance to the nearest trail-class edge, that edge's name,
    approximate length of trail actually within TRAIL_NEARBY_RADIUS_M) --
    one index query serves both the parking proximity filter and the
    trail_m_nearby quality signal. The length is approximated from the
    count of in-radius sample points per edge rather than an edge's full
    length whenever it has any qualifying point, since a trail's far end
    dipping into the radius shouldn't count the whole trail as nearby."""
    hits = list(index.within(lat, lng, TRAIL_NEARBY_RADIUS_M))
    hits_by_edge = Counter(cast(int, key) for key, _offset, _d in hits)
    trail_m_nearby = sum(
        min(count * _TRAIL_SAMPLE_SPACING_M, trail_edges[edge_idx][1]) for edge_idx, count in hits_by_edge.items()
    )
    if not hits:
        return None, None, trail_m_nearby
    nearest_key, _offset, best_dist = min(hits, key=lambda hit: hit[2])
    return best_dist, trail_edges[cast(int, nearest_key)][0], trail_m_nearby


def _label(name: str | None, kind: str, nearest_trail_name: str | None) -> str:
    if name:
        return name
    if nearest_trail_name:
        return f"Parking near {nearest_trail_name}" if kind == "parking" else nearest_trail_name
    return "Parking near trail" if kind == "parking" else "Trailhead"


def _gather_candidates(
    conn: sqlite3.Connection,
    area_id: int,
    trail_edges: list[tuple[str | None, float]],
    index: routes_spatial.NearestIndex,
) -> list[dict]:
    candidates: list[dict] = []
    for kind in ("trailhead", "parking"):
        rows = conn.execute(
            "SELECT osm_node_id, name, lat, lng FROM net_nodes WHERE area_id = ? AND kind = ?",
            [area_id, kind],
        ).fetchall()
        for row in rows:
            nearest_dist, nearest_name, trail_m_nearby = _nearby_trail_info(trail_edges, index, row["lat"], row["lng"])
            if kind == "parking" and (nearest_dist is None or nearest_dist > PARKING_TRAIL_PROXIMITY_M):
                continue
            candidates.append({
                "lat": row["lat"],
                "lng": row["lng"],
                "label": _label(row["name"], kind, nearest_name),
                "kind": kind,
                "osm_node_id": row["osm_node_id"],
                "trail_m_nearby": trail_m_nearby,
            })
    return candidates


def _dedupe(candidates: list[dict], radius_m: float) -> list[dict]:
    """Greedily keep the better-trailed candidate of any pair within
    radius_m -- two trailhead/parking nodes at the same lot shouldn't both
    become starts."""
    ordered = sorted(candidates, key=lambda c: c["trail_m_nearby"], reverse=True)
    kept: list[dict] = []
    for c in ordered:
        if all(routes_spatial.equirect_distance_m(c["lat"], c["lng"], k["lat"], k["lng"]) > radius_m for k in kept):
            kept.append(c)
    return kept


# ---------------------------------------------------------------------------
# Drive time: origin rounding, cache, OSRM table calls
# ---------------------------------------------------------------------------


def _round_origin(lat: float, lng: float) -> tuple[int, int]:
    """The grid cell, about ORIGIN_CACHE_GRID_M wide, an origin falls in, as
    integer (row, column) indices, so repeat searches from "the same place"
    reuse cached drive times. A column is that many degrees of longitude wide
    at the cell's own latitude, not the origin's: two origins in one row must
    agree on the width or they would key different columns."""
    lat_step = ORIGIN_CACHE_GRID_M / _METERS_PER_DEG_LAT
    row = round(lat / lat_step)
    lng_step = ORIGIN_CACHE_GRID_M / (_METERS_PER_DEG_LAT * max(math.cos(math.radians(row * lat_step)), 1e-6))
    return (row, round(lng / lng_step))


def _cache_lookup(
    conn: sqlite3.Connection, origin_key: tuple[int, int], node_ids: list[int]
) -> dict[int, float | None]:
    if not node_ids:
        return {}
    now = datetime.now(timezone.utc)
    measured_cutoff = (now - timedelta(days=DRIVE_TIME_CACHE_TTL_DAYS)).isoformat()
    unreachable_cutoff = (now - timedelta(days=UNREACHABLE_CACHE_TTL_DAYS)).isoformat()
    placeholders = ",".join("?" for _ in node_ids)
    rows = conn.execute(
        f"""
        SELECT dest_node_id, drive_s FROM drive_time_cache
        WHERE origin_lat_r = ? AND origin_lng_r = ? AND dest_node_id IN ({placeholders})
          AND fetched_at >= CASE WHEN drive_s IS NULL THEN ? ELSE ? END
        """,
        [origin_key[0], origin_key[1], *node_ids, unreachable_cutoff, measured_cutoff],
    ).fetchall()
    found = {r["dest_node_id"]: r["drive_s"] for r in rows}
    metrics.cache("drive_time", "hit", len(found))
    metrics.cache("drive_time", "miss", len(node_ids) - len(found))
    return found


_EXPIRE_INTERVAL_S = 3600.0
_last_expired_at: float | None = None
_expire_lock = threading.Lock()


def _expire_cache(conn: sqlite3.Connection) -> None:
    """Delete expired cache rows, at most once an hour per process: the TTLs
    are days, and the delete takes a write lock."""
    global _last_expired_at
    with _expire_lock:
        if _last_expired_at is not None and time.monotonic() - _last_expired_at < _EXPIRE_INTERVAL_S:
            return
        _last_expired_at = time.monotonic()
    now = datetime.now(timezone.utc)
    conn.execute(
        "DELETE FROM drive_time_cache WHERE fetched_at < CASE WHEN drive_s IS NULL THEN ? ELSE ? END",
        [
            (now - timedelta(days=UNREACHABLE_CACHE_TTL_DAYS)).isoformat(),
            (now - timedelta(days=DRIVE_TIME_CACHE_TTL_DAYS)).isoformat(),
        ],
    )
    conn.commit()


def _cache_store(conn: sqlite3.Connection, origin_key: tuple[int, int], results: dict[int, float | None]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO drive_time_cache (origin_lat_r, origin_lng_r, dest_node_id, drive_s, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(origin_lat_r, origin_lng_r, dest_node_id) DO UPDATE SET
            drive_s = excluded.drive_s, fetched_at = excluded.fetched_at
        """,
        [(origin_key[0], origin_key[1], node_id, drive_s, now) for node_id, drive_s in results.items()],
    )
    conn.commit()


def _fetch_with_backoff(url: str) -> bytes:
    return http_client.request(http_client.OSRM, url)


def _osrm_table_url(origin: tuple[float, float], destinations: list[tuple[float, float]]) -> str:
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in [origin, *destinations])
    return f"{OSRM_BASE}/table/v1/driving/{coords}?sources=0&annotations=duration"


def _fetch_durations(url: str) -> list[float | None] | None:
    """Per-destination durations (seconds; None where OSRM reports a
    destination unreachable), in request order -- or None if the whole call
    failed outright, so the caller can fall back rather than raise."""
    try:
        raw = _fetch_with_backoff(url)
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("code") != "Ok":
        return None
    durations = payload.get("durations")
    if not durations:
        return None
    return list(durations[0][1:])  # index 0 is the origin-to-origin entry


_osrm_unavailable_until = 0.0


def _osrm_available() -> bool:
    return time.monotonic() >= _osrm_unavailable_until


def _note_osrm_failure() -> None:
    global _osrm_unavailable_until
    _osrm_unavailable_until = time.monotonic() + OSRM_RETRY_COOLDOWN_S


def _straight_line_km(origin_lat: float, origin_lng: float, c: dict) -> float:
    return routes_spatial.equirect_distance_m(origin_lat, origin_lng, c["lat"], c["lng"]) / 1000.0


def _fastest_possible_s(origin_lat: float, origin_lng: float, c: dict) -> float:
    """Drive seconds if the road were straight and driven at
    MAX_DRIVE_KM_PER_MINUTE: a lower bound on the real time, used for ranking
    and filtering."""
    return _straight_line_km(origin_lat, origin_lng, c) / MAX_DRIVE_KM_PER_MINUTE * 60.0


def _estimated_s(origin_lat: float, origin_lng: float, c: dict) -> float:
    km = _straight_line_km(origin_lat, origin_lng, c) * STRAIGHT_LINE_DETOUR
    return km / areas.DRIVE_KM_PER_MINUTE * 60.0


class _DriveTime(TypedDict):
    drive_s: float
    estimated: bool


def _resolve_drive_times(
    conn: sqlite3.Connection, origin_lat: float, origin_lng: float, ranked: list[dict], limit: int
) -> dict[int, _DriveTime | None]:
    """Drive time per candidate in `ranked` (best first): None for a
    destination OSRM calls unreachable. At most `limit` candidates missing
    from the cache are sent to OSRM, in one request, the best-ranked first;
    candidates beyond that and not cached are left out of the result. If the
    request fails (or OSRM was down recently), the ones that would have been
    sent get straight-line estimates instead."""
    origin_key = _round_origin(origin_lat, origin_lng)
    _expire_cache(conn)
    cached = _cache_lookup(conn, origin_key, [c["osm_node_id"] for c in ranked])
    result: dict[int, _DriveTime | None] = {
        node_id: None if drive_s is None else {"drive_s": drive_s, "estimated": False}
        for node_id, drive_s in cached.items()
    }

    to_ask = [c for c in ranked if c["osm_node_id"] not in cached][:min(limit, OSRM_MAX_DESTINATIONS_PER_CALL)]
    if not to_ask:
        return result
    durations = None
    if _osrm_available():
        url = _osrm_table_url((origin_lat, origin_lng), [(c["lat"], c["lng"]) for c in to_ask])
        durations = _fetch_durations(url)
        if durations is None or len(durations) != len(to_ask):
            durations = None
            _note_osrm_failure()
    if durations is None:
        # An estimate is not an answer: it stays out of the cache so the
        # next search, once OSRM is back, asks for real times.
        for c in to_ask:
            result[c["osm_node_id"]] = {"drive_s": _estimated_s(origin_lat, origin_lng, c), "estimated": True}
        return result

    # Only real OSRM answers go in the cache; an unreachable destination
    # (OSRM's own null) is one.
    fresh = {c["osm_node_id"]: d for c, d in zip(to_ask, durations)}
    _cache_store(conn, origin_key, fresh)
    for node_id, d in fresh.items():
        result[node_id] = None if d is None else {"drive_s": d, "estimated": False}
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _rank_score(candidate: dict, drive_s: float) -> float:
    bonus = TRAILHEAD_BONUS_M if candidate["kind"] == "trailhead" else 0.0
    return candidate["trail_m_nearby"] + bonus - (drive_s / 60.0) * DRIVE_MINUTE_TRAIL_TRADEOFF_M


def drive_starts(
    conn: sqlite3.Connection,
    area_id: int,
    origin_lat: float,
    origin_lng: float,
    max_drive_minutes: float,
    k: int = 8,
) -> list[DriveStart]:
    """Trailheads and trail-adjacent parking in area_id reachable within
    max_drive_minutes of (origin_lat, origin_lng), best K first, with the
    origin itself always offered first as an out-the-door start. Raises
    ValueError for a max_drive_minutes outside (0, MAX_DRIVE_MINUTES]."""
    if not 0 < max_drive_minutes <= MAX_DRIVE_MINUTES:
        raise ValueError(f"max_drive_minutes must be above 0 and at most {MAX_DRIVE_MINUTES:g}")
    max_drive_s = max_drive_minutes * 60.0

    trail_edges, index = _load_trail_edges(conn, area_id)
    candidates = _dedupe(_gather_candidates(conn, area_id, trail_edges, index), DEDUPE_RADIUS_M)
    survivors = [c for c in candidates if _fastest_possible_s(origin_lat, origin_lng, c) <= max_drive_s]
    # No candidate can score better than it would at its fastest possible
    # time, so this order decides which ones are worth an OSRM lookup.
    survivors.sort(key=lambda c: _rank_score(c, _fastest_possible_s(origin_lat, origin_lng, c)), reverse=True)

    times = _resolve_drive_times(
        conn, origin_lat, origin_lng, survivors, max(k, 1) * OSRM_CANDIDATES_PER_RESULT
    )

    scored: list[dict] = []
    for c in survivors:
        timed = times.get(c["osm_node_id"])
        if timed is None or timed["drive_s"] > max_drive_s:
            continue
        scored.append({**c, "drive_s": timed["drive_s"], "drive_time_estimated": timed["estimated"]})
    scored.sort(key=lambda c: _rank_score(c, c["drive_s"]), reverse=True)

    _dist, _name, origin_trail_m_nearby = _nearby_trail_info(trail_edges, index, origin_lat, origin_lng)
    origin_start: DriveStart = {
        "lat": origin_lat,
        "lng": origin_lng,
        "label": "Out the door",
        "drive_s": 0.0,
        "kind": "origin",
        "osm_node_id": None,
        "trail_m_nearby": origin_trail_m_nearby,
        "drive_time_estimated": False,
    }

    top: list[DriveStart] = [
        {
            "lat": c["lat"],
            "lng": c["lng"],
            "label": c["label"],
            "drive_s": c["drive_s"],
            "kind": c["kind"],
            "osm_node_id": c["osm_node_id"],
            "trail_m_nearby": c["trail_m_nearby"],
            "drive_time_estimated": c["drive_time_estimated"],
        }
        for c in scored[:k]
    ]
    return [origin_start] + top


def athlete_view(start: DriveStart) -> dict:
    """A start in the units the HTTP and MCP surfaces report: minutes and miles."""
    return {
        "lat": start["lat"],
        "lng": start["lng"],
        "label": start["label"],
        "drive_minutes": start["drive_s"] / 60.0,
        "kind": start["kind"],
        "osm_node_id": start["osm_node_id"],
        "trail_miles_nearby": start["trail_m_nearby"] / MILE_M,
        "drive_time_estimated": start["drive_time_estimated"],
    }
