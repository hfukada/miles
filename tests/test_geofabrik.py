import gzip
import json
import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from helpers import memory_db

from miles import geofabrik


def _box(south: float, west: float, north: float, east: float) -> list:
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


def _feature(path: str | None, geometry: dict | None) -> dict:
    urls = {"pbf": f"https://download.geofabrik.de/{path}-latest.osm.pbf"} if path else {}
    return {"type": "Feature", "properties": {"id": (path or "none").split("/")[-1], "urls": urls}, "geometry": geometry}


def _polygon(south: float, west: float, north: float, east: float) -> dict:
    return {"type": "Polygon", "coordinates": _box(south, west, north, east)}


# Two states side by side inside a country, a country-sized parent, and a
# state with a hole (an enclave carved out of it).
INDEX = {
    "type": "FeatureCollection",
    "features": [
        _feature("world/land", _polygon(0, 0, 40, 40)),
        _feature("world/land/west", _polygon(0, 0, 40, 20)),
        _feature("world/land/east", _polygon(0, 20, 40, 40)),
        _feature("world/other/ringed", {
            "type": "Polygon",
            "coordinates": _box(50, 0, 60, 10) + _box(54, 4, 56, 6),
        }),
        _feature("world/other/islands", {
            "type": "MultiPolygon",
            "coordinates": [_box(70, 0, 71, 1), _box(70, 5, 71, 6)],
        }),
        _feature(None, _polygon(0, 0, 90, 90)),
        _feature("world/nogeometry", None),
    ],
}


def _raw() -> bytes:
    return json.dumps(INDEX).encode()


def test_parse_keeps_only_regions_with_an_extract_and_a_boundary():
    paths = [r.path for r in geofabrik.parse_index(_raw())]
    assert paths == ["world/land", "world/land/west", "world/land/east", "world/other/ringed", "world/other/islands"]


def test_the_smallest_containing_region_wins():
    regions = geofabrik.parse_index(_raw())
    found = geofabrik.smallest_containing(regions, (10, 2, 12, 4))
    assert found is not None and found.path == "world/land/west"


@pytest.mark.parametrize(
    ("box", "expected"),
    [
        ((10, 2, 12, 4), True),  # wholly inside
        ((10, 18, 12, 22), True),  # straddles the west/east border
        ((10, 20, 12, 22), True),  # touches west along its border
        ((10, 41, 12, 43), False),  # outside
        ((30, 10, 50, 12), True),  # polygon edge crosses the box, no vertex inside it
        ((0, 0, 40, 20), True),  # equals the region
        ((-5, -5, 45, 45), True),  # the region lies inside the box
    ],
    ids=["inside", "straddling a border", "touching a border", "outside", "edge crossing the box", "equal", "inside the box"],
)
def test_a_region_intersects_a_box_that_shares_any_point_with_its_polygon(box, expected):
    regions = {r.path: r for r in geofabrik.parse_index(_raw())}
    assert geofabrik.intersects(regions["world/land/west"], box) is expected


def test_a_box_inside_a_hole_does_not_intersect_the_region():
    regions = {r.path: r for r in geofabrik.parse_index(_raw())}
    assert geofabrik.intersects(regions["world/other/ringed"], (54.5, 4.5, 55.5, 5.5)) is False
    assert geofabrik.intersects(regions["world/other/ringed"], (52, 4.5, 55.5, 5.5)) is True


def test_an_area_straddling_two_regions_walks_up_to_their_parent():
    regions = geofabrik.parse_index(_raw())
    found = geofabrik.smallest_containing(regions, (10, 18, 12, 22))
    assert found is not None and found.path == "world/land"


def test_an_area_outside_every_region_has_no_match():
    regions = geofabrik.parse_index(_raw())
    assert geofabrik.smallest_containing(regions, (-20, -20, -19, -19)) is None


def test_a_hole_in_a_region_is_not_contained():
    regions = geofabrik.parse_index(_raw())
    assert geofabrik.smallest_containing(regions, (54.5, 4.5, 55.5, 5.5)) is None
    assert geofabrik.smallest_containing(regions, (51, 1, 52, 2)) is not None


