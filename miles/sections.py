"""Sections: the stretches of an area worth running as a unit
(adr/0004-section-first-route-search.md). They are cut from the network graph
at build time, stored per area, and rebuilt with the graph; anything that
depends on the athlete or a start point is computed per query in survey.py.

Kinds, and the v1 rules that define them. Every rule is a heuristic with a
named constant:

- trail_system: a connected component of trail-surface edges (natural-surface
  paths and unpaved tracks; sidewalks and paved paths excluded). A component
  longer than TRAIL_SYSTEM_SPLIT_M is split into units seeded by well-named
  way groups (TRAIL_UNIT_SEED_M of one name), each unit taking the edges
  nearest its seed; a component with no such names is cut into
  contiguous chunks of about UNIT_TARGET_M.
- water_path: a connected component of path edges (any surface) whose points
  lie mostly within WATER_NEAR_M of a river, stream, canal, or water body.
- named_route: the member ways of one walking, hiking, cycling, or running
  route relation, one section per connected piece.
- park_paths: path edges mostly inside one park or protected area, one
  section per park and connected piece.
- quiet_road: a connected run of one street's comfortable-tier edges with at
  most QUIET_MAX_JUNCTIONS_PER_MI junctions per mile. The tier uses the
  route preferences in force at build time.

A way may belong to sections of different kinds (a riverside trail is part of
a trail system and a water path) but to at most one section of a kind, except
that route relations may share ways. A section shorter than MIN_SECTION_M is
dropped.

A section stores only what the map decides: length, surface mix, continuity
(junctions and busy-road crossings), scenery tags, a grade histogram over the
terrain model's bins (read through the athlete's model at query time), gain,
and the entry nodes where it meets the rest of the network.

Adjustments and exclusions are athlete or research judgments keyed by OSM way
ids, never section ids, so they re-attach after a rebuild to the section of
the same kind that shares most of their ways by length in both directions.
"""

import json
import math
import sqlite3
import struct
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Hashable, cast
from typing_extensions import TypedDict

from . import gpx_parse, network, routes_spatial, terrain, tilecache

KIND_TRAIL_SYSTEM = "trail_system"
KIND_WATER_PATH = "water_path"
KIND_NAMED_ROUTE = "named_route"
KIND_PARK_PATHS = "park_paths"
KIND_QUIET_ROAD = "quiet_road"
SECTION_KINDS = (KIND_TRAIL_SYSTEM, KIND_WATER_PATH, KIND_NAMED_ROUTE, KIND_PARK_PATHS, KIND_QUIET_ROAD)

MIN_SECTION_M = 800.0
TRAIL_SYSTEM_SPLIT_M = 25_000.0
TRAIL_UNIT_SEED_M = 2_500.0
UNIT_TARGET_M = 12_000.0

# A point within this far of water counts as beside it; an edge is a water
# edge when WATER_EDGE_MIN_FRAC of its sampled points are.
WATER_NEAR_M = 75.0
WATER_EDGE_MIN_FRAC = 0.6
PARK_EDGE_MIN_FRAC = 0.6
# Summits and viewpoints this close to a section's path count for it.
POI_NEAR_M = 120.0
# Scenery is sampled this far apart along an edge.
SCENERY_STRIDE_M = 60.0
# A section carries a scenery tag when at least this share of its length has it.
TAG_WATER_SHARE = 0.4
TAG_PARK_SHARE = 0.5

QUIET_MIN_M = 1000.0
QUIET_MAX_JUNCTIONS_PER_MI = 8.0
QUIET_ROAD_HIGHWAYS = frozenset({"residential", "unclassified", "tertiary", "living_street", "road"})

# A section's own coverage of a rebuilt section for a stored judgment to
# re-attach: this share of each side's length, by way.
REATTACH_MIN_SHARE = 0.5

_MILE_M = 1609.344
_WATER_DENSIFY_M = 25.0
# Park rings are bucketed on a grid this wide; a ring spanning more than
# _RING_MAX_CELLS cells is checked for every point instead.
_RING_CELL_DEG = 0.01
_RING_MAX_CELLS = 2500
_METERS_PER_DEG_LAT = routes_spatial.METERS_PER_DEG_LAT


