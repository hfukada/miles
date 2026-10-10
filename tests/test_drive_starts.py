import http.client
import json
import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from helpers import (
    ORIGIN_LAT,
    ORIGIN_LNG,
    P1_ID,
    P2_ID,
    P3_ID,
    P4_ID,
    TH1_ID,
    TH2_ID,
    TH2_LAT,
    TH3_ID,
    TH3_LAT,
    FakeOsrm,
    NoneSampler,
    by_deg_lat,
    by_deg_lng,
    meadow_payload,
    memory_db,
    node,
    parse_osrm_coords,
    way,
)

from miles import drive_api, drive_starts, network


@pytest.fixture
def conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    network.build_network(conn, 1, [meadow_payload()], NoneSampler())
    return conn


def _search(conn: sqlite3.Connection, lat: float = ORIGIN_LAT, lng: float = ORIGIN_LNG, minutes: float = 10.0, k: int = 8):
    return drive_starts.drive_starts(conn, 1, lat, lng, max_drive_minutes=minutes, k=k)


def _ids(results) -> set:
    return {r["osm_node_id"] for r in results[1:]}


def test_drive_starts_prefilters_excludes_private_and_far_parking_and_dedupes(conn: sqlite3.Connection, osrm: FakeOsrm):
    results = _search(conn)

    # origin always comes first, as an out-the-door start, with the trail within reach counted.
    assert results[0]["kind"] == "origin"
    assert results[0]["drive_s"] == 0.0
    assert results[0]["osm_node_id"] is None
    assert results[0]["trail_m_nearby"] == pytest.approx(200.0, abs=5.0)

    node_ids = _ids(results)
    assert TH1_ID in node_ids
    assert node_ids.isdisjoint({P3_ID, P4_ID})  # private and trail-adjacency exclusions
    assert TH3_ID not in node_ids  # passes the straight-line prefilter but not the real budget at 90 s/km
    assert osrm.requested(TH3_LAT, ORIGIN_LNG)  # ...so it was asked about, and found too slow
    assert TH2_ID not in node_ids  # dropped by the straight-line prefilter
    assert not osrm.requested(TH2_LAT, ORIGIN_LNG)
    assert len(node_ids & {P1_ID, P2_ID}) == 1  # deduped to one of the two nearby parking nodes

    th1 = next(r for r in results if r["osm_node_id"] == TH1_ID)
    assert th1["label"] == "Meadow Trailhead"
    assert th1["kind"] == "trailhead"

    parking = next(r for r in results if r["osm_node_id"] in (P1_ID, P2_ID))
    assert parking["label"] == "Parking near Meadow Trail"
    assert parking["kind"] == "parking"

    assert len(osrm.calls) == 1  # one request for the whole search
    assert len(parse_osrm_coords(osrm.calls[0])) - 1 <= drive_starts.OSRM_MAX_DESTINATIONS_PER_CALL


def test_a_trailhead_reachable_by_a_fast_road_is_not_dropped_before_osrm(conn: sqlite3.Connection, osrm: FakeOsrm):
    osrm.seconds_per_km = 30.0  # 55 km in 27.5 minutes: 120 km/h, inside a 30 minute budget although 60 km/h would need 55

    results = _search(conn, minutes=30.0)

    assert TH2_ID in _ids(results)
    assert osrm.requested(TH2_LAT, ORIGIN_LNG)


def test_only_the_best_ranked_candidates_are_sent_to_osrm(conn: sqlite3.Connection, osrm: FakeOsrm, monkeypatch: pytest.MonkeyPatch):
    """Three survivors of the straight-line prefilter; with k=1 only the best
    k * OSRM_CANDIDATES_PER_RESULT by optimistic score are looked up."""
    monkeypatch.setattr(drive_starts, "OSRM_CANDIDATES_PER_RESULT", 1)

    results = _search(conn, k=1)

    assert len(osrm.calls) == 1 and len(parse_osrm_coords(osrm.calls[0])) == 2  # the origin and one destination
    assert [r["osm_node_id"] for r in results[1:]] == [TH1_ID]


