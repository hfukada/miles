"""The map-tile cache: one gzipped payload per fixed grid tile (tiles.py),
shaped like an Overpass JSON response (`{"elements": [...]}` of ways with
inline point geometry and point nodes) whatever produced it. The payload is
ground truth like a synced activity's raw_json or an uploaded GPX's raw bytes
(adr/0002): stored with its sha256, the query version that produced it, and
the source it was cut from. The trail/road graph is derived from these blobs
and rebuilt wholesale when the query version bumps.
"""

import gzip
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import NamedTuple

# Bumped whenever the cut changes what a tile holds -- gates a full re-cut of
# every cached tile, the same contract ROUTE_DERIVE_VERSION uses for uploaded
# GPX.
#
# v2 adds amenity=parking ways folded into point amenities, so a lot mapped
# as a closed way (most of them, per OSM's tagging conventions) is not
# invisible to build_network.
TILE_QUERY_VERSION = 2

# highway values relevant to a runner: every foot/trail class plus every
# road class (a road is where a route starts/ends, or is itself run on).
# Motorways are included so the graph can measure how close a trail runs to
# a highway, even though motorways are never routable edges.
HIGHWAY_VALUES = [
    "motorway", "motorway_link", "trunk", "trunk_link",
    "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential",
    "service", "living_street",
    "path", "track", "footway", "cycleway", "bridleway", "steps", "pedestrian",
]

# (key, value) node tags worth surfacing as points of interest.
NODE_TAGS = [
    ("highway", "trailhead"),
    ("amenity", "parking"),
    ("amenity", "drinking_water"),
    ("amenity", "toilets"),
]


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """BEGIN IMMEDIATE ... COMMIT around a block, rolled back if it raises.
    The connection must not already be in a transaction: SQLite cannot nest
    one, and committing the caller's pending writes here would be silent."""
    if conn.in_transaction:
        raise RuntimeError("immediate_transaction needs a connection with no transaction open")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


class EncodedTile(NamedTuple):
    sha256: str
    data: bytes


def encode_tile_elements(elements: list[dict]) -> EncodedTile:
    """The stored form of a tile's elements. Serializing and compressing are
    CPU-bound, so a caller that holds the write lock encodes first."""
    raw = json.dumps({"elements": elements}).encode("utf-8")
    return EncodedTile(hashlib.sha256(raw).hexdigest(), gzip.compress(raw))


def write_tile_elements(
    conn: sqlite3.Connection,
    gx: int,
    gy: int,
    elements: list[dict] | EncodedTile,
    *,
    source: str,
    fetched_at: str | None = None,
    inputs: dict[str, str] | None = None,
) -> None:
    """Replace a tile's row with these elements inside the caller's
    transaction, which it neither opens nor commits. fetched_at defaults to
    now. inputs maps each extract region the elements were cut from to that
    file's fetched_at, so a later cut can tell whether the tile still
    reflects the current files."""
    encoded = elements if isinstance(elements, EncodedTile) else encode_tile_elements(elements)
    conn.execute(
        """
        INSERT INTO map_tiles (gx, gy, query_version, fetched_at, sha256, data, source, inputs)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(gx, gy) DO UPDATE SET
            query_version = excluded.query_version,
            fetched_at = excluded.fetched_at,
            sha256 = excluded.sha256,
            data = excluded.data,
            source = excluded.source,
            inputs = excluded.inputs
        """,
        (
            gx, gy, TILE_QUERY_VERSION, fetched_at or datetime.now(timezone.utc).isoformat(), encoded.sha256,
            encoded.data, source, None if inputs is None else json.dumps(inputs, sort_keys=True),
        ),
    )


def store_tile_elements(
    conn: sqlite3.Connection,
    gx: int,
    gy: int,
    elements: list[dict],
    *,
    source: str,
    fetched_at: str | None = None,
    inputs: dict[str, str] | None = None,
) -> None:
    """write_tile_elements as its own committed write, the one storage tail
    every source shares so a tile's cache is identical in shape however it
    was filled."""
    encoded = encode_tile_elements(elements)
    with immediate_transaction(conn):
        write_tile_elements(conn, gx, gy, encoded, source=source, fetched_at=fetched_at, inputs=inputs)


def decode_tile_payload(data: bytes) -> dict:
    """Inverse of the gzip storage store_tile_elements writes."""
    return json.loads(gzip.decompress(data))


def get_tile_payload(conn: sqlite3.Connection, gx: int, gy: int) -> dict | None:
    """Parsed payload for a cached tile, or None if it isn't cached."""
    row = conn.execute("SELECT data FROM map_tiles WHERE gx = ? AND gy = ?", (gx, gy)).fetchone()
    if row is None:
        return None
    return decode_tile_payload(row["data"])
