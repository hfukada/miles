import json
import sqlite3

import pytest
from helpers import ORIGIN_LAT, ORIGIN_LNG, FakeOsrm, by_deg_lat, call_tool, node, none_sampler, way
from mcp.server.fastmcp import FastMCP

from miles import db, network, route_search, search_mcp

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
        summary = search_mcp._candidate_summary(candidate)
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


class _Borrowed:
    """A connection a tool may close without ending the test's own use of it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.closed = False

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def tool(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    """Call a registered search tool against the test database; the
    connections it opens are recorded on `.lent`."""
    server = FastMCP("test")
    search_mcp.register_search_tools(server)
    lent: list[_Borrowed] = []

    def open_connection() -> _Borrowed:
        lent.append(_Borrowed(conn))
        return lent[-1]

    monkeypatch.setattr(search_mcp.db, "connect", open_connection)

    def call(name: str, args: dict) -> dict:
        return call_tool(server, name, args)

    call.lent = lent  # type: ignore[attr-defined]
    return call


@pytest.mark.parametrize("name", ["find_routes", "get_route_candidate", "save_route_candidate"])
def test_every_search_tool_closes_its_connection_even_on_an_error(tool, name: str):
    args = {
        "find_routes": {"area_id": 1, "distance_mi_min": 0.1, "distance_mi_max": 0.5, "shape": "bogus",
                        "start_lat": TH_LAT, "start_lng": ORIGIN_LNG},
        "get_route_candidate": {"candidate_id": "missing"},
        "save_route_candidate": {"candidate_id": "missing", "name": "x"},
    }[name]

    result = tool(name, args)

    assert "error" in result
    assert [b.closed for b in tool.lent] == [True]


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
