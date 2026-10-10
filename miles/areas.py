"""Area import: the athlete names a place (or a point), and the tiles
covering it get cut from a regional OpenStreetMap extract and a DEM and
cached locally, so a trail/road graph can later be built over an area
without ever making its own HTTP calls.

An area is a lens onto the tile cache (tilecache.py's map_tiles, keyed by
tiles.py's fixed grid), not a copy of it -- two overlapping areas share
whatever tiles they have in common. Areas themselves follow the same
raw-data-is-ground-truth split as everything else in this project: an
area's own row (center, radius, label, pinned) is athlete-authored, while
its tile membership and import status are maintained by the background
import job and safe to recompute.

Refresh policy: map data is refreshed only when asked (import_area with
refresh=True); nothing ages out on its own. An area reports the date its map
data is as of and, once that is older than REFRESH_SUGGESTED_AFTER_DAYS, a
refresh_suggested flag. Pinning protects an area from garbage collection: an
unpinned area untouched for UNPINNED_GC_DAYS is deleted outright on the next
import_area call (any area's), and any tile no area references any longer
is deleted alongside it.
"""

import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Callable
from typing_extensions import NotRequired, TypedDict

from . import db, elevation, extracts, geocode, geofabrik, metrics, network, tilecache, tiles
from .errors import describe_error

# Rough throughput of a filtered pass over an extract, used only to estimate
# an import's expected seconds, never to gate anything.
EXTRACT_CUT_MB_PER_S = 13.7

DRIVE_KM_PER_MINUTE = 1.0
# Largest import radius, for an explicit radius_km and for the drive-minutes
# heuristic alike: a bigger area is a much bigger cut, elevation prefetch and
# graph for a search that never needs to look that far.
MAX_RADIUS_KM = 60.0

REFRESH_SUGGESTED_AFTER_DAYS = 180.0
UNPINNED_GC_DAYS = 60.0


_import_lock = threading.Lock()


class AreaNotFoundError(Exception):
    pass


class AreaImportError(Exception):
    pass


class AreaSummary(TypedDict):
    area_id: int
    label: str
    center_lat: float
    center_lng: float
    radius_km: float
    pinned: bool
    status: str
    created_at: str
    last_used_at: str
    imported_at: str | None


class AreaDetail(AreaSummary):
    drive_minutes: float | None
    tile_count: int
    region: NotRequired[str | None]
    data_as_of: str | None
    refresh_suggested: bool


class ImportEstimate(TypedDict):
    # "extract:<region>" for a registered file, "pending-extract:<region>"
    # for one still to be downloaded (several regions comma-separated for an
    # area cut from more than one extract), "pending-extract" when the region
    # is not known without fetching Geofabrik's index.
    source: str
    tile_count: int
    # None while the extract still has to be downloaded.
    estimated_seconds: float | None


class AreaStatus(TypedDict):
    area_id: int
    label: str
    status: str  # "new" | "importing" | "ready" | "error"
    phase: NotRequired[str | None]
    done: NotRequired[int | None]
    total: NotRequired[int | None]
    error: NotRequired[str | None]
    estimate: NotRequired[ImportEstimate]
    # Present when the center was geocoded from a place name: Nominatim's
    # display name for it, and the attribution its data licence requires.
    geocode: NotRequired[geocode.GeocodeResult]
    data_as_of: NotRequired[str | None]
    refresh_suggested: NotRequired[bool]


