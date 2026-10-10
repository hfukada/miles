"""Elevation sampling behind one sampler interface.

The DEM is AWS's global Terrarium-encoded PNG tiles (slippy-map x/y/z),
cached as fetched bytes in elevation_tiles keyed by (provider, zoom, x, y)
and never expired. The provider column is always "terrarium" and stays in the
key to avoid a migration.

The sampler reads the cache alone, interpolates bilinearly, and returns None
outside cached coverage (ocean tiles 404) rather than fabricating a value.
ensure_tiles fetches what a caller will sample, outside any transaction. Any
fetch failure but a 404 raises ElevationFetchError and caches nothing, so "no
data here" is never confused with "couldn't ask".
"""

import io
import math
import os
import sqlite3
import urllib.error
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable, Iterable, Protocol, Sequence, cast

from PIL import Image

from . import http_client, metrics
from .errors import describe_error, redact

TERRARIUM_URL = os.environ.get(
    "MILES_TERRARIUM_URL", "https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png"
)
TERRARIUM_ZOOM = 13
TERRARIUM_TILE_PX = 256

class ElevationSampler(Protocol):
    def sample(self, points: Sequence[tuple[float, float]]) -> list[float | None]:
        """Meters of elevation for each (lat, lng) point, None where the
        provider has no data there."""
        ...


class ElevationFetchError(RuntimeError):
    """A tile or raster could not be obtained or was not valid, as opposed to
    the provider having no data there."""


# A decoded Terrarium tile is ~200 KB of pixels.
_TERRARIUM_MEMO_TILES = 64


