import io
import math
import sqlite3
import urllib.error

import pytest
from PIL import Image

from miles import db, elevation


def _terrarium_encode(meters: float) -> tuple[int, int, int]:
    value = round(meters) + 32768
    return (value // 256, value % 256, 0)


def _pixel_to_lonlat(zoom: int, px: float, py: float) -> tuple[float, float]:
    """Inverse of elevation._lonlat_to_terrarium_pixel, used only to pick
    test coordinates that land on known pixels -- not part of the sampler."""
    n = 2 ** zoom
    lng = px / (n * elevation.TERRARIUM_TILE_PX) * 360.0 - 180.0
    y_frac = py / (n * elevation.TERRARIUM_TILE_PX)
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y_frac)))
    return (math.degrees(lat_rad), lng)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _tile_png(meters: float) -> bytes:
    return _png(Image.new("RGB", (elevation.TERRARIUM_TILE_PX,) * 2, _terrarium_encode(meters)))


def _cache_tile(conn: sqlite3.Connection, zoom: int, tx: int, ty: int, data: bytes) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO elevation_tiles (zoom, x, y, fetched_at, data) VALUES (?, ?, ?, 'now', ?)",
        (zoom, tx, ty, data),
    )
    conn.commit()


def _tile_point(zoom: int = 5, tx: int = 3, ty: int = 7) -> tuple[float, float]:
    return _pixel_to_lonlat(zoom, tx * elevation.TERRARIUM_TILE_PX + 10.0, ty * elevation.TERRARIUM_TILE_PX + 20.0)


def _cached_tile_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM elevation_tiles").fetchone()[0]


