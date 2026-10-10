"""Route search over an area's network graph
(docs/design-terrain-and-route-search.md, section 4): bound the graph around
each start, contract degree-2 chains, generate randomized candidates per
shape, score each through network.score_path, and diversify.

A search never writes to the network graph: a start point is spliced into an
in-memory copy. Candidates are kept behind a TTL until saved
(save_route_candidate) into the routes table.
"""

import hashlib
import heapq
import itertools
import json
import random
import sqlite3
import threading
import time
import uuid
from collections import ChainMap, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Collection, Iterable, Literal, Mapping, MutableMapping, Sequence, cast
from typing_extensions import NotRequired, TypedDict

from . import areas, drive_starts, network, routes, routes_spatial, terrain

Shape = Literal["loop", "out_and_back", "lollipop", "any"]

DEFAULT_N = 5
DEFAULT_TIME_BUDGET_S = 10.0
CANDIDATE_TTL_HOURS = 24.0

# How far a request's start may snap onto an edge; wider than
# AMENITY_SNAP_RADIUS_M because an athlete-given point is less precise.
START_SNAP_RADIUS_M = 500.0

BEAM_WIDTH = 4
# Step-count backstop against pathological wandering; edge length varies too
# much between street grids and trail networks to bound steps tighter.
LOOP_STEP_CEILING = 600
# A loop walk gives up after covering this multiple of the target distance.
LOOP_MAX_OVERRUN = 1.6
# A walk turns home once the distance left to the target is within this
# multiple of the shortest path back.
TURN_HOME_SLACK = 1.15
# Gate on the exact distance-home check: skipped when the straight-line
# distance to the start, inflated by this factor, already rules a turn out.
# Must stay loose enough not to skip the step where turning home becomes
# possible.
HOME_ESTIMATE_INFLATION = 1.75
# Fraction of the target distance an out-and-back turnaround or a lollipop's
# stem may land within and still be accepted.
TURNAROUND_TOLERANCE = 0.25
LOLLIPOP_STEM_FRACTION = 0.25
MAX_GENERATION_ATTEMPTS = 300
# Candidates to generate per requested result before diversifying down.
POOL_FACTOR = 3
# Consecutive attempts adding no new qualifying candidate (once n qualify)
# after which a small network is judged exhausted.
STAGNATION_ATTEMPTS = 40
DIVERSITY_WEIGHT = 2.0


class Start(TypedDict):
    lat: float
    lng: float
    label: NotRequired[str | None]
    drive_s: NotRequired[float | None]
    drive_time_estimated: NotRequired[bool]


class RouteSearchRequest(TypedDict):
    area_id: int
    starts: list[Start]
    distance_m_min: float
    distance_m_max: float
    shape: NotRequired[Shape]
    min_trail_m: NotRequired[float | None]
    min_runnable_pct: NotRequired[float | None]
    gain_m_min: NotRequired[float | None]
    gain_m_max: NotRequired[float | None]
    include_way_ids: NotRequired[list[int] | None]
    avoid_way_ids: NotRequired[list[int] | None]
    n: NotRequired[int]
    time_budget_s: NotRequired[float]
    seed: NotRequired[int | None]


class ConstraintMiss(TypedDict):
    constraint: str
    detail: str


class RouteCandidateOut(TypedDict):
    candidate_id: str
    area_id: int
    shape: str
    start: Start
    distance_m: float
    score: dict
    missed_constraints: list[ConstraintMiss]
    geometry: list[list[float | None]]  # [lat, lng, ele_m|None] triples


class RouteSearchResult(TypedDict):
    candidates: list[RouteCandidateOut]
    near_miss: bool
    budget_exceeded: bool
    seed: int


class CandidateNotFoundError(LookupError):
    pass


class AreaNotReadyError(ValueError):
    """The area is importing or its last import failed, so its graph may be
    absent, partial, or about to be replaced."""


class AreaRebuildingError(ValueError):
    """The area's network predates the current network.NETWORK_DERIVE_VERSION and is
    being rebuilt from its cached tiles; searching it now would use a stale graph."""


# ---------------------------------------------------------------------------
# Schema / connection
# ---------------------------------------------------------------------------