def test_an_area_spanning_the_parts_of_a_multipolygon_is_not_contained():
    regions = geofabrik.parse_index(_raw())
    assert geofabrik.smallest_containing(regions, (70.2, 0.2, 70.8, 0.8)) is not None
    assert geofabrik.smallest_containing(regions, (70.2, 0.2, 70.8, 5.8)) is None


class _Served:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, raw: bytes = b"", *, status: int | None = None) -> None:
        self.raw = raw
        self.status = status
        self.requests: list[tuple[str, dict]] = []
        monkeypatch.setattr(geofabrik.http_client, "request", self)

    def __call__(self, policy, url, *, headers=None, reader=None, **kwargs):
        self.requests.append((url, headers or {}))
        if self.status is not None:
            if self.status == 0:
                raise urllib.error.URLError("down")
            raise urllib.error.HTTPError(url, self.status, "x", {}, None)  # type: ignore[arg-type]

        class Resp:
            headers = {"ETag": '"v1"', "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"}

            def __init__(self, raw: bytes) -> None:
                self.raw = raw

            def read(self) -> bytes:
                return self.raw

        return reader(Resp(self.raw))


def _age_index(conn: sqlite3.Connection, days: float) -> None:
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute("UPDATE geofabrik_index SET checked_at = ?, fetched_at = ?", (when, when))
    conn.commit()