class SectionNotFoundError(LookupError):
    pass


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def init_section_schema(conn: sqlite3.Connection) -> None:
    # data is JSON: ways, surface_m, junctions, crossings, scenery,
    # grade_hist_m, elevated_m, gain_m, loss_m, entry_nodes. edges is packed
    # little-endian uint32 edge ids. section_id is never reused, so a stale id
    # cannot name a different section after a rebuild.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_sections (
            section_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            area_id      INTEGER NOT NULL,
            kind         TEXT NOT NULL,
            name         TEXT NOT NULL,
            length_m     REAL NOT NULL,
            centroid_lat REAL NOT NULL,
            centroid_lng REAL NOT NULL,
            south        REAL NOT NULL,
            west         REAL NOT NULL,
            north        REAL NOT NULL,
            east         REAL NOT NULL,
            data         TEXT NOT NULL,
            edges        BLOB NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_net_sections_area ON net_sections(area_id)")
    # Athlete and research judgment, never derived: a factor for one section
    # (kind 'adjust') or its exclusion from routes (kind 'exclude'), keyed by
    # the way ids the section had, with each way's length in it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_section_judgments (
            judgment_id  INTEGER PRIMARY KEY,
            judgment     TEXT NOT NULL CHECK (judgment IN ('adjust', 'exclude')),
            section_kind TEXT NOT NULL,
            factor       REAL,
            source       TEXT NOT NULL,
            note         TEXT,
            url          TEXT,
            created_at   TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_section_judgment_ways (
            judgment_id INTEGER NOT NULL REFERENCES net_section_judgments(judgment_id) ON DELETE CASCADE,
            way_id      INTEGER NOT NULL,
            length_m    REAL NOT NULL,
            PRIMARY KEY (judgment_id, way_id)
        ) WITHOUT ROWID
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_judgment_ways_way ON net_section_judgment_ways(way_id)")
    # Which activities have been matched against an area's current sections,
    # and how far each ran along each section.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_section_visits (
            section_id  INTEGER NOT NULL,
            activity_id INTEGER NOT NULL,
            covered_m   REAL NOT NULL,
            PRIMARY KEY (section_id, activity_id)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS net_section_matched (
            area_id     INTEGER NOT NULL,
            activity_id INTEGER NOT NULL,
            built_at    TEXT NOT NULL,
            PRIMARY KEY (area_id, activity_id)
        ) WITHOUT ROWID
    """)


def delete_area_sections(conn: sqlite3.Connection, area_id: int) -> None:
    """Drop an area's sections and everything matched against them; the
    caller commits."""
    conn.execute(
        "DELETE FROM net_section_visits WHERE section_id IN (SELECT section_id FROM net_sections WHERE area_id = ?)",
        [area_id],
    )
    conn.execute("DELETE FROM net_section_matched WHERE area_id = ?", [area_id])
    conn.execute("DELETE FROM net_sections WHERE area_id = ?", [area_id])


# ---------------------------------------------------------------------------
# Scenery gathered from tile payloads
# ---------------------------------------------------------------------------


Ring = list[tuple[float, float]]


@dataclass
class Park:
    kind: str  # "park" | "protected"
    name: str | None
    rings: list[Ring]
    bounds: tuple[float, float, float, float]  # south, west, north, east


@dataclass
class Scenery:
    """Waterways, water bodies, parks and protected areas, summits and
    viewpoints, and route relations from an area's tile payloads, each once
    however many tiles repeat it."""

    waterways: dict[int, tuple[str | None, list[tuple[float, float]]]] = field(default_factory=dict)
    waters: dict[int, tuple[str | None, list[Ring]]] = field(default_factory=dict)
    parks: dict[int, Park] = field(default_factory=dict)
    pois: dict[int, tuple[str, str | None, float, float]] = field(default_factory=dict)  # kind, name, lat, lng
    routes: dict[int, tuple[str | None, set[int]]] = field(default_factory=dict)  # name, member way ids

    def add(self, el: Mapping[str, object]) -> None:
        el_type, el_id = el.get("type"), el.get("id")
        tags = cast(dict[str, str], el.get("tags") or {})
        if not isinstance(el_id, int):
            return
        if el_type == "way":
            if tags.get("waterway") in tilecache.WATERWAY_VALUES and el_id not in self.waterways:
                geometry = cast(list[dict[str, float] | None], el.get("geometry") or [])
                points = [(p["lat"], p["lon"]) for p in geometry if p is not None]
                if len(points) >= 2:
                    self.waterways[el_id] = (tags.get("name"), points)
        elif el_type == "node":
            kind = "summit" if tags.get("natural") == "peak" else "viewpoint" if tags.get("tourism") == "viewpoint" else None
            lat, lon = el.get("lat"), el.get("lon")
            if kind is not None and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                self.pois.setdefault(el_id, (kind, tags.get("name"), float(lat), float(lon)))
        elif el_type == "area":
            area_kind = tilecache.scenery_area_kind(tags)
            raw_rings = cast(list[list[list[float]]], el.get("rings") or [])
            rings: list[Ring] = [[(p[0], p[1]) for p in ring] for ring in raw_rings]
            bounds = el.get("bounds")
            if area_kind is None or not rings or not isinstance(bounds, list):
                return
            if area_kind == "water":
                self.waters.setdefault(el_id, (tags.get("name"), rings))
            else:
                self.parks.setdefault(el_id, Park(area_kind, tags.get("name"), rings, cast("tuple[float, float, float, float]", tuple(bounds))))
        elif el_type == "relation":
            members = el.get("members")
            if isinstance(members, list):
                name = tags.get("name") or tags.get("ref")
                known = self.routes.get(el_id)
                self.routes[el_id] = (name, (known[1] if known else set()) | {m for m in members if isinstance(m, int)})


def _densify(points: Sequence[tuple[float, float]], step_m: float) -> Iterable[tuple[float, float]]:
    """The points of a polyline with extra points so none are farther apart
    than step_m."""
    for (lat1, lng1), (lat2, lng2) in zip(points, points[1:]):
        gap = routes_spatial.equirect_distance_m(lat1, lng1, lat2, lng2)
        pieces = max(1, math.ceil(gap / step_m))
        for k in range(pieces):
            t = k / pieces
            yield (lat1 + (lat2 - lat1) * t, lng1 + (lng2 - lng1) * t)
    if points:
        yield points[-1]


def _in_ring(lat: float, lng: float, ring: Ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        lat_i, lng_i = ring[i]
        lat_j, lng_j = ring[j]
        if (lat_i > lat) != (lat_j > lat) and lng < (lng_j - lng_i) * (lat - lat_i) / (lat_j - lat_i) + lng_i:
            inside = not inside
        j = i
    return inside


@dataclass(frozen=True)
class EdgeScenery:
    water_share: float
    water_name: str | None
    park_share: float
    protected_share: float
    area_key: int | None  # the park or protected area holding most of the edge
    area_name: str | None
    pois: frozenset[int]


_NO_SCENERY = EdgeScenery(0.0, None, 0.0, 0.0, None, None, frozenset())


class SceneryIndex:
    """Proximity and containment lookups over a Scenery."""

    def __init__(self, scenery: Scenery) -> None:
        self._water_names: dict[Hashable, str | None] = {}
        self._water = routes_spatial.NearestIndex(cell_size_m=100.0)
        for way_id, (name, points) in scenery.waterways.items():
            self._add_water(("w", way_id), name, points)
        for body_id, (name, rings) in scenery.waters.items():
            for ring in rings:
                self._add_water(("a", body_id), name, ring)
        self._pois = routes_spatial.NearestIndex(cell_size_m=100.0)
        for poi_id, (_kind, _name, lat, lng) in scenery.pois.items():
            self._pois.add(poi_id, [(lat, lng, 0.0)])
        self._parks = scenery.parks
        self._ring_cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        self._rings: list[tuple[int, tuple[float, float, float, float], Ring]] = []
        self._wide_rings: list[int] = []
        for key, park in scenery.parks.items():
            for ring in park.rings:
                lats, lngs = [p[0] for p in ring], [p[1] for p in ring]
                bounds = (min(lats), min(lngs), max(lats), max(lngs))
                self._rings.append((key, bounds, ring))
                rows = range(math.floor(bounds[0] / _RING_CELL_DEG), math.floor(bounds[2] / _RING_CELL_DEG) + 1)
                cols = range(math.floor(bounds[1] / _RING_CELL_DEG), math.floor(bounds[3] / _RING_CELL_DEG) + 1)
                if len(rows) * len(cols) > _RING_MAX_CELLS:
                    self._wide_rings.append(len(self._rings) - 1)
                    continue
                for row in rows:
                    for col in cols:
                        self._ring_cells[(row, col)].append(len(self._rings) - 1)
        self.poi_kinds = {poi_id: (kind, name) for poi_id, (kind, name, _lat, _lng) in scenery.pois.items()}

    def _add_water(self, key: Hashable, name: str | None, points: Sequence[tuple[float, float]]) -> None:
        self._water_names[key] = name
        self._water.add(key, ((lat, lng, 0.0) for lat, lng in _densify(points, _WATER_DENSIFY_M)))

    def sample(self, samples: Sequence[tuple[float, float]]) -> EdgeScenery:
        """Scenery along a few sampled points of an edge."""
        if not samples:
            return _NO_SCENERY
        near_water = 0
        water_names: Counter[str] = Counter()
        in_park = in_protected = 0
        areas: Counter[int] = Counter()
        pois: set[int] = set()
        for lat, lng in samples:
            hit = self._water.nearest(lat, lng, WATER_NEAR_M)
            if hit is not None:
                near_water += 1
                name = self._water_names.get(hit[0])
                if name:
                    water_names[name] += 1
            for poi_id, _offset, _d in self._pois.within(lat, lng, POI_NEAR_M):
                pois.add(cast(int, poi_id))
            if self._rings:
                park_hit = protected_hit = False
                inside: set[int] = set()
                cell = (math.floor(lat / _RING_CELL_DEG), math.floor(lng / _RING_CELL_DEG))
                for ring_index in (*self._ring_cells.get(cell, ()), *self._wide_rings):
                    key, (south, west, north, east), ring = self._rings[ring_index]
                    if key in inside or not (south <= lat <= north and west <= lng <= east):
                        continue
                    if _in_ring(lat, lng, ring):
                        inside.add(key)
                for key in inside:
                    areas[key] += 1
                    if self._parks[key].kind == "protected":
                        protected_hit = True
                    else:
                        park_hit = True
                in_park += park_hit
                in_protected += protected_hit
        n = len(samples)
        area_key = areas.most_common(1)[0][0] if areas else None
        return EdgeScenery(
            near_water / n,
            water_names.most_common(1)[0][0] if water_names else None,
            in_park / n,
            in_protected / n,
            area_key,
            self._parks[area_key].name if area_key is not None else None,
            frozenset(pois),
        )


# ---------------------------------------------------------------------------
# Edges as sections see them
# ---------------------------------------------------------------------------


@dataclass
class _Edge:
    edge_id: int
    way_id: int
    name: str | None
    highway: str
    surface_class: str
    a: int
    b: int
    length_m: float
    has_elevation: bool
    tier: str | None
    blob: bytes
    is_trail: bool = False
    is_path: bool = False
    is_quiet_road: bool = False
    is_road: bool = False


def _classify(edge: _Edge) -> None:
    surface, highway = edge.surface_class, edge.highway
    if surface in ("sidewalk", network.ACCESS_EDGE_SURFACE_CLASS):
        return
    edge.is_trail = surface == "trail" or (highway == "track" and surface == "unpaved_road")
    edge.is_path = highway in network.TRAIL_HIGHWAYS or highway in ("track", "bridleway")
    edge.is_road = edge.tier is not None and highway != "track"
    edge.is_quiet_road = edge.tier == "comfortable" and highway in QUIET_ROAD_HIGHWAYS


def _load_edges(conn: sqlite3.Connection, area_id: int, prefs: dict) -> dict[int, _Edge]:
    edges: dict[int, _Edge] = {}
    for r in conn.execute("SELECT * FROM net_edges WHERE area_id = ?", [area_id]):
        tier = None
        if r["maxspeed_mph"] is not None:
            tier = network.road_comfort_tier(
                r["highway"], r["surface_class"], r["maxspeed_mph"], r["lanes"], bool(r["has_separation"]), prefs
            )
        edge = _Edge(
            r["edge_id"], r["way_id"], r["way_name"], r["highway"], r["surface_class"], r["from_node_id"],
            r["to_node_id"], r["length_m"], bool(r["has_elevation"]), tier, bytes(r["points_blob"]),
        )
        _classify(edge)
        edges[edge.edge_id] = edge
    return edges


def _sample_points(edge: _Edge) -> list[tuple[float, float]]:
    points = network.unpack_edge_points(edge.blob)
    stride = max(1, round(SCENERY_STRIDE_M / 20.0))
    sampled = [(p[0], p[1]) for p in points[::stride]]
    return sampled or [(points[0][0], points[0][1])]


class _Graph:
    """Node adjacency over every edge of the area."""

    def __init__(self, edges: Mapping[int, _Edge]) -> None:
        self.edges = edges
        self.at_node: dict[int, list[int]] = defaultdict(list)
        for edge in edges.values():
            self.at_node[edge.a].append(edge.edge_id)
            if edge.b != edge.a:
                self.at_node[edge.b].append(edge.edge_id)

    def components(self, edge_ids: Iterable[int]) -> list[list[int]]:
        """Connected pieces of the given edges (joined through shared nodes
        only), longest first."""
        members = set(edge_ids)
        seen: set[int] = set()
        pieces: list[list[int]] = []
        for start in sorted(members):
            if start in seen:
                continue
            seen.add(start)
            piece = []
            queue = deque([start])
            while queue:
                eid = queue.popleft()
                piece.append(eid)
                edge = self.edges[eid]
                for node in (edge.a, edge.b):
                    for other in self.at_node[node]:
                        if other in members and other not in seen:
                            seen.add(other)
                            queue.append(other)
            pieces.append(piece)
        pieces.sort(key=lambda p: (-self.length(p), min(p)))
        return pieces

    def length(self, edge_ids: Iterable[int]) -> float:
        return sum(self.edges[e].length_m for e in edge_ids)


# ---------------------------------------------------------------------------
# Grade statistics per edge
# ---------------------------------------------------------------------------


@dataclass
class _GradeStats:
    hist_m: dict[int, float] = field(default_factory=dict)
    gain_m: float = 0.0
    loss_m: float = 0.0
    elevated_m: float = 0.0


def _way_chains(edge_ids: Sequence[int], edges: Mapping[int, _Edge]) -> list[list[tuple[int, bool]]]:
    """A way's edges as ordered chains of (edge_id, forward)."""
    at_node: dict[int, list[int]] = defaultdict(list)
    for eid in edge_ids:
        at_node[edges[eid].a].append(eid)
        if edges[eid].b != edges[eid].a:
            at_node[edges[eid].b].append(eid)
    unused = set(edge_ids)
    chains: list[list[tuple[int, bool]]] = []

    def walk(node: int) -> None:
        chain: list[tuple[int, bool]] = []
        while True:
            nxt = next((e for e in at_node[node] if e in unused), None)
            if nxt is None:
                break
            unused.discard(nxt)
            edge = edges[nxt]
            forward = edge.a == node
            chain.append((nxt, forward))
            node = edge.b if forward else edge.a
        if chain:
            chains.append(chain)

    for node in sorted(n for n, listed in at_node.items() if len(listed) == 1):
        walk(node)
    while unused:
        walk(edges[min(unused)].a)
    return chains