def init_search_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_search_results (
            request_id   TEXT PRIMARY KEY,
            area_id      INTEGER NOT NULL,
            created_at   TEXT NOT NULL,
            expires_at   TEXT NOT NULL,
            request_json TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_candidates (
            candidate_id TEXT PRIMARY KEY,
            request_id   TEXT NOT NULL REFERENCES net_search_results(request_id) ON DELETE CASCADE,
            area_id      INTEGER NOT NULL,
            shape        TEXT NOT NULL,
            start_json   TEXT NOT NULL,
            distance_m   REAL NOT NULL,
            score_json   TEXT NOT NULL,
            missed_json  TEXT NOT NULL,
            geometry_json TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_net_candidates_request ON net_candidates(request_id)")


def _purge_expired(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM net_candidates WHERE expires_at < ?", [now])
    conn.execute("DELETE FROM net_search_results WHERE expires_at < ?", [now])
    conn.commit()


# ---------------------------------------------------------------------------
# In-memory search graph
# ---------------------------------------------------------------------------


@dataclass
class NodeInfo:
    lat: float
    lng: float
    kind: str


@dataclass
class GraphEdge:
    """One traversable connection between two search-graph nodes.

    edge_key is a net_edges id for an untouched edge, or a negative synthetic
    id for a start-split piece or a degree-2-chain contraction (contractions
    are never scored directly). `segments` lists the real (edge_id, forward)
    steps for walking from `a` to `b`; forward says whether a step reads its
    points_blob in stored order, independent of this edge's a/b labels.
    """
    edge_key: int
    a: int
    b: int
    length_m: float
    allowed: Literal["both", "a_to_b", "b_to_a"]
    segments: list[tuple[int, bool]]

    def traverse(self, frm: int) -> tuple[int, list[tuple[int, bool]]] | None:
        if frm == self.a:
            if self.allowed == "b_to_a":
                return None
            return self.b, self.segments
        if frm == self.b:
            if self.allowed == "a_to_b":
                return None
            return self.a, [(eid, not fwd) for eid, fwd in reversed(self.segments)]
        raise ValueError(f"edge {self.edge_key} doesn't touch node {frm}")


@dataclass
class SearchGraph:
    nodes: dict[int, NodeInfo] = field(default_factory=dict)
    adjacency: dict[int, list[GraphEdge]] = field(default_factory=dict)
    edges_by_key: dict[int, GraphEdge] = field(default_factory=dict)

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges_by_key[edge.edge_key] = edge
        self.adjacency.setdefault(edge.a, []).append(edge)
        self.adjacency.setdefault(edge.b, []).append(edge)

    def remove_edge(self, edge: GraphEdge) -> None:
        del self.edges_by_key[edge.edge_key]
        self.adjacency[edge.a] = [e for e in self.adjacency.get(edge.a, []) if e is not edge]
        self.adjacency[edge.b] = [e for e in self.adjacency.get(edge.b, []) if e is not edge]


_ALLOWED_BY_ONEWAY: dict[str, Literal["both", "a_to_b", "b_to_a"]] = {
    "no": "both", "yes": "a_to_b", "-1": "b_to_a",
}


def _load_graph(conn: sqlite3.Connection, area_id: int) -> tuple[SearchGraph, dict[int, dict]]:
    """The area's full graph plus edge_rows, a lookup for geometry and way ids
    and for score_path's edges that are not in the database."""
    graph = SearchGraph()
    edge_rows: dict[int, dict] = {}
    for row in conn.execute("SELECT * FROM net_nodes WHERE area_id = ?", [area_id]).fetchall():
        graph.nodes[row["node_id"]] = NodeInfo(row["lat"], row["lng"], row["kind"])
    for row in conn.execute("SELECT * FROM net_edges WHERE area_id = ?", [area_id]).fetchall():
        edge_id = row["edge_id"]
        edge_rows[edge_id] = dict(row)
        allowed = _ALLOWED_BY_ONEWAY.get(row["oneway"], "both")
        graph.add_edge(GraphEdge(edge_id, row["from_node_id"], row["to_node_id"], row["length_m"], allowed, [(edge_id, True)]))
    return graph, edge_rows


# Loading an area's graph dwarfs the cost of searching it, so graphs are cached
# per area, keyed on net_graphs.built_at so a rebuild invalidates the entry.
_graph_cache: OrderedDict[int, tuple[str | None, SearchGraph, dict[int, dict]]] = OrderedDict()
# Each entry is a whole area's graph in memory.
_GRAPH_CACHE_MAX_AREAS = 3


def _built_at(conn: sqlite3.Connection, area_id: int) -> str | None:
    row = conn.execute("SELECT built_at FROM net_graphs WHERE area_id = ?", [area_id]).fetchone()
    return row["built_at"] if row is not None else None


# Searches run on worker threads. The first lock guards the cache and the build
# locks and is never held during a load; a build lock lets concurrent searches
# of one area share a single load.
_graph_cache_lock = threading.Lock()
_graph_build_locks: dict[int, threading.Lock] = {}


def _cache_hit(area_id: int, built_at: str | None) -> tuple[SearchGraph, dict[int, dict]] | None:
    with _graph_cache_lock:
        cached = _graph_cache.get(area_id)
        if cached is None or cached[0] != built_at:
            return None
        _graph_cache.move_to_end(area_id)
        return cached[1], cached[2]


def _cached_base_graph(conn: sqlite3.Connection, area_id: int) -> tuple[SearchGraph, dict[int, dict]]:
    built_at = _built_at(conn, area_id)
    hit = _cache_hit(area_id, built_at)
    if hit is not None:
        return hit
    with _graph_cache_lock:
        build_lock = _graph_build_locks.setdefault(area_id, threading.Lock())
    with build_lock:
        hit = _cache_hit(area_id, built_at)
        if hit is not None:
            return hit
        graph, edge_rows = _load_graph(conn, area_id)
        with _graph_cache_lock:
            _graph_cache[area_id] = (built_at, graph, edge_rows)
            _graph_cache.move_to_end(area_id)
            while len(_graph_cache) > _GRAPH_CACHE_MAX_AREAS:
                evicted, _ = _graph_cache.popitem(last=False)
                _graph_build_locks.pop(evicted, None)
    return graph, edge_rows


def _copy_graph(graph: SearchGraph) -> SearchGraph:
    """A per-request working copy. GraphEdge/NodeInfo are never mutated once
    built, so only the indexes are copied."""
    copy = SearchGraph()
    copy.nodes = dict(graph.nodes)
    copy.adjacency = {node: list(edges) for node, edges in graph.adjacency.items()}
    copy.edges_by_key = dict(graph.edges_by_key)
    return copy


def _remove_ways(graph: SearchGraph, edge_rows: Mapping[int, dict], way_ids: set[int]) -> None:
    """Drop every edge belonging to one of way_ids."""
    if not way_ids:
        return
    for edge in list(graph.edges_by_key.values()):
        row = edge_rows.get(edge.edge_key)
        if row is not None and row["way_id"] in way_ids:
            graph.remove_edge(edge)


def _bound_graph(graph: SearchGraph, centers: Sequence[tuple[float, float]], radius_m: float) -> None:
    """Drop every node farther than radius_m from all centers, and any edge that
    loses an endpoint. Straight-line distance stands in for graph distance,
    which isn't known until the graph is built."""
    def _keep(node: NodeInfo) -> bool:
        return any(routes_spatial.equirect_distance_m(node.lat, node.lng, clat, clng) <= radius_m for clat, clng in centers)

    dropped = {n for n, info in graph.nodes.items() if not _keep(info)}
    for edge in list(graph.edges_by_key.values()):
        if edge.a in dropped or edge.b in dropped:
            graph.remove_edge(edge)
    for n in dropped:
        graph.nodes.pop(n, None)
        graph.adjacency.pop(n, None)


# ---------------------------------------------------------------------------
# Splicing a search's own start point into the graph
# ---------------------------------------------------------------------------


def _edge_index(edge_rows: Mapping[int, dict], edge_ids: Iterable[int]) -> routes_spatial.NearestIndex:
    """The sample points of the given edges, keyed by edge id."""
    index = routes_spatial.NearestIndex(cell_size_m=50.0)
    for eid in edge_ids:
        network.index_edge(index, eid, edge_rows[eid]["points_blob"])
    return index


def _splice_start(
    graph: SearchGraph, edge_rows: MutableMapping[int, dict], index: routes_spatial.NearestIndex,
    lat: float, lng: float, ids: Iterable[int],
) -> int | None:
    """Snap (lat, lng) onto the graph's nearest edge and cut it there, in memory
    only. index covers the graph's current edges and is updated with each cut.
    Returns the start's node id, or None when nothing is within
    START_SNAP_RADIUS_M.
    """
    cut = network.splice_at(edge_rows, index, lat, lng, START_SNAP_RADIUS_M, ids)
    if cut is None:
        return None
    edge = graph.edges_by_key[cut.edge_key]
    split = cut.split
    if split is None:
        return edge.a if cut.offset_m < cut.row["length_m"] / 2.0 else edge.b

    node_id = next(iter(ids))
    graph.nodes[node_id] = NodeInfo(split["node_lat"], split["node_lng"], "start")
    graph.remove_edge(edge)
    graph.add_edge(
        GraphEdge(cut.before_key, edge.a, node_id, split["before"]["length_m"], edge.allowed, [(cut.before_key, True)])
    )
    graph.add_edge(
        GraphEdge(cut.after_key, node_id, edge.b, split["after"]["length_m"], edge.allowed, [(cut.after_key, True)])
    )
    return node_id


# ---------------------------------------------------------------------------
# Degree-2 chain contraction
# ---------------------------------------------------------------------------


def contract_degree2_chains(graph: SearchGraph, ids: Iterable[int], protected: Collection[int] = ()) -> None:
    """Collapse junction nodes with exactly two incident edges into one edge.
    Amenity and start nodes are never touched, and `protected` lists further
    nodes that must survive (a start snapped onto an existing junction)."""
    keep = set(protected)
    queue: list[int] = [
        n for n, edges in graph.adjacency.items()
        if graph.nodes[n].kind == "junction" and len(edges) == 2 and n not in keep
    ]
    queued = set(queue)
    while queue:
        node = queue.pop()
        queued.discard(node)
        edges = graph.adjacency.get(node, [])
        if node not in graph.nodes or graph.nodes[node].kind != "junction" or len(edges) != 2 or node in keep:
            continue
        e1, e2 = edges
        if e1.allowed != "both" or e2.allowed != "both":
            continue
        step1 = e1.traverse(node)
        step2 = e2.traverse(node)
        assert step1 is not None and step2 is not None
        n1, seg1 = step1
        n2, seg2 = step2
        if n1 == n2:
            continue  # a dangling loop back to the same neighbor; leave it
        merged_segments = [(eid, not fwd) for eid, fwd in reversed(seg1)] + seg2
        merged = GraphEdge(next(iter(ids)), n1, n2, e1.length_m + e2.length_m, "both", merged_segments)
        graph.remove_edge(e1)
        graph.remove_edge(e2)
        graph.adjacency.pop(node, None)
        graph.add_edge(merged)
        for neighbor in (n1, n2):
            if (
                neighbor in graph.nodes and graph.nodes[neighbor].kind == "junction" and neighbor not in keep
                and len(graph.adjacency.get(neighbor, [])) == 2 and neighbor not in queued
            ):
                queue.append(neighbor)
                queued.add(neighbor)


# ---------------------------------------------------------------------------
# Shortest paths
# ---------------------------------------------------------------------------


def _dijkstra(
    graph: SearchGraph, source: int, target: int | None = None, max_dist: float | None = None
) -> tuple[dict[int, float], dict[int, GraphEdge], dict[int, int]]:
    """max_dist prunes the search once the frontier passes it (safe: pops come
    in non-decreasing distance)."""
    dist: dict[int, float] = {source: 0.0}
    prev_edge: dict[int, GraphEdge] = {}
    prev_node: dict[int, int] = {}
    visited: set[int] = set()
    heap: list[tuple[float, int]] = [(0.0, source)]
    while heap:
        d, node = heapq.heappop(heap)
        if node in visited:
            continue
        if max_dist is not None and d > max_dist:
            break
        visited.add(node)
        if target is not None and node == target:
            break
        for edge in graph.adjacency.get(node, []):
            step = edge.traverse(node)
            if step is None:
                continue
            nxt, _segs = step
            nd = d + edge.length_m
            if nxt not in dist or nd < dist[nxt]:
                dist[nxt] = nd
                prev_edge[nxt] = edge
                prev_node[nxt] = node
                heapq.heappush(heap, (nd, nxt))
    return dist, prev_edge, prev_node


def _reconstruct(target: int, prev_edge: dict[int, GraphEdge], prev_node: dict[int, int]) -> list[tuple[GraphEdge, int, int]]:
    """Steps (edge, from_node, to_node) for the path a _dijkstra(graph,
    source) call found ending at target, in travel order (source first)."""
    steps: list[tuple[GraphEdge, int, int]] = []
    node = target
    while node in prev_edge:
        edge = prev_edge[node]
        frm = prev_node[node]
        steps.append((edge, frm, node))
        node = frm
    steps.reverse()
    return steps


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

Traversal = list[tuple[GraphEdge, int]]  # (edge, from_node) in travel order

ShortestPathTree = tuple[dict[int, float], dict[int, GraphEdge], dict[int, int]]


@dataclass
class _SearchContext:
    """Per-search state shared across attempts. The shortest-path tree depends
    only on (start_node, outbound target), both fixed for the search, so it is
    memoized."""
    graph: SearchGraph
    _trees: dict[tuple[int, float], ShortestPathTree] = field(default_factory=dict)

    def tree(self, start_node: int, outbound_target: float) -> ShortestPathTree:
        """The shortest-path tree from start_node, pruned to what a turnaround
        near outbound_target can need."""
        key = (start_node, outbound_target)
        if key not in self._trees:
            self._trees[key] = _dijkstra(self.graph, start_node, max_dist=outbound_target * (1.0 + TURNAROUND_TOLERANCE))
        return self._trees[key]


def _generate_loop(ctx: _SearchContext, start_node: int, target_m: float, rng: random.Random) -> Traversal | None:
    graph = ctx.graph
    start_info = graph.nodes[start_node]
    current = start_node
    dist_so_far = 0.0
    traversal: Traversal = []
    steps = 0
    while steps < LOOP_STEP_CEILING and dist_so_far <= target_m * LOOP_MAX_OVERRUN:
        steps += 1
        options = [e for e in graph.adjacency.get(current, []) if e.traverse(current) is not None]
        if traversal:
            last_key = traversal[-1][0].edge_key
            non_backtrack = [e for e in options if e.edge_key != last_key]
            if non_backtrack:
                options = non_backtrack
        if not options:
            return None
        rng.shuffle(options)
        chosen = rng.choice(options[:BEAM_WIDTH])
        step = chosen.traverse(current)
        assert step is not None
        nxt, _segs = step
        traversal.append((chosen, current))
        dist_so_far += chosen.length_m
        current = nxt
        if current == start_node:
            return traversal
        remaining = target_m - dist_so_far
        current_info = graph.nodes[current]
        straight_line_m = routes_spatial.equirect_distance_m(current_info.lat, current_info.lng, start_info.lat, start_info.lng)
        if remaining > straight_line_m * HOME_ESTIMATE_INFLATION:
            continue  # not plausibly close enough yet to be worth an exact check
        dist_map, prev_edge, prev_node = _dijkstra(graph, current, target=start_node)
        home_dist = dist_map.get(start_node)
        if home_dist is None:
            continue
        if remaining <= home_dist * TURN_HOME_SLACK:
            traversal.extend(_forward(_reconstruct(start_node, prev_edge, prev_node)))
            return traversal
    return None


def _pick_turnaround(dist_map: dict[int, float], start_node: int, target_dist: float, rng: random.Random) -> int | None:
    tolerance = target_dist * TURNAROUND_TOLERANCE
    candidates = [n for n, d in dist_map.items() if n != start_node and abs(d - target_dist) <= tolerance]
    if candidates:
        return rng.choice(candidates)
    reachable = [n for n in dist_map if n != start_node]
    if not reachable:
        return None
    return min(reachable, key=lambda n: abs(dist_map[n] - target_dist))


Steps = list[tuple[GraphEdge, int, int]]  # (edge, from_node, to_node) in travel order


def _forward(steps: Steps) -> Traversal:
    return [(edge, frm) for edge, frm, _to in steps]


def _retrace(steps: Steps) -> Traversal | None:
    """The way back along steps, or None when a one-way stretch can't be
    walked in reverse."""
    back: Traversal = []
    for edge, _frm, to in reversed(steps):
        if edge.traverse(to) is None:
            return None
        back.append((edge, to))
    return back


def _outbound_leg(
    ctx: _SearchContext, start_node: int, outbound_target: float, rng: random.Random
) -> tuple[Steps, float] | None:
    """The steps to a turnaround node near outbound_target, and its distance."""
    dist_map, prev_edge, prev_node = ctx.tree(start_node, outbound_target)
    turnaround = _pick_turnaround(dist_map, start_node, outbound_target, rng)
    if turnaround is None:
        return None
    return _reconstruct(turnaround, prev_edge, prev_node), dist_map[turnaround]


def _generate_out_and_back(ctx: _SearchContext, start_node: int, target_m: float, rng: random.Random) -> Traversal | None:
    leg = _outbound_leg(ctx, start_node, target_m / 2.0, rng)
    if leg is None:
        return None
    out_steps, _dist = leg
    back = _retrace(out_steps)
    return None if back is None else _forward(out_steps) + back


def _generate_lollipop(ctx: _SearchContext, start_node: int, target_m: float, rng: random.Random) -> Traversal | None:
    leg = _outbound_leg(ctx, start_node, target_m * LOLLIPOP_STEM_FRACTION, rng)
    if leg is None:
        return None
    stem_steps, stem_dist = leg
    loop_steps = _generate_loop(ctx, stem_steps[-1][2], max(target_m - 2 * stem_dist, 0.0), rng)
    if loop_steps is None:
        return None
    back = _retrace(stem_steps)
    return None if back is None else _forward(stem_steps) + loop_steps + back


_GENERATORS = {"loop": _generate_loop, "out_and_back": _generate_out_and_back, "lollipop": _generate_lollipop}


def _flatten(traversal: Traversal) -> list[tuple[int, bool]]:
    segments: list[tuple[int, bool]] = []
    for edge, frm in traversal:
        step = edge.traverse(frm)
        assert step is not None
        segments.extend(step[1])
    return segments


def _geometry(edge_rows: Mapping[int, dict], segments: Sequence[tuple[int, bool]]) -> list[tuple[float, float, float | None]]:
    points: list[tuple[float, float, float | None]] = []
    for edge_id, forward in segments:
        row = edge_rows.get(edge_id)
        if row is None:
            continue
        pts = network.unpack_edge_points(bytes(row["points_blob"]))
        seq = pts if forward else list(reversed(pts))
        for lat, lng, _d, ele in seq:
            if points and points[-1][0] == lat and points[-1][1] == lng:
                continue
            points.append((lat, lng, ele))
    return points


# ---------------------------------------------------------------------------
# Constraint checking
# ---------------------------------------------------------------------------


def _runnable_pct(score: dict) -> float:
    """"Runnable" for filtering purposes counts both comfortably-runnable
    and grind (runnable but slow) distance -- only "hike" is excluded."""
    return cast(float, score["runnability"]["run_pct"]) + cast(float, score["runnability"]["grind_pct"])


def _soft_misses(score: dict, request: RouteSearchRequest, touched_way_ids: set[int]) -> list[ConstraintMiss]:
    misses: list[ConstraintMiss] = []
    dmin, dmax = request["distance_m_min"], request["distance_m_max"]
    distance_m = cast(float, score["distance_m"])
    if not (dmin <= distance_m <= dmax):
        misses.append({"constraint": "distance_range", "detail": f"{distance_m:.0f}m outside [{dmin:.0f}, {dmax:.0f}]"})

    min_trail = request.get("min_trail_m")
    if min_trail is not None and cast(float, score["trail_distance_m"]) < min_trail:
        misses.append({"constraint": "min_trail_m", "detail": f"{score['trail_distance_m']:.0f}m trail, need {min_trail:.0f}m"})

    min_runnable = request.get("min_runnable_pct")
    if min_runnable is not None:
        runnable_pct = _runnable_pct(score)
        if runnable_pct < min_runnable:
            misses.append({"constraint": "min_runnable_pct", "detail": f"{runnable_pct:.0f}% runnable, need {min_runnable:.0f}%"})

    gain_min, gain_max = request.get("gain_m_min"), request.get("gain_m_max")
    if gain_min is not None or gain_max is not None:
        if score["gain_m"] is None:
            misses.append({"constraint": "gain_range", "detail": "no elevation data"})
        elif not score["has_elevation"]:
            misses.append({
                "constraint": "gain_range",
                "detail": f"elevation covers only {score['elevation_coverage_pct']:.0f}% of the route",
            })
        else:
            gain_m = cast(float, score["gain_m"])
            if gain_min is not None and gain_m < gain_min:
                misses.append({"constraint": "gain_range", "detail": f"{gain_m:.0f}m gain, need >= {gain_min:.0f}m"})
            if gain_max is not None and gain_m > gain_max:
                misses.append({"constraint": "gain_range", "detail": f"{gain_m:.0f}m gain, need <= {gain_max:.0f}m"})

    include = request.get("include_way_ids")
    if include:
        missing = set(include) - touched_way_ids
        if missing:
            misses.append({"constraint": "include_way_ids", "detail": f"missing way ids {sorted(missing)}"})
    return misses


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    shape: str
    start: Start
    segments: list[tuple[int, bool]]
    score: dict
    missed: list[ConstraintMiss]


def _quality(candidate: _Candidate, target_m: float) -> float:
    closeness = 1.0 - min(abs(cast(float, candidate.score["distance_m"]) - target_m) / max(target_m, 1.0), 1.0)
    return closeness + _runnable_pct(candidate.score) / 100.0


def _diversify(candidates: list[_Candidate], edge_rows: Mapping[int, dict], target_m: float, n: int) -> list[_Candidate]:
    remaining = list(candidates)
    selected: list[_Candidate] = []
    while remaining and len(selected) < n:
        def penalized(c: _Candidate) -> float:
            if not selected:
                return _quality(c, target_m)
            c_ids = {eid for eid, _ in c.segments}
            shared_m = 0.0
            for s in selected:
                s_ids = {eid for eid, _ in s.segments}
                shared_m += sum(edge_rows.get(eid, {}).get("length_m", 0.0) for eid in c_ids & s_ids)
            fraction = min(shared_m / max(cast(float, c.score["distance_m"]), 1.0), 1.0)
            return _quality(c, target_m) - DIVERSITY_WEIGHT * fraction

        best = max(remaining, key=penalized)
        selected.append(best)
        remaining.remove(best)
    return selected


def _default_seed(request: RouteSearchRequest) -> int:
    """A request with no explicit seed still reproduces: a hash of its
    parameters (Python's string hash is randomized per process)."""
    canonical = json.dumps(request, sort_keys=True, default=str)
    return int.from_bytes(hashlib.sha256(canonical.encode()).digest()[:8], "big")


def resolve_starts(
    conn: sqlite3.Connection,
    area_id: int,
    *,
    starts: Sequence[Start] | None,
    origin: tuple[float, float] | None,
    max_drive_minutes: float | None,
    drive_k: int,
    origin_field: str = "start_lat/start_lng",
) -> list[Start]:
    """An explicit starts list, a single start at origin, or (with
    max_drive_minutes) the drive-to-trailhead set around origin, which
    includes origin itself. origin_field names the caller's origin parameter
    in error messages."""
    if max_drive_minutes is not None:
        if starts:
            raise ValueError("max_drive_minutes and starts are mutually exclusive")
        if origin is None:
            raise ValueError(f"max_drive_minutes needs {origin_field} as the drive origin")
        return cast(list[Start], drive_starts.drive_starts(conn, area_id, origin[0], origin[1], max_drive_minutes, k=drive_k))
    if starts:
        return list(starts)
    if origin is not None:
        return [{"lat": origin[0], "lng": origin[1]}]
    raise ValueError(f"find_routes needs {origin_field} or a starts list")


def find_routes(conn: sqlite3.Connection, request: RouteSearchRequest) -> RouteSearchResult:
    starts_in = request["starts"]
    if not starts_in:
        raise ValueError("find_routes needs at least one start")
    # Absent, None, and empty-list parameters are the same request, seed included.
    request = cast(RouteSearchRequest, {k: v for k, v in request.items() if v not in (None, [])})
    if request["distance_m_min"] > request["distance_m_max"]:
        raise ValueError("distance_m_min must be <= distance_m_max")

    area_id = request["area_id"]
    shape = request.get("shape", "any")
    n = request.get("n", DEFAULT_N)
    if n < 1:
        raise ValueError("n must be at least 1")
    time_budget_s = request.get("time_budget_s", DEFAULT_TIME_BUDGET_S)
    if time_budget_s <= 0:
        raise ValueError("time_budget_s must be positive")
    seed = request.get("seed")
    if seed is None:
        seed = _default_seed(request)
    rng = random.Random(seed)

    area_status = conn.execute("SELECT status FROM areas WHERE area_id = ?", [area_id]).fetchone()
    if area_status is not None and area_status["status"] != "ready":
        # A failed or interrupted refresh leaves the committed graph searchable.
        has_graph = conn.execute("SELECT 1 FROM net_graphs WHERE area_id = ?", [area_id]).fetchone()
        if has_graph is None:
            raise AreaNotReadyError(
                f"area {area_id} is not ready (status: {area_status['status']}); "
                "check get_area_status and search again when it is ready"
            )

    if areas.rebuild_stale_network(conn, area_id):
        raise AreaRebuildingError(
            f"area {area_id} is being rebuilt for a newer network model; "
            "check get_area_status and search again when it is ready"
        )

    prefs = network.get_route_preferences(conn)
    model = terrain.current_model(conn)
    runnability = model.score_profile

    base_graph, cached_edge_rows = _cached_base_graph(conn, area_id)
    graph = _copy_graph(base_graph)
    # Avoided ways leave the graph entirely, so no candidate can touch one.
    avoid_way_ids = set(request.get("avoid_way_ids") or []) | network.avoided_way_ids(conn)
    _remove_ways(graph, cached_edge_rows, avoid_way_ids)
    # Layered so splicing's synthetic rows stay request-local, off the shared cache.
    edge_rows: MutableMapping[int, dict] = ChainMap({}, cached_edge_rows)
    target_m = (request["distance_m_min"] + request["distance_m_max"]) / 2.0
    radius_m = request["distance_m_max"] / 2.0
    _bound_graph(graph, [(s["lat"], s["lng"]) for s in starts_in], radius_m)

    ids = itertools.count(-1, -1)
    start_index = _edge_index(edge_rows, graph.edges_by_key)
    start_nodes: list[tuple[int, Start]] = []
    for s in starts_in:
        node_id = _splice_start(graph, edge_rows, start_index, s["lat"], s["lng"], ids)
        if node_id is not None:
            start_nodes.append((node_id, s))
    if not start_nodes:
        raise ValueError("no network found near any given start")

    contract_degree2_chains(graph, ids, protected=[node_id for node_id, _ in start_nodes])
    ctx = _SearchContext(graph=graph)

    # Handed to score_path as extra_edges so it never re-queries net_edges.
    extra_edges = edge_rows
    shapes_to_try: list[str] = [shape] if shape != "any" else ["loop", "out_and_back", "lollipop"]

    deadline = time.monotonic() + time_budget_s
    qualifying: dict[frozenset[int], _Candidate] = {}
    near_misses: dict[frozenset[int], _Candidate] = {}
    budget_exceeded = False
    attempts = 0
    # Generate well past n so diversification has a pool to choose from.
    pool_target = n * POOL_FACTOR
    attempts_since_new = 0
    while len(qualifying) < pool_target and attempts < MAX_GENERATION_ATTEMPTS:
        if len(qualifying) >= n and attempts_since_new >= STAGNATION_ATTEMPTS:
            break
        if time.monotonic() > deadline:
            budget_exceeded = len(qualifying) < n
            break
        attempts += 1
        attempts_since_new += 1
        gen_shape = rng.choice(shapes_to_try)
        start_node, start_info = rng.choice(start_nodes)
        traversal = _GENERATORS[gen_shape](ctx, start_node, target_m, rng)
        if not traversal:
            continue
        segments = _flatten(traversal)
        if not segments:
            continue
        try:
            score = cast(dict, network.score_path(conn, area_id, segments, prefs, runnability, extra_edges=extra_edges))
        except ValueError:
            continue
        if score["road_exposure"]["violations"]:
            continue
        touched_way_ids = {edge_rows[eid]["way_id"] for eid, _ in segments if eid in edge_rows}
        misses = _soft_misses(score, request, touched_way_ids)
        candidate = _Candidate(gen_shape, start_info, segments, score, misses)
        # The same set of edges walked in either direction is one route.
        identity = frozenset(eid for eid, _ in segments)
        pool = near_misses if misses else qualifying
        if identity not in pool:
            pool[identity] = candidate
            if not misses:
                attempts_since_new = 0

    if qualifying:
        selected = _diversify(list(qualifying.values()), edge_rows, target_m, n)
        near_miss = False
    else:
        ranked = sorted(near_misses.values(), key=lambda c: (len(c.missed), -_quality(c, target_m)))
        selected = ranked[:n]
        near_miss = True

    request_id = _persist_request(conn, area_id, request, seed)
    result_candidates = [_persist_candidate(conn, area_id, request_id, edge_rows, c) for c in selected]
    conn.commit()
    return {
        "candidates": result_candidates,
        "near_miss": near_miss,
        "budget_exceeded": budget_exceeded,
        "seed": seed,
    }


def _persist_request(conn: sqlite3.Connection, area_id: int, request: RouteSearchRequest, seed: int) -> str:
    now = datetime.now(timezone.utc)
    request_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO net_search_results (request_id, area_id, created_at, expires_at, request_json) VALUES (?, ?, ?, ?, ?)",
        [
            request_id, area_id, now.isoformat(), (now + timedelta(hours=CANDIDATE_TTL_HOURS)).isoformat(),
            json.dumps({**request, "seed": seed}, default=str),
        ],
    )
    return request_id


def _persist_candidate(
    conn: sqlite3.Connection, area_id: int, request_id: str, edge_rows: Mapping[int, dict], candidate: _Candidate
) -> RouteCandidateOut:
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(hours=CANDIDATE_TTL_HOURS)).isoformat()
    candidate_id = str(uuid.uuid4())
    geometry = _geometry(edge_rows, candidate.segments)
    conn.execute(
        """
        INSERT INTO net_candidates (
            candidate_id, request_id, area_id, shape, start_json, distance_m,
            score_json, missed_json, geometry_json, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            candidate_id, request_id, area_id, candidate.shape, json.dumps(candidate.start),
            candidate.score["distance_m"], json.dumps(candidate.score), json.dumps(candidate.missed),
            json.dumps(geometry), now.isoformat(), expires_at,
        ],
    )
    return {
        "candidate_id": candidate_id,
        "area_id": area_id,
        "shape": candidate.shape,
        "start": candidate.start,
        "distance_m": candidate.score["distance_m"],
        "score": candidate.score,
        "missed_constraints": candidate.missed,
        "geometry": [list(p) for p in geometry],
    }


# ---------------------------------------------------------------------------
# Candidate retrieval / saving
# ---------------------------------------------------------------------------


def get_route_candidate(conn: sqlite3.Connection, candidate_id: str) -> RouteCandidateOut:
    _purge_expired(conn)
    row = conn.execute("SELECT * FROM net_candidates WHERE candidate_id = ?", [candidate_id]).fetchone()
    if row is None:
        raise CandidateNotFoundError(f"no candidate {candidate_id} (or it expired)")
    return {
        "candidate_id": row["candidate_id"],
        "area_id": row["area_id"],
        "shape": row["shape"],
        "start": json.loads(row["start_json"]),
        "distance_m": row["distance_m"],
        "score": json.loads(row["score_json"]),
        "missed_constraints": json.loads(row["missed_json"]),
        "geometry": json.loads(row["geometry_json"]),
    }


def save_route_candidate(conn: sqlite3.Connection, candidate_id: str, name: str) -> dict:
    """Upload a GPX built from a candidate's stored geometry as a route
    (source "generated")."""
    candidate = get_route_candidate(conn, candidate_id)
    points = [(cast(float, lat), cast(float, lng), ele) for lat, lng, ele in candidate["geometry"]]
    gpx_bytes = routes.build_gpx(name, points)
    filename = f"generated-{candidate_id}.gpx"
    route_ids = routes.upload_gpx(conn, gpx_bytes, filename, name=name, source="generated")
    return {"route_id": route_ids[0], "candidate_id": candidate_id, "name": name}