def test_the_request_never_exceeds_osrms_destination_limit(conn: sqlite3.Connection, osrm: FakeOsrm, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(drive_starts, "OSRM_MAX_DESTINATIONS_PER_CALL", 2)

    _search(conn)

    assert len(osrm.calls) == 1 and len(parse_osrm_coords(osrm.calls[0])) == 3


def test_cached_candidates_do_not_count_against_the_osrm_limit(conn: sqlite3.Connection, osrm: FakeOsrm, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(drive_starts, "OSRM_CANDIDATES_PER_RESULT", 1)
    _search(conn, k=1)
    osrm.calls.clear()

    _search(conn, k=1)

    assert len(osrm.calls) == 1 and len(parse_osrm_coords(osrm.calls[0])) == 2  # the next candidate in line, not the cached one


@pytest.mark.parametrize("minutes", [0.0, -5.0, drive_starts.MAX_DRIVE_MINUTES + 1])
def test_max_drive_minutes_is_capped(conn: sqlite3.Connection, minutes: float):
    with pytest.raises(ValueError, match="max_drive_minutes"):
        _search(conn, minutes=minutes)


def test_a_trailhead_outranks_a_lot_with_a_little_more_trail_in_reach(osrm: FakeOsrm):
    """The trailhead bonus is worth more than the extra trail a lot in the
    middle of a 1.3 km path can reach."""
    lat0, lng0 = ORIGIN_LAT, ORIGIN_LNG
    payload = {"elements": [
        node(1, lat0, lng0), node(2, lat0 + by_deg_lat(1300.0), lng0),
        way(10, [1, 2], {"highway": "path"}),
        node(20, lat0, lng0, {"highway": "trailhead", "name": "Gate"}),
        node(21, lat0 + by_deg_lat(650.0), lng0 + by_deg_lng(lat0, 100.0), {"amenity": "parking"}),
    ]}
    conn = memory_db()
    network.build_network(conn, 1, [payload], NoneSampler())
    osrm.seconds_per_km = 1.0

    results = _search(conn)

    assert [r["kind"] for r in results[1:]] == ["trailhead", "parking"]


@pytest.mark.parametrize(
    "case, expected_trail_m",
    [("trail running on past the radius", drive_starts.TRAIL_NEARBY_RADIUS_M), ("sidewalk beside a road", 0.0), ("trail far from any road", 300.0)],
)
def test_trail_m_nearby_counts_only_trail_within_reach_and_not_a_sidewalk(case: str, expected_trail_m: float):
    lat0, lng0 = 6.0, 11.0
    if case == "trail running on past the radius":
        elements = [node(901, lat0, lng0), node(902, lat0 + by_deg_lat(3000.0), lng0), way(9001, [901, 902], {"highway": "path"})]
        origin = (lat0, lng0)
    elif case == "sidewalk beside a road":
        end_lng = lng0 + by_deg_lng(lat0, 300.0)
        sidewalk_lat = lat0 + by_deg_lat(10.0)
        elements = [
            node(1001, lat0, lng0), node(1002, lat0, end_lng),
            way(10001, [1001, 1002], {"highway": "residential", "name": "Elm Street"}),
            node(1011, sidewalk_lat, lng0), node(1012, sidewalk_lat, end_lng),
            way(10002, [1011, 1012], {"highway": "footway"}),
        ]
        origin = (sidewalk_lat, lng0)
    else:
        elements = [
            node(1101, lat0, lng0), node(1102, lat0 + by_deg_lat(300.0), lng0),
            way(11001, [1101, 1102], {"highway": "footway", "name": "Backcountry Path"}),
        ]
        origin = (lat0, lng0)
    conn = memory_db()
    network.build_network(conn, 1, [{"elements": elements}], NoneSampler())

    assert _search(conn, *origin)[0]["trail_m_nearby"] == pytest.approx(expected_trail_m, abs=50.0 if expected_trail_m else 1e-9)


def _straight_s(meters: float) -> float:
    return meters / 1000.0 * 60.0 * drive_starts.STRAIGHT_LINE_DETOUR  # areas.DRIVE_KM_PER_MINUTE is 1


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("refused"),
        http.client.IncompleteRead(b"ab", 10),
        TimeoutError(),
        ConnectionResetError(),
        json.dumps({"code": "Ok", "durations": [[0.0]]}).encode(),
    ],
    ids=["refused", "incomplete read", "timeout", "reset", "malformed answer"],
)
def test_a_failed_osrm_request_falls_back_to_flagged_estimates_that_are_not_cached(conn: sqlite3.Connection, osrm: FakeOsrm, failure):
    if isinstance(failure, bytes):
        osrm.body = failure
    else:
        osrm.fail_with = failure

    results = _search(conn)

    assert results[0]["kind"] == "origin" and results[0]["drive_time_estimated"] is False
    estimated = results[1:]
    assert TH1_ID in _ids(results)
    assert all(r["drive_time_estimated"] for r in estimated)
    th1 = next(r for r in estimated if r["osm_node_id"] == TH1_ID)
    assert th1["drive_s"] == pytest.approx(_straight_s(300.0), rel=0.05)
    assert conn.execute("SELECT COUNT(*) FROM drive_time_cache").fetchone()[0] == 0


def test_osrm_is_left_alone_for_ten_minutes_after_a_failure_then_tried_again(conn: sqlite3.Connection, osrm: FakeOsrm, monkeypatch: pytest.MonkeyPatch):
    osrm.fail_with = urllib.error.URLError("connection refused")
    now = [1000.0]
    monkeypatch.setattr(drive_starts.time, "monotonic", lambda: now[0])
    _search(conn)
    assert len(osrm.calls) == 1

    now[0] += drive_starts.OSRM_RETRY_COOLDOWN_S - 1
    _search(conn)
    assert len(osrm.calls) == 1  # still cooling down: no request

    now[0] += 2
    osrm.fail_with = None
    recovered = _search(conn)
    assert len(osrm.calls) == 2
    assert TH1_ID in _ids(recovered)
    assert not any(r["drive_time_estimated"] for r in recovered)


