"""Regional OpenStreetMap extracts (Geofabrik .osm.pbf), the only map source
for area import. A registered extract is ground truth like an uploaded GPX
(adr/0002, adr/0003): its file and sha256 are only replaced
wholesale on an explicit refresh, never by age. A refresh is a conditional
request that a 304 answers without a download.

geofabrik.py chooses which regions cover an area; one at a border is cut from
several extracts. cut_area filters each extract down to the tile payloads
tilecache.py stores and merges them, so callers never need to know which
extracts produced a tile.

Parking is mostly mapped as a closed way, so cutting folds an amenity=parking
way into a synthetic point amenity (_parking_center_element).
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
from typing import cast
from typing_extensions import TypedDict

import osmium
import osmium.filter
import osmium.io
import osmium.osm

from . import db, geofabrik, http_client, metrics, tilecache, tiles

GEOFABRIK_BASE_URL = geofabrik.GEOFABRIK_BASE_URL

_logger = logging.getLogger(__name__)

EXTRACT_DIR = db.DB_PATH.parent / "extracts"

# A registered extract is not re-checked within this long of its last fetch or
# check, even when a caller asks.
EXTRACT_MIN_REFETCH_INTERVAL_HOURS = 24.0

# No GET for a region within this long of the last one, whatever came of it,
# so a retry loop cannot re-download a large file. Force obeys it too.
REQUEST_COOLDOWN_HOURS = 1.0

# Larger extracts are refused unless MILES_ALLOW_LARGE_EXTRACTS is set:
# continent-sized files take tens of gigabytes to cut.
EXTRACT_MAX_BYTES = 2 * 1024**3
ALLOW_LARGE_ENV = "MILES_ALLOW_LARGE_EXTRACTS"

# A killed download leaves a .part file; ones this old are swept before the next download.
_STALE_PART_AGE_S = 24 * 3600.0

# Above this file size cut_area's node-location index moves to disk: an
# in-memory index runs at several times the file's size.
DISK_INDEX_THRESHOLD_BYTES = 200 * 1024 * 1024

_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
PROGRESS_INTERVAL_S = 1.0


class ExtractDownloadError(Exception):
    """An extract could not be obtained: the download, its checksum, the region, or a local file failed."""


class ExtractTooLargeError(ExtractDownloadError):
    """The extract is over EXTRACT_MAX_BYTES. Refused from the declared size
    before any GET, so it starts no cooldown."""


class ExtractChecksumUnavailableError(ExtractDownloadError):
    """The published checksum could not be fetched. The body is never requested
    without it; the cooldown still applies."""


class ExtractRecord(TypedDict):
    region: str
    path: str
    sha256: str
    fetched_at: str
    # When the file was last confirmed current (a download or a 304).
    checked_at: str
    # The server's Last-Modified (ISO, UTC) when it gave one, else fetched_at:
    # the date the data in the file is as of.
    data_as_of: str
    etag: str | None
    last_modified: str | None


def init_extract_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS map_extracts (
            region        TEXT PRIMARY KEY,
            path          TEXT NOT NULL,
            sha256        TEXT NOT NULL,
            fetched_at    TEXT NOT NULL,
            checked_at    TEXT NOT NULL,
            data_as_of    TEXT NOT NULL,
            etag          TEXT,
            last_modified TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extract_requests (
            region       TEXT PRIMARY KEY,
            requested_at TEXT NOT NULL
        )
    """)
    geofabrik.init_index_schema(conn)
    conn.commit()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _row_to_record(row: sqlite3.Row) -> ExtractRecord:
    return cast(ExtractRecord, dict(row))


def get_extract(conn: sqlite3.Connection, region: str) -> ExtractRecord | None:
    row = conn.execute("SELECT * FROM map_extracts WHERE region = ?", (region,)).fetchone()
    return _row_to_record(row) if row is not None else None


def list_extracts(conn: sqlite3.Connection) -> list[ExtractRecord]:
    return [_row_to_record(r) for r in conn.execute("SELECT * FROM map_extracts ORDER BY region").fetchall()]


# Region name for an extract registered from a file the user supplies.
LOCAL_REGION_PREFIX = "local:"


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
    """The regions to cut bbox from, in select_extracts' precedence. Without
    allow_network, only stored data is used and the result is empty when it
    can't say; with it, a missing index is fetched, a preferred region is
    validated, and an uncovered area is an error. Raises geofabrik.RegionError."""
    paths = split_regions(preferred_region)
    if paths:
        if allow_network:
            for path in paths:
                geofabrik.validate_region(conn, path)
        return paths
    covering = geofabrik.choose_regions(conn, bbox) if allow_network else geofabrik.choose_cached_regions(conn, bbox)
    return covering or []


# Called with the bytes written so far and the declared size (None if none).
DownloadProgress = Callable[[int, int | None], None]