def _fetch(policy: http_client.ServicePolicy, url: str) -> bytes | None:
    """The response body, None for a 404, or ElevationFetchError once retries
    are exhausted."""
    try:
        return http_client.request(policy, url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise ElevationFetchError(f"{policy.name} request failed: HTTP {e.code} for {redact(url)}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ElevationFetchError(f"{policy.name} request failed: {describe_error(e, None)} for {redact(url)}") from e


def _fetch_terrarium_tile(url: str) -> bytes | None:
    return _fetch(http_client.TERRARIUM, url)


def _decode_terrarium(data: bytes) -> Image.Image | None:
    """The tile as an RGB image, or None when the bytes are not a full-size PNG
    (an error page served as 200, a truncated body)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format != "PNG" or img.size != (TERRARIUM_TILE_PX, TERRARIUM_TILE_PX):
                return None
            return img.convert("RGB")
    except (OSError, ValueError):
        return None


def _lonlat_to_terrarium_pixel(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    """Fractional global pixel coordinates at a slippy-map zoom level, for
    bilinear interpolation."""
    n = 2 ** zoom
    x = (lng + 180.0) / 360.0 * n * TERRARIUM_TILE_PX
    lat_rad = math.radians(max(min(lat, 85.05), -85.05))
    y = (
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * n
        * TERRARIUM_TILE_PX
    )
    return x, y


def terrarium_tiles_covering(
    south: float, west: float, north: float, east: float, zoom: int = TERRARIUM_ZOOM, margin_px: int = 1
) -> list[tuple[int, int]]:
    """Every DEM tile (x, y) a bilinear sample in the box can read; margin_px
    covers the neighbouring pixel at the box's edge."""
    x_min, y_max = _lonlat_to_terrarium_pixel(south, west, zoom)
    x_max, y_min = _lonlat_to_terrarium_pixel(north, east, zoom)
    last = 2 ** zoom - 1
    tx0 = max(int(math.floor(x_min - margin_px)) // TERRARIUM_TILE_PX, 0)
    tx1 = min(int(math.floor(x_max + margin_px + 1)) // TERRARIUM_TILE_PX, last)
    ty0 = max(int(math.floor(y_min - margin_px)) // TERRARIUM_TILE_PX, 0)
    ty1 = min(int(math.floor(y_max + margin_px + 1)) // TERRARIUM_TILE_PX, last)
    return [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]


def ensure_tiles(
    conn: sqlite3.Connection,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    points: Iterable[tuple[float, float]] = (),
    zoom: int = TERRARIUM_ZOOM,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Fetch every uncached DEM tile a sample over bbox (south, west, north,
    east) or along the path through points can read; each consecutive pair's
    bounding box is covered. Each tile is committed as it arrives, so the
    caller must not be in a transaction (RuntimeError): its writes would be
    published half-finished and the lock held across network fetches."""
    if conn.in_transaction:
        raise RuntimeError("ensure_tiles commits each tile and must run outside a transaction")
    if bbox is not None:
        needed = terrarium_tiles_covering(*bbox, zoom=zoom)
    else:
        path = list(points)
        segments = zip(path, path[1:]) if len(path) > 1 else ((p, p) for p in path)
        needed = sorted(
            {
                t
                for (lat_a, lng_a), (lat_b, lng_b) in segments
                for t in terrarium_tiles_covering(
                    min(lat_a, lat_b), min(lng_a, lng_b), max(lat_a, lat_b), max(lng_a, lng_b), zoom=zoom
                )
            }
        )
    if on_progress:
        on_progress(0, len(needed))
    for done, (tx, ty) in enumerate(needed, 1):
        _ensure_tile(conn, zoom, tx, ty)
        if on_progress:
            on_progress(done, len(needed))


def _ensure_tile(conn: sqlite3.Connection, zoom: int, tx: int, ty: int) -> None:
    row = conn.execute(
        "SELECT data FROM elevation_tiles WHERE provider = 'terrarium' AND zoom = ? AND x = ? AND y = ?",
        (zoom, tx, ty),
    ).fetchone()
    if row is not None and _decode_terrarium(row["data"]) is not None:
        return
    data = _fetch_terrarium_tile(TERRARIUM_URL.format(z=zoom, x=tx, y=ty))
    if data is None:
        return
    if _decode_terrarium(data) is None:
        raise ElevationFetchError(f"terrarium tile {zoom}/{tx}/{ty} is not a valid PNG tile")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO elevation_tiles (provider, zoom, x, y, fetched_at, data) VALUES ('terrarium', ?, ?, ?, ?, ?)",
            (zoom, tx, ty, datetime.now(timezone.utc).isoformat(), data),
        )
        conn.commit()
    except sqlite3.OperationalError as e:
        conn.rollback()
        raise ElevationFetchError(f"terrarium tile {zoom}/{tx}/{ty} could not be cached: {e}") from e


class TerrariumSampler:
    """Cache-only: a tile not in elevation_tiles reads as missing coverage."""

    def __init__(self, conn: sqlite3.Connection, zoom: int = TERRARIUM_ZOOM) -> None:
        self._conn = conn
        self._zoom = zoom
        # Bounded, so a large area doesn't keep every decoded tile.
        self._tile_image = lru_cache(maxsize=_TERRARIUM_MEMO_TILES)(self._read_tile_image)

    def _read_tile_image(self, tx: int, ty: int) -> Image.Image | None:
        row = self._conn.execute(
            "SELECT data FROM elevation_tiles WHERE provider = 'terrarium' AND zoom = ? AND x = ? AND y = ?",
            (self._zoom, tx, ty),
        ).fetchone()
        # A corrupt cached tile counts as missing.
        image = _decode_terrarium(row["data"]) if row is not None else None
        metrics.cache("elevation_tiles", "miss" if image is None else "hit")
        return image

    def _pixel_elevation(self, global_x: int, global_y: int) -> float | None:
        tx, ty = global_x // TERRARIUM_TILE_PX, global_y // TERRARIUM_TILE_PX
        image = self._tile_image(tx, ty)
        if image is None:
            return None
        px, py = global_x % TERRARIUM_TILE_PX, global_y % TERRARIUM_TILE_PX
        pixel = cast(tuple[int, int, int], image.getpixel((px, py)))
        r, g, b = pixel
        return (r * 256 + g + b / 256.0) - 32768.0

    def sample(self, points: Sequence[tuple[float, float]]) -> list[float | None]:
        results: list[float | None] = []
        for lat, lng in points:
            gx, gy = _lonlat_to_terrarium_pixel(lat, lng, self._zoom)
            x0, y0 = int(math.floor(gx)), int(math.floor(gy))
            fx, fy = gx - x0, gy - y0
            corners = {}
            for dx in (0, 1):
                for dy in (0, 1):
                    corners[(dx, dy)] = self._pixel_elevation(x0 + dx, y0 + dy)
            if any(v is None for v in corners.values()):
                results.append(None)
                continue
            top = corners[(0, 0)] * (1 - fx) + corners[(1, 0)] * fx  # type: ignore[operator]
            bottom = corners[(0, 1)] * (1 - fx) + corners[(1, 1)] * fx  # type: ignore[operator]
            results.append(top * (1 - fy) + bottom * fy)
        return results


def sampler_for(conn: sqlite3.Connection) -> ElevationSampler:
    """A sampler over what is cached in elevation_tiles; no network, so request
    and startup paths can't hang on a fetch. Callers that may wait run
    ensure_tiles first."""
    return TerrariumSampler(conn)