def init_area_schema(conn: sqlite3.Connection) -> None:
    # source is "extract:<region>"; rows written before extracts were the only
    # map source may say "overpass" and stay readable. inputs is a JSON object
    # of region -> fetched_at of the extract files a tile was cut from, NULL
    # for tiles uploaded or written before it existed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS map_tiles (
            gx            INTEGER NOT NULL,
            gy            INTEGER NOT NULL,
            query_version INTEGER NOT NULL,
            fetched_at    TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            data          BLOB NOT NULL,
            source        TEXT NOT NULL DEFAULT 'overpass',
            PRIMARY KEY (gx, gy)
        )
    """)
    db.add_missing_columns(conn, "map_tiles", "source TEXT NOT NULL DEFAULT 'overpass'", "inputs TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS elevation_tiles (
            provider   TEXT NOT NULL,
            zoom       INTEGER NOT NULL,
            x          INTEGER NOT NULL,
            y          INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            data       BLOB NOT NULL,
            PRIMARY KEY (provider, zoom, x, y)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            query        TEXT PRIMARY KEY,
            lat          REAL NOT NULL,
            lng          REAL NOT NULL,
            display_name TEXT,
            fetched_at   TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS geocode_misses (
            query      TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS areas (
            area_id            INTEGER PRIMARY KEY,
            label              TEXT NOT NULL,
            center_lat         REAL NOT NULL,
            center_lng         REAL NOT NULL,
            radius_km          REAL NOT NULL,
            drive_minutes      REAL,
            elevation_provider TEXT NOT NULL DEFAULT 'terrarium',
            pinned             INTEGER NOT NULL DEFAULT 0,
            status             TEXT NOT NULL DEFAULT 'new',
            status_phase       TEXT,
            status_done        INTEGER,
            status_total       INTEGER,
            status_error       TEXT,
            created_at         TEXT NOT NULL,
            last_used_at       TEXT NOT NULL,
            imported_at        TEXT,
            region             TEXT
        )
    """)
    db.add_missing_columns(conn, "areas", "region TEXT")
    # No FK on (gx, gy) -> map_tiles: a row here is written before the tile
    # fetch that creates its map_tiles row completes (see _do_import), and a
    # tile can also be evicted by gc_tiles once no area references it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS area_tiles (
            area_id INTEGER NOT NULL REFERENCES areas(area_id),
            gx      INTEGER NOT NULL,
            gy      INTEGER NOT NULL,
            PRIMARY KEY (area_id, gx, gy)
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Row <-> dict helpers
# ---------------------------------------------------------------------------


