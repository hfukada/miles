"""Regional OpenStreetMap extracts (Geofabrik .osm.pbf), the only map source
for area import: one bulk download covers however many areas fall inside it.
A registered extract is ground truth exactly like an uploaded GPX (adr/0002,
adr/0003): its own file, sha256, and the bounds read from the file's own
header are never mutated, only replaced wholesale on an explicit refresh.
Nothing refreshes an extract by age; the caller asks, and the download is a
conditional request whose HEAD answers 304 for an extract Geofabrik has not
rebuilt, so that case costs one small request and no download.
Which regions cover an area is geofabrik.py's choice; an area at a border is
cut from several extracts. cut_area filters each extract down to the
Overpass-JSON-shaped tile payloads tilecache.py stores and merges them, so
area_tile_payloads and build_network never need to know which extracts
produced a given tile.

Parking is mostly mapped as a closed way (an area), not a node, so cutting a
way tagged amenity=parking folds it into a synthetic point amenity -- see
_parking_center_element.
"""

import hashlib
import http.client
import json
import logging
import math
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import urllib.error
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing_extensions import TypedDict

import osmium
import osmium.filter
import osmium.io
import osmium.osm

from . import db, geofabrik, http_client, metrics, tilecache, tiles

GEOFABRIK_BASE_URL = geofabrik.GEOFABRIK_BASE_URL

_logger = logging.getLogger(__name__)

EXTRACT_DIR = db.DB_PATH.parent / "extracts"

# A registered extract is not asked about again (a conditional refresh) within
# this long of its last successful fetch or check, even when a caller asks
# for a refresh sooner.
EXTRACT_MIN_REFETCH_INTERVAL_HOURS = 24.0

# No GET for a region (its checksum or its file) within this long of the last
# one, whatever came of it, so a retry loop cannot re-download a large file
# over and over. --force-extract obeys it too.
REQUEST_COOLDOWN_HOURS = 1.0

# A regional extract larger than this is refused unless MILES_ALLOW_LARGE_EXTRACTS
# is set: continent-sized files take tens of gigabytes to cut.
EXTRACT_MAX_BYTES = 2 * 1024**3
ALLOW_LARGE_ENV = "MILES_ALLOW_LARGE_EXTRACTS"

# A download interrupted without cleanup (a killed process) leaves its
# .part file behind; ones this old are swept at the start of the next download.
_STALE_PART_AGE_S = 24 * 3600.0

# Above this file size, cut_area's node-location index moves off the heap
# and onto disk (osmium's with_locations(storage=...)). An in-memory
# flex_mem index runs at several times the file's size, so this threshold
# keeps a single pass under roughly 1 GB.
DISK_INDEX_THRESHOLD_BYTES = 200 * 1024 * 1024

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
PROGRESS_INTERVAL_S = 1.0


class ExtractDownloadError(Exception):
    """An extract could not be obtained: the download, its checksum, the region, or a local file failed."""


class ExtractTooLargeError(ExtractDownloadError):
    """The extract is over EXTRACT_MAX_BYTES. Refused from the size the
    server declares before any GET, so retrying with the limit lifted or a
    smaller region waits out no cooldown."""


class ExtractChecksumUnavailableError(ExtractDownloadError):
    """The published checksum could not be fetched. The body is never
    requested without it, so this costs two small requests; the cooldown
    still applies, so a retry loop or a lock waiter does not keep asking."""


class ExtractRecord(TypedDict):
    region: str
    path: str
    sha256: str
    fetched_at: str
    bounds_south: float | None
    bounds_west: float | None
    bounds_north: float | None
    bounds_east: float | None
    # When the file was last confirmed current (a download or a 304).
    checked_at: str
    # The server's Last-Modified (ISO, UTC) when it gave one, else fetched_at:
    # the date the data in the file is as of.
    data_as_of: str
    etag: str | None
    last_modified: str | None


_EXTRA_COLUMNS = ("checked_at TEXT", "data_as_of TEXT", "etag TEXT", "last_modified TEXT")


def _large_extracts_allowed() -> bool:
    return os.environ.get(ALLOW_LARGE_ENV, "").strip().lower() in ("1", "true", "yes")


def init_extract_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS map_extracts (
            region        TEXT PRIMARY KEY,
            path          TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            fetched_at    TEXT NOT NULL,
            bounds_south  REAL,
            bounds_west   REAL,
            bounds_north  REAL,
            bounds_east   REAL,
            checked_at    TEXT,
            data_as_of    TEXT,
            etag          TEXT,
            last_modified TEXT
        )
    """)
    db.add_missing_columns(conn, "map_extracts", *_EXTRA_COLUMNS)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extract_requests (
            region       TEXT PRIMARY KEY,
            requested_at TEXT NOT NULL
        )
    """)
    conn.execute("DROP TABLE IF EXISTS extract_attempts")
    geofabrik.init_index_schema(conn)
    conn.commit()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _row_to_record(row: sqlite3.Row) -> ExtractRecord:
    return {
        "region": row["region"],
        "path": row["path"],
        "sha256": row["sha256"],
        "fetched_at": row["fetched_at"],
        "bounds_south": row["bounds_south"],
        "bounds_west": row["bounds_west"],
        "bounds_north": row["bounds_north"],
        "bounds_east": row["bounds_east"],
        "checked_at": row["checked_at"] or row["fetched_at"],
        "data_as_of": row["data_as_of"] or row["fetched_at"],
        "etag": row["etag"],
        "last_modified": row["last_modified"],
    }