def _grade_stats(edges: Mapping[int, _Edge], wanted: set[int]) -> dict[int, _GradeStats]:
    """Smoothed-profile grade histogram (meters per terrain grade bin) and
    gain/loss for each wanted edge. Profiles are smoothed over whole runs of
    one way, as a path is when scored, since a single edge is too short to
    pick a smoothing window from."""
    by_way: dict[int, list[int]] = defaultdict(list)
    wanted_ways = {edges[e].way_id for e in wanted}
    for edge in edges.values():
        if edge.way_id in wanted_ways and edge.has_elevation:
            by_way[edge.way_id].append(edge.edge_id)
    stats: dict[int, _GradeStats] = {e: _GradeStats() for e in wanted}
    for way_edges in by_way.values():
        for chain in _way_chains(way_edges, edges):
            dist: list[float] = []
            ele: list[float] = []
            owner: list[int] = []  # the edge each step (point i to i+1) belongs to
            offset = 0.0
            for edge_id, forward in chain:
                edge = edges[edge_id]
                points = network.unpack_edge_points(edge.blob)
                if not forward:
                    points = [(lat, lng, edge.length_m - d, e) for lat, lng, d, e in reversed(points)]
                for k, (_lat, _lng, d, e) in enumerate(points):
                    if k == 0 and dist:
                        continue
                    if e is None:
                        continue
                    dist.append(offset + d)
                    ele.append(e)
                    if len(dist) > 1:
                        owner.append(edge_id)
                offset += edge.length_m
            if len(dist) < 3:
                continue
            smoothed, _half = gpx_parse.roughness_smoothed(dist, ele)
            for i, edge_id in enumerate(owner):
                step = dist[i + 1] - dist[i]
                st = stats.get(edge_id)
                if st is None or step <= 0:
                    continue
                rise = smoothed[i + 1] - smoothed[i]
                bin_index = terrain.grade_bin(rise / step * 100.0)
                st.hist_m[bin_index] = st.hist_m.get(bin_index, 0.0) + step
                st.elevated_m += step
                if rise > 0:
                    st.gain_m += rise
                else:
                    st.loss_m -= rise
    return stats