def _get_area_row(conn: sqlite3.Connection, area_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM areas WHERE area_id = ?", (area_id,)).fetchone()
    if row is None:
        raise AreaNotFoundError(f"no area {area_id}")
    return row


def _row_to_summary(row: sqlite3.Row) -> AreaSummary:
    return {
        "area_id": row["area_id"],
        "label": row["label"],
        "center_lat": row["center_lat"],
        "center_lng": row["center_lng"],
        "radius_km": row["radius_km"],
        "pinned": bool(row["pinned"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "imported_at": row["imported_at"],
    }


def _row_to_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> AreaDetail:
    tile_count = conn.execute(
        "SELECT COUNT(*) FROM area_tiles WHERE area_id = ?", (row["area_id"],)
    ).fetchone()[0]
    summary = _row_to_summary(row)
    data_as_of, refresh_suggested = area_freshness(conn, row["area_id"])
    return {
        **summary,
        "drive_minutes": row["drive_minutes"],
        "tile_count": tile_count,
        "region": row["region"],
        "data_as_of": data_as_of,
        "refresh_suggested": refresh_suggested,
    }


def area_freshness(conn: sqlite3.Connection, area_id: int) -> tuple[str | None, bool]:
    """(data_as_of, refresh_suggested) for an area: the oldest date any of its
    tiles' data is as of -- the oldest of the extracts' own dates for tiles cut
    from one or several, the cut time for tiles from an uploaded cut -- and
    whether that is older than REFRESH_SUGGESTED_AFTER_DAYS. (None, False)
    before any tile is cached."""
    rows = conn.execute(
        """
        SELECT mt.source, MIN(mt.fetched_at) AS fetched_at
        FROM area_tiles atl
        JOIN map_tiles mt ON mt.gx = atl.gx AND mt.gy = atl.gy
        WHERE atl.area_id = ?
        GROUP BY mt.source
        """,
        (area_id,),
    ).fetchall()
    dates: list[str] = []
    for row in rows:
        regions = extracts.regions_of_source(row["source"])
        records = [extracts.get_extract(conn, r) for r in regions]
        dates.append(min([r["data_as_of"] for r in records if r is not None], default=row["fetched_at"]))
    as_of = min(dates, default=None)
    if as_of is None:
        return None, False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(as_of)
    return as_of, age > timedelta(days=REFRESH_SUGGESTED_AFTER_DAYS)


def _row_to_status(row: sqlite3.Row) -> AreaStatus:
    return {
        "area_id": row["area_id"],
        "label": row["label"],
        "status": row["status"],
        "phase": row["status_phase"],
        "done": row["status_done"],
        "total": row["status_total"],
        "error": row["status_error"],
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def get_area(conn: sqlite3.Connection, area_id: int) -> AreaDetail:
    return _row_to_detail(conn, _get_area_row(conn, area_id))


def rebuild_stale_network(conn: sqlite3.Connection, area_id: int) -> bool:
    """Rebuild an area's network from its cached tiles when it was built by
    an older network.NETWORK_DERIVE_VERSION -- no extract fetch,
    the same job import_tiles runs. Returns True while the stale graph is
    being replaced (started by this call or already running), which callers
    must not search or treat as ready."""
    if not network.graph_is_stale(conn, area_id):
        return False
    if _try_start_import(conn, area_id):
        threading.Thread(target=_run_finish_only_job, args=(area_id,), daemon=True).start()
    return True


def get_area_status(conn: sqlite3.Connection, area_id: int) -> AreaStatus:
    _get_area_row(conn, area_id)
    rebuild_stale_network(conn, area_id)
    status = _row_to_status(_get_area_row(conn, area_id))
    status["data_as_of"], status["refresh_suggested"] = area_freshness(conn, area_id)
    return status


def list_areas(conn: sqlite3.Connection) -> list[AreaSummary]:
    rows = conn.execute("SELECT * FROM areas ORDER BY last_used_at DESC").fetchall()
    return [_row_to_summary(r) for r in rows]


def pin_area(conn: sqlite3.Connection, area_id: int) -> AreaDetail:
    _get_area_row(conn, area_id)
    conn.execute("UPDATE areas SET pinned = 1 WHERE area_id = ?", (area_id,))
    conn.commit()
    return get_area(conn, area_id)


def unpin_area(conn: sqlite3.Connection, area_id: int) -> AreaDetail:
    _get_area_row(conn, area_id)
    conn.execute("UPDATE areas SET pinned = 0 WHERE area_id = ?", (area_id,))
    conn.commit()
    return get_area(conn, area_id)


# ---------------------------------------------------------------------------
# Garbage collection
# ---------------------------------------------------------------------------


def gc_areas_and_tiles(conn: sqlite3.Connection) -> None:
    """Delete unpinned areas untouched for UNPINNED_GC_DAYS and any map tile
    no remaining area references. Run opportunistically at the start of
    import_area rather than on a schedule -- there's no daemon here."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=UNPINNED_GC_DAYS)).isoformat()
    stale_ids = [
        r["area_id"]
        for r in conn.execute(
            "SELECT area_id FROM areas WHERE pinned = 0 AND last_used_at < ?", (cutoff,)
        ).fetchall()
    ]
    for area_id in stale_ids:
        conn.execute("DELETE FROM area_tiles WHERE area_id = ?", (area_id,))
        conn.execute("DELETE FROM net_edges WHERE area_id = ?", (area_id,))
        conn.execute("DELETE FROM net_nodes WHERE area_id = ?", (area_id,))
        conn.execute("DELETE FROM net_graphs WHERE area_id = ?", (area_id,))
        conn.execute("DELETE FROM areas WHERE area_id = ?", (area_id,))
    if stale_ids:
        conn.commit()

    conn.execute("""
        DELETE FROM map_tiles
        WHERE NOT EXISTS (
            SELECT 1 FROM area_tiles
            WHERE area_tiles.gx = map_tiles.gx AND area_tiles.gy = map_tiles.gy
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _resolve_radius_km(radius_km: float | None, drive_minutes: float | None) -> float:
    if radius_km is not None:
        if not 0 < radius_km <= MAX_RADIUS_KM:
            raise ValueError(f"radius_km must be above 0 and at most {MAX_RADIUS_KM:g}")
        return radius_km
    if drive_minutes is not None:
        # A flat km-per-minute stand-in for a real drive-time isochrone,
        # capped so a long drive estimate doesn't request a wildly
        # oversized import.
        return min(drive_minutes * DRIVE_KM_PER_MINUTE, MAX_RADIUS_KM)
    raise ValueError("import_area needs radius_km or drive_minutes")


def _resolve_center(
    conn: sqlite3.Connection, place: str | None, lat: float | None, lng: float | None, refresh_geocode: bool = False
) -> tuple[float, float, geocode.GeocodeResult | None]:
    """(lat, lng, geocode result when the center came from a place name)."""
    if place is not None:
        try:
            found = geocode.lookup(conn, place, refresh=refresh_geocode)
        except geocode.GeocodeError as e:
            raise AreaImportError(str(e)) from e
        return (found["lat"], found["lng"], found)
    if lat is not None and lng is not None:
        return (lat, lng, None)
    raise ValueError("import_area needs place, or lat and lng")


def _try_start_import(conn: sqlite3.Connection, area_id: int) -> bool:
    """Atomically flip an area to 'importing' unless an import already holds
    it. SQLite serializes writers, so two callers racing this UPDATE can't
    both see rowcount > 0."""
    cur = conn.execute(
        """
        UPDATE areas SET status = 'importing', status_phase = NULL,
            status_done = NULL, status_total = NULL, status_error = NULL
        WHERE area_id = ? AND status != 'importing'
        """,
        (area_id,),
    )
    conn.commit()
    return cur.rowcount > 0



def _estimate_import(conn: sqlite3.Connection, bbox: tuple[float, float, float, float], region: str | None) -> ImportEstimate:
    """Estimate an import's source, unit of work, and expected seconds --
    never triggers a download or an index fetch, just checks what is already
    registered and cached."""
    needed = tiles.tiles_in_bbox(*bbox)
    planned = extracts.planned_regions(conn, bbox, region)
    records = [extracts.get_extract(conn, path) for path in planned]
    if records and all(r is not None and os.path.isfile(r["path"]) for r in records):
        size_mb = sum(os.path.getsize(r["path"]) for r in records if r is not None) / (1024 * 1024)
        return {
            "source": extracts.source_of_regions(planned),
            "tile_count": len(needed),
            "estimated_seconds": max(5.0, size_mb / EXTRACT_CUT_MB_PER_S),
        }
    chosen = ",".join(planned)
    return {
        "source": f"pending-extract:{chosen}" if chosen else "pending-extract",
        "tile_count": len(needed),
        "estimated_seconds": None,
    }


def import_area(
    conn: sqlite3.Connection,
    *,
    place: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float | None = None,
    drive_minutes: float | None = None,
    label: str,
    pinned: bool | None = None,
    region: str | list[str] | None = None,
    refresh: bool = False,
    refresh_geocode: bool = False,
) -> AreaStatus:
    """Create (or reuse, keyed by label) an area and start a background
    import unless one is already running. Returns immediately with the
    area's current status plus an import estimate -- poll get_area_status
    for progress.

    region is a Geofabrik path (e.g. "continent/country"), or several as a list
    or comma-separated, the athlete has told us to use for this place; it's
    used only when no already-registered extract covers the bbox, and
    downloaded on demand. Without one, the cheapest set of Geofabrik regions
    covering the area is chosen (several at a border). A path Geofabrik's
    index does not list is refused (checked at once when the index has been
    fetched before, else by the background job).
    pinned and region are left as they are on an existing area when None,
    so re-importing never silently unpins an area or forgets its region.
    Giving no place, coordinates, or size for an existing label reuses the
    area's stored center and radius.

    Cached map data is reused as it is. refresh=True re-fetches it: a
    registered extract is re-downloaded if Geofabrik has a newer one (at most
    once a day per region). refresh_geocode=True looks `place` up in
    Nominatim again instead of trusting the cached answer."""
    if not label:
        raise ValueError("import_area needs a label")
    region_paths = extracts.split_regions(region)
    region = ",".join(region_paths) or None
    known = geofabrik.cached_regions(conn) if region_paths else None
    if known is not None:
        for path in region_paths:
            if extracts.get_extract(conn, path) is None:
                try:
                    geofabrik.check_region(known, path)
                except geofabrik.RegionError as e:
                    raise ValueError(str(e)) from e
    gc_areas_and_tiles(conn)

    existing = conn.execute("SELECT * FROM areas WHERE label = ?", (label,)).fetchone()
    geocoded: geocode.GeocodeResult | None = None
    if (
        existing is not None
        and place is None and lat is None and lng is None and radius_km is None and drive_minutes is None
    ):
        center_lat, center_lng = existing["center_lat"], existing["center_lng"]
        resolved_radius, drive_minutes = existing["radius_km"], existing["drive_minutes"]
    else:
        center_lat, center_lng, geocoded = _resolve_center(conn, place, lat, lng, refresh_geocode)
        resolved_radius = _resolve_radius_km(radius_km, drive_minutes)
    now = datetime.now(timezone.utc).isoformat()

    if existing is not None:
        area_id = int(existing["area_id"])
        conn.execute(
            """
            UPDATE areas SET center_lat = ?, center_lng = ?, radius_km = ?, drive_minutes = ?,
                pinned = COALESCE(?, pinned), region = COALESCE(?, region), last_used_at = ?
            WHERE area_id = ?
            """,
            (
                center_lat, center_lng, resolved_radius, drive_minutes,
                None if pinned is None else int(pinned), region, now, area_id,
            ),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO areas (
                label, center_lat, center_lng, radius_km, drive_minutes,
                pinned, region, status, created_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (label, center_lat, center_lng, resolved_radius, drive_minutes, int(bool(pinned)), region, now, now),
        )
        area_id = int(cur.lastrowid)  # type: ignore[arg-type]
    conn.commit()

    bbox = tiles.bbox_for_center_radius(center_lat, center_lng, resolved_radius)
    estimate = _estimate_import(conn, bbox, region)

    if _try_start_import(conn, area_id):
        threading.Thread(target=_run_import_job, args=(area_id, refresh), daemon=True).start()
    else:
        conn.execute("UPDATE areas SET last_used_at = ? WHERE area_id = ?", (now, area_id))
        conn.commit()
    status = get_area_status(conn, area_id)
    status["estimate"] = estimate
    if geocoded is not None:
        status["geocode"] = geocoded
    return status


def refresh_area(conn: sqlite3.Connection, label: str, *, force_extract: bool = False) -> AreaStatus:
    """Re-fetch an existing area's map data now, in this thread, and return
    its final status. For the CLI; the API and MCP tool start the same job in
    the background through import_area(refresh=True). force_extract replaces
    the area's regional extract unconditionally, for a file that is bad but
    still registered."""
    row = conn.execute("SELECT area_id FROM areas WHERE label = ?", (label,)).fetchone()
    if row is None:
        raise AreaNotFoundError(f"no area labeled {label!r}")
    area_id = int(row["area_id"])
    if not _try_start_import(conn, area_id):
        raise AreaImportError(f"area {label!r} is already importing")
    _run_import_job(area_id, True, force_extract)
    return get_area_status(conn, area_id)


def _set_progress(conn: sqlite3.Connection, area_id: int, *, phase: str, done: int, total: int | None) -> None:
    conn.execute(
        "UPDATE areas SET status_phase = ?, status_done = ?, status_total = ? WHERE area_id = ?",
        (phase, done, total, area_id),
    )
    conn.commit()


def select_area_extracts(
    conn: sqlite3.Connection, area_id: int, refresh: bool = False, force_extract: bool = False
) -> list[extracts.ExtractRecord]:
    """The extracts to cut area_id from (extracts.select_extracts), downloading
    any that are missing. While another import holds a region's download the
    area's phase reads "waiting-for-download"; while this one downloads
    a region itself it reads "downloading" with the bytes received (the total is None
    when the server declared no size)."""
    area = _get_area_row(conn, area_id)
    bbox = tiles.bbox_for_center_radius(area["center_lat"], area["center_lng"], area["radius_km"])

    def downloading(written: int, expected: int | None) -> None:
        _set_progress(conn, area_id, phase="downloading", done=written, total=expected)

    return extracts.select_extracts(
        conn,
        bbox,
        area["region"],
        refresh=refresh,
        force=force_extract,
        on_wait=lambda: _set_progress(conn, area_id, phase="waiting-for-download", done=0, total=1),
        on_progress=downloading,
    )


def _fetch_tiles_phase(
    conn: sqlite3.Connection,
    area_id: int,
    area: sqlite3.Row,
    refresh: bool = False,
    force_extract: bool = False,
    selected: list[extracts.ExtractRecord] | None = None,
) -> None:
    """Fill map_tiles for everything area's bbox needs from the extracts
    extracts.select_extracts picks (one fast local pass over each registered
    file, downloaded first when none covers the area) -- see
    docs/design-terrain-and-route-search.md section 2 and extracts.py's
    module docstring. A caller that already ran select_area_extracts passes
    its result as selected. The area's tile membership
    is recorded before any tile is fetched, so gc_areas_and_tiles run by a
    concurrent import can't evict tiles this one is still filling. The tiles
    are cut again unless every one was already cut from exactly the extracts
    it now needs, at their current fetched_at (extracts.tiles_cut_from);
    refresh matters only through the extract it may replace."""
    bbox = tiles.bbox_for_center_radius(area["center_lat"], area["center_lng"], area["radius_km"])
    needed = tiles.tiles_in_bbox(*bbox)
    total = len(needed)

    conn.execute("DELETE FROM area_tiles WHERE area_id = ?", (area_id,))
    conn.executemany(
        "INSERT INTO area_tiles (area_id, gx, gy) VALUES (?, ?, ?)", [(area_id, gx, gy) for gx, gy in needed]
    )
    conn.commit()

    if selected is None:
        selected = select_area_extracts(conn, area_id, refresh, force_extract)
    _set_progress(conn, area_id, phase="tiles", done=0, total=total)
    if extracts.tiles_cut_from(conn, selected, needed):
        metrics.cache("map_tiles", "hit", total)
    else:
        metrics.cache("map_tiles", "miss", total)
        extracts.cut_area(conn, selected, bbox)
    _set_progress(conn, area_id, phase="tiles", done=total, total=total)


def _payload_extent(payloads: Iterable[dict]) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) of every node and way-geometry coordinate in
    the payloads, or None when they hold none."""
    lats: list[float] = []
    lngs: list[float] = []
    for payload in payloads:
        for el in payload.get("elements", []):
            points = el.get("geometry") or []
            if el.get("type") == "node":
                points = [el]
            for p in points:
                if p is not None and p.get("lat") is not None and p.get("lon") is not None:
                    lats.append(p["lat"])
                    lngs.append(p["lon"])
    if not lats:
        return None
    return min(lats), min(lngs), max(lats), max(lngs)


def _finish_import(conn: sqlite3.Connection, area_id: int, area: sqlite3.Row) -> None:
    """Elevation prefetch + network build, shared by _do_import (tiles just
    fetched by this process) and import_tiles (tiles uploaded by the CLI,
    already fetched and cut somewhere else)."""
    needed = [(r["gx"], r["gy"]) for r in conn.execute(
        "SELECT gx, gy FROM area_tiles WHERE area_id = ?", (area_id,)
    ).fetchall()]
    total = len(needed)
    missing = missing_tiles(conn, area_id)
    if missing:
        raise AreaImportError(f"{len(missing)} of {total} map tiles are missing from the cache")

    # Every DEM tile under the area is fetched here, in the background job and
    # outside any transaction. The build then reads the cache alone: a fetch
    # during it would hold the write lock across network calls. Ways are stored
    # with their full geometry and run past the area's tile edge, so the range
    # follows the coordinates in the payloads, not the tile boundaries.
    extent = _payload_extent(area_tile_payloads(conn, area_id))
    elevation.ensure_tiles(
        conn,
        bbox=extent,
        on_progress=lambda done, total: _set_progress(conn, area_id, phase="elevation", done=done, total=total),
    )

    _set_progress(conn, area_id, phase="network", done=0, total=1)
    network.build_network(conn, area_id, area_tile_payloads(conn, area_id), elevation.sampler_for(conn))
    _set_progress(conn, area_id, phase="network", done=1, total=1)


def _do_import(
    conn: sqlite3.Connection,
    area_id: int,
    refresh: bool = False,
    force_extract: bool = False,
    selected: list[extracts.ExtractRecord] | None = None,
) -> None:
    """The actual tile + elevation fetch, against a caller-owned connection.
    Split from _run_import_job so it can run synchronously (tests; a caller
    that already holds an open connection) without a second thread opening
    a second connection to the same database."""
    area = _get_area_row(conn, area_id)
    _fetch_tiles_phase(conn, area_id, area, refresh, force_extract, selected)
    _finish_import(conn, area_id, area)


class TilePayload(TypedDict):
    gx: int
    gy: int
    elements: list[dict]


def import_tiles(
    conn: sqlite3.Connection,
    *,
    label: str,
    center_lat: float,
    center_lng: float,
    radius_km: float,
    tile_payloads: list[TilePayload],
    pinned: bool | None = None,
    source: str = "extract:cli",
) -> AreaStatus:
    """Create (or reuse, keyed by label) an area from tiles already cut and
    uploaded by miles-area-import, skipping the tile-fetch phase entirely,
    then run the same elevation-prefetch + network-build tail _do_import
    does. For an operator with a pbf the miles-api host can't reach itself:
    cut it locally, upload just the result."""
    if not label:
        raise ValueError("import_tiles needs a label")
    gc_areas_and_tiles(conn)
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute("SELECT area_id FROM areas WHERE label = ?", (label,)).fetchone()
    if existing is not None:
        area_id = int(existing["area_id"])
        conn.execute(
            """
            UPDATE areas SET center_lat = ?, center_lng = ?, radius_km = ?,
                pinned = COALESCE(?, pinned), last_used_at = ?
            WHERE area_id = ?
            """,
            (center_lat, center_lng, radius_km, None if pinned is None else int(pinned), now, area_id),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO areas (
                label, center_lat, center_lng, radius_km,
                pinned, status, created_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (label, center_lat, center_lng, radius_km, int(bool(pinned)), now, now),
        )
        area_id = int(cur.lastrowid)  # type: ignore[arg-type]
    conn.commit()

    for tile in tile_payloads:
        tilecache.store_tile_elements(conn, tile["gx"], tile["gy"], tile["elements"], source=source)
        conn.execute(
            "INSERT OR IGNORE INTO area_tiles (area_id, gx, gy) VALUES (?, ?, ?)",
            (area_id, tile["gx"], tile["gy"]),
        )
    conn.commit()

    if _try_start_import(conn, area_id):
        threading.Thread(target=_run_finish_only_job, args=(area_id,), daemon=True).start()
    else:
        conn.execute("UPDATE areas SET last_used_at = ? WHERE area_id = ?", (now, area_id))
        conn.commit()
    return get_area_status(conn, area_id)


def _run_job(
    area_id: int,
    body: Callable[[sqlite3.Connection, int], None],
    before_lock: Callable[[sqlite3.Connection, int], None] | None = None,
) -> None:
    """Thread target shared by import_area and import_tiles: opens its own
    connection (a sqlite3.Connection isn't safe to share across threads),
    runs body, and reports ready/error into the areas row, polled by
    get_area_status.

    body runs under _import_lock. before_lock runs first, outside it, for
    work that may wait on another import's extract download, which can take
    hours and must not hold up every other import."""
    conn = db.connect()
    try:
        # One import's network phase at a time, process-wide: each one is a
        # burst of requests to shared public services (see http_client).
        _set_progress(conn, area_id, phase="queued", done=0, total=1)
        if before_lock is not None:
            before_lock(conn, area_id)
            _set_progress(conn, area_id, phase="queued", done=0, total=1)
        with _import_lock:
            body(conn, area_id)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE areas SET status = 'ready', status_phase = NULL, status_done = NULL,
                status_total = NULL, status_error = NULL, imported_at = ?, last_used_at = ?
            WHERE area_id = ?
            """,
            (now, now, area_id),
        )
        conn.commit()
    except Exception as e:
        # Whatever the failed job left uncommitted must not be saved along
        # with the error status.
        conn.rollback()
        conn.execute(
            "UPDATE areas SET status = 'error', status_error = ? WHERE area_id = ?",
            (describe_error(e), area_id),
        )
        conn.commit()
    finally:
        conn.close()


def _run_import_job(area_id: int, refresh: bool = False, force_extract: bool = False) -> None:
    selected: list[list[extracts.ExtractRecord]] = []

    def prepare(conn: sqlite3.Connection, aid: int) -> None:
        selected.append(select_area_extracts(conn, aid, refresh, force_extract))

    _run_job(
        area_id,
        lambda conn, aid: _do_import(conn, aid, refresh, force_extract, selected[0]),
        before_lock=prepare,
    )


def _run_finish_only_job(area_id: int) -> None:
    """Thread target for import_tiles: the caller already stored the tiles,
    so only the elevation + network-build tail runs here."""
    def body(conn: sqlite3.Connection, aid: int) -> None:
        _finish_import(conn, aid, _get_area_row(conn, aid))
    _run_job(area_id, body)


# ---------------------------------------------------------------------------
# Reading an area's cached map data
# ---------------------------------------------------------------------------


def missing_tiles(conn: sqlite3.Connection, area_id: int) -> list[tuple[int, int]]:
    """Tiles the area references that have no map_tiles row."""
    rows = conn.execute(
        """
        SELECT atl.gx, atl.gy FROM area_tiles atl
        LEFT JOIN map_tiles mt ON mt.gx = atl.gx AND mt.gy = atl.gy
        WHERE atl.area_id = ? AND mt.gx IS NULL
        """,
        (area_id,),
    ).fetchall()
    return [(r["gx"], r["gy"]) for r in rows]


def area_tile_payloads(conn: sqlite3.Connection, area_id: int) -> Iterator[dict]:
    """Parsed payload for every tile this area has imported, one dict
    per tile. Raises AreaNotFoundError for an unknown area_id; yields
    nothing for an area whose import hasn't fetched any tiles yet."""
    _get_area_row(conn, area_id)
    rows = conn.execute(
        """
        SELECT mt.data FROM area_tiles atl
        JOIN map_tiles mt ON mt.gx = atl.gx AND mt.gy = atl.gy
        WHERE atl.area_id = ?
        """,
        (area_id,),
    ).fetchall()
    for row in rows:
        yield tilecache.decode_tile_payload(row["data"])