def _age_cache_rows(conn: sqlite3.Connection, *, unreachable: bool, days: float) -> None:
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute(f"UPDATE drive_time_cache SET fetched_at = ? WHERE drive_s IS {'' if unreachable else 'NOT '}NULL", (when,))
    conn.commit()


def test_an_unreachable_answer_is_cached_but_expires_after_a_week(conn: sqlite3.Connection, osrm: FakeOsrm):
    osrm.unreachable = True
    _search(conn)
    asked = len(osrm.calls)
    assert conn.execute("SELECT COUNT(*) FROM drive_time_cache WHERE drive_s IS NULL").fetchone()[0] > 0

    _search(conn)
    assert len(osrm.calls) == asked  # answered from the cache

    _age_cache_rows(conn, unreachable=True, days=drive_starts.UNREACHABLE_CACHE_TTL_DAYS + 1)
    _search(conn)
    assert len(osrm.calls) > asked


def test_a_measured_time_outlives_the_unreachable_answer_and_expires_after_six_months(conn: sqlite3.Connection, osrm: FakeOsrm):
    _search(conn)
    asked = len(osrm.calls)

    _age_cache_rows(conn, unreachable=False, days=drive_starts.UNREACHABLE_CACHE_TTL_DAYS + 1)
    _search(conn)
    assert len(osrm.calls) == asked

    _age_cache_rows(conn, unreachable=False, days=drive_starts.DRIVE_TIME_CACHE_TTL_DAYS + 1)
    _search(conn)
    assert len(osrm.calls) > asked


def test_expired_rows_are_swept_at_most_once_an_hour_per_process(conn: sqlite3.Connection, osrm: FakeOsrm):
    stale = (datetime.now(timezone.utc) - timedelta(days=drive_starts.UNREACHABLE_CACHE_TTL_DAYS + 1)).isoformat()

    def seed_and_count() -> int:
        conn.execute("INSERT OR REPLACE INTO drive_time_cache VALUES (0, 0, 999, NULL, ?)", (stale,))
        conn.commit()
        _search(conn)
        return conn.execute("SELECT COUNT(*) FROM drive_time_cache WHERE dest_node_id = 999").fetchone()[0]

    assert seed_and_count() == 0
    assert seed_and_count() == 1  # a second search within the hour leaves it


@pytest.mark.parametrize("fails", [False, True])
def test_the_trailheads_endpoint_closes_its_connection(monkeypatch: pytest.MonkeyPatch, fails: bool):
    closed: list[bool] = []

    class _Conn:
        def close(self) -> None:
            closed.append(True)

    def fake_drive_starts(conn, *args, **kwargs):
        if fails:
            raise ValueError("bad")
        return []

    monkeypatch.setattr(drive_api.db, "connect", lambda: _Conn())
    monkeypatch.setattr(drive_api.drive_starts, "drive_starts", fake_drive_starts)
    if fails:
        with pytest.raises(drive_api.HTTPException):
            drive_api.get_trailheads(1, 0.0, 0.0, 10.0)
    else:
        assert drive_api.get_trailheads(1, 0.0, 0.0, 10.0) == []
    assert closed == [True]


@pytest.mark.parametrize(
    "d_lat_m, d_lng_m, shares_the_cache",
    [(4.0, 4.0, True), (250.0, 0.0, False), (0.0, 250.0, False)],
    ids=["a few metres apart", "a cell north", "a cell east"],
)
def test_searches_from_nearby_origins_share_cached_drive_times_and_distant_ones_do_not(
    conn: sqlite3.Connection, osrm: FakeOsrm, d_lat_m: float, d_lng_m: float, shares_the_cache: bool
):
    _search(conn)
    asked = len(osrm.calls)

    _search(conn, ORIGIN_LAT + by_deg_lat(d_lat_m), ORIGIN_LNG + by_deg_lng(ORIGIN_LAT, d_lng_m))

    assert (len(osrm.calls) == asked) is shares_the_cache


def test_origins_in_one_row_agree_on_the_column_width(conn: sqlite3.Connection, osrm: FakeOsrm):
    """Origins at slightly different latitudes within one row must not key
    different columns because the column width followed their own latitude."""
    lat_step = drive_starts.ORIGIN_CACHE_GRID_M / drive_starts._METERS_PER_DEG_LAT
    centre = round(ORIGIN_LAT / lat_step) * lat_step
    _search(conn, centre - by_deg_lat(40.0), ORIGIN_LNG)
    asked = len(osrm.calls)

    _search(conn, centre + by_deg_lat(40.0), ORIGIN_LNG)

    assert len(osrm.calls) == asked