# ---------------------------------------------------------------------------
# Building sections
# ---------------------------------------------------------------------------


class SectionDraft(TypedDict):
    kind: str
    name: str
    edge_ids: list[int]


def _top_name(edges: Iterable[_Edge]) -> str | None:
    by_name: Counter[str] = Counter()
    for edge in edges:
        if edge.name:
            by_name[edge.name] += int(edge.length_m)
    return by_name.most_common(1)[0][0] if by_name else None


def _trail_system_names(edges: Sequence[_Edge]) -> str:
    named = {e.name for e in edges if e.name}
    top = _top_name(edges)
    if top is None:
        return "Unnamed trail system"
    return f"{top} trail system" if len(named) > 1 else top


def _split_trail_system(graph: _Graph, component: list[int]) -> list[tuple[str | None, list[int]]]:
    """Units of a very large trail component as (seed name or None, edges)."""
    edges = graph.edges
    by_name: Counter[str] = Counter()
    for eid in component:
        if edges[eid].name:
            by_name[cast(str, edges[eid].name)] += int(edges[eid].length_m)
    seeds = {name for name, total in by_name.items() if total >= TRAIL_UNIT_SEED_M}
    members = set(component)
    label: dict[int, str | None] = {}
    queue: deque[int] = deque()
    if seeds:
        for eid in sorted(component):
            if edges[eid].name in seeds:
                label[eid] = edges[eid].name
                queue.append(eid)
    else:
        # Fill contiguous chunks outward from the westernmost edge.
        order: list[int] = []
        start = min(component, key=lambda e: (network.unpack_edge_points(edges[e].blob)[0][1], e))
        seen = {start}
        bfs = deque([start])
        while bfs:
            eid = bfs.popleft()
            order.append(eid)
            for node in (edges[eid].a, edges[eid].b):
                for other in sorted(graph.at_node[node]):
                    if other in members and other not in seen:
                        seen.add(other)
                        bfs.append(other)
        chunk = 0
        filled = 0.0
        for eid in order:
            if filled >= UNIT_TARGET_M:
                chunk, filled = chunk + 1, 0.0
            label[eid] = f"\0chunk{chunk}"
            filled += edges[eid].length_m
        units: dict[str, list[int]] = defaultdict(list)
        for eid, lab in label.items():
            units[cast(str, lab)].append(eid)
        return [(None, ids) for _lab, ids in sorted(units.items())]
    while queue:
        eid = queue.popleft()
        for node in (edges[eid].a, edges[eid].b):
            for other in sorted(graph.at_node[node]):
                if other in members and other not in label:
                    label[other] = label[eid]
                    queue.append(other)
    units = defaultdict(list)
    for eid, lab in label.items():
        units[cast(str, lab)].append(eid)
    return [(lab, ids) for lab, ids in sorted(units.items())]


