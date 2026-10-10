import itertools
import json
import sqlite3
import threading
import time
import pytest
from helpers import ImmediateThread, climbing_sampler, node, none_sampler, way

from miles import areas, network, route_search, routes as routes_service


def _interp(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


class _IdAllocator:
    def __init__(self, start: int) -> None:
        self._n = start

    def __call__(self) -> int:
        self._n += 1
        return self._n


def _add_segmented_edge(
    nodes_out: list[dict],
    ways_out: list[dict],
    node_ids: _IdAllocator,
    way_ids: _IdAllocator,
    a_id: int,
    a_ll: tuple[float, float],
    b_id: int,
    b_ll: tuple[float, float],
    segments: int,
    highway: str,
    tags: dict | None = None,
) -> int:
    """segments separate 2-node ways from a to b via fresh intermediate
    nodes -- each intermediate is shared by exactly two ways, so
    build_network's junction detection naturally splits every segment into
    its own edge, rather than treating the whole span as one long way.
    Returns the way_id used for the *first* segment (the rest follow it)."""
    prev_id = a_id
    first_way_id: int | None = None
    for i in range(1, segments + 1):
        if i < segments:
            nid = node_ids()
            nodes_out.append(node(nid, *_interp(a_ll, b_ll, i / segments)))
        else:
            nid = b_id
        wid = way_ids()
        if first_way_id is None:
            first_way_id = wid
        ways_out.append(way(wid, [prev_id, nid], dict(tags or {}, highway=highway)))
        prev_id = nid
    assert first_way_id is not None
    return first_way_id


# A diamond-shaped trail ring (corners N/E/S/W, each side split into six
# trail segments) plus two chords across it: a long, fast "busy" road
# (N-S) whose own length alone exceeds the default max continuous
# uncomfortable-road distance, and a shorter "quiet" residential road
# (E-W). ~29 edges: 24 trail + 3 busy-road + 2 quiet-road.
N_ID, E_ID, S_ID, W_ID = 1, 2, 3, 4
N_LL = (40.005, -105.0)
S_LL = (39.995, -105.0)
E_LL = (40.0, -104.996)
W_LL = (40.0, -105.004)

BUSY_WAY_TAGS = {"name": "Busy Rd", "maxspeed": "50 mph"}
QUIET_WAY_TAGS = {"name": "Quiet Rd"}


def _ring_elements(node_ids: _IdAllocator, way_ids: _IdAllocator) -> tuple[list[dict], list[dict]]:
    """The diamond's perimeter: four corners joined by six-segment trail sides."""
    nodes: list[dict] = [node(N_ID, *N_LL), node(E_ID, *E_LL), node(S_ID, *S_LL), node(W_ID, *W_LL)]
    ways: list[dict] = []
    for a_id, a_ll, b_id, b_ll in [(N_ID, N_LL, E_ID, E_LL), (E_ID, E_LL, S_ID, S_LL), (S_ID, S_LL, W_ID, W_LL), (W_ID, W_LL, N_ID, N_LL)]:
        _add_segmented_edge(nodes, ways, node_ids, way_ids, a_id, a_ll, b_id, b_ll, segments=6, highway="path")
    return nodes, ways


def _add_single_way_path(
    nodes_out: list[dict],
    ways_out: list[dict],
    node_ids: _IdAllocator,
    way_id: int,
    a_id: int,
    a_ll: tuple[float, float],
    b_id: int,
    b_ll: tuple[float, float],
    waypoints: int,
    highway: str,
    tags: dict | None = None,
) -> None:
    """One way from a to b with `waypoints` interior points that belong to
    no other way -- geometry only, not a junction, so this stays a single
    edge regardless of how many points shape it."""
    node_seq = [a_id]
    for i in range(1, waypoints + 1):
        nid = node_ids()
        nodes_out.append(node(nid, *_interp(a_ll, b_ll, i / (waypoints + 1))))
        node_seq.append(nid)
    node_seq.append(b_id)
    ways_out.append(way(way_id, node_seq, dict(tags or {}, highway=highway)))


def _build_diamond_area(conn: sqlite3.Connection, area_id: int = 1) -> dict[str, int]:
    node_ids = _IdAllocator(100)
    way_ids = _IdAllocator(0)
    nodes, ways = _ring_elements(node_ids, way_ids)

    busy_way_id = way_ids()
    _add_single_way_path(nodes, ways, node_ids, busy_way_id, N_ID, N_LL, S_ID, S_LL, waypoints=2, highway="secondary", tags=BUSY_WAY_TAGS)
    quiet_way_id = way_ids()
    _add_single_way_path(nodes, ways, node_ids, quiet_way_id, E_ID, E_LL, W_ID, W_LL, waypoints=1, highway="residential", tags=QUIET_WAY_TAGS)

    stats = network.build_network(conn, area_id, [{"elements": [*nodes, *ways]}], none_sampler())
    assert stats["edge_count"] >= 24
    return {"busy_way_id": busy_way_id, "quiet_way_id": quiet_way_id}


def _build_ring_area(conn: sqlite3.Connection, area_id: int = 4, sampler=None) -> tuple[list[dict], list[dict]]:
    """Only the diamond's perimeter: exactly one loop exists, ridden either way."""
    nodes, ways = _ring_elements(_IdAllocator(100), _IdAllocator(0))
    network.build_network(conn, area_id, [{"elements": [*nodes, *ways]}], sampler or none_sampler())
    return nodes, ways


def _side_length_m(conn: sqlite3.Connection, area_id: int, way_id: int) -> float:
    rows = conn.execute("SELECT length_m FROM net_edges WHERE area_id = ? AND way_id = ?", [area_id, way_id]).fetchall()
    return sum(r["length_m"] for r in rows)


def _perimeter_length_m(conn: sqlite3.Connection, area_id: int) -> float:
    rows = conn.execute(
        "SELECT length_m FROM net_edges WHERE area_id = ? AND highway = 'path'", [area_id]
    ).fetchall()
    return sum(r["length_m"] for r in rows)


def _way_points(conn: sqlite3.Connection, way_id: int) -> set[tuple[float, float]]:
    points: set[tuple[float, float]] = set()
    for row in conn.execute("SELECT points_blob FROM net_edges WHERE way_id = ?", [way_id]).fetchall():
        for lat, lng, _d, _e in network.unpack_edge_points(bytes(row["points_blob"])):
            points.add((round(lat, 6), round(lng, 6)))
    return points


def _candidate_points(candidate: route_search.RouteCandidateOut) -> set[tuple[float, float]]:
    return {(round(lat, 6), round(lng, 6)) for lat, lng, _e in candidate["geometry"]}  # type: ignore[misc]


def _ring_points(conn: sqlite3.Connection) -> set[tuple[float, float]]:
    points: set[tuple[float, float]] = set()
    for row in conn.execute("SELECT points_blob FROM net_edges WHERE highway = 'path'").fetchall():
        for lat, lng, _d, _e in network.unpack_edge_points(bytes(row["points_blob"])):
            points.add((round(lat, 6), round(lng, 6)))
    return points


def _start_near_n() -> dict:
    # Partway along the first N->E segment, not exactly on any node -- a
    # search's own start point should splice a fresh node in here.
    lat, lng = _interp(N_LL, E_LL, 1.0 / 12.0)
    return {"lat": lat, "lng": lng}


def _base_request(area_id: int, distance_m_min: float, distance_m_max: float, **overrides) -> route_search.RouteSearchRequest:
    request: route_search.RouteSearchRequest = {
        "area_id": area_id,
        "starts": [_start_near_n()],
        "distance_m_min": distance_m_min,
        "distance_m_max": distance_m_max,
        "shape": "loop",
        "n": 5,
        "time_budget_s": 10.0,
        "seed": 12345,
    }
    request.update(overrides)  # type: ignore[typeddict-item]
    return request


def test_loop_candidates_land_within_the_distance_range(conn: sqlite3.Connection):
    _build_diamond_area(conn)
    perimeter = _perimeter_length_m(conn, 1)

    request = _base_request(1, perimeter * 0.5, perimeter * 1.5)
    result = route_search.find_routes(conn, request)

    assert result["candidates"], "expected at least one qualifying loop"
    assert result["near_miss"] is False
    for candidate in result["candidates"]:
        assert candidate["shape"] == "loop"
        assert request["distance_m_min"] <= candidate["distance_m"] <= request["distance_m_max"]
        assert candidate["missed_constraints"] == []


def test_busy_road_loop_is_never_returned(conn: sqlite3.Connection):
    ids = _build_diamond_area(conn)
    busy_len = _side_length_m(conn, 1, ids["busy_way_id"])
    assert busy_len > network.get_route_preferences(conn)["max_continuous_uncomfortable_m"]

    result = route_search.find_routes(conn, _base_request(1, busy_len * 0.5, busy_len * 3.0, n=8, time_budget_s=5.0))

    assert result["candidates"], "the ring alone offers loops in range"
    busy_only = _way_points(conn, ids["busy_way_id"]) - _ring_points(conn)
    assert busy_only
    for candidate in result["candidates"]:
        assert candidate["score"]["road_exposure"]["violations"] == []
        assert not (_candidate_points(candidate) & busy_only)


def _triangle_length_m(conn: sqlite3.Connection, quiet_way_id: int) -> float:
    """The loop N -> E -> (quiet chord) -> W -> N: two ring sides plus the chord."""
    return _perimeter_length_m(conn, 1) / 2.0 + _side_length_m(conn, 1, quiet_way_id)


def test_an_avoided_road_is_excluded_whether_named_in_the_request_or_overridden_persistently(conn: sqlite3.Connection):
    ids = _build_diamond_area(conn)
    quiet_only = _way_points(conn, ids["quiet_way_id"]) - _ring_points(conn)
    assert quiet_only
    triangle = _triangle_length_m(conn, ids["quiet_way_id"])

    # A range that only the loop over the quiet chord can fill.
    wanted = _base_request(1, triangle * 0.97, triangle * 1.03, n=5, time_budget_s=5.0)
    baseline = route_search.find_routes(conn, wanted)
    assert baseline["candidates"] and not baseline["near_miss"]
    assert all(_candidate_points(c) & quiet_only for c in baseline["candidates"])

    avoided = route_search.find_routes(conn, {**wanted, "avoid_way_ids": [ids["quiet_way_id"]]})
    assert avoided["candidates"], "an avoided way must not leave the search empty-handed"
    for candidate in avoided["candidates"]:
        assert not (_candidate_points(candidate) & quiet_only)

    network.set_way_override(conn, ids["quiet_way_id"], "avoid")  # the persistent form of the same request
    for candidate in route_search.find_routes(conn, wanted)["candidates"]:
        assert not (_candidate_points(candidate) & quiet_only)


def test_near_miss_reports_the_missed_constraints(conn: sqlite3.Connection):
    _build_diamond_area(conn)
    perimeter = _perimeter_length_m(conn, 1)

    # Nothing in this tiny network can possibly be this long.
    result = route_search.find_routes(conn, _base_request(1, perimeter * 50, perimeter * 51, time_budget_s=3.0))

    assert result["near_miss"] is True
    assert result["candidates"]
    for candidate in result["candidates"]:
        assert candidate["missed_constraints"]
        assert any(m["constraint"] == "distance_range" for m in candidate["missed_constraints"])


def test_diversify_returns_distinct_candidates_and_a_loop_ridden_in_either_direction_is_one(conn: sqlite3.Connection):
    _build_diamond_area(conn)
    perimeter = _perimeter_length_m(conn, 1)

    result = route_search.find_routes(conn, _base_request(1, perimeter * 0.3, perimeter * 1.3, n=4, shape="any", time_budget_s=8.0))
    assert len(result["candidates"]) >= 3

    # The same loop run clockwise and counter-clockwise is one route, and
    # n copies of it is not a set of options.
    geometries = [frozenset(_candidate_points(c)) for c in result["candidates"]]
    assert len(set(geometries)) == len(geometries), "every returned candidate must be a different route"

    _build_ring_area(conn)
    ring = conn.execute("SELECT SUM(length_m) FROM net_edges WHERE area_id = 4").fetchone()[0]
    only_loop = route_search.find_routes(conn, _base_request(4, ring * 0.9, ring * 1.1, n=5, shape="loop", time_budget_s=5.0))
    assert len(only_loop["candidates"]) == 1


def test_a_start_on_an_existing_degree_two_junction_survives_contraction(conn: sqlite3.Connection):
    _build_ring_area(conn)
    perimeter = conn.execute("SELECT SUM(length_m) FROM net_edges WHERE area_id = 4").fetchone()[0]
    lat, lng = _interp(N_LL, E_LL, 1.0 / 6.0)  # exactly on the ring's first interior junction

    result = route_search.find_routes(
        conn,
        _base_request(4, perimeter * 0.9, perimeter * 1.1, starts=[{"lat": lat, "lng": lng}], n=3, shape="loop", time_budget_s=5.0),
    )

    assert result["candidates"]
    assert abs(result["candidates"][0]["distance_m"] - perimeter) < 1.0


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"n": 0}, "n must be"),
        ({"n": -1}, "n must be"),
        ({"time_budget_s": 0}, "time_budget_s"),
        ({"starts": []}, "start"),
    ],
    ids=["zero n", "negative n", "zero time budget", "no starts"],
)
def test_a_request_without_a_start_or_with_a_nonpositive_n_or_budget_is_rejected(conn: sqlite3.Connection, overrides: dict, message: str):
    _build_diamond_area(conn)
    with pytest.raises(ValueError, match=message):
        route_search.find_routes(conn, _base_request(1, 100.0, 200.0, **overrides))