class _TileServer:
    """The provider behind http_client.request: answers each tile URL with
    `outcome` (bytes, or an exception to raise) and records the URLs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, outcome: bytes | Exception = b"") -> None:
        self.outcome = outcome
        self.urls: list[str] = []
        monkeypatch.setattr(elevation.http_client, "request", self._request)

    def _request(self, policy, url, **kwargs):
        self.urls.append(url)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def no_fetch(monkeypatch: pytest.MonkeyPatch) -> _TileServer:
    """A provider that records requests; tests assert none were made."""
    return _TileServer(monkeypatch, AssertionError("the sampler must not fetch"))


def test_terrarium_sampler_exact_pixel_matches_encoded_elevation(conn: sqlite3.Connection, no_fetch: _TileServer):
    # A flat 500m tile: bilinear interpolation over a constant field returns
    # the constant regardless of the query point's fractional pixel offset.
    _cache_tile(conn, 5, 3, 7, _tile_png(500.0))

    assert elevation.TerrariumSampler(conn, zoom=5).sample([_tile_point()]) == [pytest.approx(500.0, abs=0.01)]
    assert no_fetch.urls == []


def test_terrarium_sampler_interpolates_a_gradient(conn: sqlite3.Connection):
    zoom = 5
    n = elevation.TERRARIUM_TILE_PX

    img = Image.new("RGB", (n, n))
    pixels = img.load()
    for x in range(n):
        for y in range(n):
            pixels[x, y] = _terrarium_encode(float(x))  # elevation == pixel column
    _cache_tile(conn, zoom, 0, 0, _png(img))

    # Exactly halfway between columns 10 and 11 should average to 10.5.
    lat, lng = _pixel_to_lonlat(zoom, 10.5, 50.0)
    assert elevation.TerrariumSampler(conn, zoom=zoom).sample([(lat, lng)]) == [pytest.approx(10.5, abs=0.01)]


def test_a_corrupt_cached_tile_reads_as_missing_without_writing_and_is_refetched(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _cache_tile(conn, 5, 3, 7, b"garbage")

    assert elevation.TerrariumSampler(conn, zoom=5).sample([_tile_point()]) == [None]
    assert not conn.in_transaction
    assert _cached_tile_count(conn) == 1

    _TileServer(monkeypatch, _tile_png(500.0))
    elevation.ensure_tiles(conn, points=[_tile_point()], zoom=5)
    assert elevation.TerrariumSampler(conn, zoom=5).sample([_tile_point()]) == [pytest.approx(500.0, abs=0.01)]


def test_ensure_tiles_fetches_and_caches_each_missing_tile_once(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    server = _TileServer(monkeypatch, _tile_png(500.0))
    lat, lng = _tile_point(5, 3, 7)

    elevation.ensure_tiles(conn, points=[(lat, lng), (lat, lng)], zoom=5)
    assert server.urls and all("/5/" in u for u in server.urls)
    fetched_once = len(server.urls)
    assert _cached_tile_count(conn) == fetched_once
    assert elevation.TerrariumSampler(conn, zoom=5).sample([(lat, lng)]) == [pytest.approx(500.0, abs=0.01)]

    elevation.ensure_tiles(conn, points=[(lat, lng)], zoom=5)
    assert len(server.urls) == fetched_once


def test_ensure_tiles_refuses_to_run_inside_a_transaction(conn: sqlite3.Connection, no_fetch: _TileServer):
    conn.execute("INSERT INTO meta (key, value) VALUES ('probe', '1')")
    assert conn.in_transaction
    with pytest.raises(RuntimeError):
        elevation.ensure_tiles(conn, points=[(1.0, 2.0)])


@pytest.mark.parametrize(
    "body, locked",
    [(b"<html>slow down</html>", False), (_png(Image.new("RGB", (16, 16), (0, 0, 0))), False), (_tile_png(100.0), True)],
    ids=["not a png", "wrong size", "a valid tile that cannot be written"],
)
def test_ensure_tiles_caches_nothing_and_raises_for_a_body_that_is_not_a_full_size_png_or_cannot_be_written(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, body: bytes, locked: bool
):
    _TileServer(monkeypatch, body)
    if locked:
        holder = db.connect()
        holder.execute("BEGIN IMMEDIATE")
        conn = sqlite3.connect(db.DB_PATH, timeout=0)
    with pytest.raises(elevation.ElevationFetchError):
        elevation.ensure_tiles(conn, points=[(1.0, 2.0)])
    if locked:
        holder.rollback()
    else:
        assert _cached_tile_count(conn) == 0


def test_fetch_failure_raises_rather_than_reading_as_no_data(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _TileServer(monkeypatch, urllib.error.URLError("connection refused"))
    with pytest.raises(elevation.ElevationFetchError):
        elevation.ensure_tiles(conn, points=[(1.0, 2.0)])


def test_a_404_tile_reads_as_no_data_and_the_sampler_never_fetches_it(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    server = _TileServer(monkeypatch, urllib.error.HTTPError("http://x", 404, "not found", {}, None))  # type: ignore[arg-type]
    elevation.ensure_tiles(conn, points=[(1.0, 2.0)])
    assert _cached_tile_count(conn) == 0
    asked = len(server.urls)
    assert elevation.sampler_for(conn).sample([(1.0, 2.0)]) == [None]
    assert len(server.urls) == asked


def test_fetching_a_tile_commits_at_once_and_does_not_hold_the_callers_write_lock(monkeypatch: pytest.MonkeyPatch):
    caller = db.connect()
    during_fetch: list[bool] = []

    def fetch(policy, url, **kwargs) -> bytes:
        probe = sqlite3.connect(db.DB_PATH, timeout=0.2)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
            during_fetch.append(True)
        except sqlite3.OperationalError:
            during_fetch.append(False)
        finally:
            probe.close()
        return _tile_png(100.0)

    monkeypatch.setattr(elevation.http_client, "request", fetch)
    elevation.ensure_tiles(caller, points=[_tile_point()], zoom=5)
    assert during_fetch and all(during_fetch)
    assert not caller.in_transaction  # each fetched tile is committed at once
    assert _cached_tile_count(db.connect()) > 0


def test_the_covering_tiles_include_every_tile_a_sample_inside_the_box_can_read(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    server = _TileServer(monkeypatch, _tile_png(100.0))
    elevation.ensure_tiles(conn, points=[_tile_point(5, 3, 7), _tile_point(5, 6, 7)], zoom=5)
    assert {u.rsplit("/", 2)[-2] for u in server.urls} >= {"3", "4", "5", "6"}  # tiles between two distant points

    south, west, north, east = 1.1, 2.4, 1.45, 2.8
    covering = set(elevation.terrarium_tiles_covering(south, west, north, east))
    steps = 40
    for i in range(steps + 1):
        for j in range(steps + 1):
            lat = south + (north - south) * i / steps
            lng = west + (east - west) * j / steps
            gx, gy = elevation._lonlat_to_terrarium_pixel(lat, lng, elevation.TERRARIUM_ZOOM)
            for dx in (0, 1):
                for dy in (0, 1):
                    tile = (int(gx + dx) // elevation.TERRARIUM_TILE_PX, int(gy + dy) // elevation.TERRARIUM_TILE_PX)
                    assert tile in covering