def _draft_trail_systems(graph: _Graph) -> list[SectionDraft]:
    drafts: list[SectionDraft] = []
    for component in graph.components(e.edge_id for e in graph.edges.values() if e.is_trail):
        if graph.length(component) < MIN_SECTION_M:
            continue
        if graph.length(component) <= TRAIL_SYSTEM_SPLIT_M:
            drafts.append({"kind": KIND_TRAIL_SYSTEM, "name": _trail_system_names([graph.edges[e] for e in component]), "edge_ids": component})
            continue
        for seed, unit in _split_trail_system(graph, component):
            for piece in graph.components(unit):
                if graph.length(piece) < MIN_SECTION_M:
                    continue
                name = f"{seed} area" if seed else "Unnamed trail system"
                drafts.append({"kind": KIND_TRAIL_SYSTEM, "name": name, "edge_ids": piece})
    return drafts


def _draft_water_paths(graph: _Graph, scenery: Mapping[int, EdgeScenery]) -> list[SectionDraft]:
    candidates = [
        e.edge_id for e in graph.edges.values()
        if e.is_path and scenery[e.edge_id].water_share >= WATER_EDGE_MIN_FRAC
    ]
    drafts: list[SectionDraft] = []
    for piece in graph.components(candidates):
        if graph.length(piece) < MIN_SECTION_M:
            continue
        edges = [graph.edges[e] for e in piece]
        water = Counter(scenery[e].water_name for e in piece if scenery[e].water_name)
        name = _top_name(edges)
        if name is None:
            name = f"Path along {water.most_common(1)[0][0]}" if water else "Unnamed waterside path"
        drafts.append({"kind": KIND_WATER_PATH, "name": name, "edge_ids": piece})
    return drafts


def _draft_named_routes(graph: _Graph, scenery: Scenery) -> list[SectionDraft]:
    by_way: dict[int, list[int]] = defaultdict(list)
    for edge in graph.edges.values():
        if edge.is_path or edge.is_road:
            by_way[edge.way_id].append(edge.edge_id)
    drafts: list[SectionDraft] = []
    for _rel_id, (rel_name, members) in sorted(scenery.routes.items()):
        edge_ids = [e for w in sorted(members) for e in by_way.get(w, [])]
        pieces = [p for p in graph.components(edge_ids) if graph.length(p) >= MIN_SECTION_M]
        for n, piece in enumerate(pieces, start=1):
            name = rel_name or _top_name([graph.edges[e] for e in piece]) or "Unnamed route"
            drafts.append({"kind": KIND_NAMED_ROUTE, "name": name if len(pieces) == 1 else f"{name} (part {n})", "edge_ids": piece})
    return drafts


def _draft_park_paths(graph: _Graph, scenery: Mapping[int, EdgeScenery]) -> list[SectionDraft]:
    by_area: dict[int, list[int]] = defaultdict(list)
    for edge in graph.edges.values():
        es = scenery[edge.edge_id]
        if edge.is_path and es.area_key is not None and max(es.park_share, es.protected_share) >= PARK_EDGE_MIN_FRAC:
            by_area[es.area_key].append(edge.edge_id)
    drafts: list[SectionDraft] = []
    for _area_key, edge_ids in sorted(by_area.items()):
        for piece in graph.components(edge_ids):
            if graph.length(piece) < MIN_SECTION_M:
                continue
            area_name = scenery[piece[0]].area_name
            drafts.append({
                "kind": KIND_PARK_PATHS,
                "name": f"Paths in {area_name}" if area_name else "Paths in an unnamed park",
                "edge_ids": piece,
            })
    return drafts


def _junctions_in(graph: _Graph, edge_ids: Iterable[int]) -> int:
    nodes = {n for e in edge_ids for n in (graph.edges[e].a, graph.edges[e].b)}
    return sum(1 for n in nodes if len(graph.at_node[n]) >= 3)


