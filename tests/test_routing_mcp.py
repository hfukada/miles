import json
import sqlite3
from contextlib import contextmanager

import pytest
from helpers import ORIGIN_LAT, ORIGIN_LNG, FakeOsrm, by_deg_lat, call_tool, node, none_sampler, way
from mcp.server.fastmcp import FastMCP

from miles import network, route_search, routing_mcp

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


@pytest.mark.parametrize("as_json_text", [False, True])
def test_find_routes_tool_takes_starts_and_way_ids_as_lists_or_json_text_and_a_seed_makes_it_reproducible(tool, as_json_text: bool):
    starts = [{"lat": TH_LAT, "lng": ORIGIN_LNG, "label": "Lot"}]
    avoid = [999999]
    args = {
        "area_id": 1, "distance_mi_min": 0.1, "distance_mi_max": 0.5, "shape": "loop", "seed": 3,
        "starts": json.dumps(starts) if as_json_text else starts,
        "avoid_way_ids": json.dumps(avoid) if as_json_text else avoid,
    }
    result = tool("find_routes", args)
    assert "error" not in result
    assert result["candidates"][0]["start"]["label"] == "Lot"
    assert "longest_hike_mi" in result["candidates"][0]["runnability"]
    again = tool("find_routes", args)
    assert result["seed"] == again["seed"] == 3  # a seed makes the search reproducible
    assert [c["distance_mi"] for c in result["candidates"]] == [c["distance_mi"] for c in again["candidates"]]
