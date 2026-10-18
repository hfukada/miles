import json
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

import pytest
from helpers import ORIGIN_LAT, ORIGIN_LNG, FakeOsrm, by_deg_lat, call_tool, node, none_sampler, way
from mcp.server.fastmcp import FastMCP

from test_sections import Map, pt

from miles import network, route_search, routing_mcp, search_jobs, survey

# A single 400m trail with a named trailhead at its near end, a mile or so
# from the drive origin -- enough distance for OSRM drive time to matter,
# short enough that a wide search window still finds a loop.
TH_LAT = ORIGIN_LAT + by_deg_lat(1600.0)
A_LAT = TH_LAT
B_LAT = A_LAT + by_deg_lat(200.0)
C_LNG = ORIGIN_LNG + by_deg_lat(200.0)  # reuse the same meters-per-degree at this latitude
TH_ID, A_ID, B_ID, C_ID = 1, 2, 3, 4


@pytest.fixture
def conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    payload = {
        "elements": [
            node(TH_ID, TH_LAT, ORIGIN_LNG, {"highway": "trailhead", "name": "Ridge Trailhead"}),
            node(A_ID, A_LAT, ORIGIN_LNG), node(B_ID, B_LAT, ORIGIN_LNG), node(C_ID, B_LAT, C_LNG),
            way(10, [A_ID, B_ID], {"highway": "path"}),
            way(11, [B_ID, C_ID], {"highway": "path"}),
            way(12, [C_ID, A_ID], {"highway": "path"}),
        ]
    }
    network.build_network(conn, 1, [payload], none_sampler())
    return conn


def _drive_starts(conn: sqlite3.Connection) -> list:
    return route_search.resolve_starts(
        conn, 1, starts=None, origin=(ORIGIN_LAT, ORIGIN_LNG), max_drive_minutes=10.0, drive_k=8
    )


def test_drive_mode_fans_out_over_osrm_and_carries_each_starts_label_and_drive_time(conn: sqlite3.Connection, osrm: FakeOsrm, monkeypatch: pytest.MonkeyPatch):
    osrm.seconds_per_km = 60.0

    starts = _drive_starts(conn)

    assert osrm.calls, "drive mode should have hit the fake OSRM responder"
    labels = {s.get("label") for s in starts}
    assert labels == {"Out the door", "Ridge Trailhead"}
    assert next(s for s in starts if s["label"] == "Out the door")["drive_s"] == 0.0
    assert next(s for s in starts if s["label"] == "Ridge Trailhead")["drive_s"] > 0.0

    request: route_search.RouteSearchRequest = {
        "area_id": 1, "starts": starts, "distance_m_min": 100.0, "distance_m_max": 800.0,
        "shape": "loop", "n": 5, "time_budget_s": 5.0, "seed": 1,
    }
    result = route_search.find_routes(conn, request)
    assert result["candidates"]
    for candidate in result["candidates"]:
        summary = routing_mcp._candidate_summary(candidate)
        assert summary["start"]["label"] == candidate["start"]["label"] in labels
        assert (summary["start"]["drive_minutes"] == 0.0) is (candidate["start"]["label"] == "Out the door")


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"starts": [{"lat": 1.0, "lng": 1.0}], "origin": (ORIGIN_LAT, ORIGIN_LNG), "max_drive_minutes": 10.0}, "mutually exclusive"),
        ({"starts": None, "origin": None, "max_drive_minutes": None}, "start_lat/start_lng"),
        ({"starts": None, "origin": None, "max_drive_minutes": 10.0}, "start_lat/start_lng"),
    ],
)
def test_resolve_starts_refuses_contradictory_or_missing_start_arguments(conn: sqlite3.Connection, arguments: dict, message: str):
    with pytest.raises(ValueError, match=message):
        route_search.resolve_starts(conn, 1, drive_k=8, **arguments)


