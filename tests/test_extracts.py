import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import threading
import time
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import FakeResponse, fake_extract_record, write_pbf

from miles import db, extracts, metrics, tilecache, tiles

# Tile (0, 0) covers lat/lng in [0, 0.05); tile (1, 0) covers lng in [0.05, 0.10).


def _sample_pbf(path: Path) -> None:
    write_pbf(
        path,
        nodes=[
            (1, 0.010, 0.010, {}),
            (2, 0.020, 0.020, {}),
            (3, 0.010, 0.060, {}),
            (4, 0.020, 0.070, {}),
            (5, 0.015, 0.015, {"amenity": "drinking_water"}),
            # A closed way tagged amenity=parking, mapped as an area.
            (10, 0.011, 0.011, {}),
            (11, 0.011, 0.013, {}),
            (12, 0.013, 0.013, {}),
            # Far outside tiles (0, 0) and (1, 0): a large extract, small
            # area cut.
            (20, 5.000, 5.000, {}),
            (21, 5.010, 5.010, {}),
        ],
        ways=[
            (100, [1, 2], {"highway": "path"}),  # tile (0, 0)
            (101, [3, 4], {"highway": "footway"}),  # tile (1, 0)
            (200, [10, 11, 12, 10], {"amenity": "parking"}),  # closed, tile (0, 0)
            (300, [1, 2], {"building": "yes"}),  # not highway/amenity of interest
            (400, [20, 21], {"highway": "path"}),  # tagged of interest, but nowhere near the cut area
        ],
    )


@pytest.fixture
def sample_pbf(tmp_path: Path) -> Path:
    path = tmp_path / "sample.osm.pbf"
    _sample_pbf(path)
    return path


# ---------------------------------------------------------------------------
# cut_area
# ---------------------------------------------------------------------------


def test_cut_area_writes_the_ways_nodes_and_parking_centres_of_interest(conn: sqlite3.Connection, sample_pbf: Path):
    record = extracts.register_local_extract(conn, sample_pbf, "test/sample")

    tile_ids = extracts.cut_area(conn, record, tiles.tile_bounds(0, 0))
    assert (0, 0) in tile_ids

    payload = tilecache.get_tile_payload(conn, 0, 0)
    assert payload is not None
    ways = [el for el in payload["elements"] if el["type"] == "way"]
    # Way 300 (building) and 400 (far away) are absent; the parking area 200
    # appears only as a point.
    assert [w["id"] for w in ways] == [100]
    assert ways[0]["tags"] == {"highway": "path"}
    assert ways[0]["nodes"] == [1, 2]
    assert ways[0]["geometry"] == [{"lat": 0.010, "lon": 0.010}, {"lat": 0.020, "lon": 0.020}]

    water = [el for el in payload["elements"] if el["type"] == "node" and el["id"] == 5]
    assert water == [{"type": "node", "id": 5, "lat": 0.015, "lon": 0.015, "tags": {"amenity": "drinking_water"}}]

    (parking,) = [el for el in payload["elements"] if el["type"] == "node" and el["id"] == -200]
    assert parking["tags"] == {"amenity": "parking"}
    # Centroid of the three distinct boundary nodes (10, 11, 12).
    assert parking["lat"] == pytest.approx((0.011 + 0.011 + 0.013) / 3)
    assert parking["lon"] == pytest.approx((0.011 + 0.013 + 0.013) / 3)


def test_cut_area_splits_ways_across_the_tiles_they_occupy(conn: sqlite3.Connection, sample_pbf: Path):
    record = extracts.register_local_extract(conn, sample_pbf, "test/sample")

    tile_ids = extracts.cut_area(conn, record, (0.0, 0.0, 0.049, 0.099))  # tiles (0, 0) and (1, 0), nothing past them
    assert set(tile_ids) == {(0, 0), (1, 0)}

    payload0 = tilecache.get_tile_payload(conn, 0, 0)
    payload1 = tilecache.get_tile_payload(conn, 1, 0)
    assert payload0 is not None and payload1 is not None
    assert {el["id"] for el in payload0["elements"] if el["type"] == "way"} == {100}
    assert {el["id"] for el in payload1["elements"] if el["type"] == "way"} == {101}


def test_cut_area_with_a_disk_index_matches_the_in_memory_cut_and_leaves_no_index(
    conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch
):
    record = extracts.register_local_extract(conn, sample_pbf, "test/sample")
    extracts.cut_area(conn, record, (0.0, 0.0, 0.04, 0.04))
    in_memory = tilecache.get_tile_payload(conn, 0, 0)

    monkeypatch.setattr(extracts, "DISK_INDEX_THRESHOLD_BYTES", 1)
    storages: list[str] = []
    real_index = extracts._location_index

    @contextmanager
    def recording_index(path: Path) -> Iterator[str]:
        with real_index(path) as storage:
            storages.append(storage)
            yield storage

    monkeypatch.setattr(extracts, "_location_index", recording_index)
    conn2 = db.connect()
    record2 = extracts.register_local_extract(conn2, sample_pbf, "test/sample")
    extracts.cut_area(conn2, record2, (0.0, 0.0, 0.04, 0.04))

    assert [s.split(",")[0] for s in storages] == ["sparse_file_array"]
    assert tilecache.get_tile_payload(conn2, 0, 0) == in_memory
    assert [p for p in extracts.EXTRACT_DIR.iterdir() if p.name.startswith(".locations")] == []


# ---------------------------------------------------------------------------
# Registration / bounds
# ---------------------------------------------------------------------------


def test_register_local_extract_hashes_and_reads_bounds_via_fallback_scan(conn: sqlite3.Connection, sample_pbf: Path):
    record = extracts.register_local_extract(conn, sample_pbf, "test/sample")
    assert record["region"] == "test/sample"
    assert record["path"] == str(sample_pbf)
    assert record["sha256"] == extracts._sha256_file(sample_pbf)
    assert record["bounds_south"] == pytest.approx(0.010)
    assert record["bounds_north"] == pytest.approx(5.010)
    assert record["bounds_west"] == pytest.approx(0.010)
    assert record["bounds_east"] == pytest.approx(5.010)

    extracts.register_local_extract(conn, sample_pbf, "test/sample")
    assert len(extracts.list_extracts(conn)) == 1  # the region key is reused

    with pytest.raises(extracts.ExtractDownloadError):
        extracts.register_local_extract(conn, "/no/such/file.osm.pbf", "test/missing")