def get_extract(conn: sqlite3.Connection, region: str) -> ExtractRecord | None:
    row = conn.execute("SELECT * FROM map_extracts WHERE region = ?", (region,)).fetchone()
    return _row_to_record(row) if row is not None else None


def list_extracts(conn: sqlite3.Connection) -> list[ExtractRecord]:
    return [_row_to_record(r) for r in conn.execute("SELECT * FROM map_extracts ORDER BY region").fetchall()]


def _extract_bounds(record: ExtractRecord) -> tuple[float, float, float, float] | None:
    south, west, north, east = record["bounds_south"], record["bounds_west"], record["bounds_north"], record["bounds_east"]
    if south is None or west is None or north is None or east is None:
        return None
    return (south, west, north, east)


def _bbox_within(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> bool:
    i_south, i_west, i_north, i_east = inner
    o_south, o_west, o_north, o_east = outer
    return o_south <= i_south and o_west <= i_west and o_north >= i_north and o_east >= i_east


LOCAL_REGION_PREFIX = "local:"


def find_registered_extract(
    conn: sqlite3.Connection, bbox: tuple[float, float, float, float], *, local_only: bool = False
) -> ExtractRecord | None:
    """The smallest already-registered extract whose header bounds fully
    contain bbox, with no download attempt -- for callers that must never
    block on network I/O. A header bbox is a rectangle that usually reaches
    across the region's borders, so a hit says nothing about whether the
    region's polygon covers the area; select_extracts trusts it only for
    local extracts and when no region index is stored. local_only restricts
    the search to extracts registered from a file (LOCAL_REGION_PREFIX)."""
    candidates: list[tuple[float, ExtractRecord]] = []
    for record in list_extracts(conn):
        if local_only and not record["region"].startswith(LOCAL_REGION_PREFIX):
            continue
        bounds = _extract_bounds(record)
        if bounds is not None and _bbox_within(bbox, bounds):
            area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
            candidates.append((area, record))
    if not candidates:
        return None
    return min(candidates, key=lambda t: t[0])[1]


def split_regions(value: str | Sequence[str] | None) -> list[str]:
    """Region paths from one path, a comma-separated list of them, or a
    sequence of either. Paths never contain commas."""
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    return [part.strip() for item in items for part in item.split(",") if part.strip()]


def source_of_regions(regions: Iterable[str]) -> str:
    """The map_tiles.source for a tile holding data from these regions:
    `extract:<region>`, or the regions joined with commas in sorted order
    for several."""
    return "extract:" + ",".join(sorted(set(regions)))


def tile_source(extract_list: Sequence[ExtractRecord]) -> str:
    """The map_tiles.source a cut from exactly these extracts is tagged with."""
    return source_of_regions(e["region"] for e in extract_list)


def regions_of_source(source: str) -> list[str]:
    """The regions in a tile_source, empty for a source that is not an
    extract's."""
    return split_regions(source.removeprefix("extract:")) if source.startswith("extract:") else []


def planned_regions(
    conn: sqlite3.Connection,
    bbox: tuple[float, float, float, float],
    preferred_region: str | Sequence[str] | None = None,
    *,
    allow_network: bool = False,
) -> list[str]:
    """The regions to cut bbox from, in order of precedence (see
    select_extracts). Without allow_network the answer comes from what is
    already stored, and is empty when that is not enough to say (no index
    stored and nothing registered contains bbox); with it, an index never
    stored is fetched, a preferred region is checked against the index, and
    an area nothing covers is an error. Raises geofabrik.RegionError."""
    local = find_registered_extract(conn, bbox, local_only=True)
    if local is not None:
        return [local["region"]]
    paths = split_regions(preferred_region)
    if paths:
        if allow_network:
            for path in paths:
                geofabrik.validate_region(conn, path)
        return paths
    found = find_registered_extract(conn, bbox)
    if found is not None and geofabrik.cached_regions(conn) is None:
        return [found["region"]]
    try:
        covering = geofabrik.choose_regions(conn, bbox) if allow_network else geofabrik.choose_cached_regions(conn, bbox)
    except geofabrik.RegionError:
        if found is None:
            raise
        covering = None
    if covering:
        return covering
    return [found["region"]] if found is not None else []


# Called with the bytes written so far and the size the server declared, or
# None when it declared none.
DownloadProgress = Callable[[int, int | None], None]


def throttled_progress(
    on_progress: DownloadProgress, *, interval: float = PROGRESS_INTERVAL_S, clock: Callable[[], float] = time.monotonic
) -> Callable[..., None]:
    """A reporter(written, expected, force) that forwards to on_progress at
    most once per interval seconds, and always when force is true. An
    exception from on_progress is logged and dropped: reporting progress must
    never fail the download it reports on."""
    last: list[float | None] = [None]

    def report(written: int, expected: int | None, force: bool = False) -> None:
        now = clock()
        if not force and last[0] is not None and now - last[0] < interval:
            return
        last[0] = now
        try:
            on_progress(written, expected)
        except Exception:
            _logger.warning("download progress callback failed", exc_info=True)

    return report


def _refreshed(
    conn: sqlite3.Connection,
    extract: ExtractRecord,
    *,
    refresh: bool,
    force: bool,
    on_wait: Callable[[], None] | None = None,
    on_progress: DownloadProgress | None = None,
) -> ExtractRecord:
    if extract["region"].startswith(LOCAL_REGION_PREFIX):
        # Registered from a file on disk; Geofabrik has no such region.
        if not Path(extract["path"]).is_file():
            raise ExtractDownloadError(
                f"the file registered for {extract['region']} is missing; register it again with miles-area-import --pbf"
            )
        return extract
    if not Path(extract["path"]).is_file():
        return download_extract(conn, extract["region"], force=force, on_wait=on_wait, on_progress=on_progress)
    if not refresh and not force:
        return extract
    try:
        return download_extract(
            conn, extract["region"], refresh=True, force=force, on_wait=on_wait, on_progress=on_progress
        )
    except ExtractDownloadError:
        if force:
            raise
        return extract


def _too_large_hint(conn: sqlite3.Connection, region: str, bbox: tuple[float, float, float, float]) -> str:
    try:
        known = geofabrik.cached_regions(conn)
    except sqlite3.Error:
        known = None
    children = geofabrik.covering_children(known, region, bbox) if known is not None else None
    if not children:
        return ""
    paths = [c.path for c in children]
    return f"; smaller regions that cover this area: {', '.join(paths)} (pass region={','.join(paths)})"


def select_extracts(
    conn: sqlite3.Connection,
    bbox: tuple[float, float, float, float],
    preferred_region: str | Sequence[str] | None = None,
    *,
    refresh: bool = False,
    force: bool = False,
    on_wait: Callable[[], None] | None = None,
    on_progress: DownloadProgress | None = None,
) -> list[ExtractRecord]:
    """The extracts to cut bbox from, in order of precedence:

    - the smallest extract registered from a local file (`local:` region)
      whose header bounds contain bbox, since such a file has no region
      boundary to consult;
    - the athlete's preferred_region (one path or several), if given;
    - the cheapest set of Geofabrik regions whose boundaries cover bbox
      (geofabrik.choose_regions: one region, or several at a border) once
      the region index is stored;
    - the smallest registered extract whose header bounds contain bbox, but
      only when the region index has never been stored (no network is
      touched to decide) or no region in it covers bbox.

    Region paths are then looked up in the registry, so an already-downloaded
    region is reused and only a missing one is downloaded, one after another;
    a registered extract whose file has gone missing is downloaded again.
    Header bounds are not used to decide coverage while the index is
    available: they reach across borders, and an area there needs every
    region that touches it.

    A found extract is used as it is. refresh=True first asks Geofabrik
    whether it has a newer one (see download_extract); an extract that can't
    be refreshed -- one registered from a local file, say, whose region name
    Geofabrik doesn't know -- is used as it is. force=True replaces the file
    unconditionally and reports a failure instead of falling back to the old
    one. on_wait is called once if a download is waiting on another thread's
    download of the same region; on_progress receives the bytes written and the expected total of a
    download this call performs itself. A region over the size cap is refused
    with the smaller regions that would cover the area named in the error."""
    try:
        paths = planned_regions(conn, bbox, preferred_region, allow_network=True)
    except geofabrik.RegionError as e:
        raise ExtractDownloadError(str(e)) from e
    chosen: list[ExtractRecord] = []
    for path in paths:
        registered = get_extract(conn, path)
        try:
            if registered is not None:
                chosen.append(
                    _refreshed(
                        conn, registered, refresh=refresh, force=force, on_wait=on_wait, on_progress=on_progress
                    )
                )
            else:
                chosen.append(download_extract(conn, path, force=force, on_wait=on_wait, on_progress=on_progress))
        except ExtractTooLargeError as e:
            raise ExtractTooLargeError(f"{e}{_too_large_hint(conn, path, bbox)}") from e
    return chosen


def tile_extracts(
    conn: sqlite3.Connection, selected: Sequence[ExtractRecord], tile_ids: Iterable[tuple[int, int]]
) -> dict[tuple[int, int], list[ExtractRecord]]:
    """The extracts each tile is cut from: every registered Geofabrik extract
    whose file is present and whose region polygon shares area with the tile
    (the header bbox for a region the stored index does not list, or when no
    index is stored), plus the selected extracts that do the same. An extract
    with neither a polygon nor header bounds touches only tiles of a selected
    one. A region is left out of a tile when a registered region nested under
    its path contains the whole tile, since the parent holds the same data and
    is far larger to read; a tile straddling the child's edge keeps the
    parent, as the rest of the tile may lie in an unregistered sibling. When a selected extract is a local file there is no
    boundary to consult, so the selection alone is used. A tile no extract
    touches falls back to the selection."""
    tile_list = list(tile_ids)
    if any(e["region"].startswith(LOCAL_REGION_PREFIX) for e in selected):
        return {t: list(selected) for t in tile_list}
    known = geofabrik.cached_regions(conn)
    by_path = {r.path: r for r in known} if known is not None else {}
    chosen = {e["region"] for e in selected}
    others = [
        e for e in list_extracts(conn)
        if e["region"] not in chosen and not e["region"].startswith(LOCAL_REGION_PREFIX) and Path(e["path"]).is_file()
    ]
    candidates = [*selected, *others]

    def touches(e: ExtractRecord, bounds: tuple[float, float, float, float]) -> bool:
        region = by_path.get(e["region"])
        if region is not None:
            return geofabrik.intersects(region, bounds)
        header = _extract_bounds(e)
        if header is None:
            return e["region"] in chosen
        return tiles.bbox_overlaps(header, bounds)

    def contains(e: ExtractRecord, bounds: tuple[float, float, float, float]) -> bool:
        region = by_path.get(e["region"])
        if region is not None:
            return geofabrik.contains_bbox(region, bounds)
        header = _extract_bounds(e)
        return header is not None and _bbox_within(bounds, header)

    def without_ancestors(touching: list[ExtractRecord], bounds: tuple[float, float, float, float]) -> list[ExtractRecord]:
        holders = [e["region"] for e in touching if contains(e, bounds)]
        return [
            e for e in touching
            if not any(h.startswith(e["region"].rstrip("/") + "/") for h in holders)
        ]

    result: dict[tuple[int, int], list[ExtractRecord]] = {}
    for tile in tile_list:
        bounds = tiles.tile_bounds(*tile)
        touching = [e for e in candidates if touches(e, bounds)]
        result[tile] = without_ancestors(touching, bounds) or list(selected)
    return result


def _stamps(extract_list: Iterable[ExtractRecord]) -> dict[str, str]:
    return {e["region"]: e["fetched_at"] for e in extract_list}


def tiles_cut_from(
    conn: sqlite3.Connection, extract: ExtractRecord | Sequence[ExtractRecord], tile_ids: list[tuple[int, int]]
) -> bool:
    """True when every tile in tile_ids was cut, at the current query version,
    from exactly the extracts tile_extracts names for it, each at its current
    fetched_at, so cutting again would reproduce what's already cached. A tile
    that lacks a registered region now intersecting it, was cut from a file
    since replaced, or came from elsewhere (an upload, an older version) is
    cut again."""
    extract_list = [extract] if isinstance(extract, dict) else list(extract)
    for (gx, gy), tile_set in tile_extracts(conn, extract_list, tile_ids).items():
        row = conn.execute(
            "SELECT source, query_version, inputs FROM map_tiles WHERE gx = ? AND gy = ?", (gx, gy)
        ).fetchone()
        if (
            row is None
            or row["query_version"] != tilecache.TILE_QUERY_VERSION
            or row["source"] != tile_source(tile_set)
            or row["inputs"] != json.dumps(_stamps(tile_set), sort_keys=True)
        ):
            return False
    return True


# ---------------------------------------------------------------------------
# Bounds from a pbf's own header
# ---------------------------------------------------------------------------


def _scan_bounds(path: Path) -> tuple[float, float, float, float] | None:
    """Fallback when a pbf's header carries no bbox -- real Geofabrik
    extracts always have one; small hand-written test fixtures don't. One
    pass over every node's own coordinates."""
    lats: list[float] = []
    lons: list[float] = []
    for obj in osmium.FileProcessor(str(path), osmium.osm.osm_entity_bits.NODE):
        if not isinstance(obj, osmium.osm.Node):
            continue
        lats.append(obj.location.lat)
        lons.append(obj.location.lon)
    if not lats:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


def _read_bounds(path: Path) -> tuple[float, float, float, float] | None:
    reader = osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.NOTHING)
    try:
        box = reader.header().box()
        if box.valid():
            return (box.bottom_left.lat, box.bottom_left.lon, box.top_right.lat, box.top_right.lon)
    finally:
        reader.close()
    return _scan_bounds(path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_DOWNLOAD_CHUNK_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Registration / download
# ---------------------------------------------------------------------------


def _extract_path(region: str) -> Path:
    return EXTRACT_DIR / f"{region.replace('/', '-')}.osm.pbf"


def _http_date_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat()


def register_local_extract(
    conn: sqlite3.Connection,
    path: Path | str,
    region: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> ExtractRecord:
    """Register a file already on disk as region's extract, without ever
    trying to download it -- for a pbf obtained out of band: the CLI's own
    --pbf argument, or a downloaded file download_extract just placed.
    etag and last_modified are the server's validators for a downloaded file;
    data_as_of is the Last-Modified date when there is one, else now."""
    path = Path(path)
    if not path.is_file():
        raise ExtractDownloadError(f"no such file: {path}")
    sha256 = _sha256_file(path)
    bounds = _read_bounds(path) or (None, None, None, None)
    now = datetime.now(timezone.utc).isoformat()
    data_as_of = _http_date_to_iso(last_modified) or now
    conn.execute(
        """
        INSERT INTO map_extracts (
            region, path, sha256, fetched_at, bounds_south, bounds_west, bounds_north, bounds_east,
            checked_at, data_as_of, etag, last_modified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(region) DO UPDATE SET
            path = excluded.path, sha256 = excluded.sha256, fetched_at = excluded.fetched_at,
            bounds_south = excluded.bounds_south, bounds_west = excluded.bounds_west,
            bounds_north = excluded.bounds_north, bounds_east = excluded.bounds_east,
            checked_at = excluded.checked_at, data_as_of = excluded.data_as_of,
            etag = excluded.etag, last_modified = excluded.last_modified
        """,
        (region, str(path), sha256, now, *bounds, now, data_as_of, etag, last_modified),
    )
    conn.commit()
    record = get_extract(conn, region)
    assert record is not None
    return record


class _Download(TypedDict):
    etag: str | None
    last_modified: str | None


def _md5_of(text: bytes) -> str:
    """The digest in a Geofabrik `.md5` file ("<hex>  <name>")."""
    fields = text.decode("utf-8", errors="replace").split()
    if not fields or len(fields[0]) != 32:
        raise ExtractDownloadError("the .md5 file next to the extract is not a checksum")
    return fields[0].lower()


def _declared_size(resp: object) -> int | None:
    declared = getattr(getattr(resp, "headers", None), "get", lambda _k: None)("Content-Length")
    return int(declared) if declared and str(declared).isdigit() else None


def _head_info(requested: str) -> Callable[[object], tuple[str, int | None]]:
    """A reader for http_client.request that returns the URL a response was
    finally served from, after redirects, and the size it declares, without
    reading its body."""

    def read(resp: object) -> tuple[str, int | None]:
        return getattr(resp, "geturl", lambda: requested)(), _declared_size(resp)

    return read


def _published_md5(file_url: str) -> str:
    """The digest published beside file_url, which must be the URL the body
    was actually served from: `-latest` redirects to a dated file, and a
    rebuild between the body and this request would otherwise pair a new
    digest with an old file. One retry on a network failure; failing twice
    says nothing about the file."""
    for attempt in range(2):
        try:
            return _md5_of(http_client.request(http_client.GEOFABRIK, f"{file_url}.md5"))
        except (OSError, http.client.HTTPException) as e:
            if attempt == 1:
                raise ExtractChecksumUnavailableError(f"could not fetch the checksum for {file_url}: {e}") from e
    raise AssertionError("unreachable")


def _sweep_stale_parts(directory: Path) -> None:
    cutoff = time.time() - _STALE_PART_AGE_S
    for part in directory.glob("*.part"):
        try:
            if part.stat().st_mtime < cutoff:
                part.unlink()
        except OSError:
            pass


def _download_with_backoff(
    url: str,
    dest: Path,
    validators: dict[str, str],
    *,
    allow_large: bool = False,
    on_progress: DownloadProgress | None = None,
    on_request: Callable[[], None] | None = None,
) -> _Download | None:
    """Stream url to a uniquely named .part file beside dest, verify it, and
    move it over dest. Returns the response's validators, or None when the
    server answered 304 Not Modified to the conditional headers in validators
    (dest is then untouched).

    A HEAD request comes first: it yields the URL the download redirects to
    and the declared size, which is refused over EXTRACT_MAX_BYTES (unless
    allow_large) before anything else is requested. on_request is then called
    once, just before the first GET (the checksum, then the file).

    Verified before dest is replaced: the bytes written equal Content-Length
    (a connection cut mid-body is otherwise a quietly truncated file), the
    size is within EXTRACT_MAX_BYTES unless allow_large, and the MD5 matches
    the `<url>.md5` Geofabrik publishes. The .part file is removed on every
    exit. on_progress is called with 0 bytes before the first chunk, then at
    most once a second (PROGRESS_INTERVAL_S) while chunks arrive, and with the
    final count; the expected total is None when the server declared no size.
    A failing on_progress is logged and ignored."""
    part = dest.with_name(f"{dest.name}.{os.getpid()}.{secrets.token_hex(4)}.part")
    allowed = allow_large or _large_extracts_allowed()

    def too_large(size: int) -> ExtractDownloadError:
        return ExtractTooLargeError(
            f"{url} is {size / 1024**3:.1f} GB, over the {EXTRACT_MAX_BYTES / 1024**3:.0f} GB limit; "
            f"set {ALLOW_LARGE_ENV}=1 to allow it, or cut the area elsewhere and upload it with miles-area-import --pbf"
        )

    def to_disk(resp: object) -> tuple[str, _Download]:
        headers = getattr(resp, "headers", None)
        get = getattr(headers, "get", lambda _k: None)
        expected = _declared_size(resp)
        digest = hashlib.md5(usedforsecurity=False)
        written = 0
        report = throttled_progress(on_progress) if on_progress is not None else None
        if report is not None:
            report(0, expected, True)
        with part.open("wb") as fh:
            while True:
                chunk = resp.read(_DOWNLOAD_CHUNK_BYTES)  # type: ignore[attr-defined]
                if not chunk:
                    break
                written += len(chunk)
                if written > EXTRACT_MAX_BYTES and not allowed:
                    raise too_large(written)
                digest.update(chunk)
                fh.write(chunk)
                if report is not None:
                    report(written, expected)
            if report is not None:
                report(written, expected, True)
            fh.flush()
            os.fsync(fh.fileno())
        if expected is not None and written != expected:
            raise ExtractDownloadError(f"short read from {url}: got {written} of {expected} bytes")
        return digest.hexdigest(), {"etag": get("ETag"), "last_modified": get("Last-Modified")}

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # The checksum comes first, from the URL the download redirects to,
        # so a missing one costs two small requests rather than the file.
        # The body is then fetched from that same URL.
        target, size = http_client.request(
            http_client.GEOFABRIK, url, headers=validators, reader=_head_info(url), method="HEAD"
        )
        if size is not None and size > EXTRACT_MAX_BYTES and not allowed:
            raise too_large(size)
        if on_request is not None:
            on_request()
        published = _published_md5(target)
        actual, validators_out = http_client.request(
            http_client.GEOFABRIK, target, headers=validators, reader=to_disk
        )
        if actual != published:
            raise ExtractDownloadError(f"checksum mismatch for {url}: downloaded {actual}, published {published}")
        os.replace(part, dest)
        return validators_out
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise ExtractDownloadError(f"failed to download {url}: {e}") from e
    except (OSError, http.client.HTTPException) as e:
        # URLError, timeouts and connection resets are OSErrors; an
        # IncompleteRead is an HTTPException.
        raise ExtractDownloadError(f"failed to download {url}: {e}") from e
    finally:
        part.unlink(missing_ok=True)


def _conditional_headers(existing: ExtractRecord) -> dict[str, str]:
    """Validators for asking whether the stored file is still current. The
    ETag and Last-Modified the server gave are used as they are; a file
    registered without them is compared by the time it was downloaded."""
    if not Path(existing["path"]).is_file():
        return {}
    headers: dict[str, str] = {}
    if existing["etag"]:
        headers["If-None-Match"] = existing["etag"]
    if existing["last_modified"]:
        headers["If-Modified-Since"] = existing["last_modified"]
    elif not existing["etag"]:
        headers["If-Modified-Since"] = format_datetime(datetime.fromisoformat(existing["fetched_at"]), usegmt=True)
    return headers


def _stamp_request(conn: sqlite3.Connection, region: str) -> None:
    conn.execute(
        """
        INSERT INTO extract_requests (region, requested_at) VALUES (?, ?)
        ON CONFLICT(region) DO UPDATE SET requested_at = excluded.requested_at
        """,
        (region, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _raise_if_cooling_down(conn: sqlite3.Connection, region: str) -> None:
    row = conn.execute("SELECT requested_at FROM extract_requests WHERE region = ?", (region,)).fetchone()
    if row is None:
        return
    age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(row["requested_at"])).total_seconds()
    if age_s < REQUEST_COOLDOWN_HOURS * 3600.0:
        raise ExtractDownloadError(
            f"{region} was requested from Geofabrik {int(age_s // 60)} min ago; "
            f"not contacting Geofabrik again for {int((REQUEST_COOLDOWN_HOURS * 3600.0 - age_s) // 60) + 1} min"
        )


def _validators_for(
    conn: sqlite3.Connection, region: str, refresh: bool, force: bool
) -> tuple[ExtractRecord | None, dict[str, str] | None]:
    """(registered extract, conditional headers) when Geofabrik has to be
    contacted for region, or (registered extract, None) when the registered
    one can be used as it is."""
    existing = get_extract(conn, region)
    if existing is None or not Path(existing["path"]).is_file():
        return existing, {}
    if force:
        return existing, {}
    if not refresh:
        return existing, None
    last_contact = datetime.fromisoformat(max(existing["fetched_at"], existing["checked_at"]))
    if (datetime.now(timezone.utc) - last_contact).total_seconds() < EXTRACT_MIN_REFETCH_INTERVAL_HOURS * 3600.0:
        return existing, None
    return existing, _conditional_headers(existing)


_region_locks: dict[str, threading.Lock] = {}
_region_locks_guard = threading.Lock()


def _region_lock(region: str) -> threading.Lock:
    with _region_locks_guard:
        return _region_locks.setdefault(region, threading.Lock())


def download_extract(
    conn: sqlite3.Connection,
    region: str,
    *,
    base_url: str | None = None,
    refresh: bool = False,
    force: bool = False,
    allow_large: bool = False,
    on_wait: Callable[[], None] | None = None,
    on_progress: DownloadProgress | None = None,
) -> ExtractRecord:
    """Register region's Geofabrik extract, downloading it only if none is
    registered or its file is gone. A registered extract is returned as it is
    unless refresh=True, which asks Geofabrik with a conditional request (its
    HEAD carries the validators): a 304 keeps the file and only updates
    checked_at, a 200 replaces it. force=True
    downloads unconditionally and replaces a file that is bad but still
    registered.

    A registered extract is not asked about again within
    EXTRACT_MIN_REFETCH_INTERVAL_HOURS of its last success, refresh or not,
    and no region gets a GET (checksum or file) within REQUEST_COOLDOWN_HOURS
    of its last one, force included, so a caller retrying a failed import in
    a loop can't hammer Geofabrik. A refusal for size happens before any GET
    and starts no cooldown. A per-region
    lock keeps two importers from fetching the same file at once; one that finds
    it taken waits and then uses the file the holder registered. on_wait, if
    given, is called once when the lock is found taken; on_progress receives the bytes written and expected total
    while this call is downloading a body (not for a 304)."""
    if region.startswith(LOCAL_REGION_PREFIX):
        raise ExtractDownloadError(f"{region} is registered from a local file and cannot be downloaded")
    existing, validators = _validators_for(conn, region, refresh, force)
    if validators is None:
        assert existing is not None
        metrics.cache("extract", "hit")
        return existing
    _raise_if_cooling_down(conn, region)

    lock = _region_lock(region)
    if not lock.acquire(blocking=False):
        if on_wait is not None:
            on_wait()
        lock.acquire()
    try:
        existing, validators = _validators_for(conn, region, refresh, force)
        if validators is None:
            assert existing is not None
            metrics.cache("extract", "hit")
            return existing
        _raise_if_cooling_down(conn, region)
        path = _extract_path(region)
        path.parent.mkdir(parents=True, exist_ok=True)
        _sweep_stale_parts(path.parent)
        url = f"{(base_url or GEOFABRIK_BASE_URL).rstrip('/')}/{region}-latest.osm.pbf"
        try:
            downloaded = _download_with_backoff(
                url,
                path,
                validators,
                allow_large=allow_large,
                on_progress=on_progress,
                on_request=lambda: _stamp_request(conn, region),
            )
            if downloaded is None:
                assert existing is not None
                metrics.cache("extract", "not_modified")
                conn.execute(
                    "UPDATE map_extracts SET checked_at = ? WHERE region = ?",
                    (datetime.now(timezone.utc).isoformat(), region),
                )
                conn.commit()
                refreshed = get_extract(conn, region)
                assert refreshed is not None
                return refreshed
            metrics.cache("extract", "miss")
            record = register_local_extract(
                conn, path, region, etag=downloaded["etag"], last_modified=downloaded["last_modified"]
            )
        except Exception as e:
            _logger.warning("extract download for %s failed: %s", region, e)
            raise
        return record
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Cutting an area out of an extract
# ---------------------------------------------------------------------------


@contextmanager
def _location_index(path: Path) -> Iterator[str]:
    """The node-location storage spec for one cut: in memory for a small file,
    else a throwaway on-disk index removed when the pass ends. A sparse index
    stores only the node ids actually present, where a dense one allocates
    space for the whole id range."""
    if path.stat().st_size < DISK_INDEX_THRESHOLD_BYTES:
        yield "flex_mem"
        return
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=EXTRACT_DIR, prefix=".locations-") as tmp:
        yield f"sparse_file_array,{Path(tmp) / 'locations.idx'}"


def _way_bbox(obj: osmium.osm.Way) -> tuple[float, float, float, float] | None:
    """A way's bbox over its located nodes, tracked as running extremes so no
    per-way list is built -- run before building its full element, so an
    extract's ways outside the requested area are rejected without ever
    materializing their tags or geometry."""
    south = west = math.inf
    north = east = -math.inf
    for n in obj.nodes:
        if n.location.valid():
            south, north = min(south, n.lat), max(north, n.lat)
            west, east = min(west, n.lon), max(east, n.lon)
    if south == math.inf:
        return None
    return (south, west, north, east)


def _way_element(obj: osmium.osm.Way, tags: dict[str, str]) -> dict | None:
    if tags.get("highway") not in tilecache.HIGHWAY_VALUES:
        return None
    node_ids: list[int] = []
    geometry: list[dict[str, float] | None] = []
    any_located = False
    for n in obj.nodes:
        node_ids.append(n.ref)
        if n.location.valid():
            geometry.append({"lat": n.lat, "lon": n.lon})
            any_located = True
        else:
            geometry.append(None)
    if not any_located:
        return None
    return {"type": "way", "id": obj.id, "tags": tags, "nodes": node_ids, "geometry": geometry}


def _parking_center_element(obj: osmium.osm.Way, tags: dict[str, str]) -> dict | None:
    """A closed way tagged amenity=parking, folded into a synthetic point
    amenity -- id is the way's own id negated, since real OSM ids are never
    negative, so it can never collide with an amenity mapped on a node."""
    if tags.get("amenity") != "parking" or not obj.is_closed():
        return None
    nodes = list(obj.nodes)[:-1]  # a closed way repeats its first node last
    lats: list[float] = []
    lons: list[float] = []
    for n in nodes:
        if not n.location.valid():
            return None
        lats.append(n.lat)
        lons.append(n.lon)
    if len(lats) < 3:
        return None
    return {
        "type": "node",
        "id": -obj.id,
        "lat": sum(lats) / len(lats),
        "lon": sum(lons) / len(lons),
        "tags": tags,
    }


def _node_element(obj: osmium.osm.Node, tags: dict[str, str]) -> dict | None:
    if not any(tags.get(k) == v for k, v in tilecache.NODE_TAGS):
        return None
    return {"type": "node", "id": obj.id, "lat": obj.location.lat, "lon": obj.location.lon, "tags": tags}


def _located_points(element: dict) -> int:
    return sum(1 for p in element.get("geometry") or [] if p is not None)


def merge_elements(element_lists: Iterable[list[dict]]) -> list[dict]:
    """The union of several cuts' elements with each OSM element once: a way
    crossing the border between two extracts is in both, keyed by type and id.
    An extract may hold only part of a border way's nodes. Two copies with the
    same node list are combined point by point, a missing point filled from
    the other copy; otherwise the copy with more located points is kept (the
    earlier one on a tie)."""
    merged: dict[tuple[object, object], dict] = {}
    for elements in element_lists:
        for el in elements:
            key = (el.get("type"), el.get("id"))
            kept = merged.get(key)
            merged[key] = el if kept is None else _merge_copies(kept, el)
    return list(merged.values())


def _merge_copies(kept: dict, other: dict) -> dict:
    nodes = kept.get("nodes")
    if nodes is not None and nodes == other.get("nodes"):
        mine, theirs = kept.get("geometry") or [], other.get("geometry") or []
        if len(mine) == len(theirs):
            return {**kept, "geometry": [a if a is not None else b for a, b in zip(mine, theirs)]}
    return other if _located_points(other) > _located_points(kept) else kept


def cut_area(
    conn: sqlite3.Connection,
    extract: ExtractRecord | Sequence[ExtractRecord],
    bbox: tuple[float, float, float, float],
) -> list[tuple[int, int]]:
    """Write the Overpass-JSON-shaped map_tiles cache for every 0.05 deg tile
    bbox touches. The cut covers each tile whole, not just the part inside
    bbox, so overlapping areas write the same content.

    A tile's content is a function of the files alone: tile_extracts names
    the extracts it is cut from (every registered Geofabrik extract whose
    region intersects it, not only those passed in), each file gets one
    filtered pass over the tiles it serves, and an element in several files
    is kept once (merge_elements). Stored elements are never read back, so a
    way deleted or shortened upstream disappears from the tile the next time
    its extract is refreshed and the tile cut again, and every write replaces
    the whole row in its own transaction. The tile's source lists its
    regions, and its fetched_at is the oldest fetched_at among them, so it
    is never newer than the oldest data it holds. Tiles from a local file
    (`miles-area-import --pbf`) are for places without Geofabrik coverage; a
    later cut from Geofabrik extracts replaces them. Returns the (gx, gy)
    tiles written."""
    extract_list = [extract] if isinstance(extract, dict) else list(extract)
    tile_ids = tiles.tiles_in_bbox(*bbox)
    per_tile = tile_extracts(conn, extract_list, tile_ids)
    records: dict[str, ExtractRecord] = {}
    serves: dict[str, list[tuple[int, int]]] = {}
    for tile, tile_set in per_tile.items():
        for e in tile_set:
            records[e["region"]] = e
            serves.setdefault(e["region"], []).append(tile)
    cut: dict[str, dict[tuple[int, int], list[dict]]] = {}
    for region, served in serves.items():
        bounds = [tiles.tile_bounds(gx, gy) for gx, gy in served]
        cut_bbox = (
            min(b[0] for b in bounds), min(b[1] for b in bounds),
            max(b[2] for b in bounds), max(b[3] for b in bounds),
        )
        cut[region] = tiles.assign_elements(_cut_elements(records[region], cut_bbox), served)
    for tile in tile_ids:
        tile_set = per_tile[tile]
        encoded = tilecache.encode_tile_elements(merge_elements(cut[e["region"]][tile] for e in tile_set))
        with tilecache.immediate_transaction(conn):
            tilecache.write_tile_elements(
                conn, tile[0], tile[1], encoded,
                source=tile_source(tile_set),
                fetched_at=min(e["fetched_at"] for e in tile_set),
                inputs=_stamps(tile_set),
            )
    return tile_ids


def _cut_elements(extract: ExtractRecord, bbox: tuple[float, float, float, float]) -> list[dict]:
    """The tile-payload elements of one extract's file that fall in bbox.

    A regional extract covers far more ground than any one area -- the
    KeyFilter keeps every highway/amenity way in the whole file, so a tag
    and bbox check runs before building an element's tags/geometry at all,
    or memory scales with the extract's size instead of the area's.
    """
    path = Path(extract["path"])
    elements: list[dict] = []

    with _location_index(path) as storage:
        fp = osmium.FileProcessor(str(path)).with_locations(storage).with_filter(
            osmium.filter.KeyFilter("highway", "amenity")
        )
        for obj in fp:
            if isinstance(obj, osmium.osm.Way):
                if obj.tags.get("highway") not in tilecache.HIGHWAY_VALUES and obj.tags.get("amenity") != "parking":
                    continue
                way_bbox = _way_bbox(obj)
                if way_bbox is None or not tiles.bbox_overlaps(way_bbox, bbox):
                    continue
                tags = dict(obj.tags)
                el = _parking_center_element(obj, tags) or _way_element(obj, tags)
                if el is not None:
                    elements.append(el)
            elif isinstance(obj, osmium.osm.Node):
                lat, lon = obj.location.lat, obj.location.lon
                if not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
                    continue
                el = _node_element(obj, dict(obj.tags))
                if el is not None:
                    elements.append(el)
    return elements