def test_one_search_is_stored_as_one_request_row_with_the_real_request(conn: sqlite3.Connection):
    _build_diamond_area(conn)
    perimeter = _perimeter_length_m(conn, 1)
    result = route_search.find_routes(conn, _base_request(1, perimeter * 0.3, perimeter * 1.3, n=3, shape="any"))
    assert len(result["candidates"]) > 1

    rows = conn.execute("SELECT request_json FROM net_search_results").fetchall()
    assert len(rows) == 1
    stored = json.loads(rows[0]["request_json"])
    assert stored["distance_m_max"] == pytest.approx(perimeter * 1.3)
    assert stored["seed"] == result["seed"]
    request_ids = {r[0] for r in conn.execute("SELECT DISTINCT request_id FROM net_candidates").fetchall()}
    assert len(request_ids) == 1


@pytest.fixture
def empty_graph_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(route_search, "_graph_cache", route_search.OrderedDict())
    monkeypatch.setattr(route_search, "_graph_build_locks", {})
    monkeypatch.setattr(route_search, "_built_at", lambda conn, area_id: "v1")


def test_concurrent_searches_of_one_area_load_its_graph_once(empty_graph_cache, monkeypatch: pytest.MonkeyPatch):
    loads: list[int] = []

    def slow_load(conn, area_id):
        loads.append(area_id)
        time.sleep(0.05)
        return route_search.SearchGraph(), {}

    monkeypatch.setattr(route_search, "_load_graph", slow_load)
    results: list = []
    threads = [
        threading.Thread(target=lambda a=a: results.append(route_search._cached_base_graph(None, a)))
        for a in [1] * 6 + [2] * 6
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(loads) == [1, 2]
    assert len(results) == 12
    assert len({id(graph) for graph, _ in results}) == 2


def test_graph_cache_survives_threads_evicting_while_others_read(empty_graph_cache, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(route_search, "_load_graph", lambda conn, area_id: (route_search.SearchGraph(), {}))
    errors: list[Exception] = []

    def hammer(offset: int) -> None:
        try:
            for i in range(300):
                route_search._cached_base_graph(None, (offset + i) % 8)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(route_search._graph_cache) <= route_search._GRAPH_CACHE_MAX_AREAS


def test_same_seed_is_reproducible(conn: sqlite3.Connection):
    _build_diamond_area(conn)
    perimeter = _perimeter_length_m(conn, 1)

    request = _base_request(1, perimeter * 0.3, perimeter * 1.3, n=4, shape="any", time_budget_s=8.0, seed=42)
    first = route_search.find_routes(conn, request)
    second = route_search.find_routes(conn, request)

    first_summary = [(c["shape"], round(c["distance_m"], 2)) for c in first["candidates"]]
    second_summary = [(c["shape"], round(c["distance_m"], 2)) for c in second["candidates"]]
    assert first_summary == second_summary
    assert first["seed"] == second["seed"] == 42


def test_save_route_candidate_creates_a_route_and_an_unknown_candidate_raises(conn: sqlite3.Connection):
    _build_diamond_area(conn)
    perimeter = _perimeter_length_m(conn, 1)
    result = route_search.find_routes(conn, _base_request(1, perimeter * 0.5, perimeter * 1.5, n=1))
    assert result["candidates"]
    candidate_id = result["candidates"][0]["candidate_id"]

    saved = route_search.save_route_candidate(conn, candidate_id, "Diamond Loop")
    detail = routes_service.get_route(conn, saved["route_id"])
    assert detail["name"] == "Diamond Loop"
    assert detail["source"] == "generated"
    assert detail["distance_m"] is not None and detail["distance_m"] > 0

    with pytest.raises(route_search.CandidateNotFoundError):
        route_search.get_route_candidate(conn, "not-a-real-id")


# ---------------------------------------------------------------------------
# A dense street-grid network: every intersection has degree 4, so nothing
# contracts away and a long loop needs many short blocks -- the shape that
# exposed a per-attempt walk bounded by a fixed step count rather than a
# distance: any target requiring more steps than the cap allowed could never
# close, no matter how much time budget was left.
# ---------------------------------------------------------------------------


def _build_grid_area(conn: sqlite3.Connection, size: int, step_deg: float, area_id: int) -> None:
    node_ids = _IdAllocator(20_000)
    way_ids = _IdAllocator(20_000)
    nodes: list[dict] = []
    ways: list[dict] = []
    grid_ids: dict[tuple[int, int], int] = {}
    for i in range(size):
        for j in range(size):
            nid = node_ids()
            grid_ids[(i, j)] = nid
            nodes.append(node(nid, 40.0 + i * step_deg, -105.0 + j * step_deg))
    for i in range(size):
        for j in range(size):
            if i + 1 < size:
                ways.append(way(way_ids(), [grid_ids[(i, j)], grid_ids[(i + 1, j)]], {"highway": "residential"}))
            if j + 1 < size:
                ways.append(way(way_ids(), [grid_ids[(i, j)], grid_ids[(i, j + 1)]], {"highway": "residential"}))

    stats = network.build_network(conn, area_id, [{"elements": [*nodes, *ways]}], none_sampler())
    assert stats["edge_count"] == 2 * size * (size - 1)


def test_long_loop_succeeds_on_a_dense_grid_network(conn: sqlite3.Connection):
    size, step_deg = 40, 0.0009  # ~100m blocks, a ~4km x 4km grid
    _build_grid_area(conn, size=size, step_deg=step_deg, area_id=3)

    start = {"lat": 40.0 + (size // 2) * step_deg, "lng": -105.0 + (size // 2) * step_deg}
    request: route_search.RouteSearchRequest = {
        "area_id": 3,
        "starts": [start],
        "distance_m_min": 7000.0,
        "distance_m_max": 9000.0,
        "shape": "loop",
        "n": 3,
        "time_budget_s": 10.0,
        "seed": 99,
    }
    result = route_search.find_routes(conn, request)

    assert not result["budget_exceeded"]
    assert not result["near_miss"]
    assert result["candidates"]
    for c in result["candidates"]:
        assert request["distance_m_min"] <= c["distance_m"] <= request["distance_m_max"]


def _insert_area_row(conn: sqlite3.Connection, status: str) -> int:
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO areas (area_id, label, center_lat, center_lng, radius_km, pinned, status, created_at, last_used_at) "
        "VALUES (1, 'a', 40.0, -105.0, 5, 0, ?, ?, ?)",
        (status, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


def test_search_refuses_a_graph_built_by_an_older_network_version(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    area_id = _insert_area_row(conn, "ready")
    conn.execute(
        "INSERT INTO net_graphs (area_id, derive_version, built_at, node_count, edge_count, excluded_way_count) "
        "VALUES (?, ?, 'now', 0, 0, 0)",
        (area_id, network.NETWORK_DERIVE_VERSION - 1),
    )
    conn.commit()
    started: list[int] = []
    monkeypatch.setattr(areas.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(areas, "_run_finish_only_job", started.append)

    with pytest.raises(route_search.AreaRebuildingError):
        route_search.find_routes(conn, _base_request(area_id, 1000.0, 2000.0))
    assert started == [area_id]


@pytest.mark.parametrize("status", ["importing", "error", "new"])
def test_search_refuses_a_first_import_that_has_no_graph_yet_and_uses_a_committed_graph_during_a_refresh(conn: sqlite3.Connection, status: str):
    _insert_area_row(conn, status)

    with pytest.raises(route_search.AreaNotReadyError, match="not ready"):
        route_search.find_routes(conn, _base_request(1, 1000.0, 2000.0))

    _build_diamond_area(conn)
    assert route_search.find_routes(conn, _base_request(1, 1000.0, 20000.0))["candidates"]


def test_a_search_from_a_set_back_trailhead_does_not_flag_its_access_spur_as_missing_elevation(conn: sqlite3.Connection):
    nodes, ways = _ring_elements(_IdAllocator(100), _IdAllocator(0))
    lot_lat, lot_lng = _interp(N_LL, E_LL, 0.5)
    lot = (lot_lat + 0.0004, lot_lng)
    nodes.append(node(9000, *lot, tags={"amenity": "parking"}))
    network.build_network(conn, 6, [{"elements": [*nodes, *ways]}], climbing_sampler(2.0))
    assert conn.execute("SELECT * FROM net_edges WHERE area_id = 6 AND way_id < 0").fetchone() is not None

    request = _base_request(6, 1000.0, 20000.0, starts=[{"lat": lot[0], "lng": lot[1]}], gain_m_min=0.0)
    result = route_search.find_routes(conn, request)

    assert not result["near_miss"]
    assert result["candidates"]
    for candidate in result["candidates"]:
        assert not candidate["missed_constraints"]
        assert candidate["score"]["has_elevation"] is True


def test_budget_exceeded_is_reported_only_when_fewer_than_n_qualified(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _build_diamond_area(conn)
    perimeter = _perimeter_length_m(conn, 1)
    clock = itertools.chain([0.0, 0.0], itertools.repeat(100.0))
    monkeypatch.setattr(route_search.time, "monotonic", lambda: next(clock))

    # Deadline hit before anything was generated: fewer than n qualify.
    result = route_search.find_routes(conn, _base_request(1, perimeter * 0.9, perimeter * 1.1, n=3, time_budget_s=1.0))
    assert result["budget_exceeded"] is True

    # Running out the budget with n candidates in hand is not an overrun.
    ticks = itertools.count()
    monkeypatch.setattr(route_search.time, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr(route_search, "STAGNATION_ATTEMPTS", 10**6)
    result = route_search.find_routes(conn, _base_request(1, perimeter * 0.9, perimeter * 1.1, n=1, time_budget_s=60.0))
    assert result["candidates"] and not result["near_miss"]
    assert result["budget_exceeded"] is False