# ---------------------------------------------------------------------------
# Region selection
# ---------------------------------------------------------------------------


def test_find_registered_extract_picks_the_smallest_containing_bbox_and_none_when_nothing_contains_it(conn: sqlite3.Connection):
    now = datetime.now(timezone.utc).isoformat()
    for region, bound in (("big", 10), ("small", 1)):
        conn.execute(
            "INSERT INTO map_extracts (region, path, sha256, fetched_at, bounds_south, bounds_west, bounds_north, bounds_east) "
            "VALUES (?, 'x', 'x', ?, ?, ?, ?, ?)",
            (region, now, -bound, -bound, bound, bound),
        )
    conn.commit()

    found = extracts.find_registered_extract(conn, (-0.5, -0.5, 0.5, 0.5))
    assert found is not None and found["region"] == "small"
    assert extracts.find_registered_extract(conn, (20, 20, 21, 21)) is None


@pytest.mark.parametrize(
    "preferred, chosen_from_index",
    [
        (None, ["example/region-a"]),
        ("example/region-a", None),
        ("example/region-a, example/region-b", None),
    ],
    ids=["the index's choice", "one preferred region", "two preferred regions"],
)
def test_select_extracts_downloads_the_preferred_regions_after_validating_them_or_the_indexs_choice(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, preferred: str | None, chosen_from_index: list[str] | None
):
    validated: list[str] = []
    monkeypatch.setattr(extracts.geofabrik, "validate_region", lambda conn, region: validated.append(region))
    monkeypatch.setattr(
        extracts.geofabrik, "choose_regions",
        lambda conn, bbox: chosen_from_index if chosen_from_index is not None else pytest.fail("an explicit region wins"),
    )
    monkeypatch.setattr(extracts, "download_extract", lambda conn, region, **kw: fake_extract_record(region))

    result = extracts.select_extracts(conn, (-0.1, -0.1, 0.1, 0.1), preferred_region=preferred)

    expected = chosen_from_index or [r.strip() for r in (preferred or "").split(",")]
    assert [r["region"] for r in result] == expected
    assert validated == ([] if preferred is None else expected)