def throttled_progress(
    on_progress: DownloadProgress, *, interval: float = PROGRESS_INTERVAL_S, clock: Callable[[], float] = time.monotonic
) -> Callable[..., None]:
    """A reporter(written, expected, force) forwarding to on_progress at most
    once per interval, and always when force is true. A failing on_progress is
    logged and dropped so it can't fail the download."""
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

    - the preferred_region (one path or several), if given;
    - the cheapest Geofabrik regions whose boundaries cover bbox
      (geofabrik.choose_regions).

    Paths are looked up in the registry; a missing region or file is
    downloaded, one after another.

    refresh=True first asks Geofabrik for a newer file (see download_extract). force=True replaces the file
    and reports failure instead of falling back. on_wait is called once if a
    download waits on another thread's download of the same region;
    on_progress receives bytes written and expected total. A region over the
    size cap is refused, naming the smaller regions that would cover the area."""
    try:
        paths = planned_regions(conn, bbox, preferred_region, allow_network=True)
    except geofabrik.RegionError as e:
        raise ExtractDownloadError(str(e)) from e
    chosen: list[ExtractRecord] = []
    for path in paths:
        try:
            chosen.append(
                download_extract(conn, path, refresh=refresh, force=force, on_wait=on_wait, on_progress=on_progress)
            )
        except ExtractTooLargeError as e:
            raise ExtractTooLargeError(f"{e}{_too_large_hint(conn, path, bbox)}") from e
    return chosen


def tile_extracts(
    conn: sqlite3.Connection, selected: Sequence[ExtractRecord], tile_ids: Iterable[tuple[int, int]]
) -> dict[tuple[int, int], list[ExtractRecord]]:
    """The extracts each tile is cut from: every registered extract with its
    file present whose region polygon (from the stored Geofabrik index) shares
    area with the tile, plus the selected extracts. A region missing from the
    index touches only the tiles of a selected extract. A region is dropped
    from a tile when a registered region nested under its path contains the
    whole tile, as the parent is far larger to read; a tile straddling the
    child's edge keeps the parent. A tile no extract touches falls back to
    the selection."""
    tile_list = list(tile_ids)
    known = geofabrik.cached_regions(conn)
    by_path = {r.path: r for r in known} if known is not None else {}
    chosen = {e["region"] for e in selected}
    others = [
        e for e in list_extracts(conn)
        if e["region"] not in chosen and Path(e["path"]).is_file()
    ]
    candidates = [*selected, *others]

    def touches(e: ExtractRecord, bounds: tuple[float, float, float, float]) -> bool:
        region = by_path.get(e["region"])
        if region is not None:
            return geofabrik.intersects(region, bounds)
        return e["region"] in chosen

    def contains(e: ExtractRecord, bounds: tuple[float, float, float, float]) -> bool:
        region = by_path.get(e["region"])
        return region is not None and geofabrik.contains_bbox(region, bounds)

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
    """True when every tile was cut at the current query version from exactly
    the extracts tile_extracts names for it, each at its current fetched_at,
    so cutting again would reproduce the cache."""
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
    """Register a file already on disk as region's extract, without
    downloading (a --pbf upload, or a file download_extract just placed).
    etag and last_modified are the server's validators; data_as_of is the
    Last-Modified date, else now."""
    path = Path(path)
    if not path.is_file():
        raise ExtractDownloadError(f"no such file: {path}")
    sha256 = _sha256_file(path)
    now = datetime.now(timezone.utc).isoformat()
    data_as_of = _http_date_to_iso(last_modified) or now
    conn.execute(
        """
        INSERT INTO map_extracts (
            region, path, sha256, fetched_at, checked_at, data_as_of, etag, last_modified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(region) DO UPDATE SET
            path = excluded.path, sha256 = excluded.sha256, fetched_at = excluded.fetched_at,
            checked_at = excluded.checked_at, data_as_of = excluded.data_as_of,
            etag = excluded.etag, last_modified = excluded.last_modified
        """,
        (region, str(path), sha256, now, now, data_as_of, etag, last_modified),
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
    """A reader returning the post-redirect URL and declared size without
    reading the body."""

    def read(resp: object) -> tuple[str, int | None]:
        return getattr(resp, "geturl", lambda: requested)(), _declared_size(resp)

    return read


def _published_md5(file_url: str) -> str:
    """The digest published beside file_url, which must be the URL the body was
    served from: `-latest` redirects to a dated file, and a rebuild in between
    would pair a new digest with an old file."""
    try:
        return _md5_of(http_client.request(http_client.GEOFABRIK, f"{file_url}.md5"))
    except (OSError, http.client.HTTPException) as e:
        raise ExtractChecksumUnavailableError(f"could not fetch the checksum for {file_url}: {e}") from e


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
    on_progress: DownloadProgress | None = None,
    on_request: Callable[[], None] | None = None,
) -> _Download | None:
    """Stream url to a unique .part file beside dest, verify it, and move it
    over dest. Returns the response's validators, or None on a 304 to the
    conditional headers in validators (dest untouched).

    A HEAD comes first for the redirect target and declared size, refused over
    EXTRACT_MAX_BYTES unless ALLOW_LARGE_ENV is set. on_request is called once, just
    before the first GET.

    Verified before dest is replaced: bytes written equal Content-Length (a cut
    connection otherwise leaves a truncated file), the size is within the cap,
    and the MD5 matches the published `<url>.md5`. The .part file is removed
    on every exit. on_progress gets 0 bytes first, then at most once per
    PROGRESS_INTERVAL_S, then the final count."""
    part = dest.with_name(f"{dest.name}.{os.getpid()}.{secrets.token_hex(4)}.part")
    allowed = os.environ.get(ALLOW_LARGE_ENV, "").strip().lower() in ("1", "true", "yes")

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
        # The checksum is fetched first so a missing one costs two small
        # requests, not the file; the body then comes from the same URL.
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
        # URLError, timeouts and resets are OSErrors; IncompleteRead is an HTTPException.
        raise ExtractDownloadError(f"failed to download {url}: {e}") from e
    finally:
        part.unlink(missing_ok=True)


def _conditional_headers(existing: ExtractRecord) -> dict[str, str]:
    """Validators for asking whether the stored file is current; a file
    registered without any is compared by its download time."""
    if not Path(existing["path"]).is_file():
        return {}
    return http_client.conditional_headers(existing["etag"], existing["last_modified"]) or {
        "If-Modified-Since": format_datetime(datetime.fromisoformat(existing["fetched_at"]), usegmt=True)
    }


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
    """(registered extract, conditional headers) when Geofabrik must be
    contacted, or (registered extract, None) when the registered one is used
    as is."""
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
    refresh: bool = False,
    force: bool = False,
    on_wait: Callable[[], None] | None = None,
    on_progress: DownloadProgress | None = None,
) -> ExtractRecord:
    """Register region's Geofabrik extract, downloading only if none is
    registered or its file is gone. A registered extract is returned as is
    unless refresh=True, which makes a conditional request: a 304 only updates
    checked_at, a 200 replaces the file, and a failure leaves the registered
    file in use. force=True downloads unconditionally, replacing a bad but
    registered file, and reports failure.

    Within EXTRACT_MIN_REFETCH_INTERVAL_HOURS of its last success a registered
    extract is not asked about again, and within REQUEST_COOLDOWN_HOURS of the
    last GET (force included) no GET is made, so a retry loop can't hammer
    Geofabrik. A size refusal precedes any GET and starts no cooldown. A
    per-region lock stops two importers fetching one file; a waiter then uses
    the holder's file. on_wait is called once if the lock is taken;
    on_progress receives bytes written and expected total (not for a 304)."""
    registered = get_extract(conn, region)
    fallback = registered if registered is not None and refresh and not force and Path(registered["path"]).is_file() else None
    try:
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
            url = f"{GEOFABRIK_BASE_URL.rstrip('/')}/{region}-latest.osm.pbf"
            downloaded = _download_with_backoff(
                url, path, validators, on_progress=on_progress, on_request=lambda: _stamp_request(conn, region)
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
            return register_local_extract(
                conn, path, region, etag=downloaded["etag"], last_modified=downloaded["last_modified"]
            )
        finally:
            lock.release()
    except ExtractDownloadError:
        if fallback is None:
            raise
        return fallback


# ---------------------------------------------------------------------------
# Cutting an area out of an extract
# ---------------------------------------------------------------------------


@contextmanager
def _location_index(path: Path) -> Iterator[str]:
    """The node-location storage spec for one cut: in memory for a small file,
    else a throwaway on-disk index. Sparse stores only the node ids present;
    a dense one allocates the whole id range."""
    if path.stat().st_size < DISK_INDEX_THRESHOLD_BYTES:
        yield "flex_mem"
        return
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=EXTRACT_DIR, prefix=".locations-") as tmp:
        yield f"sparse_file_array,{Path(tmp) / 'locations.idx'}"


def _way_bbox(obj: osmium.osm.Way) -> tuple[float, float, float, float] | None:
    """A way's bbox over its located nodes, as running extremes so ways outside
    the area are rejected before their tags or geometry are materialized."""
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
    """A closed amenity=parking way as a synthetic point amenity. The id is the
    way's negated, so it can't collide with a mapped node (OSM ids are positive)."""
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
    """The union of several cuts' elements, each OSM element once (keyed by type
    and id). An extract may hold only part of a border way's nodes: copies with
    the same node list are combined point by point; otherwise the copy with
    more located points wins (the earlier on a tie)."""
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
    """Write the map_tiles cache for every 0.05 deg tile bbox touches. Each tile
    is cut whole, so overlapping areas write the same content.

    A tile's content depends on the files alone: tile_extracts names its
    extracts (every registered region intersecting it, not only those passed
    in), each file gets one filtered pass over the tiles it serves, and
    merge_elements dedupes. Each write replaces the whole row in its own
    transaction, so upstream deletions disappear on the next cut. The tile's
    source lists its regions and its fetched_at is the oldest among them. A
    later Geofabrik cut replaces tiles from a local file. Returns the (gx, gy)
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
    """The tile-payload elements of one extract's file that fall in bbox. The
    KeyFilter keeps every highway/amenity way in the file, so tag and bbox
    checks precede building elements; otherwise memory scales with the extract."""
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