def _draft_quiet_roads(graph: _Graph) -> list[SectionDraft]:
    by_name: dict[str, list[int]] = defaultdict(list)
    for edge in graph.edges.values():
        if edge.is_quiet_road and edge.name:
            by_name[edge.name].append(edge.edge_id)
    drafts: list[SectionDraft] = []
    for name, edge_ids in sorted(by_name.items()):
        for piece in graph.components(edge_ids):
            length = graph.length(piece)
            if length < QUIET_MIN_M:
                continue
            if _junctions_in(graph, piece) / (length / _MILE_M) > QUIET_MAX_JUNCTIONS_PER_MI:
                continue
            drafts.append({"kind": KIND_QUIET_ROAD, "name": name, "edge_ids": piece})
    return drafts


def _describe(
    graph: _Graph,
    draft: SectionDraft,
    scenery: Scenery,
    edge_scenery: Mapping[int, EdgeScenery],
    index: SceneryIndex,
    grades: Mapping[int, _GradeStats],
) -> dict | None:
    edges = graph.edges
    ids = draft["edge_ids"]
    members = set(ids)
    length = graph.length(ids)

    way_length: dict[int, float] = defaultdict(float)
    way_name: dict[int, str | None] = {}
    surface: dict[str, float] = defaultdict(float)
    for eid in ids:
        e = edges[eid]
        way_length[e.way_id] += e.length_m
        way_name[e.way_id] = e.name
        surface[e.surface_class] += e.length_m
    ways = [[way_id, way_length[way_id], way_name[way_id]] for way_id in sorted(way_length, key=lambda w: (-way_length[w], w))]

    nodes = {n for e in ids for n in (edges[e].a, edges[e].b)}
    entry_nodes: list[int] = []
    junctions = crossings = 0
    for node in sorted(nodes):
        around = graph.at_node[node]
        if len(around) >= 3:
            junctions += 1
        outside = [o for o in around if o not in members]
        if outside:
            entry_nodes.append(node)
            passes_through = len(around) - len(outside) >= 2
            if passes_through and any(edges[o].tier in ("tolerable", "uncomfortable") and edges[o].is_road for o in outside):
                crossings += 1

    lat_sum = lng_sum = weight = 0.0
    south = west = math.inf
    north = east = -math.inf
    for eid in ids:
        points = network.unpack_edge_points(edges[eid].blob)
        mid = points[len(points) // 2]
        lat_sum += mid[0] * edges[eid].length_m
        lng_sum += mid[1] * edges[eid].length_m
        weight += edges[eid].length_m
        for lat, lng, _d, _e in points:
            south, north = min(south, lat), max(north, lat)
            west, east = min(west, lng), max(east, lng)

    def share(attr: str) -> float:
        return sum(getattr(edge_scenery[e], attr) * edges[e].length_m for e in ids) / length if length else 0.0

    pois: set[int] = set()
    for eid in ids:
        pois |= edge_scenery[eid].pois
    summits = sorted(p for p in pois if index.poi_kinds[p][0] == "summit")
    viewpoints = sorted(p for p in pois if index.poi_kinds[p][0] == "viewpoint")
    route_names = sorted({
        name for name, rel_members in scenery.routes.values() if name and rel_members & {edges[e].way_id for e in ids}
    })
    water_names = Counter(edge_scenery[e].water_name for e in ids if edge_scenery[e].water_name)
    area_names = Counter(edge_scenery[e].area_name for e in ids if edge_scenery[e].area_name)
    water_share, park_share, protected_share = share("water_share"), share("park_share"), share("protected_share")
    tags = [
        tag for tag, on in (
            ("near_water", water_share >= TAG_WATER_SHARE),
            ("in_park", park_share >= TAG_PARK_SHARE),
            ("protected_area", protected_share >= TAG_PARK_SHARE),
            ("named_route", bool(route_names)),
            ("viewpoint", bool(viewpoints)),
            ("summit", bool(summits)),
        ) if on
    ]

    hist = [0.0] * terrain.N_GRADE_BINS
    gain = loss = elevated = 0.0
    for eid in ids:
        st = grades.get(eid)
        if st is None:
            continue
        for b, meters in st.hist_m.items():
            hist[b] += meters
        gain, loss, elevated = gain + st.gain_m, loss + st.loss_m, elevated + st.elevated_m

    return {
        "name": draft["name"],
        "length_m": length,
        "centroid": (lat_sum / weight, lng_sum / weight),
        "bbox": (south, west, north, east),
        "data": {
            "ways": ways,
            "surface_m": dict(surface),
            "junctions": junctions,
            "crossings": crossings,
            "scenery": {
                "near_water_share": water_share,
                "in_park_share": park_share,
                "protected_share": protected_share,
                "water_names": [n for n, _c in water_names.most_common(3)],
                "area_names": [n for n, _c in area_names.most_common(3)],
                "route_names": route_names,
                "summits": len(summits),
                "viewpoints": len(viewpoints),
                "summit_names": [n for n in (index.poi_kinds[p][1] for p in summits) if n][:3],
                "tags": tags,
            },
            "grade_hist_m": hist,
            "elevated_m": elevated,
            "gain_m": gain,
            "loss_m": loss,
            "entry_nodes": entry_nodes,
        },
    }


def rebuild_sections(conn: sqlite3.Connection, area_id: int, scenery: Scenery) -> int:
    """Replace an area's sections from its freshly written graph; the caller
    commits. Returns the section count."""
    prefs = network.get_route_preferences(conn)
    edges = _load_edges(conn, area_id, prefs)
    graph = _Graph(edges)
    index = SceneryIndex(scenery)

    edge_scenery: dict[int, EdgeScenery] = {}
    for edge in edges.values():
        if edge.is_path or edge.is_quiet_road:
            edge_scenery[edge.edge_id] = index.sample(_sample_points(edge))
        else:
            edge_scenery[edge.edge_id] = _NO_SCENERY

    drafts = [
        *_draft_trail_systems(graph),
        *_draft_water_paths(graph, edge_scenery),
        *_draft_named_routes(graph, scenery),
        *_draft_park_paths(graph, edge_scenery),
        *_draft_quiet_roads(graph),
    ]
    grades = _grade_stats(edges, {e for d in drafts for e in d["edge_ids"]})

    delete_area_sections(conn, area_id)
    count = 0
    order = {kind: i for i, kind in enumerate(SECTION_KINDS)}
    drafts.sort(key=lambda d: (order[d["kind"]], -graph.length(d["edge_ids"]), min(d["edge_ids"])))
    for draft in drafts:
        described = _describe(graph, draft, scenery, edge_scenery, index, grades)
        if described is None:
            continue
        lat, lng = described["centroid"]
        south, west, north, east = described["bbox"]
        conn.execute(
            """
            INSERT INTO net_sections (
                area_id, kind, name, length_m, centroid_lat, centroid_lng, south, west, north, east, data, edges
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                area_id, draft["kind"], described["name"], described["length_m"], lat, lng, south, west, north, east,
                json.dumps(described["data"]), _pack_ids(sorted(draft["edge_ids"])),
            ],
        )
        count += 1
    return count


def _pack_ids(ids: Sequence[int]) -> bytes:
    return struct.pack(f"<{len(ids)}I", *ids)


def _unpack_ids(blob: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(blob) // 4}I", blob))


# ---------------------------------------------------------------------------
# Reading sections
# ---------------------------------------------------------------------------


@dataclass
class Section:
    section_id: int
    area_id: int
    kind: str
    name: str
    length_m: float
    centroid: tuple[float, float]
    bbox: tuple[float, float, float, float]
    data: dict
    edge_blob: bytes = field(repr=False, default=b"")

    @property
    def edge_ids(self) -> list[int]:
        return _unpack_ids(self.edge_blob)

    @property
    def way_lengths(self) -> dict[int, float]:
        return {w[0]: w[1] for w in self.data["ways"]}


def _row_section(row: sqlite3.Row) -> Section:
    return Section(
        row["section_id"], row["area_id"], row["kind"], row["name"], row["length_m"],
        (row["centroid_lat"], row["centroid_lng"]), (row["south"], row["west"], row["north"], row["east"]),
        json.loads(row["data"]), bytes(row["edges"]),
    )


def load_sections(conn: sqlite3.Connection, area_id: int, kinds: Sequence[str] | None = None) -> list[Section]:
    rows = conn.execute("SELECT * FROM net_sections WHERE area_id = ? ORDER BY section_id", [area_id]).fetchall()
    return [s for s in map(_row_section, rows) if kinds is None or s.kind in kinds]


def get_section(conn: sqlite3.Connection, section_id: int) -> Section:
    row = conn.execute("SELECT * FROM net_sections WHERE section_id = ?", [section_id]).fetchone()
    if row is None:
        raise SectionNotFoundError(
            f"no section {section_id}: section ids change when an area's network is rebuilt; survey the area again"
        )
    return _row_section(row)


def section_geometry(conn: sqlite3.Connection, section: Section, max_points: int = 1500) -> list[list[list[float]]]:
    """A section's ways as simplified [lat, lng] polylines, one per run of a
    way, thinned until the whole fits max_points."""
    edges = _load_edges_by_id(conn, section.edge_ids)
    by_way: dict[int, list[int]] = defaultdict(list)
    for edge in edges.values():
        by_way[edge.way_id].append(edge.edge_id)
    lines: list[list[tuple[float, float]]] = []
    for way_edges in by_way.values():
        for chain in _way_chains(way_edges, edges):
            line: list[tuple[float, float]] = []
            for edge_id, forward in chain:
                points = [(p[0], p[1]) for p in network.unpack_edge_points(edges[edge_id].blob)]
                line.extend(points if forward else reversed(points))
            lines.append(line)
    tolerance = 10.0
    while True:
        simplified = [routes_spatial.simplify_polyline(line, tolerance) for line in lines]
        if sum(len(line) for line in simplified) <= max_points or tolerance > 2000.0:
            return [[[round(lat, 6), round(lng, 6)] for lat, lng in line] for line in simplified]
        tolerance *= 1.6


def _load_edges_by_id(conn: sqlite3.Connection, edge_ids: Sequence[int]) -> dict[int, _Edge]:
    edges: dict[int, _Edge] = {}
    for start in range(0, len(edge_ids), 500):
        chunk = edge_ids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for r in conn.execute(f"SELECT * FROM net_edges WHERE edge_id IN ({placeholders})", chunk).fetchall():
            edge = _Edge(
                r["edge_id"], r["way_id"], r["way_name"], r["highway"], r["surface_class"], r["from_node_id"],
                r["to_node_id"], r["length_m"], bool(r["has_elevation"]), None, bytes(r["points_blob"]),
            )
            edges[edge.edge_id] = edge
    return edges


# ---------------------------------------------------------------------------
# Adjustments and exclusions
# ---------------------------------------------------------------------------

FACTOR_MIN = 0.5
FACTOR_MAX = 2.0
ADJUSTMENT_SOURCES = ("athlete", "research")


class Judgment(TypedDict):
    judgment_id: int
    judgment: str
    section_kind: str
    factor: float | None
    source: str
    note: str | None
    url: str | None
    ways: dict[int, float]


def _all_judgments(conn: sqlite3.Connection) -> list[Judgment]:
    rows = conn.execute("SELECT * FROM net_section_judgments ORDER BY judgment_id").fetchall()
    ways: dict[int, dict[int, float]] = defaultdict(dict)
    for r in conn.execute("SELECT judgment_id, way_id, length_m FROM net_section_judgment_ways").fetchall():
        ways[r["judgment_id"]][r["way_id"]] = r["length_m"]
    return [
        {
            "judgment_id": r["judgment_id"], "judgment": r["judgment"], "section_kind": r["section_kind"],
            "factor": r["factor"], "source": r["source"], "note": r["note"], "url": r["url"],
            "ways": ways[r["judgment_id"]],
        }
        for r in rows
    ]


def judgment_matches(judgment: Judgment, section: Section) -> bool:
    """Whether a stored judgment belongs to a section: same kind, and each
    covers at least REATTACH_MIN_SHARE of the other's length through the
    ways they share."""
    if judgment["section_kind"] != section.kind:
        return False
    mine, theirs = judgment["ways"], section.way_lengths
    shared = mine.keys() & theirs.keys()
    if not shared:
        return False
    total_mine, total_theirs = sum(mine.values()), sum(theirs.values())
    return (
        total_mine > 0 and total_theirs > 0
        and sum(mine[w] for w in shared) / total_mine >= REATTACH_MIN_SHARE
        and sum(theirs[w] for w in shared) / total_theirs >= REATTACH_MIN_SHARE
    )


class StoredAdjustment(TypedDict):
    factor: float
    source: str
    note: str | None
    url: str | None


class SectionJudgments(TypedDict):
    adjustment: StoredAdjustment | None
    exclusion: dict | None  # note, source


def judgments_for(conn: sqlite3.Connection, sections: Sequence[Section]) -> dict[int, SectionJudgments]:
    """The newest stored adjustment and exclusion matching each section."""
    stored = _all_judgments(conn)
    out: dict[int, SectionJudgments] = {}
    for section in sections:
        found: SectionJudgments = {"adjustment": None, "exclusion": None}
        for j in stored:  # oldest first, so a later one replaces an earlier
            if not judgment_matches(j, section):
                continue
            if j["judgment"] == "adjust":
                found["adjustment"] = {
                    "factor": cast(float, j["factor"]), "source": j["source"], "note": j["note"], "url": j["url"],
                }
            else:
                found["exclusion"] = {"note": j["note"], "source": j["source"]}
        out[section.section_id] = found
    return out


def excluded_way_ids(conn: sqlite3.Connection) -> set[int]:
    """Every way of a section the athlete has excluded."""
    rows = conn.execute(
        """
        SELECT DISTINCT w.way_id FROM net_section_judgment_ways w
        JOIN net_section_judgments j USING (judgment_id) WHERE j.judgment = 'exclude'
        """
    ).fetchall()
    return {r["way_id"] for r in rows}


def _drop_matching(conn: sqlite3.Connection, section: Section, judgment: str) -> None:
    for j in _all_judgments(conn):
        if j["judgment"] == judgment and judgment_matches(j, section):
            conn.execute("DELETE FROM net_section_judgment_ways WHERE judgment_id = ?", [j["judgment_id"]])
            conn.execute("DELETE FROM net_section_judgments WHERE judgment_id = ?", [j["judgment_id"]])


def _store(
    conn: sqlite3.Connection, section: Section, judgment: str, factor: float | None, source: str,
    note: str | None, url: str | None,
) -> None:
    _drop_matching(conn, section, judgment)
    cur = conn.execute(
        """
        INSERT INTO net_section_judgments (judgment, section_kind, factor, source, note, url, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [judgment, section.kind, factor, source, note, url, datetime.now(timezone.utc).isoformat()],
    )
    conn.executemany(
        "INSERT INTO net_section_judgment_ways (judgment_id, way_id, length_m) VALUES (?, ?, ?)",
        [(cur.lastrowid, way_id, length) for way_id, length in section.way_lengths.items()],
    )
    conn.commit()


def clamp_factor(factor: float) -> float:
    if not isinstance(factor, (int, float)) or math.isnan(factor):
        raise ValueError("factor must be a number")
    return max(FACTOR_MIN, min(FACTOR_MAX, float(factor)))


def adjust_section(
    conn: sqlite3.Connection, section_id: int, factor: float, note: str | None, source: str = "athlete",
    url: str | None = None,
) -> dict:
    """Store a bounded factor for a section, replacing any earlier one."""
    if source not in ADJUSTMENT_SOURCES:
        raise ValueError(f"source must be one of {', '.join(ADJUSTMENT_SOURCES)}")
    if source == "research" and not url:
        raise ValueError("a research adjustment needs a url for the source")
    section = get_section(conn, section_id)
    clamped = clamp_factor(factor)
    _store(conn, section, "adjust", clamped, source, note, url)
    return {"section_id": section_id, "name": section.name, "factor": clamped, "requested_factor": factor, "source": source, "note": note, "url": url}


def exclude_section(conn: sqlite3.Connection, section_id: int, note: str | None) -> dict:
    """Keep a section's ways out of every route search until undone."""
    section = get_section(conn, section_id)
    _store(conn, section, "exclude", None, "athlete", note, None)
    return {"section_id": section_id, "name": section.name, "excluded": True, "note": note}


def clear_section_adjustment(conn: sqlite3.Connection, section_id: int, exclusion: bool = False) -> dict:
    """Remove a section's stored adjustment; with exclusion=True, its exclusion too."""
    section = get_section(conn, section_id)
    _drop_matching(conn, section, "adjust")
    if exclusion:
        _drop_matching(conn, section, "exclude")
    conn.commit()
    return {"section_id": section_id, "name": section.name, "adjustment_cleared": True, "exclusion_cleared": exclusion}