def test_select_extracts_reports_an_unknown_region_as_a_download_error(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    def unknown(conn, region):
        raise extracts.geofabrik.RegionError("unknown Geofabrik region 'nope'")

    monkeypatch.setattr(extracts.geofabrik, "validate_region", unknown)
    with pytest.raises(extracts.ExtractDownloadError, match="unknown Geofabrik region"):
        extracts.select_extracts(conn, (-0.1, -0.1, 0.1, 0.1), preferred_region="nope")


# ---------------------------------------------------------------------------
# Download policy
# ---------------------------------------------------------------------------


def _register_downloaded(conn: sqlite3.Connection, pbf: Path, **kwargs) -> None:
    extracts.register_local_extract(conn, pbf, "test/sample", **kwargs)


def _set_times(conn: sqlite3.Connection, region: str, *, fetched_days_ago: float, checked_days_ago: float | None = None) -> None:
    fetched = (datetime.now(timezone.utc) - timedelta(days=fetched_days_ago)).isoformat()
    checked = (datetime.now(timezone.utc) - timedelta(days=checked_days_ago if checked_days_ago is not None else fetched_days_ago)).isoformat()
    conn.execute("UPDATE map_extracts SET fetched_at = ?, checked_at = ? WHERE region = ?", (fetched, checked, region))
    conn.commit()


def _fake_download(validators_seen: list, etag: str | None = None):
    def fake(url: str, dest: Path, validators: dict, **kw):
        validators_seen.append((url, validators))
        _sample_pbf(dest)
        return {"etag": etag, "last_modified": None}

    return fake


@pytest.mark.parametrize(
    "flags, fetched_days_ago, checked_days_ago",
    [({}, 900, None), ({"refresh": True}, 60, 1 / 24)],
    ids=["any age without a refresh", "a refresh within the minimum interval"],
)
def test_a_registered_extract_is_reused_without_contacting_geofabrik(
    conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch, flags: dict, fetched_days_ago: float, checked_days_ago: float | None
):
    _register_downloaded(conn, sample_pbf)
    _set_times(conn, "test/sample", fetched_days_ago=fetched_days_ago, checked_days_ago=checked_days_ago)
    monkeypatch.setattr(extracts, "_download_with_backoff", lambda url, dest, validators, **kw: pytest.fail("no download"))

    assert extracts.download_extract(conn, "test/sample", **flags)["region"] == "test/sample"


def test_refresh_sends_the_stored_validators_and_replaces_the_file_on_a_200(conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch):
    _register_downloaded(conn, sample_pbf, etag='"abc"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")
    _set_times(conn, "test/sample", fetched_days_ago=3)
    calls: list[tuple[str, dict]] = []

    def fake_download(url: str, dest: Path, validators: dict, **kw):
        calls.append((url, validators))
        _sample_pbf(dest)
        return {"etag": '"def"', "last_modified": "Tue, 02 Jan 2024 00:00:00 GMT"}

    monkeypatch.setattr(extracts, "_download_with_backoff", fake_download)
    record = extracts.download_extract(conn, "test/sample", refresh=True)

    assert "test/sample-latest.osm.pbf" in calls[0][0]
    assert calls[0][1] == {"If-None-Match": '"abc"', "If-Modified-Since": "Mon, 01 Jan 2024 00:00:00 GMT"}
    assert record["etag"] == '"def"'
    assert record["data_as_of"].startswith("2024-01-02")


def test_a_file_registered_without_validators_is_compared_by_download_time(conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch):
    _register_downloaded(conn, sample_pbf)
    _set_times(conn, "test/sample", fetched_days_ago=30)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(extracts, "_download_with_backoff", _fake_download(calls))

    extracts.download_extract(conn, "test/sample", refresh=True)

    sent = calls[0][1]
    assert set(sent) == {"If-Modified-Since"}
    assert sent["If-Modified-Since"].endswith("GMT")


def test_a_304_keeps_the_file_and_only_moves_checked_at(conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch):
    _register_downloaded(conn, sample_pbf, etag='"abc"')
    _set_times(conn, "test/sample", fetched_days_ago=30)
    before = extracts.get_extract(conn, "test/sample")
    assert before is not None
    monkeypatch.setattr(extracts, "_download_with_backoff", lambda url, dest, validators, **kw: None)

    after = extracts.download_extract(conn, "test/sample", refresh=True)

    assert after["sha256"] == before["sha256"]
    assert after["fetched_at"] == before["fetched_at"]
    assert after["checked_at"] > before["checked_at"]


def _serve(monkeypatch: pytest.MonkeyPatch, body: bytes, *, md5: str | None = None, headers: dict | None = None, send: int | None = None) -> list:
    """Stand in for http_client.request: the pbf URL gets body, its .md5 the
    published digest."""
    seen: list = []
    digest = md5 if md5 is not None else hashlib.md5(body).hexdigest()

    def request(policy, url, *, headers=None, reader=None, **kwargs):
        seen.append((url, headers))
        if url.endswith(".md5"):
            return f"{digest}  y.osm.pbf\n".encode()
        return reader(FakeResponse(body, response_headers, send=send))

    response_headers = headers
    monkeypatch.setattr(extracts.http_client, "request", request)
    return seen


def test_a_download_sends_conditional_headers_and_records_the_response_validators(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    seen = _serve(
        monkeypatch, b"data",
        headers={"ETag": '"e1"', "Last-Modified": "Wed, 03 Jan 2024 00:00:00 GMT", "Content-Length": "4"},
    )
    dest = tmp_path / "y.pbf"
    result = extracts._download_with_backoff("http://x/y.pbf", dest, {"If-None-Match": '"e0"'})

    assert seen[0] == ("http://x/y.pbf", {"If-None-Match": '"e0"'})
    assert seen[1][0] == "http://x/y.pbf.md5"
    assert result == {"etag": '"e1"', "last_modified": "Wed, 03 Jan 2024 00:00:00 GMT"}
    assert dest.read_bytes() == b"data"
    assert list(tmp_path.glob("*.part")) == []


def _incomplete_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(policy, url, *, reader=None, **kwargs):
        raise http.client.IncompleteRead(b"abc", 5)

    monkeypatch.setattr(extracts.http_client, "request", broken)


@pytest.mark.parametrize(
    "serve, message",
    [
        (lambda mp: _serve(mp, b"0123456789", send=6), "short read"),
        (lambda mp: _serve(mp, b"data", md5="not-a-digest"), "not a checksum"),
        (_incomplete_read, None),
    ],
    ids=["a short read", "a malformed md5 file", "an incomplete read"],
)
def test_a_download_that_cannot_be_verified_is_an_error_and_leaves_no_part_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, serve, message: str | None):
    serve(monkeypatch)
    dest = tmp_path / "y.pbf"
    with pytest.raises(extracts.ExtractDownloadError, match=message):
        extracts._download_with_backoff("http://x/y.pbf", dest, {})
    assert not dest.exists() and list(tmp_path.iterdir()) == []


def test_a_checksum_mismatch_is_an_error_and_keeps_the_old_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _serve(monkeypatch, b"new data", md5="0" * 32)
    dest = tmp_path / "y.pbf"
    dest.write_bytes(b"old")
    with pytest.raises(extracts.ExtractDownloadError, match="checksum mismatch"):
        extracts._download_with_backoff("http://x/y.pbf", dest, {})
    assert dest.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["y.pbf"]


def test_a_size_over_the_cap_is_refused_whether_declared_or_only_seen_while_streaming(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    methods: list[str | None] = []

    def request(policy, url, *, headers=None, reader=None, method=None, **kwargs):
        methods.append(method)
        return reader(FakeResponse(b"data", {"Content-Length": str(extracts.EXTRACT_MAX_BYTES + 1)}))

    monkeypatch.setattr(extracts.http_client, "request", request)
    with pytest.raises(extracts.ExtractTooLargeError, match="over the 2 GB limit"):
        extracts.download_extract(conn, "test/sample")
    assert methods == ["HEAD"]
    assert conn.execute("SELECT COUNT(*) FROM extract_requests").fetchone()[0] == 0
    assert extracts.get_extract(conn, "test/sample") is None

    monkeypatch.setattr(extracts, "EXTRACT_MAX_BYTES", 3)
    _serve(monkeypatch, b"data", headers={})
    with pytest.raises(extracts.ExtractDownloadError, match="limit"):
        extracts._download_with_backoff("http://x/y.pbf", tmp_path / "y.pbf", {})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("override", ["argument", "env"])
def test_the_size_cap_can_be_overridden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: str):
    monkeypatch.setattr(extracts, "EXTRACT_MAX_BYTES", 3)
    _serve(monkeypatch, b"data")
    if override == "env":
        monkeypatch.setenv(extracts.ALLOW_LARGE_ENV, "1")
    extracts._download_with_backoff("http://x/y.pbf", tmp_path / "y.pbf", {}, allow_large=override == "argument")
    assert (tmp_path / "y.pbf").read_bytes() == b"data"


def test_two_downloads_never_share_a_part_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    names: list[str] = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        if self.suffix == ".part":
            names.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    _serve(monkeypatch, b"data")
    extracts._download_with_backoff("http://x/y.pbf", tmp_path / "y.pbf", {})
    extracts._download_with_backoff("http://x/y.pbf", tmp_path / "y.pbf", {})
    assert len(set(names)) == 2


def _pbf_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "served.osm.pbf"
    _sample_pbf(path)
    return path.read_bytes()


def test_a_download_registers_only_after_verification(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _serve(monkeypatch, _pbf_bytes(tmp_path), md5="0" * 32)

    with pytest.raises(extracts.ExtractDownloadError):
        extracts.download_extract(conn, "test/sample")

    assert extracts.get_extract(conn, "test/sample") is None
    assert list(extracts.EXTRACT_DIR.glob("*.part")) == []


def test_a_request_blocks_another_contact_for_an_hour(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def failing(url, dest, validators, *, on_request, **kw):
        calls.append(url)
        on_request()
        raise extracts.ExtractDownloadError("short read")

    monkeypatch.setattr(extracts, "_download_with_backoff", failing)
    with pytest.raises(extracts.ExtractDownloadError, match="short read"):
        extracts.download_extract(conn, "test/sample")
    with pytest.raises(extracts.ExtractDownloadError, match="not contacting Geofabrik again"):
        extracts.download_extract(conn, "test/sample")
    assert len(calls) == 1

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    conn.execute("UPDATE extract_requests SET requested_at = ?", (old,))
    conn.commit()
    with pytest.raises(extracts.ExtractDownloadError, match="short read"):
        extracts.download_extract(conn, "test/sample")
    assert len(calls) == 2


def test_force_obeys_the_hourly_cooldown_after_a_successful_download(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    seen = _serve(monkeypatch, _pbf_bytes(tmp_path))
    extracts.download_extract(conn, "test/sample")
    requests_made = len(seen)

    with pytest.raises(extracts.ExtractDownloadError, match="not contacting Geofabrik again"):
        extracts.download_extract(conn, "test/sample", force=True)
    assert len(seen) == requests_made

    conn.execute("UPDATE extract_requests SET requested_at = ?", ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    conn.commit()
    extracts.download_extract(conn, "test/sample", force=True)
    assert len(seen) > requests_made


def test_a_second_download_of_a_region_waits_for_the_first_and_uses_its_file(monkeypatch: pytest.MonkeyPatch):
    in_download = threading.Event()
    waiting = threading.Event()
    release = threading.Event()
    downloads: list[str] = []

    def slow_download(url, dest, validators, **kw):
        downloads.append(url)
        in_download.set()
        release.wait(timeout=5)
        _sample_pbf(dest)
        return {"etag": None, "last_modified": None}

    monkeypatch.setattr(extracts, "_download_with_backoff", slow_download)
    first = threading.Thread(target=lambda: extracts.download_extract(db.connect(), "test/sample"))
    first.start()
    assert in_download.wait(timeout=5)

    waits: list[int] = []
    records: list[extracts.ExtractRecord] = []

    def on_wait() -> None:
        waits.append(1)
        waiting.set()

    second = threading.Thread(target=lambda: records.append(extracts.download_extract(db.connect(), "test/sample", on_wait=on_wait)))
    second.start()
    assert waiting.wait(timeout=5)
    assert second.is_alive()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert len(downloads) == 1
    assert waits == [1], "reported once, however long the wait"
    assert records[0]["region"] == "test/sample"


def test_downloads_of_different_regions_do_not_wait_for_each_other(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    def fake(url, dest, validators, **kw):
        assert extracts._region_lock("test/other").acquire(blocking=False), "another region's lock was held"
        extracts._region_lock("test/other").release()
        _sample_pbf(dest)
        return {"etag": None, "last_modified": None}

    monkeypatch.setattr(extracts, "_download_with_backoff", fake)
    extracts.download_extract(conn, "test/sample")


def test_a_registered_extract_whose_file_is_gone_is_downloaded_again(conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch):
    _register_downloaded(conn, sample_pbf)
    sample_pbf.unlink()
    downloads: list[tuple[str, dict]] = []
    monkeypatch.setattr(extracts, "_download_with_backoff", _fake_download(downloads))

    (record,) = extracts.select_extracts(conn, (0.012, 0.012, 0.04, 0.04))

    assert [validators for _url, validators in downloads] == [{}]
    assert Path(record["path"]).is_file()


def test_planned_regions_falls_back_to_the_registered_extract_containing_the_area(conn: sqlite3.Connection, sample_pbf: Path):
    _register_downloaded(conn, sample_pbf)
    assert extracts.planned_regions(conn, (0.012, 0.012, 0.04, 0.04)) == ["test/sample"]
    assert extracts.planned_regions(conn, (40.0, 40.0, 41.0, 41.0)) == []


def test_force_replaces_a_registered_file_unconditionally(conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch):
    _register_downloaded(conn, sample_pbf, etag='"abc"')
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(extracts, "_download_with_backoff", _fake_download(seen, etag='"new"'))

    record = extracts.download_extract(conn, "test/sample", force=True)

    assert [validators for _url, validators in seen] == [{}]
    assert record["etag"] == '"new"'


def test_a_local_extract_is_never_refreshed_from_geofabrik_and_a_missing_file_is_reported(
    conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch
):
    extracts.register_local_extract(conn, sample_pbf, "local:sample")
    monkeypatch.setattr(extracts, "_download_with_backoff", lambda *a, **kw: pytest.fail("contacted Geofabrik"))
    monkeypatch.setattr(extracts.http_client, "request", lambda *a, **kw: pytest.fail("contacted Geofabrik"))
    bbox = (0.012, 0.012, 0.04, 0.04)

    for flags in ({"refresh": True}, {"force": True}):
        assert [e["region"] for e in extracts.select_extracts(conn, bbox, **flags)] == ["local:sample"]

    sample_pbf.unlink()
    with pytest.raises(extracts.ExtractDownloadError, match="missing"):
        extracts.select_extracts(conn, bbox, refresh=True)


def test_stale_part_files_are_swept_before_a_download(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    directory = extracts.EXTRACT_DIR
    directory.mkdir()
    old, fresh = directory / "a.1.part", directory / "b.2.part"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    ancient = time.time() - 3 * 86400
    os.utime(old, (ancient, ancient))
    monkeypatch.setattr(extracts, "_download_with_backoff", _fake_download([]))

    extracts.download_extract(conn, "test/sample")

    assert not old.exists() and fresh.exists()


def test_extract_cache_metrics_count_hits_misses_and_not_modified(conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch):
    metrics.reset()
    _register_downloaded(conn, sample_pbf)
    extracts.download_extract(conn, "test/sample")
    _set_times(conn, "test/sample", fetched_days_ago=30)
    monkeypatch.setattr(extracts, "_download_with_backoff", lambda url, dest, validators, **kw: None)
    extracts.download_extract(conn, "test/sample", refresh=True)

    assert metrics.get(metrics.CACHE_REQUESTS, cache="extract", result="hit") == 1
    assert metrics.get(metrics.CACHE_REQUESTS, cache="extract", result="not_modified") == 1


# ---------------------------------------------------------------------------
# Refresh and re-cut policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "region, asks_geofabrik",
    [("test/sample", True), ("local:sample", False)],
    ids=["a downloaded extract is re-asked", "a local extract is kept when Geofabrik has no such file"],
)
def test_select_extracts_refresh_asks_geofabrik_and_keeps_an_unrefreshable_extract(
    conn: sqlite3.Connection, sample_pbf: Path, monkeypatch: pytest.MonkeyPatch, region: str, asks_geofabrik: bool
):
    extracts.register_local_extract(conn, sample_pbf, region)
    _set_times(conn, region, fetched_days_ago=10)
    calls: list[tuple[str, dict]] = []
    download = _fake_download(calls)

    def fake(url: str, dest: Path, validators: dict, **kw):
        if not asks_geofabrik:
            raise extracts.ExtractDownloadError("404")
        return download(url, dest, validators, **kw)

    monkeypatch.setattr(extracts, "_download_with_backoff", fake)

    found = extracts.select_extracts(conn, (0.012, 0.012, 0.04, 0.04), refresh=True)

    assert [f["region"] for f in found] == [region]
    assert len(calls) == int(asks_geofabrik)


def test_tiles_cut_from_is_true_only_for_the_current_extract_file(conn: sqlite3.Connection, sample_pbf: Path):
    record = extracts.register_local_extract(conn, sample_pbf, "test/sample")
    tile_ids = [(0, 0), (1, 0)]
    assert extracts.tiles_cut_from(conn, record, tile_ids) is False

    extracts.cut_area(conn, record, (0.0, 0.0, 0.09, 0.09))
    assert extracts.tiles_cut_from(conn, record, tile_ids) is True

    _set_times(conn, "test/sample", fetched_days_ago=-1)  # extract re-registered after the cut
    newer = extracts.get_extract(conn, "test/sample")
    assert newer is not None
    assert extracts.tiles_cut_from(conn, newer, tile_ids) is False

    for source in ("extract:test/other", "overpass"):
        tilecache.store_tile_elements(conn, 0, 0, [], source=source)
        assert extracts.tiles_cut_from(conn, record, [(0, 0)]) is False, source


# ---------------------------------------------------------------------------
# Areas that straddle regions
# ---------------------------------------------------------------------------

# Tile (0, 0) and tile (1, 0) meet at lng 0.05; region-a ends there.
BORDER_LNG = 0.05
STRADDLING_BBOX = (0.0, 0.0, 0.049, 0.095)


def _region_feature(path: str, south: float, west: float, north: float, east: float) -> dict:
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {
        "type": "Feature",
        "properties": {"urls": {"pbf": f"https://download.example.test/{path}-latest.osm.pbf"}},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def _border_index() -> bytes:
    return json.dumps({
        "type": "FeatureCollection",
        "features": [
            _region_feature("example/parent", -1, -1, 1, 5),
            _region_feature("example/parent/region-a", -1, -1, 1, BORDER_LNG),
            _region_feature("example/parent/region-b", -1, BORDER_LNG, 1, 1),
        ],
    }).encode()


def _serve_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extracts.geofabrik.http_client, "request", lambda policy, url, **kw: kw["reader"](FakeResponse(_border_index(), {})))


def _border_pbfs(tmp_path: Path) -> dict[str, Path]:
    """Region A holds way 100 only up to the border node, B holds all of it;
    each also holds a way the other lacks."""
    a, b = tmp_path / "a.osm.pbf", tmp_path / "b.osm.pbf"
    write_pbf(
        a,
        nodes=[(1, 0.010, 0.010, {}), (2, 0.010, 0.040, {}), (3, 0.020, 0.020, {})],
        ways=[(100, [1, 2], {"highway": "path"}), (150, [1, 3], {"highway": "track"})],
    )
    write_pbf(
        b,
        nodes=[(1, 0.010, 0.010, {}), (2, 0.010, 0.040, {}), (4, 0.010, 0.080, {}), (5, 0.020, 0.070, {})],
        ways=[(100, [1, 2, 4], {"highway": "path"}), (200, [4, 5], {"highway": "footway"})],
    )
    return {"example/parent/region-a": a, "example/parent/region-b": b}


def _border_download(sources: dict[str, Path], downloaded: list[str]):
    def fake_download(url: str, dest: Path, validators: dict, **kw):
        region = url.split("/", 3)[3].removesuffix("-latest.osm.pbf")
        downloaded.append(region)
        shutil.copy(sources[region], dest)
        return {"etag": None, "last_modified": None}

    return fake_download


def test_an_area_across_a_border_is_cut_from_both_children_and_never_downloads_the_parent(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sources = _border_pbfs(tmp_path)
    _serve_index(monkeypatch)
    downloaded: list[str] = []
    monkeypatch.setattr(extracts, "_download_with_backoff", _border_download(sources, downloaded))
    monkeypatch.setattr(extracts, "GEOFABRIK_BASE_URL", "https://download.example.test")

    selected = extracts.select_extracts(conn, STRADDLING_BBOX)

    assert sorted(downloaded) == ["example/parent/region-a", "example/parent/region-b"]
    assert sorted(e["region"] for e in selected) == ["example/parent/region-a", "example/parent/region-b"]

    tile_ids = extracts.cut_area(conn, selected, STRADDLING_BBOX)
    assert tile_ids == [(0, 0), (1, 0)]

    def ways(gx: int) -> dict[int, dict]:
        payload = tilecache.get_tile_payload(conn, gx, 0)
        assert payload is not None
        found = [el for el in payload["elements"] if el["type"] == "way"]
        assert len({el["id"] for el in found}) == len(found), "each way once per tile"
        return {el["id"]: el for el in found}

    west, east = ways(0), ways(1)
    assert set(west) == {100, 150}
    assert set(east) == {100, 200}
    assert west[100]["nodes"] == [1, 2, 4], "the copy with more nodes wins"
    assert extracts.tiles_cut_from(conn, selected, tile_ids)
    assert extracts.tiles_cut_from(conn, selected[:1], tile_ids), "tiles holding more regions satisfy fewer"
    assert not extracts.tiles_cut_from(conn, [*selected, fake_extract_record("example/other")], tile_ids)
    source = conn.execute("SELECT DISTINCT source FROM map_tiles").fetchall()
    assert [r["source"] for r in source] == ["extract:example/parent/region-a,example/parent/region-b"]


def _tile_ways(conn: sqlite3.Connection, gx: int) -> dict[int, dict]:
    payload = tilecache.get_tile_payload(conn, gx, 0)
    assert payload is not None
    return {el["id"]: el for el in payload["elements"] if el["type"] == "way"}


def test_a_registered_extract_whose_header_box_reaches_across_the_border_does_not_stand_in_for_both_regions(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    sources = _border_pbfs(tmp_path)
    _serve_index(monkeypatch)
    extracts.geofabrik.regions(conn)
    extracts.register_local_extract(conn, sources["example/parent/region-b"], "example/parent/region-b")
    conn.execute("UPDATE map_extracts SET bounds_south = -1, bounds_west = -1, bounds_north = 1, bounds_east = 1")
    conn.commit()
    assert extracts.find_registered_extract(conn, STRADDLING_BBOX) is not None
    downloaded: list[str] = []
    monkeypatch.setattr(extracts, "_download_with_backoff", _border_download(sources, downloaded))
    monkeypatch.setattr(extracts, "GEOFABRIK_BASE_URL", "https://download.example.test")

    selected = extracts.select_extracts(conn, STRADDLING_BBOX)
    extracts.cut_area(conn, selected, STRADDLING_BBOX)

    assert downloaded == ["example/parent/region-a"]
    assert sorted(e["region"] for e in selected) == ["example/parent/region-a", "example/parent/region-b"]
    assert set(_tile_ways(conn, 0)) == {100, 150}
    assert set(_tile_ways(conn, 1)) == {100, 200}
    assert sorted(extracts.planned_regions(conn, STRADDLING_BBOX)) == [
        "example/parent/region-a",
        "example/parent/region-b",
    ]


TILE00_BBOX = (0.0, 0.0, 0.04, 0.049)
BOTH = "extract:example/parent/region-a,example/parent/region-b"


def _tile_row(conn: sqlite3.Connection, gx: int = 0) -> sqlite3.Row:
    return conn.execute("SELECT * FROM map_tiles WHERE gx = ? AND gy = 0", (gx,)).fetchone()


def _register_border_regions(conn: sqlite3.Connection, tmp_path: Path) -> list[extracts.ExtractRecord]:
    return [extracts.register_local_extract(conn, p, region) for region, p in _border_pbfs(tmp_path).items()]


def test_a_cut_from_one_region_includes_every_other_registered_region_touching_the_tile(conn: sqlite3.Connection, tmp_path: Path):
    both = _register_border_regions(conn, tmp_path)
    only_a = [r for r in both if r["region"] == "example/parent/region-a"]

    extracts.cut_area(conn, only_a, TILE00_BBOX)

    assert _tile_ways(conn, 0)[100]["nodes"] == [1, 2, 4], "region-b's longer copy of the border way is cut too"
    assert _tile_row(conn)["source"] == BOTH
    assert extracts.tiles_cut_from(conn, only_a, [(0, 0)])
    assert extracts.tiles_cut_from(conn, both, [(0, 0)])


def test_a_stored_element_that_no_file_holds_is_dropped_by_a_cut(conn: sqlite3.Connection, tmp_path: Path):
    a, b = _register_border_regions(conn, tmp_path)
    stale = {"type": "way", "id": 999, "nodes": [1, 2], "geometry": [{"lat": 0.01, "lon": 0.01}, {"lat": 0.02, "lon": 0.02}]}
    tilecache.store_tile_elements(conn, 0, 0, [stale], source=BOTH)

    extracts.cut_area(conn, [a, b], TILE00_BBOX)

    assert set(_tile_ways(conn, 0)) == {100, 150}


def test_a_way_deleted_in_one_region_leaves_the_border_tile_when_only_that_region_is_refreshed(conn: sqlite3.Connection, tmp_path: Path):
    _register_border_regions(conn, tmp_path)
    a_record = extracts.get_extract(conn, "example/parent/region-a")
    assert a_record is not None
    extracts.cut_area(conn, [a_record], TILE00_BBOX)
    assert 150 in _tile_ways(conn, 0)

    refreshed = tmp_path / "a-refreshed.osm.pbf"
    write_pbf(
        refreshed,
        nodes=[(1, 0.010, 0.010, {}), (2, 0.010, 0.040, {})],
        ways=[(100, [1, 2], {"highway": "path"})],
    )
    new_a = extracts.register_local_extract(conn, refreshed, "example/parent/region-a")
    assert extracts.tiles_cut_from(conn, [new_a], [(0, 0)]) is False, "a refreshed file calls for a recut"
    extracts.cut_area(conn, [new_a], TILE00_BBOX)

    assert set(_tile_ways(conn, 0)) == {100}
    assert _tile_ways(conn, 0)[100]["nodes"] == [1, 2, 4], "region-b is unchanged and still contributes"
    assert extracts.tiles_cut_from(conn, [new_a], [(0, 0)])


def test_a_tile_is_as_fresh_as_the_oldest_extract_it_was_cut_from(conn: sqlite3.Connection, tmp_path: Path):
    _register_border_regions(conn, tmp_path)
    _set_times(conn, "example/parent/region-a", fetched_days_ago=30)
    _set_times(conn, "example/parent/region-b", fetched_days_ago=2)
    a = extracts.get_extract(conn, "example/parent/region-a")
    b = extracts.get_extract(conn, "example/parent/region-b")
    assert a is not None and b is not None

    extracts.cut_area(conn, [b], TILE00_BBOX)

    assert _tile_row(conn)["fetched_at"] == a["fetched_at"]
    assert extracts.tiles_cut_from(conn, [b], [(0, 0)]), "an old file does not make the tile look stale forever"

    _set_times(conn, "example/parent/region-b", fetched_days_ago=0)
    newer_b = extracts.get_extract(conn, "example/parent/region-b")
    assert newer_b is not None
    assert extracts.tiles_cut_from(conn, [newer_b], [(0, 0)]) is False, "a refreshed younger file is noticed too"


def test_a_cut_from_registered_regions_replaces_a_tile_imported_from_a_local_file(conn: sqlite3.Connection, tmp_path: Path):
    a, b = _register_border_regions(conn, tmp_path)
    local_pbf = tmp_path / "only.osm.pbf"
    write_pbf(
        local_pbf,
        nodes=[(1, 0.010, 0.010, {}), (9, 0.020, 0.030, {})],
        ways=[(777, [1, 9], {"highway": "track"})],
    )
    local = extracts.register_local_extract(conn, local_pbf, "local:only")
    extracts.cut_area(conn, [local], TILE00_BBOX)
    assert set(_tile_ways(conn, 0)) == {777}
    assert _tile_row(conn)["source"] == "extract:local:only"
    assert extracts.tiles_cut_from(conn, [a, b], [(0, 0)]) is False

    extracts.cut_area(conn, [a, b], TILE00_BBOX)

    assert set(_tile_ways(conn, 0)) == {100, 150}
    assert _tile_row(conn)["source"] == BOTH


def test_an_uploaded_tile_is_not_current_for_a_cut(conn: sqlite3.Connection, tmp_path: Path):
    a, b = _register_border_regions(conn, tmp_path)
    tilecache.store_tile_elements(conn, 0, 0, [], source=BOTH)
    assert extracts.tiles_cut_from(conn, [a, b], [(0, 0)]) is False


def test_a_tile_write_that_fails_leaves_the_stored_tile_alone(conn: sqlite3.Connection):
    tilecache.store_tile_elements(conn, 0, 0, [{"type": "node", "id": 1, "lat": 0.01, "lon": 0.01}], source="extract:x")

    with pytest.raises(RuntimeError):
        with tilecache.immediate_transaction(conn):
            tilecache.write_tile_elements(conn, 0, 0, [], source="extract:y")
            raise RuntimeError("interrupted")

    assert conn.in_transaction is False
    assert _tile_row(conn)["source"] == "extract:x"


def test_a_tile_write_holds_the_write_lock_so_a_second_writer_waits():
    first, second = db.connect(), db.connect()
    second.execute("PRAGMA busy_timeout = 0")
    with tilecache.immediate_transaction(first):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            tilecache.store_tile_elements(second, 0, 0, [], source="extract:x")
        tilecache.write_tile_elements(first, 0, 0, [], source="extract:y")
    tilecache.store_tile_elements(second, 0, 0, [], source="extract:z")
    assert first.execute("SELECT source FROM map_tiles").fetchone()["source"] == "extract:z"


def test_merge_keeps_one_copy_of_each_element_preferring_more_located_nodes_and_fills_gaps_from_the_other():
    short = {"type": "way", "id": 2, "geometry": [{"lat": 0, "lon": 0}, None]}
    long = {"type": "way", "id": 2, "geometry": [{"lat": 0, "lon": 0}, {"lat": 0, "lon": 1}, {"lat": 0, "lon": 2}]}
    node = {"type": "node", "id": 1, "lat": 0, "lon": 0}
    merged = extracts.merge_elements([[short, node], [long]])
    assert sorted(((e["type"], len(e.get("geometry") or [])) for e in merged)) == [("node", 0), ("way", 3)]

    first = {"type": "way", "id": 1, "nodes": [1, 2, 3], "geometry": [{"lat": 0, "lon": 0}, None, {"lat": 0, "lon": 2}]}
    second = {"type": "way", "id": 1, "nodes": [1, 2, 3], "geometry": [None, {"lat": 0, "lon": 1}, {"lat": 0, "lon": 2}]}
    (merged,) = extracts.merge_elements([[first], [second]])
    assert merged["geometry"] == [{"lat": 0, "lon": 0}, {"lat": 0, "lon": 1}, {"lat": 0, "lon": 2}]


def test_a_size_refusal_starts_no_cooldown_and_names_the_smaller_regions(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _serve_index(monkeypatch)

    def refuse(url: str, dest: Path, validators: dict, **kw):
        raise extracts.ExtractTooLargeError(f"{url} is 4.0 GB, over the 2 GB limit")

    monkeypatch.setattr(extracts, "_download_with_backoff", refuse)
    with pytest.raises(extracts.ExtractTooLargeError) as caught:
        extracts.select_extracts(conn, STRADDLING_BBOX, preferred_region="example/parent")
    assert "example/parent/region-a" in str(caught.value) and "example/parent/region-b" in str(caught.value)
    assert conn.execute("SELECT COUNT(*) FROM extract_requests").fetchone()[0] == 0
    with pytest.raises(extracts.ExtractTooLargeError):
        extracts.select_extracts(conn, STRADDLING_BBOX, preferred_region="example/parent")


def test_an_explicit_region_on_a_fresh_install_is_not_blocked_by_an_unreachable_index(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    def down(policy, url, **kw):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(extracts.geofabrik.http_client, "request", down)
    monkeypatch.setattr(extracts, "_download_with_backoff", _fake_download([]))

    selected = extracts.select_extracts(conn, STRADDLING_BBOX, preferred_region="example/region-a")

    assert [e["region"] for e in selected] == ["example/region-a"]


def test_the_checksum_fetch_is_retried_once_and_a_verified_download_is_kept_without_reading_it_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    body = b"data"
    md5_calls: list[str] = []

    def request(policy, url, *, headers=None, reader=None, **kwargs):
        if url.endswith(".md5"):
            md5_calls.append(url)
            if len(md5_calls) == 1:
                raise urllib.error.URLError("reset")
            digest = hashlib.md5(body).hexdigest() if len(md5_calls) == 2 else "0" * 32
            return f"{digest}  y\n".encode()
        return reader(FakeResponse(body))

    monkeypatch.setattr(extracts.http_client, "request", request)
    extracts._download_with_backoff("http://x/y.pbf", tmp_path / "y.pbf", {})
    assert (tmp_path / "y.pbf").read_bytes() == body
    assert len(md5_calls) == 2  # one failed attempt, one retry; none after the body


def test_the_checksum_is_fetched_from_the_url_the_body_was_served_from(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    body = b"data"
    seen: list[str] = []

    class Resp(FakeResponse):
        def geturl(self) -> str:
            return "http://x/y-240101.osm.pbf"

    def request(policy, url, *, headers=None, reader=None, method=None, **kwargs):
        seen.append(f"{method or 'GET'} {url}")
        if url.endswith(".md5"):
            return f"{hashlib.md5(body).hexdigest()}  y-240101.osm.pbf\n".encode()
        return reader(Resp(body))

    monkeypatch.setattr(extracts.http_client, "request", request)
    extracts._download_with_backoff("http://x/y-latest.osm.pbf", tmp_path / "y.pbf", {})
    assert seen == [
        "HEAD http://x/y-latest.osm.pbf",
        "GET http://x/y-240101.osm.pbf.md5",
        "GET http://x/y-240101.osm.pbf",
    ]


def test_an_unavailable_checksum_aborts_before_the_body_and_starts_the_cooldown(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    bodies: list[str] = []
    contacts: list[str] = []

    def request(policy, url, *, headers=None, reader=None, method=None, **kwargs):
        contacts.append(url)
        if url.endswith(".md5"):
            raise urllib.error.URLError("down")
        if method != "HEAD":
            bodies.append(url)
        return reader(FakeResponse(b"data"))

    monkeypatch.setattr(extracts.http_client, "request", request)
    with pytest.raises(extracts.ExtractChecksumUnavailableError):
        extracts.download_extract(conn, "test/sample")
    assert bodies == []
    assert conn.execute("SELECT COUNT(*) FROM extract_requests WHERE region = 'test/sample'").fetchone()[0] == 1

    contacted_before = len(contacts)
    with pytest.raises(extracts.ExtractDownloadError, match="not contacting Geofabrik again"):
        extracts.download_extract(conn, "test/sample")
    assert len(contacts) == contacted_before


# ---------------------------------------------------------------------------
# Which registered extracts a tile is cut from
# ---------------------------------------------------------------------------

INTERIOR_TILE = (-1, 0)  # lng [-0.05, 0): wholly inside region-a, and so inside the parent


def _register_family(conn: sqlite3.Connection, tmp_path: Path) -> dict[str, extracts.ExtractRecord]:
    sources = _border_pbfs(tmp_path)
    parent_pbf = tmp_path / "parent.osm.pbf"
    shutil.copy(sources["example/parent/region-a"], parent_pbf)
    files = {"example/parent": parent_pbf, **sources}
    return {region: extracts.register_local_extract(conn, p, region) for region, p in files.items()}


def _store_border_index(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_index(monkeypatch)
    extracts.geofabrik.regions(conn)


def _regions_of(per_tile: dict, tile: tuple[int, int]) -> list[str]:
    return sorted(e["region"] for e in per_tile[tile])


PARENT, REGION_A, REGION_B = "example/parent", "example/parent/region-a", "example/parent/region-b"


@pytest.mark.parametrize(
    "unregistered, selected, tiles, expected",
    [
        ([], [REGION_A], [INTERIOR_TILE, (3, 0)], {INTERIOR_TILE: [REGION_A], (3, 0): [REGION_B]}),
        ([REGION_B], [REGION_A], [INTERIOR_TILE, (1, 0)], {INTERIOR_TILE: [REGION_A], (1, 0): [PARENT, REGION_A]}),
        ([], [], [(0, 0)], {(0, 0): [PARENT, REGION_A, REGION_B]}),
    ],
    ids=["a parent is left out where a child wholly contains the tile", "a parent is kept where a sibling is unregistered", "a border tile keeps the parent beside both siblings"],
)
def test_which_registered_extracts_a_tile_is_cut_from(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unregistered: list[str], selected: list[str], tiles: list, expected: dict
):
    family = _register_family(conn, tmp_path)
    _store_border_index(conn, monkeypatch)
    for region in unregistered:
        conn.execute("DELETE FROM map_extracts WHERE region = ?", (region,))
    conn.commit()

    per_tile = extracts.tile_extracts(conn, [family[r] for r in selected], tiles)

    assert {tile: _regions_of(per_tile, tile) for tile in tiles} == expected


def test_an_interior_cut_reads_only_the_child(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    family = _register_family(conn, tmp_path)
    _store_border_index(conn, monkeypatch)
    read: list[str] = []
    real = extracts._cut_elements

    def spy(extract, bbox):
        read.append(extract["region"])
        return real(extract, bbox)

    monkeypatch.setattr(extracts, "_cut_elements", spy)
    selected = [family["example/parent"]]

    extracts.cut_area(conn, selected, (0.01, -0.04, 0.02, -0.01))
    assert read == ["example/parent/region-a"]
    assert extracts.tiles_cut_from(conn, selected, [INTERIOR_TILE])


def test_the_index_polygon_decides_which_registered_extracts_touch_a_tile(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    family = _register_family(conn, tmp_path)
    _store_border_index(conn, monkeypatch)
    conn.execute("UPDATE map_extracts SET bounds_south = -1, bounds_west = -1, bounds_north = 1, bounds_east = 1")
    conn.commit()
    selected = [family["example/parent/region-a"]]

    per_tile = extracts.tile_extracts(conn, selected, [INTERIOR_TILE])

    assert _regions_of(per_tile, INTERIOR_TILE) == ["example/parent/region-a"], "region-b's wide header box is ignored"


def test_a_registered_extract_whose_file_is_missing_is_not_cut_from(conn: sqlite3.Connection, tmp_path: Path):
    sources = _border_pbfs(tmp_path)
    a, b = (extracts.register_local_extract(conn, p, region) for region, p in sources.items())
    sources["example/parent/region-b"].unlink()

    per_tile = extracts.tile_extracts(conn, [a], [(0, 0)])

    assert _regions_of(per_tile, (0, 0)) == ["example/parent/region-a"]
    assert b["region"] not in {e["region"] for e in per_tile[(0, 0)]}


def test_an_extract_with_no_bounds_touches_only_tiles_of_a_selected_cut(conn: sqlite3.Connection, tmp_path: Path):
    a, b = _register_border_regions(conn, tmp_path)
    conn.execute(
        "UPDATE map_extracts SET bounds_south = NULL, bounds_west = NULL, bounds_north = NULL, bounds_east = NULL "
        "WHERE region = ?",
        (b["region"],),
    )
    conn.commit()
    unbounded = extracts.get_extract(conn, b["region"])
    assert unbounded is not None

    assert _regions_of(extracts.tile_extracts(conn, [a], [(40, 40)]), (40, 40)) == [a["region"]]
    assert _regions_of(extracts.tile_extracts(conn, [unbounded], [(40, 40)]), (40, 40)) == [b["region"]]


# ---------------------------------------------------------------------------
# Download progress reporting
# ---------------------------------------------------------------------------


def test_throttled_progress_forwards_at_most_once_per_interval_and_always_when_forced():
    now = [0.0]
    seen: list[tuple[int, int | None]] = []
    report = extracts.throttled_progress(lambda w, e: seen.append((w, e)), interval=1.0, clock=lambda: now[0])

    report(0, 100)
    now[0] = 0.4
    report(10, 100)
    now[0] = 1.1
    report(20, 100)
    now[0] = 1.2
    report(30, 100)
    report(100, 100, True)

    assert seen == [(0, 100), (20, 100), (100, 100)]


def test_a_failing_progress_callback_does_not_stop_the_reporter():
    calls: list[int] = []

    def broken(written: int, expected: int | None) -> None:
        calls.append(written)
        raise sqlite3.OperationalError("database is locked")

    report = extracts.throttled_progress(broken, interval=0.0)
    report(1, None)
    report(2, None)

    assert calls == [1, 2]


def test_an_extract_registry_from_before_conditional_downloads_still_reads():
    legacy = sqlite3.connect(":memory:")
    legacy.row_factory = sqlite3.Row
    legacy.execute(
        "CREATE TABLE map_extracts (region TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, "
        "fetched_at TEXT NOT NULL, bounds_south REAL, bounds_west REAL, bounds_north REAL, bounds_east REAL)"
    )
    legacy.execute("INSERT INTO map_extracts VALUES ('r/x', '/p', 's', '2024-01-01T00:00:00+00:00', 0, 0, 1, 1)")
    extracts.init_extract_schema(legacy)

    record = extracts.get_extract(legacy, "r/x")

    assert record is not None
    assert record["checked_at"] == record["data_as_of"] == "2024-01-01T00:00:00+00:00"
    assert record["etag"] is None