def test_the_index_is_fetched_once_and_then_served_from_the_database(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    served = _Served(monkeypatch, _raw())

    assert geofabrik.choose_regions(conn, (10, 2, 12, 4)) == ["world/land/west"]
    assert geofabrik.choose_regions(conn, (10, 22, 12, 24)) == ["world/land/east"]

    assert len(served.requests) == 1
    assert served.requests[0][0].endswith("/index-v1.json")
    stored = conn.execute("SELECT data FROM geofabrik_index").fetchone()
    assert json.loads(gzip.decompress(stored["data"])) == INDEX


def test_an_old_index_is_refreshed_with_a_conditional_get_and_a_304_keeps_it(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    _Served(monkeypatch, _raw())
    geofabrik.regions(conn)
    _age_index(conn, 31)
    served = _Served(monkeypatch, status=304)

    assert geofabrik.choose_regions(conn, (10, 2, 12, 4)) == ["world/land/west"]

    assert served.requests[0][1] == {"If-None-Match": '"v1"', "If-Modified-Since": "Mon, 01 Jan 2024 00:00:00 GMT"}
    served = _Served(monkeypatch, status=500)
    geofabrik.regions(conn)
    assert served.requests == []


def test_a_failed_refresh_keeps_serving_the_stale_index(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    _Served(monkeypatch, _raw())
    geofabrik.regions(conn)
    _age_index(conn, 90)
    _Served(monkeypatch, status=0)

    assert geofabrik.choose_regions(conn, (10, 2, 12, 4)) == ["world/land/west"]


def test_a_failed_first_fetch_is_a_region_error(monkeypatch: pytest.MonkeyPatch):
    _Served(monkeypatch, status=0)
    with pytest.raises(geofabrik.RegionError, match="region index"):
        geofabrik.choose_regions(memory_db(), (10, 2, 12, 4))


def test_an_unparseable_index_is_not_stored(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    _Served(monkeypatch, b"<html>")
    with pytest.raises(geofabrik.RegionError, match="not valid"):
        geofabrik.regions(conn)
    assert geofabrik.cached_regions(conn) is None


def test_choosing_with_no_containing_region_points_at_the_pbf_import(monkeypatch: pytest.MonkeyPatch):
    _Served(monkeypatch, _raw())
    with pytest.raises(geofabrik.RegionError, match="miles-area-import --pbf"):
        geofabrik.choose_regions(memory_db(), (-20, -20, -19, -19))


def test_an_unknown_region_is_refused_with_suggestions(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    _Served(monkeypatch, _raw())
    geofabrik.validate_region(conn, "world/land/west")
    with pytest.raises(geofabrik.RegionError, match="did you mean world/land/west"):
        geofabrik.validate_region(conn, "west")
    with pytest.raises(geofabrik.RegionError, match="unknown Geofabrik region"):
        geofabrik.validate_region(conn, "nowhere/at/all")


def test_the_cached_choice_never_touches_the_network(monkeypatch: pytest.MonkeyPatch):
    conn = memory_db()
    served = _Served(monkeypatch, _raw())
    assert geofabrik.choose_cached_regions(conn, (10, 2, 12, 4)) is None
    assert served.requests == []
    geofabrik.regions(conn)
    assert geofabrik.choose_cached_regions(conn, (10, 2, 12, 4)) == ["world/land/west"]


# A parent larger than its three children together, and a parent exactly
# tiled by its three children.
SPLIT_INDEX = {
    "type": "FeatureCollection",
    "features": [
        _feature("example/big", _polygon(0, 0, 50, 100)),
        _feature("example/big/part-a", _polygon(0, 0, 40, 20)),
        _feature("example/big/part-b", _polygon(0, 20, 40, 40)),
        _feature("example/big/part-c", _polygon(0, 40, 40, 100)),
        _feature("example/tri/left", _polygon(50, 0, 60, 10)),
        _feature("example/tri/mid", _polygon(50, 10, 60, 20)),
        _feature("example/tri/right", _polygon(50, 20, 60, 30)),
        _feature("example/tri", _polygon(50, 0, 60, 30)),
    ],
}


def _split_conn(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    conn = memory_db()
    _Served(monkeypatch, json.dumps(SPLIT_INDEX).encode())
    return conn


def test_an_area_across_a_border_is_covered_by_the_children_not_the_parent(monkeypatch: pytest.MonkeyPatch):
    conn = _split_conn(monkeypatch)
    assert sorted(geofabrik.choose_regions(conn, (10, 18, 12, 22))) == ["example/big/part-a", "example/big/part-b"]


def test_an_area_inside_one_child_takes_that_child(monkeypatch: pytest.MonkeyPatch):
    conn = _split_conn(monkeypatch)
    assert geofabrik.choose_regions(conn, (10, 2, 12, 4)) == ["example/big/part-a"]


def test_a_parent_exactly_covered_by_its_children_wins_as_one_region(monkeypatch: pytest.MonkeyPatch):
    conn = _split_conn(monkeypatch)
    assert geofabrik.choose_regions(conn, (55, 8, 57, 22)) == ["example/tri"]


def test_an_area_spanning_three_children_of_a_larger_parent_takes_all_three(monkeypatch: pytest.MonkeyPatch):
    conn = _split_conn(monkeypatch)
    found = geofabrik.choose_regions(conn, (10, 15, 12, 60))
    assert sorted(found) == ["example/big/part-a", "example/big/part-b", "example/big/part-c"]


def test_the_children_that_cover_an_area_under_a_named_parent(monkeypatch: pytest.MonkeyPatch):
    conn = _split_conn(monkeypatch)
    known = geofabrik.regions(conn)
    found = geofabrik.covering_children(known, "example/big", (10, 18, 12, 22))
    assert found is not None and sorted(r.path for r in found) == ["example/big/part-a", "example/big/part-b"]
    assert geofabrik.covering_children(known, "example/big", (10, 18, 12, 120)) is None


def test_an_explicit_region_is_accepted_unchecked_when_the_index_is_unreachable(monkeypatch: pytest.MonkeyPatch):
    _Served(monkeypatch, status=0)
    geofabrik.validate_region(memory_db(), "example/anything")


def test_a_typo_gets_a_close_match_suggestion(monkeypatch: pytest.MonkeyPatch):
    conn = _split_conn(monkeypatch)
    with pytest.raises(geofabrik.RegionError, match="did you mean example/big/part-a"):
        geofabrik.validate_region(conn, "example/big/part-aa")