@pytest.fixture
def tool(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    """Call a registered search tool against the test database, which stays
    open across calls."""
    server = FastMCP("test")
    routing_mcp.register_routing_tools(server)

    @contextmanager
    def lend(opener=None):
        yield conn

    monkeypatch.setattr(routing_mcp.db, "connection", lend)

    def call(name: str, args: dict) -> dict:
        return call_tool(server, name, args)

    return call


@pytest.fixture(autouse=True)
def _no_leftover_searches(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    """Searches run on the test connection, so none may outlive the test."""
    jobs: OrderedDict = OrderedDict()
    monkeypatch.setattr(search_jobs, "_jobs", jobs)
    yield
    for job in jobs.values():
        assert job.done.wait(10)


def run_search(tool, args: dict) -> dict:
    """find_routes, then get_search until it finishes."""
    started = tool("find_routes", args)
    if "search_id" not in started:
        return started
    return tool("get_search", {"search_id": started["search_id"], "wait_s": 40})


@pytest.mark.parametrize("as_json_text", [False, True])
def test_find_routes_tool_takes_starts_and_way_ids_as_lists_or_json_text_and_a_seed_makes_it_reproducible(tool, as_json_text: bool):
    starts = [{"lat": TH_LAT, "lng": ORIGIN_LNG, "label": "Lot"}]
    avoid = [999999]
    args = {
        "area_id": 1, "distance_mi_min": 0.1, "distance_mi_max": 0.5, "shape": "loop", "seed": 3,
        "starts": json.dumps(starts) if as_json_text else starts,
        "avoid_way_ids": json.dumps(avoid) if as_json_text else avoid,
    }
    result = run_search(tool, args)
    assert "error" not in result
    assert result["candidates"][0]["start"]["label"] == "Lot"
    assert "longest_hike_mi" in result["candidates"][0]["runnability"]
    again = run_search(tool, args)
    assert result["seed"] == again["seed"] == 3  # a seed makes the search reproducible
    assert [c["distance_mi"] for c in result["candidates"]] == [c["distance_mi"] for c in again["candidates"]]


def test_get_route_candidate_lists_the_ways_in_travel_order_in_miles(tool):
    args = {
        "area_id": 1, "distance_mi_min": 0.1, "distance_mi_max": 0.5, "shape": "loop", "seed": 3,
        "starts": [{"lat": TH_LAT, "lng": ORIGIN_LNG, "label": "Lot"}],
    }
    summary = run_search(tool, args)["candidates"][0]
    assert "ways" not in summary

    detail = tool("get_route_candidate", {"candidate_id": summary["candidate_id"]})
    ways = detail["ways"]
    assert {w["way_id"] for w in ways} == {10, 11, 12}
    assert all(a["way_id"] != b["way_id"] for a, b in zip(ways, ways[1:]))
    assert all(set(w) == {"way_id", "name", "surface_class", "length_mi"} for w in ways)
    assert sum(w["length_mi"] for w in ways) == pytest.approx(summary["distance_mi"], rel=0.01)


def test_find_routes_requires_a_distance_range(tool):
    with pytest.raises(Exception, match="distance_mi_min"):
        tool("find_routes", {"area_id": 1, "starts": [{"lat": TH_LAT, "lng": ORIGIN_LNG}]})


def test_find_routes_tool_reports_the_sections_a_candidate_runs_and_takes_the_section_arguments(tool, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(survey, "FRAGMENT_MIN_M", 0.0)
    monkeypatch.setattr(survey, "FRAGMENT_RUN_SHARE", 0.0)
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="Access Street", maxspeed="25 mph")
    m.square(0, 300, 600, name="Ridge Loop")
    m.build(conn, area_id=2)
    group_id = conn.execute("SELECT section_id FROM net_sections WHERE area_id = 2 AND level = 'group'").fetchone()[0]
    lat, lng = pt(0, 0)
    args = {
        "area_id": 2, "distance_mi_min": 1.7, "distance_mi_max": 2.0, "start_lat": lat, "start_lng": lng,
        "sections": [group_id], "repeat_value": 0.3, "surface_preference": "singletrack", "unfamiliar_only": True,
    }

    result = run_search(tool, args)

    assert result["candidates"] and "warnings" not in result
    top = result["candidates"][0]
    assert top["built_from"] == "sections" and top["shape"] == "lollipop"
    used = top["sections_used"][0]
    assert set(used) == {"section_id", "block_id", "name", "miles"} and used["section_id"] == group_id
    assert used["miles"] == pytest.approx(1.49, abs=0.05)
    assert top["stops"] == []
    refused = tool("find_routes", {**args, "repeat_value": 2.0})
    assert "repeat_value" in refused["error"] and "search_id" not in refused
    stop = {"lat": lat, "lng": lng, "label": "home", "at_mi": 1}
    with_stop = run_search(tool, {**args, "stops": [stop]})["candidates"][0]
    assert with_stop["stops"][0]["label"] == "home" and with_stop["stops"][0]["wanted_mi"] == 1
    assert "no section 999999" in run_search(tool, {**args, "sections": [999999]})["error"]


@pytest.mark.parametrize("origin", ["http://localhost", "http://localhost:6274", "https://miles-host"])
def test_clients_from_an_allowed_host_pass_the_origin_check_with_or_without_a_port(origin: str) -> None:
    from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings

    from miles.mcp_server import allowed_origins

    hosts = ["localhost:*", "miles-host:*"]
    middleware = TransportSecurityMiddleware(
        TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=allowed_origins(hosts))
    )
    assert middleware._validate_origin(origin)
    assert not middleware._validate_origin("http://elsewhere.test")


LOOP_ARGS = {
    "area_id": 1, "distance_mi_min": 0.1, "distance_mi_max": 0.5, "shape": "loop", "seed": 3,
    "starts": [{"lat": TH_LAT, "lng": ORIGIN_LNG, "label": "Lot"}],
}


@pytest.fixture
def gated_walks(monkeypatch: pytest.MonkeyPatch):
    """Loop generation that, from its `after`-th call on, announces itself and
    blocks until released. Returns (entered, release, set_after)."""
    entered, release = threading.Event(), threading.Event()
    calls = {"n": 0, "after": 0}
    real = route_search._GENERATORS["loop"]

    def gated(*args):
        calls["n"] += 1
        if calls["n"] > calls["after"]:
            entered.set()
            assert release.wait(10)
        return real(*args)

    monkeypatch.setitem(route_search._GENERATORS, "loop", gated)
    monkeypatch.setattr(route_search, "DEFAULT_TIME_BUDGET_S", 60.0)
    return entered, release, calls


def test_find_routes_returns_a_search_id_at_once_and_a_second_search_queues_behind_it(
    tool, gated_walks, monkeypatch: pytest.MonkeyPatch
):
    entered, release, _ = gated_walks
    monkeypatch.setattr(search_jobs, "MAX_WAIT_S", 0.2)

    first = tool("find_routes", LOOP_ARGS)
    assert first["status"] == "running" and first["candidates"] == []
    assert entered.wait(10)
    second = tool("find_routes", LOOP_ARGS)
    assert second["status"] == "queued"

    # The wait is capped, and gives up with the search still running.
    began = time.monotonic()
    assert tool("get_search", {"search_id": first["search_id"], "wait_s": 999})["status"] == "running"
    assert time.monotonic() - began < 5

    release.set()
    for reply in (first, second):
        finished = tool("get_search", {"search_id": reply["search_id"], "wait_s": 10})
        assert finished["status"] == "done" and finished["candidates"] and finished["seed"] == 3


def test_cancel_search_keeps_the_candidates_found_so_far_and_they_stay_usable(
    tool, gated_walks, monkeypatch: pytest.MonkeyPatch
):
    entered, release, calls = gated_walks
    calls["after"] = 1
    monkeypatch.setattr(search_jobs, "CANCEL_WAIT_S", 0.1)

    search_id = tool("find_routes", LOOP_ARGS)["search_id"]
    assert entered.wait(10)
    cancelled = tool("cancel_search", {"search_id": search_id})
    assert cancelled["status"] == "cancelled" and cancelled["candidates"]

    release.set()
    final = tool("get_search", {"search_id": search_id, "wait_s": 10})
    assert final["status"] == "cancelled" and final["candidates"] and "seed" in final
    assert tool("get_route_candidate", {"candidate_id": final["candidates"][0]["candidate_id"]})["ways"]


def test_get_search_reports_an_unknown_or_expired_search(tool, monkeypatch: pytest.MonkeyPatch):
    assert "unknown or expired" in tool("get_search", {"search_id": "nope"})["error"]
    assert "unknown or expired" in tool("cancel_search", {"search_id": "nope"})["error"]

    monkeypatch.setattr(search_jobs, "JOB_TTL_S", -1.0)
    old = run_search(tool, LOOP_ARGS)["search_id"]
    tool("find_routes", LOOP_ARGS)  # starting a search drops finished jobs past their TTL
    assert "unknown or expired" in tool("get_search", {"search_id": old})["error"]
