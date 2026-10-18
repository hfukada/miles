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

Drafts that cover the same stretch are then merged: drafts whose shared length
is at least MERGE_MIN_SHARE of the shorter one's join (transitively) into one
group over the union of their edges. The group keeps every contributing kind
(`kinds`, the longest contributor's first and its `kind`), named for the best
named contributor; `parallel_m` records the length that duplicates a parallel
path beside it (both banks of a waterway), which a runner covers only one of.
The pieces of one route relation that meet within ROUTE_JOIN_GAP_M along the
network are joined, with the edges that connect them, before any of this.

Two levels are stored: groups, which the survey ranks as an overview, and the
stretches each group is cut into, the units a runner picks between and route
assembly runs along (`building_blocks`). A group's longest connected run (or,
where paths run beside one another, the longest of them) is its spine, a path
ordered west to east; every other edge of the group belongs to the part of the
spine nearest it, so a stretch of a river spans both banks between the same
cut points. The spine is cut at bridges and tunnels, then roads that meet it
(busy ones ahead of quiet streets), trailheads and parking, junctions with
other ways, and changes of the path's name, in that priority. Intervals under
STRETCH_MIN_M are merged into a neighbor, dropping the lower-priority cut, and
intervals over STRETCH_MAX_M are split at the junction (or node) nearest their
middle. A cut is named for the road that meets it, else the bridge, trailhead
or parking, park, or differently named way there, else by how far along the
spine it lies. A branch off the spine of at least BRANCH_MIN_M is a unit of its
own; a shorter one is a spur, noted on the stretch it hangs from and counted in
its length.

A mesh of trails (a trail system whose longest path is under MESH_SPINE_SHARE of
its length, with no banks beside the spine) is instead divided into regions:
its edges are cut at access points, roads and hubs into connected parts, small
ones joined to a neighbor and large ones split to REGION_MIN_M..REGION_MAX_M of
trail, each region named for its main trails and walked as a few trails.

A stretch stores what a group does, plus its ordered chains (one per side, or
the trails through a region), end nodes, the sides it spans, spur notes, and a
role naming its place in the group; its single-path length counts the paths
beside its spine once, and a group's single-path length is the sum of its
stretches'.

Surface mix separates `singletrack` from `track` (network.py: _trail_kind)
among trail edges; every other edge reports its surface class.

A section stores only what the map decides: length, surface mix, continuity
(junctions and busy-road crossings), scenery tags, a grade histogram over the
terrain model's bins (read through the athlete's model at query time), gain,
and the entry nodes where it meets the rest of the network.

Adjustments and exclusions are athlete or research judgments keyed by OSM way
ids, never section ids, so they re-attach after a rebuild to the group or
stretch of the same level and kind that shares most of their ways by length in
both directions.
"""

import bisect
import heapq
import json
import math
import sqlite3
import struct
from collections import Counter, defaultdict, deque
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Hashable, cast
from typing_extensions import NotRequired, TypedDict

from . import gpx_parse, network, routes_spatial, terrain, tilecache

KIND_TRAIL_SYSTEM = "trail_system"
KIND_WATER_PATH = "water_path"
KIND_NAMED_ROUTE = "named_route"
KIND_PARK_PATHS = "park_paths"
KIND_QUIET_ROAD = "quiet_road"
SECTION_KINDS = (KIND_TRAIL_SYSTEM, KIND_WATER_PATH, KIND_NAMED_ROUTE, KIND_PARK_PATHS, KIND_QUIET_ROAD)

# A section row is a group (the drafts that cover one stretch of the map,
# stored as the union of their edges) or a stretch of a group.
GROUP = "group"
STRETCH = "stretch"

MIN_SECTION_M = 800.0
TRAIL_SYSTEM_SPLIT_M = 25_000.0
TRAIL_UNIT_SEED_M = 2_500.0
UNIT_TARGET_M = 12_000.0

# Names given to sections with nothing better to call them; a merged section
# takes any other name over these.
NAME_TRAIL_SYSTEM = "Unnamed trail system"
NAME_WATER_PATH = "Unnamed waterside path"
NAME_PARK_PATHS = "Paths in an unnamed park"
NAME_ROUTE = "Unnamed route"
_GENERIC_NAMES = frozenset({NAME_TRAIL_SYSTEM, NAME_WATER_PATH, NAME_PARK_PATHS, NAME_ROUTE})
_PATH_ALONG = "Path along "

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

# Drafts join one group when they share this much of the shorter one's length.
MERGE_MIN_SHARE = 0.5
# Pieces of one route relation this close along the network are one section.
ROUTE_JOIN_GAP_M = 200.0
# A stretch lists at most this many paths beside its own as sides.
MAX_SIDE_CHAINS = 3
# A same-named chain of paths at least PARALLEL_MIN_M long, with at least
# PARALLEL_MIN_SHARE of its length within PARALLEL_NEAR_M of a longer chain of
# the same name, runs parallel to it.
PARALLEL_MIN_M = 400.0
PARALLEL_NEAR_M = 200.0
PARALLEL_MIN_SHARE = 0.5
# A stretch's side path must advance along the spine by this share of its own
# length.
PARALLEL_MIN_PROGRESS = 0.6

# A section's own coverage of a rebuilt section for a stored judgment to
# re-attach: this share of each side's length, by way.
REATTACH_MIN_SHARE = 0.5

_MILE_M = 1609.344
_WATER_DENSIFY_M = 25.0

# Stretches: the cut points of a group, in priority order (a lower number wins
# where several coincide, and is the last to be merged away), and the lengths
# they aim for, measured along the group's spine.
CUT_STRUCTURE = 1  # a bridge or tunnel
CUT_BUSY_ROAD = 2  # a tolerable or uncomfortable road meets the group
CUT_TRAILHEAD = 3  # a trailhead or parking
CUT_JUNCTION = 4  # any other way meets the group at a junction
CUT_NAME_CHANGE = 5  # the path's name changes
CUT_INNER = 6  # a junction inside the group: only used to split a long stretch
CUT_NONE = 7  # a plain node: only used to split a long stretch
STRETCH_MIN_M = 1500.0
STRETCH_MAX_M = 6000.0
# A branch off the spine at least this long becomes a unit of its own; a
# shorter one is a spur, noted on the stretch it hangs from and counted in its
# length.
BRANCH_MIN_M = 3000.0
# A trail system where the longest path through it is under MESH_SPINE_SHARE of
# its length, at least MESH_TRAIL_SHARE of it natural-surface trail and less than
# MESH_PARALLEL_SHARE of it paths beside the spine (the banks of a waterway), is
# a mesh: it is divided into regions, not cut along a spine.
MESH_SPINE_SHARE = 0.5
MESH_TRAIL_SHARE = 0.6
MESH_PARALLEL_SHARE = 0.2
# A region is a connected part of a mesh between access points and hubs, merged
# or split to stay between these lengths; a node where at least HUB_DEGREE of
# the group's edges meet is a hub.
REGION_MIN_M = 3000.0
REGION_MAX_M = 10_000.0
HUB_DEGREE = 4
# A region's traversal is up to this many walks, each at least MIN_CHAIN_M.
REGION_MAX_CHAINS = 8
MIN_CHAIN_M = 300.0
# A spur shorter than SPUR_NOTE_MIN_M is not mentioned; of the rest, the longest
# MAX_SPUR_NOTES are listed on a stretch and the others summed.
SPUR_NOTE_MIN_M = 160.0
MAX_SPUR_NOTES = 3
_SPINE_CELL_M = 100.0
_SPINE_SEARCH_RADII_M = (150.0, 600.0, 2500.0, 10_000.0)

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
    # cannot name a different section after a rebuild. A group row (level
    # 'group') holds the union of the edges of the stretches whose group_id is
    # its section_id.
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
            edges        BLOB NOT NULL,
            level        TEXT NOT NULL DEFAULT 'group',
            group_id     INTEGER
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
            created_at   TEXT NOT NULL,
            level        TEXT NOT NULL DEFAULT 'group'
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
    trail_kind: str | None = None
    structure: str | None = None
    is_trail: bool = False
    is_path: bool = False
    is_quiet_road: bool = False
    is_road: bool = False


def _classify(edge: _Edge) -> None:
    surface, highway = edge.surface_class, edge.highway
    if surface in ("sidewalk", network.ACCESS_EDGE_SURFACE_CLASS):
        return
    edge.is_trail = edge.trail_kind is not None
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
            r["to_node_id"], r["length_m"], bool(r["has_elevation"]), tier, bytes(r["points_blob"]), r["trail_kind"],
            r["structure"],
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
    # Set on a section merged from drafts of more than one kind.
    kinds: NotRequired[list[str]]
    other_names: NotRequired[list[str]]


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
        return NAME_TRAIL_SYSTEM
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
                name = f"{seed} area" if seed else NAME_TRAIL_SYSTEM
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
            name = f"{_PATH_ALONG}{water.most_common(1)[0][0]}" if water else NAME_WATER_PATH
        drafts.append({"kind": KIND_WATER_PATH, "name": name, "edge_ids": piece})
    return drafts


def _join_pieces(graph: _Graph, pieces: list[list[int]]) -> list[list[int]]:
    """Pieces of one route relation that meet within ROUTE_JOIN_GAP_M along
    the network, each group as one list of edges with those that connect its
    pieces. A relation's members leave the cut or have gaps, so one route
    arrives as several pieces."""
    if len(pieces) < 2:
        return pieces
    edges = graph.edges
    piece_of_node: dict[int, int] = {}
    for i, piece in enumerate(pieces):
        for eid in piece:
            for node in (edges[eid].a, edges[eid].b):
                piece_of_node.setdefault(node, i)
    parent = list(range(len(pieces)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    bridges: list[tuple[int, list[int]]] = []
    for i, piece in enumerate(pieces):
        starts = {n for eid in piece for n in (edges[eid].a, edges[eid].b)}
        dist = {n: 0.0 for n in starts}
        via: dict[int, tuple[int, int]] = {}  # node -> (edge, previous node)
        heap = [(0.0, n) for n in sorted(starts)]
        heapq.heapify(heap)
        reached: set[int] = set()
        while heap:
            d, node = heapq.heappop(heap)
            if node in reached or d > dist[node]:
                continue
            reached.add(node)
            owner = piece_of_node.get(node)
            if owner is not None and owner != i:
                if find(owner) != find(i):
                    parent[find(owner)] = find(i)
                path: list[int] = []
                cursor = node
                while cursor in via:
                    eid, cursor = via[cursor]
                    path.append(eid)
                bridges.append((i, path))
                continue
            for eid in graph.at_node[node]:
                edge = edges[eid]
                other = edge.b if edge.a == node else edge.a
                nd = d + edge.length_m
                if nd <= ROUTE_JOIN_GAP_M and nd < dist.get(other, math.inf):
                    dist[other] = nd
                    via[other] = (eid, node)
                    heapq.heappush(heap, (nd, other))
    groups: dict[int, set[int]] = defaultdict(set)
    for i, piece in enumerate(pieces):
        groups[find(i)].update(piece)
    for i, path in bridges:
        groups[find(i)].update(path)
    joined = [sorted(g) for g in groups.values()]
    joined.sort(key=lambda p: (-graph.length(p), min(p)))
    return joined


def _draft_named_routes(graph: _Graph, scenery: Scenery) -> list[SectionDraft]:
    by_way: dict[int, list[int]] = defaultdict(list)
    for edge in graph.edges.values():
        if edge.is_path or edge.is_road:
            by_way[edge.way_id].append(edge.edge_id)
    drafts: list[SectionDraft] = []
    for _rel_id, (rel_name, members) in sorted(scenery.routes.items()):
        edge_ids = [e for w in sorted(members) for e in by_way.get(w, [])]
        pieces = [p for p in _join_pieces(graph, graph.components(edge_ids)) if graph.length(p) >= MIN_SECTION_M]
        for n, piece in enumerate(pieces, start=1):
            name = rel_name or _top_name([graph.edges[e] for e in piece]) or NAME_ROUTE
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
                "name": f"Paths in {area_name}" if area_name else NAME_PARK_PATHS,
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


def _name_rank(name: str) -> int:
    """2 for a name from the map, 1 for one made from nearby water, 0 for none."""
    if name in _GENERIC_NAMES:
        return 0
    return 1 if name.startswith(_PATH_ALONG) else 2


def _merge_drafts(graph: _Graph, drafts: list[SectionDraft]) -> list[SectionDraft]:
    """One draft per group: drafts that share MERGE_MIN_SHARE of the shorter
    one's length are joined, transitively, over the union of their edges."""
    lengths = [graph.length(d["edge_ids"]) for d in drafts]
    at_edge: dict[int, list[int]] = defaultdict(list)
    for i, draft in enumerate(drafts):
        for eid in draft["edge_ids"]:
            at_edge[eid].append(i)
    shared: dict[tuple[int, int], float] = defaultdict(float)
    for eid, holders in at_edge.items():
        for k, a in enumerate(holders):
            for b in holders[k + 1:]:
                shared[(a, b)] += graph.edges[eid].length_m
    parent = list(range(len(drafts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for (a, b), meters in shared.items():
        if meters >= MERGE_MIN_SHARE * min(lengths[a], lengths[b]):
            parent[max(find(a), find(b))] = min(find(a), find(b))
    clusters: dict[int, list[SectionDraft]] = defaultdict(list)
    for i, draft in enumerate(drafts):
        clusters[find(i)].append(draft)
    return [_group_draft(graph, members) for _root, members in sorted(clusters.items())]


def _group_draft(graph: _Graph, members: list[SectionDraft]) -> SectionDraft:
    """The draft of the group a cluster of drafts forms."""
    if len(members) == 1:
        return members[0]
    order = {kind: i for i, kind in enumerate(SECTION_KINDS)}
    by_kind: dict[str, set[int]] = defaultdict(set)
    for d in members:
        by_kind[d["kind"]].update(d["edge_ids"])
    kinds = sorted(by_kind, key=lambda k: (-graph.length(by_kind[k]), order[k]))
    lead = max(
        members,
        key=lambda d: (
            _name_rank(d["name"]), d["kind"] == KIND_NAMED_ROUTE, graph.length(d["edge_ids"]), -order[d["kind"]], d["name"]
        ),
    )
    return {
        "kind": kinds[0],
        "name": lead["name"],
        "edge_ids": sorted(set().union(*by_kind.values())),
        "kinds": kinds,
        "other_names": sorted({d["name"] for d in members} - {lead["name"]})[:3],
    }


def _chains(graph: _Graph, edge_ids: Iterable[int]) -> list[list[int]]:
    """Path edges as runs of one name (unnamed ways together), joined through
    shared nodes, at least PARALLEL_MIN_M long, longest first."""
    by_name: dict[str | None, list[int]] = defaultdict(list)
    for eid in edge_ids:
        edge = graph.edges[eid]
        if edge.is_path:
            by_name[edge.name].append(eid)
    chains = [c for ids in by_name.values() for c in graph.components(ids) if graph.length(c) >= PARALLEL_MIN_M]
    chains.sort(key=lambda c: (-graph.length(c), min(c)))
    return chains


def _parallel_chains(graph: _Graph, edge_ids: Iterable[int]) -> list[list[int]]:
    """Chains of paths that run beside one another, whatever they are named
    (the two banks of a waterway): the longest first, then each chain with at
    least PARALLEL_MIN_SHARE of its length within PARALLEL_NEAR_M of the
    chains before it. Empty when nothing runs parallel."""
    chains = _chains(graph, edge_ids)
    if len(chains) < 2:
        return []
    index = routes_spatial.NearestIndex(cell_size_m=PARALLEL_NEAR_M)
    for eid in chains[0]:
        network.index_edge(index, eid, graph.edges[eid].blob)
    found = [chains[0]] + [c for c in chains[1:] if _sample_share_near(graph, c, index, PARALLEL_NEAR_M) >= PARALLEL_MIN_SHARE]
    return found if len(found) >= 2 else []


def _centroid(graph: _Graph, edge_ids: Iterable[int]) -> tuple[float, float]:
    lat = lng = weight = 0.0
    for eid in edge_ids:
        points = network.unpack_edge_points(graph.edges[eid].blob)
        mid = points[len(points) // 2]
        w = graph.edges[eid].length_m
        lat, lng, weight = lat + mid[0] * w, lng + mid[1] * w, weight + w
    return (lat / weight, lng / weight) if weight else (0.0, 0.0)


def _compass(d_lat: float, d_lng: float, lat: float) -> str:
    """The cardinal direction an offset points toward."""
    d_lng_m = d_lng * math.cos(math.radians(lat))
    if abs(d_lat) >= abs(d_lng_m):
        return "north" if d_lat > 0 else "south"
    return "east" if d_lng_m > 0 else "west"


def _side_labels(graph: _Graph, chains: Sequence[Iterable[int]]) -> list[str]:
    """The compass side of the chains' common centre that each lies on,
    measured along the axis they are spread along."""
    centres = [_centroid(graph, c) for c in chains]
    mean_lat = sum(c[0] for c in centres) / len(centres)
    mean_lng = sum(c[1] for c in centres) / len(centres)
    spread_lat = max(c[0] for c in centres) - min(c[0] for c in centres)
    spread_lng = (max(c[1] for c in centres) - min(c[1] for c in centres)) * math.cos(math.radians(mean_lat))
    return [
        _compass(
            lat - mean_lat if spread_lat >= spread_lng else 0.0,
            lng - mean_lng if spread_lat < spread_lng else 0.0,
            mean_lat,
        )
        for lat, lng in centres
    ]


def _describe(
    graph: _Graph,
    draft: SectionDraft,
    scenery: Scenery,
    edge_scenery: Mapping[int, EdgeScenery],
    index: SceneryIndex,
    grades: Mapping[int, _GradeStats],
    parallel_m: float = 0.0,
) -> dict:
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
        surface[e.trail_kind or e.surface_class] += e.length_m
    ways = [[way_id, way_length[way_id], way_name[way_id]] for way_id in sorted(way_length, key=lambda w: (-way_length[w], w))]
    kinds = draft.get("kinds") or [draft["kind"]]

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

    south = west = math.inf
    north = east = -math.inf
    for eid in ids:
        for lat, lng, _d, _e in network.unpack_edge_points(edges[eid].blob):
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
        "centroid": _centroid(graph, ids),
        "bbox": (south, west, north, east),
        "data": {
            "ways": ways,
            "kinds": kinds,
            "other_names": draft.get("other_names", []),
            "parallel_m": parallel_m,
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


# ---------------------------------------------------------------------------
# Stretches
# ---------------------------------------------------------------------------


@dataclass
class _Stretch:
    edge_ids: list[int]
    chains: list[list[int]]
    end_nodes: tuple[int, int]
    sides: list[str]
    single_path_m: float
    edge_length_m: float
    role: str
    notes: list[str] = field(default_factory=list)


@dataclass
class _Cut:
    priority: int
    # None where no name fits; the stretch's role then gives the position.
    label: str | None
    distance_m: float


@dataclass
class _Spine:
    """The through-path of a connected run of edges: its nodes in order, the
    distance along it to each, and an index answering "how far along the
    spine is the point nearest here"."""
    steps: list[tuple[int, bool]]
    nodes: list[int]
    cum: list[float]
    index: routes_spatial.NearestIndex

    def locate(self, lat: float, lng: float) -> tuple[float, float]:
        """(distance along the spine, distance from it) of the spine point
        nearest here."""
        for radius in _SPINE_SEARCH_RADII_M:
            hit = self.index.nearest(lat, lng, radius)
            if hit is not None:
                return hit[1], hit[2]
        return 0.0, math.inf

    def position(self, lat: float, lng: float) -> float:
        return self.locate(lat, lng)[0]


def _node_coords(graph: _Graph, edge_ids: Iterable[int]) -> dict[int, tuple[float, float]]:
    coords: dict[int, tuple[float, float]] = {}
    for eid in edge_ids:
        edge = graph.edges[eid]
        points = network.unpack_edge_points(edge.blob)
        coords.setdefault(edge.a, (points[0][0], points[0][1]))
        coords.setdefault(edge.b, (points[-1][0], points[-1][1]))
    return coords


def _longest_path(graph: _Graph, edge_ids: Sequence[int]) -> list[tuple[int, bool]]:
    """A connected set of edges as one ordered path of (edge_id, forward):
    the whole cycle when the edges form one, otherwise the longest path between
    two of its nodes, found by two shortest-path sweeps (exact for a tree,
    close for anything else). Edges off the path are left out."""
    edges = graph.edges
    adj: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for eid in edge_ids:
        edge = edges[eid]
        adj[edge.a].append((eid, edge.b))
        if edge.b != edge.a:
            adj[edge.b].append((eid, edge.a))
    if all(sum(2 if edges[eid].a == edges[eid].b else 1 for eid, _o in around) == 2 for around in adj.values()):
        return _way_chains(edge_ids, edges)[0]

    def sweep(source: int) -> tuple[int, dict[int, tuple[int, int]]]:
        dist = {source: 0.0}
        prev: dict[int, tuple[int, int]] = {}
        heap = [(0.0, source)]
        while heap:
            d, node = heapq.heappop(heap)
            if d > dist.get(node, math.inf):
                continue
            for eid, other in adj[node]:
                nd = d + edges[eid].length_m
                if nd < dist.get(other, math.inf):
                    dist[other] = nd
                    prev[other] = (node, eid)
                    heapq.heappush(heap, (nd, other))
        far = min(dist, key=lambda n: (-dist[n], n))
        return far, prev

    first, _ = sweep(min(adj))
    last, prev = sweep(first)
    steps = []
    node = last
    while node in prev:
        before, eid = prev[node]
        steps.append((eid, edges[eid].a == before))
        node = before
    steps.reverse()
    return steps


def _ends(graph: _Graph, steps: Sequence[tuple[int, bool]]) -> tuple[int, int]:
    """The nodes a path of (edge_id, forward) steps starts and ends at."""
    first, last = graph.edges[steps[0][0]], graph.edges[steps[-1][0]]
    return (first.a if steps[0][1] else first.b), (last.b if steps[-1][1] else last.a)


def _make_spine(graph: _Graph, edge_ids: Sequence[int]) -> _Spine:
    """The spine of a connected set of edges, running west to east (south to
    north where it runs more north-south), so that stretches come in a
    stable order."""
    steps = _longest_path(graph, edge_ids)
    start_node, end_node = _ends(graph, steps)
    if start_node != end_node:
        coords = _node_coords(graph, [steps[0][0], steps[-1][0]])
        (start_lat, start_lng), (end_lat, end_lng) = coords[start_node], coords[end_node]
        d_lng = (end_lng - start_lng) * math.cos(math.radians(start_lat))
        if (d_lng < 0) if abs(d_lng) >= abs(end_lat - start_lat) else (end_lat < start_lat):
            steps = [(eid, not forward) for eid, forward in reversed(steps)]
    nodes = [_ends(graph, steps)[0]]
    cum = [0.0]
    index = routes_spatial.NearestIndex(cell_size_m=_SPINE_CELL_M)
    for eid, forward in steps:
        edge = graph.edges[eid]
        points = network.unpack_edge_points(edge.blob)
        base = cum[-1]
        index.add(eid, (
            (lat, lng, base + (d if forward else edge.length_m - d)) for lat, lng, d, _e in points
        ))
        nodes.append(edge.b if forward else edge.a)
        cum.append(base + edge.length_m)
    return _Spine(steps, nodes, cum, index)


def _sample_share_near(graph: _Graph, edge_ids: Iterable[int], index: routes_spatial.NearestIndex, radius_m: float) -> float:
    near = weight = 0.0
    for eid in edge_ids:
        points = network.unpack_edge_points(graph.edges[eid].blob)
        sampled = points[::5] or points
        hits = sum(1 for lat, lng, _d, _e in sampled if index.nearest(lat, lng, radius_m) is not None)
        near += graph.edges[eid].length_m * hits / len(sampled)
        weight += graph.edges[eid].length_m
    return near / weight if weight else 0.0


def _spine_unit(graph: _Graph, edge_ids: Sequence[int]) -> _Spine:
    """The spine of a connected run of edges: where it holds paths that run
    beside one another, the longest of them, so a ring of two banks and the
    rungs between them is not walked as a loop."""
    parallel = _parallel_chains(graph, edge_ids)
    return _make_spine(graph, parallel[0] if parallel else edge_ids)


_TIER_RANK = {"uncomfortable": 0, "tolerable": 1, "comfortable": 2}


def _node_label(
    graph: _Graph, node: int, members: set[int], spine: Collection[int], amenity: tuple[str, str | None] | None,
    edge_scenery: Mapping[int, EdgeScenery], group_name: str,
) -> str | None:
    """What to call the place a unit is cut at, in order: the name of a road
    that meets it (the busiest first), of a bridge or tunnel it carries, of a
    trailhead or parking there, of the park the path enters or leaves, and of
    a way it joins that is named differently from the path and the group.
    None when none of these fit."""
    edges = graph.edges
    around = graph.at_node[node]
    inside = [edges[e] for e in around if e in members]
    outside = [edges[e] for e in around if e not in members]
    roads = [e for e in outside if e.is_road and e.name]
    if roads:
        return min(roads, key=lambda e: (_TIER_RANK.get(cast(str, e.tier), 3), cast(str, e.name))).name
    own = {edges[e].name for e in around if e in spine and not edges[e].structure} | {group_name}
    bridge = next((e.name for e in inside if e.structure and e.name and e.name not in own), None)
    if bridge:
        return bridge
    parks = {edge_scenery[e.edge_id].area_name for e in inside}
    park = next(iter(sorted(p for p in parks if p)), None) if len(parks) > 1 or amenity is not None else None
    if amenity is not None:
        return amenity[1] or (f"{park} {amenity[0]}" if park else amenity[0])
    if park:
        return park
    joined = sorted({
        e.name for e in (*inside, *outside)
        if e.name and e.name not in own and e.surface_class != network.ACCESS_EDGE_SURFACE_CLASS
    })
    return f"junction with {joined[0]}" if joined else None


def _cut_candidates(
    graph: _Graph, members: set[int], spine: _Spine, amenities: Mapping[int, tuple[str, str | None]],
    coords: Mapping[int, tuple[float, float]], edge_scenery: Mapping[int, EdgeScenery], group_name: str,
) -> dict[int, _Cut]:
    """Where a unit could be cut, keyed by the index of the nearest spine node
    (the ends excluded): at most one per node, the best by priority, with the
    distance from the node to the spine it was snapped from."""
    edges = graph.edges
    on_spine = {n: i for i, n in reversed(list(enumerate(spine.nodes)))}
    spine_edges = {eid for eid, _f in spine.steps}
    found: dict[int, _Cut] = {}

    def offer(node: int, priority: int, label: str | None) -> None:
        if node in on_spine:
            idx, dist = on_spine[node], 0.0
        else:
            lat, lng = coords[node]
            pos, dist = spine.locate(lat, lng)
            idx = bisect.bisect_left(spine.cum, pos)
            if idx > 0 and (idx == len(spine.cum) or pos - spine.cum[idx - 1] <= spine.cum[idx] - pos):
                idx -= 1
        if idx <= 0 or idx >= len(spine.nodes) - 1:
            return
        cut = _Cut(priority, label, dist)
        old = found.get(idx)
        if old is None or (cut.priority, cut.distance_m, cut.label or "") < (old.priority, old.distance_m, old.label or ""):
            found[idx] = cut

    nodes = {n for eid in members for n in (edges[eid].a, edges[eid].b)}
    for node in sorted(nodes):
        around = graph.at_node[node]
        inside = [e for e in around if e in members]
        outside = [e for e in around if e not in members]
        amenity = _amenity_at(graph, node, amenities)
        label = _node_label(graph, node, members, spine_edges, amenity, edge_scenery, group_name)
        if any(edges[e].structure for e in inside):
            offer(node, CUT_STRUCTURE, label)
        if any(edges[e].is_road and edges[e].tier in ("tolerable", "uncomfortable") for e in outside):
            offer(node, CUT_BUSY_ROAD, label)
        if amenity is not None:
            offer(node, CUT_TRAILHEAD, label)
        if any(edges[e].surface_class != network.ACCESS_EDGE_SURFACE_CLASS for e in outside) and len(around) >= 3:
            offer(node, CUT_JUNCTION, label)
        elif len(inside) >= 3:
            offer(node, CUT_INNER, label)
    for i in range(1, len(spine.steps)):
        before, after = edges[spine.steps[i - 1][0]].name, edges[spine.steps[i][0]].name
        if before and after and before != after:
            offer(spine.nodes[i], CUT_NAME_CHANGE, after)
    return found


def _amenity_at(
    graph: _Graph, node: int, amenities: Mapping[int, tuple[str, str | None]]
) -> tuple[str, str | None] | None:
    """The trailhead or parking at a node, or at the far end of an access spur from it."""
    amenity = amenities.get(node)
    for e in graph.at_node[node]:
        edge = graph.edges[e]
        far = edge.b if edge.a == node else edge.a
        if edge.surface_class == network.ACCESS_EDGE_SURFACE_CLASS and far in amenities:
            amenity = amenities[far]
    return amenity


def _choose_cuts(cum: Sequence[float], found: Mapping[int, _Cut]) -> list[tuple[int, str | None, int]]:
    """The spine node indices a unit is cut at, as (index, label, priority):
    every candidate of a priority up to CUT_NAME_CHANGE, then the shortest
    interval under STRETCH_MIN_M is merged into a neighbor (dropping the
    lower-priority of its two cuts, then the one that leaves the shorter
    merged interval) until none is left, then any interval over STRETCH_MAX_M
    is split at the candidate nearest its middle that leaves both halves at
    least STRETCH_MIN_M, or at the node nearest the middle if there is none."""
    last = len(cum) - 1
    cuts = sorted(i for i, c in found.items() if c.priority <= CUT_NAME_CHANGE)
    while cuts:
        stops = [0, *cuts, last]
        lengths = [cum[b] - cum[a] for a, b in zip(stops, stops[1:])]
        short = [k for k, length in enumerate(lengths) if length < STRETCH_MIN_M]
        if not short:
            break
        k = min(short, key=lambda k: (lengths[k], k))
        options = [j for j in (k - 1, k) if 0 <= j < len(cuts)]
        drop = min(options, key=lambda j: (-found[cuts[j]].priority, lengths[j] + lengths[j + 1], j))
        del cuts[drop]
    labels: dict[int, tuple[str | None, int]] = {i: (found[i].label, found[i].priority) for i in cuts}
    work = deque(zip([0, *cuts], [*cuts, last]))
    while work:
        a, b = work.popleft()
        if cum[b] - cum[a] <= STRETCH_MAX_M:
            continue
        middle = (cum[a] + cum[b]) / 2.0
        roomy = [i for i in range(a + 1, b) if cum[i] - cum[a] >= STRETCH_MIN_M and cum[b] - cum[i] >= STRETCH_MIN_M]
        if not roomy:
            continue
        known = [i for i in roomy if i in found]
        if known:
            pick = min(known, key=lambda i: (abs(cum[i] - middle), found[i].priority, i))
            labels[pick] = (found[pick].label, found[pick].priority)
        else:
            pick = min(roomy, key=lambda i: (abs(cum[i] - middle), i))
            labels[pick] = (None, CUT_NONE)
        work.append((a, pick))
        work.append((pick, b))
    return [(i, *labels[i]) for i in sorted(labels)]


def _stretch_sides(
    graph: _Graph, spine: _Spine, stretch_steps: Sequence[tuple[int, bool]], others: Sequence[int]
) -> tuple[list[list[int]], float, set[int]]:
    """The paths running beside a stretch's spine part: the longest
    MAX_SIDE_CHAINS of them as ordered edge chains in the spine's direction,
    the length of all the other paths' runs, which a runner covers only one of,
    and every edge of those runs. A run counts only if it advances along the
    spine for at least PARALLEL_MIN_PROGRESS of its length, so a switchback or
    a loop beside the spine is not taken for a second bank."""
    near = routes_spatial.NearestIndex(cell_size_m=PARALLEL_NEAR_M)
    for eid, _f in stretch_steps:
        network.index_edge(near, eid, graph.edges[eid].blob)
    beside = [
        e for e in others
        if graph.edges[e].is_path and _sample_share_near(graph, [e], near, PARALLEL_NEAR_M) >= PARALLEL_MIN_SHARE
    ]
    runs: list[tuple[float, list[int]]] = []
    lengths = [sum(graph.edges[e].length_m for e, _f in stretch_steps)]
    for run in graph.components(beside):
        if graph.length(run) < PARALLEL_MIN_M:
            continue
        steps = _longest_path(graph, run)
        start, end = _ends(graph, steps)
        coords = _node_coords(graph, [steps[0][0], steps[-1][0]])
        start_pos, end_pos = spine.position(*coords[start]), spine.position(*coords[end])
        length = sum(graph.edges[e].length_m for e, _f in steps)
        if abs(end_pos - start_pos) < PARALLEL_MIN_PROGRESS * length:
            continue
        if start_pos > end_pos:
            steps = [(eid, not forward) for eid, forward in reversed(steps)]
        runs.append((length, [eid for eid, _f in steps]))
        lengths.append(length)
    duplicated = sum(lengths) - max(lengths)
    chains = [chain for _length, chain in sorted(runs, key=lambda r: -r[0])[:MAX_SIDE_CHAINS]]
    return chains, duplicated, {e for _length, chain in runs for e in chain}


def _unique(labels: Sequence[str]) -> list[str]:
    seen: Counter[str] = Counter()
    out = []
    for label in labels:
        seen[label] += 1
        out.append(label if seen[label] == 1 else f"{label} {seen[label]}")
    return out


def _branch_tag(graph: _Graph, members: set[int], branch: Sequence[int]) -> str:
    """What a branch hangs off, for its stretches' roles: "branch off <name
    of the way it leaves>", or just "branch"."""
    inside = set(branch)
    names: Counter[str] = Counter()
    for eid in branch:
        for node in (graph.edges[eid].a, graph.edges[eid].b):
            for other in graph.at_node[node]:
                if other in members and other not in inside and graph.edges[other].name:
                    names[cast(str, graph.edges[other].name)] += 1
    return f"branch off {names.most_common(1)[0][0]}" if names else "branch"


def _spur_note(graph: _Graph, branch: Sequence[int], unit_lat: float, unit_lng: float) -> tuple[float, str]:
    """A spur's length and the note it leaves on the stretch it hangs from."""
    length = graph.length(branch)
    name = _top_name([graph.edges[e] for e in branch])
    if name is None:
        lat, lng = _centroid(graph, branch)
        name = f"the {_compass(lat - unit_lat, lng - unit_lng, unit_lat)}"
    return length, f"{name} ({length / _MILE_M:.1f} mi)"


def _spur_notes(spurs: Sequence[tuple[float, str]]) -> list[str]:
    """The longest MAX_SPUR_NOTES spurs as "includes spur to ..." notes, the
    rest summed."""
    ordered = sorted((sp for sp in spurs if sp[0] >= SPUR_NOTE_MIN_M), key=lambda sp: (-sp[0], sp[1]))
    notes = [f"includes spur to {text}" for _length, text in ordered[:MAX_SPUR_NOTES]]
    rest = ordered[MAX_SPUR_NOTES:]
    if rest:
        notes.append(f"includes {len(rest)} more spurs ({sum(length for length, _t in rest) / _MILE_M:.1f} mi)")
    return notes


def _position_label(cum: Sequence[float], index: int) -> str:
    """A cut with no name, by how far along its spine it lies."""
    share = cum[index] / cum[-1] if cum[-1] > 0 else 0.5
    return "midpoint" if abs(share - 0.5) <= 0.05 else f"mile {cum[index] / _MILE_M:.1f}"


def _role(k: int, n: int, start_dir: str, end_dir: str, start_label: str | None, end_label: str | None) -> str:
    """A stretch's place among the n of its unit, by the places it runs between."""
    if n == 1:
        return "whole"
    if k == 0:
        return f"{start_dir} end to {end_label}"
    if k == n - 1:
        return f"{start_label} to {end_dir} end"
    return f"between {start_label} and {end_label}"


def _cut_stretches(
    graph: _Graph, edge_ids: Sequence[int], amenities: Mapping[int, tuple[str, str | None]],
    edge_scenery: Mapping[int, EdgeScenery], group_name: str, kinds: Sequence[str],
) -> list[_Stretch]:
    """A group's stretches in position order: along its spine, or for a mesh of
    trails (_is_mesh) as regions."""
    along = _cut_along_spines(graph, edge_ids, amenities, edge_scenery, group_name)
    if _is_mesh(graph, edge_ids, kinds):
        beside = sum(st.edge_length_m - st.single_path_m for st in along)
        if beside < MESH_PARALLEL_SHARE * graph.length(edge_ids):
            group_lat, group_lng = _centroid(graph, edge_ids)
            return _regions(graph, edge_ids, amenities, group_lat, group_lng)
    return along


def _cut_along_spines(
    graph: _Graph, edge_ids: Sequence[int], amenities: Mapping[int, tuple[str, str | None]],
    edge_scenery: Mapping[int, EdgeScenery], group_name: str,
) -> list[_Stretch]:
    """Each unit is cut at the best access and decision points along its
    spine, and its other edges fall in the stretch whose part of the spine
    lies nearest them, except a branch of at least BRANCH_MIN_M that is not
    beside the spine: that becomes a unit of its own, listed after the unit it
    hangs from. Shorter branches are noted on their stretch as spurs."""
    all_coords = _node_coords(graph, edge_ids)
    group_lat, group_lng = _centroid(graph, edge_ids)
    components = graph.components(edge_ids)
    out: list[_Stretch] = []
    queue: deque[tuple[_Spine, list[int], str | None]] = deque(
        (_spine_unit(graph, comp), comp, "part" if i else None) for i, comp in enumerate(components)
    )
    while queue:
        spine, members, how = queue.popleft()
        member_set = set(members)
        found = _cut_candidates(graph, member_set, spine, amenities, all_coords, edge_scenery, group_name)
        cuts = _choose_cuts(spine.cum, found)
        stops = [0, *(i for i, _l, _p in cuts), len(spine.steps)]
        names = {i: label or _position_label(spine.cum, i) for i, label, _p in cuts}
        repeated = {label for label, times in Counter(names.values()).items() if times > 1}
        names = {i: f"{label} at mile {spine.cum[i] / _MILE_M:.1f}" if label in repeated else label for i, label in names.items()}
        stop_positions = [spine.cum[i] for i in stops[1:-1]]
        spine_edge_ids = {eid for eid, _f in spine.steps}
        off_spine: list[list[int]] = [[] for _ in range(len(stops) - 1)]
        for eid in sorted(member_set - spine_edge_ids):
            points = network.unpack_edge_points(graph.edges[eid].blob)
            mid = points[len(points) // 2]
            off_spine[bisect.bisect_right(stop_positions, spine.position(mid[0], mid[1]))].append(eid)
        unit_lat, unit_lng = _centroid(graph, members)
        start_lat, start_lng = all_coords[spine.nodes[0]]
        end_lat, end_lng = all_coords[spine.nodes[-1]]
        start_dir = _compass(start_lat - unit_lat, start_lng - unit_lng, unit_lat)
        end_dir = _compass(end_lat - unit_lat, end_lng - unit_lng, unit_lat)
        tag = f"{_compass(unit_lat - group_lat, unit_lng - group_lng, group_lat)} {how}" if how else ""
        n = len(off_spine)
        sides = [
            _stretch_sides(graph, spine, spine.steps[a:b], off_spine[k]) for k, (a, b) in enumerate(zip(stops, stops[1:]))
        ]
        beside = {e for _chains, _dup, edges_beside in sides for e in edges_beside}
        spurs: list[list[tuple[float, str]]] = [[] for _ in range(n)]
        folded: list[list[int]] = [[] for _ in range(n)]
        where = {eid: k for k, es in enumerate(off_spine) for eid in es}
        for branch in graph.components([e for es in off_spine for e in es if e not in beside]):
            if graph.length(branch) >= BRANCH_MIN_M:
                queue.append((_spine_unit(graph, branch), branch, _branch_tag(graph, member_set, branch)))
                continue
            by_k: Counter[int] = Counter()
            for e in branch:
                by_k[where[e]] += int(graph.edges[e].length_m)
            k = max(by_k, key=lambda i: (by_k[i], -i))
            folded[k].extend(branch)
            spurs[k].append(_spur_note(graph, branch, unit_lat, unit_lng))
        for k, (a, b) in enumerate(zip(stops, stops[1:])):
            chains, duplicated, beside_k = sides[k]
            primary = [eid for eid, _f in spine.steps[a:b]]
            union = primary + sorted(beside_k) + folded[k]
            role = tag if n == 1 and tag else _role(k, n, start_dir, end_dir, names.get(a), names.get(b))
            out.append(_Stretch(
                sorted(union), [primary, *chains], (spine.nodes[a], spine.nodes[b]),
                _unique(_side_labels(graph, [primary, *chains])) if chains else [],
                graph.length(union) - duplicated, graph.length(union), f"{tag}: {role}" if tag and n > 1 else role,
                _spur_notes(spurs[k]),
            ))
    return out


# ---------------------------------------------------------------------------
# Regions of a mesh
# ---------------------------------------------------------------------------


def _is_mesh(graph: _Graph, edge_ids: Sequence[int], kinds: Sequence[str]) -> bool:
    """Whether a group is a mesh of trails (see MESH_SPINE_SHARE)."""
    group_m = graph.length(edge_ids)
    if KIND_TRAIL_SYSTEM not in kinds or group_m <= 0:
        return False
    if sum(graph.edges[e].length_m for e in edge_ids if graph.edges[e].is_trail) < MESH_TRAIL_SHARE * group_m:
        return False
    steps = _longest_path(graph, graph.components(edge_ids)[0])
    return sum(graph.edges[e].length_m for e, _f in steps) < MESH_SPINE_SHARE * group_m


def _anchors(
    graph: _Graph, edge_ids: Collection[int], amenities: Mapping[int, tuple[str, str | None]]
) -> set[int]:
    """The nodes a mesh is cut at: trailheads and parking, where a road
    meets it, and hubs where at least HUB_DEGREE of its edges meet."""
    members = set(edge_ids)
    found: set[int] = set()
    for node in {n for e in members for n in (graph.edges[e].a, graph.edges[e].b)}:
        around = graph.at_node[node]
        inside = [e for e in around if e in members]
        if len(inside) >= HUB_DEGREE or _amenity_at(graph, node, amenities) is not None:
            found.add(node)
        elif any(graph.edges[e].is_road for e in around if e not in members):
            found.add(node)
    return found


def _region_edges(graph: _Graph, edge_ids: Collection[int], cut_nodes: Collection[int]) -> list[list[int]]:
    """The edges split into the parts that stay connected without passing
    through a cut node."""
    members = set(edge_ids)
    cuts = set(cut_nodes)
    seen: set[int] = set()
    parts: list[list[int]] = []
    for start in sorted(members):
        if start in seen:
            continue
        seen.add(start)
        part, queue = [], deque([start])
        while queue:
            eid = queue.popleft()
            part.append(eid)
            for node in (graph.edges[eid].a, graph.edges[eid].b):
                if node in cuts:
                    continue
                for other in graph.at_node[node]:
                    if other in members and other not in seen:
                        seen.add(other)
                        queue.append(other)
        parts.append(part)
    return parts


def _neighbors(graph: _Graph, regions: Sequence[Sequence[int]]) -> dict[int, set[int]]:
    """For each region, the regions that share a node with it."""
    at: dict[int, set[int]] = defaultdict(set)
    for i, region in enumerate(regions):
        for e in region:
            at[graph.edges[e].a].add(i)
            at[graph.edges[e].b].add(i)
    out: dict[int, set[int]] = defaultdict(set)
    for owners in at.values():
        for i in owners:
            out[i] |= owners - {i}
    return out


def _merge_small_regions(graph: _Graph, regions: list[list[int]]) -> list[list[int]]:
    """Regions under REGION_MIN_M joined to the shortest neighbor they fit
    with within REGION_MAX_M, smallest first; one with no such neighbor stays."""
    regions = [list(r) for r in regions]
    stuck: set[int] = set()
    while True:
        lengths = [graph.length(r) for r in regions]
        small = [i for i, length in enumerate(lengths) if length < REGION_MIN_M and i not in stuck]
        if not small:
            return regions
        i = min(small, key=lambda i: (lengths[i], min(regions[i])))
        fits = [j for j in _neighbors(graph, regions).get(i, ()) if lengths[i] + lengths[j] <= REGION_MAX_M]
        if not fits:
            stuck.add(i)
            continue
        j = min(fits, key=lambda j: (lengths[j], min(regions[j])))
        regions[j].extend(regions[i])
        del regions[i]
        stuck = {k - (k > i) for k in stuck if k != i}


def _bisect_region(graph: _Graph, region: Sequence[int]) -> list[list[int]]:
    """A region split in two at the median of its edges along the axis it is
    longer on, each half then taken apart into its connected pieces."""
    mids = {}
    for e in region:
        points = network.unpack_edge_points(graph.edges[e].blob)
        mids[e] = points[len(points) // 2]
    mean_lat = sum(m[0] for m in mids.values()) / len(mids)
    span_lat = max(m[0] for m in mids.values()) - min(m[0] for m in mids.values())
    span_lng = (max(m[1] for m in mids.values()) - min(m[1] for m in mids.values())) * math.cos(math.radians(mean_lat))
    axis = 0 if span_lat >= span_lng else 1
    ordered = sorted(region, key=lambda e: (mids[e][axis], e))
    half, running = [], 0.0
    total = graph.length(region)
    for e in ordered:
        if running >= total / 2.0:
            break
        half.append(e)
        running += graph.edges[e].length_m
    rest = [e for e in ordered if e not in set(half)]
    return [*graph.components(half), *graph.components(rest)] if rest else [list(region)]


def _bearings(graph: _Graph, eid: int, forward: bool) -> tuple[float, float]:
    """The compass bearing, in radians, an edge leaves its start with and
    arrives at its end with, walked forward or back."""
    points = network.unpack_edge_points(graph.edges[eid].blob)
    if not forward:
        points = points[::-1]

    def bearing(p: tuple, q: tuple) -> float:
        return math.atan2((q[1] - p[1]) * math.cos(math.radians(p[0])), q[0] - p[0])

    return bearing(points[0], points[1]), bearing(points[-2], points[-1])


def _walk(graph: _Graph, start: int, unused: set[int]) -> list[tuple[int, bool]]:
    """A trail through the unused edges from a node: at each node the edge
    that turns least from the way in (the longest on the first step), the
    lowest id on a tie, until none is left."""
    steps: list[tuple[int, bool]] = []
    node = start
    heading: float | None = None
    while True:
        options = [e for e in graph.at_node[node] if e in unused]
        if not options:
            return steps
        if heading is None:
            eid = min(options, key=lambda e: (-graph.edges[e].length_m, e))
        else:
            ahead = heading
            eid = min(
                options,
                key=lambda e: (abs((_bearings(graph, e, graph.edges[e].a == node)[0] - ahead + math.pi) % (2 * math.pi) - math.pi), e),
            )
        unused.discard(eid)
        forward = graph.edges[eid].a == node
        steps.append((eid, forward))
        heading = _bearings(graph, eid, forward)[1]
        node = graph.edges[eid].b if forward else graph.edges[eid].a


def _traversal(graph: _Graph, edge_ids: Sequence[int]) -> list[list[tuple[int, bool]]]:
    """A region's edges as up to REGION_MAX_CHAINS trails, longest first, each
    at least MIN_CHAIN_M: the first from whichever of a few end points covers
    most, the rest from what that leaves."""
    unused = set(edge_ids)
    chains: list[list[tuple[int, bool]]] = []
    while unused and len(chains) < REGION_MAX_CHAINS:
        degree: Counter[int] = Counter()
        for e in unused:
            degree[graph.edges[e].a] += 1
            degree[graph.edges[e].b] += 1
        starts = sorted(n for n, d in degree.items() if d % 2 == 1) or sorted(degree)
        best: list[tuple[int, bool]] = []
        for node in starts[:4]:
            trial = _walk(graph, node, set(unused))
            if sum(graph.edges[e].length_m for e, _f in trial) > sum(graph.edges[e].length_m for e, _f in best):
                best = trial
        if sum(graph.edges[e].length_m for e, _f in best) < MIN_CHAIN_M:
            break
        chains.append(best)
        unused -= {e for e, _f in best}
    return chains


def _region_role(
    graph: _Graph, edge_ids: Sequence[int], amenities: Mapping[int, tuple[str, str | None]], lat: float, lng: float,
    group_lat: float, group_lng: float,
) -> str:
    """A region by the trails that make most of it, else the access point it
    holds, else which part of the group it is."""
    by_name: Counter[str] = Counter()
    for e in edge_ids:
        if graph.edges[e].name:
            by_name[cast(str, graph.edges[e].name)] += int(graph.edges[e].length_m)
    named = sum(by_name.values())
    if named:
        top = [name for name, _m in sorted(by_name.items(), key=lambda kv: (-kv[1], kv[0]))[:2]]
        if len(top) == 2 and by_name[top[0]] >= 0.6 * named:
            top = top[:1]
        return f"{' and '.join(top)} area"
    nodes = {n for e in edge_ids for n in (graph.edges[e].a, graph.edges[e].b)}
    access = sorted(a[1] for n in nodes if (a := _amenity_at(graph, n, amenities)) is not None and a[1])
    if access:
        return f"area around {access[0]}"
    return f"{_compass(lat - group_lat, lng - group_lng, group_lat)} part"


def _regions(
    graph: _Graph, edge_ids: Sequence[int], amenities: Mapping[int, tuple[str, str | None]],
    group_lat: float, group_lng: float,
) -> list[_Stretch]:
    """A mesh as stretches that are regions: its edges cut at access points
    and hubs into connected parts, small ones joined to a neighbor and large
    ones split, each region a stretch whose chains are trails through it."""
    regions = _merge_small_regions(graph, _region_edges(graph, edge_ids, _anchors(graph, edge_ids, amenities)))
    for _ in range(len(edge_ids)):
        big = [r for r in regions if graph.length(r) > REGION_MAX_M]
        if not big:
            break
        halves = [part for r in big for part in _bisect_region(graph, r)]
        if len(halves) <= len(big):
            break
        regions = _merge_small_regions(graph, [r for r in regions if graph.length(r) <= REGION_MAX_M] + halves)
    centres = {i: _centroid(graph, r) for i, r in enumerate(regions)}
    order = sorted(range(len(regions)), key=lambda i: (centres[i][1], centres[i][0], min(regions[i])))
    roles = [
        "whole" if len(regions) == 1 else _region_role(graph, regions[i], amenities, *centres[i], group_lat, group_lng)
        for i in order
    ]
    taken = Counter(roles)
    roles = _unique([
        role if taken[role] == 1 else f"{role} ({_compass(centres[i][0] - group_lat, centres[i][1] - group_lng, group_lat)})"
        for i, role in zip(order, roles)
    ])
    out: list[_Stretch] = []
    for i, role in zip(order, roles):
        walks = _traversal(graph, regions[i])
        chains = [[e for e, _f in w] for w in walks]
        ends = _ends(graph, walks[0]) if walks else (graph.edges[regions[i][0]].a, graph.edges[regions[i][0]].b)
        length = graph.length(regions[i])
        out.append(_Stretch(sorted(regions[i]), chains, ends, [], length, length, role))
    return out


def rebuild_sections(conn: sqlite3.Connection, area_id: int, scenery: Scenery) -> int:
    """Replace an area's sections from its freshly written graph; the caller
    commits. Each group of overlapping drafts is stored as a group row with
    its stretches. Returns the group count."""
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

    groups = _merge_drafts(graph, [
        *_draft_trail_systems(graph),
        *_draft_water_paths(graph, edge_scenery),
        *_draft_named_routes(graph, scenery),
        *_draft_park_paths(graph, edge_scenery),
        *_draft_quiet_roads(graph),
    ])
    order = {kind: i for i, kind in enumerate(SECTION_KINDS)}
    amenities = {
        r["node_id"]: (r["kind"], r["name"])
        for r in conn.execute(
            "SELECT node_id, kind, name FROM net_nodes WHERE area_id = ? AND kind IN ('trailhead', 'parking')", [area_id]
        )
    }
    planned: list[tuple[SectionDraft, float, list[_Stretch]]] = []
    for group in groups:
        stretches = _cut_stretches(
            graph, group["edge_ids"], amenities, edge_scenery, group["name"], group.get("kinds") or [group["kind"]]
        )
        parallel_m = max(0.0, graph.length(group["edge_ids"]) - sum(st.single_path_m for st in stretches))
        planned.append((group, parallel_m, stretches))
    planned.sort(key=lambda g: (order[g[0]["kind"]], -graph.length(g[0]["edge_ids"]), min(g[0]["edge_ids"])))
    grades = _grade_stats(edges, {e for group, _p, _st in planned for e in group["edge_ids"]})

    delete_area_sections(conn, area_id)

    def insert(draft: SectionDraft, described: dict, level: str, group_id: int | None, extra: Mapping[str, object]) -> int:
        lat, lng = described["centroid"]
        south, west, north, east = described["bbox"]
        cur = conn.execute(
            """
            INSERT INTO net_sections (
                area_id, kind, name, length_m, centroid_lat, centroid_lng, south, west, north, east, data, edges,
                level, group_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                area_id, draft["kind"], described["name"], described["length_m"], lat, lng, south, west, north, east,
                json.dumps({**described["data"], **extra}), _pack_ids(sorted(draft["edge_ids"])), level, group_id,
            ],
        )
        return cast(int, cur.lastrowid)

    count = 0
    for group, parallel_m, stretches in planned:
        described_group = _describe(graph, group, scenery, edge_scenery, index, grades, parallel_m)
        group_id = insert(group, described_group, GROUP, None, {})
        count += 1
        for st in stretches:
            draft: SectionDraft = {
                "kind": group["kind"], "name": f"{group['name']}: {st.role}", "edge_ids": st.edge_ids,
                "kinds": group.get("kinds") or [group["kind"]],
            }
            described = _describe(graph, draft, scenery, edge_scenery, index, grades, st.edge_length_m - st.single_path_m)
            insert(draft, described, STRETCH, group_id, {
                "role": st.role, "chains": st.chains, "end_nodes": list(st.end_nodes), "sides": st.sides,
                "notes": st.notes,
            })
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
    level: str = GROUP
    group_id: int | None = None

    @property
    def edge_ids(self) -> list[int]:
        return _unpack_ids(self.edge_blob)

    @property
    def way_lengths(self) -> dict[int, float]:
        return {w[0]: w[1] for w in self.data["ways"]}

    @property
    def kinds(self) -> list[str]:
        """Every kind that contributed to the section, `kind` first."""
        return self.data.get("kinds") or [self.kind]

    @property
    def single_path_m(self) -> float:
        """Length with the paths that run beside same-named ones counted once."""
        return max(0.0, self.length_m - self.data.get("parallel_m", 0.0))


def _row_section(row: sqlite3.Row) -> Section:
    return Section(
        row["section_id"], row["area_id"], row["kind"], row["name"], row["length_m"],
        (row["centroid_lat"], row["centroid_lng"]), (row["south"], row["west"], row["north"], row["east"]),
        json.loads(row["data"]), bytes(row["edges"]), row["level"], row["group_id"],
    )


def load_sections(
    conn: sqlite3.Connection, area_id: int, kinds: Sequence[str] | None = None, level: str | None = None
) -> list[Section]:
    """An area's groups and stretches, or one level of them."""
    rows = conn.execute("SELECT * FROM net_sections WHERE area_id = ? ORDER BY section_id", [area_id]).fetchall()
    return [s for s in map(_row_section, rows) if (kinds is None or s.kind in kinds) and (level is None or s.level == level)]


def stretches_of(conn: sqlite3.Connection, group_id: int) -> list[Section]:
    """A group's stretches in position order."""
    rows = conn.execute(
        "SELECT * FROM net_sections WHERE group_id = ? AND level = ? ORDER BY section_id", [group_id, STRETCH]
    ).fetchall()
    return [_row_section(r) for r in rows]


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
                r["to_node_id"], r["length_m"], bool(r["has_elevation"]), None, bytes(r["points_blob"]), r["trail_kind"],
                r["structure"],
            )
            edges[edge.edge_id] = edge
    return edges


# ---------------------------------------------------------------------------
# Building blocks for route assembly
# ---------------------------------------------------------------------------


class Block(TypedDict):
    """A stretch as route assembly uses it. Node and edge ids are the area
    graph's (net_nodes.node_id, net_edges.edge_id). Each chain is one side's
    path from one end to the other, edges in order with consecutive edges
    sharing a node; chains[0] is the primary side and runs from end_nodes[0]
    to end_nodes[1] (the same node for a loop). The chains hold the through-
    path only; edge_ids is every edge of the stretch, spurs and branches
    included. Where a stretch's primary chain ends at the node the next
    requested stretch of its group starts from, the first stretch's chains[0]
    runs on through the following ones, which then hold only their other
    sides; entry_nodes then lists the nodes any of them can be joined at."""
    block_id: int
    section_id: int
    name: str
    chains: list[list[int]]
    end_nodes: tuple[int, int]
    entry_nodes: list[int]
    edge_ids: list[int]


def block_of(stretch: Section) -> Block:
    return {
        "block_id": stretch.section_id,
        "section_id": cast(int, stretch.group_id),
        "name": stretch.name,
        "chains": [list(c) for c in stretch.data["chains"]],
        "end_nodes": (stretch.data["end_nodes"][0], stretch.data["end_nodes"][1]),
        "entry_nodes": stretch.data["entry_nodes"],
        "edge_ids": stretch.edge_ids,
    }


def building_blocks(
    conn: sqlite3.Connection, area_id: int, section_ids: Collection[int] | None = None
) -> list[Block]:
    """Stretches of the given groups or stretches (all in the area when None),
    in group order and position order within a group, ready for route
    assembly."""
    if section_ids is None:
        rows = conn.execute(
            "SELECT * FROM net_sections WHERE area_id = ? AND level = ? ORDER BY group_id, section_id", [area_id, STRETCH]
        ).fetchall()
        found = [_row_section(r) for r in rows]
    else:
        wanted: dict[int, None] = {}
        singles: dict[int, Section] = {}
        for section_id in section_ids:
            section = get_section(conn, section_id)
            if section.area_id != area_id:
                raise ValueError(f"section {section_id} belongs to area {section.area_id}, not area {area_id}")
            if section.level == STRETCH:
                singles[section.section_id] = section
            else:
                wanted[section.section_id] = None
        found = [*singles.values()]
        for group_id in wanted:
            found.extend(s for s in stretches_of(conn, group_id) if s.section_id not in singles)
        found.sort(key=lambda s: (cast(int, s.group_id), s.section_id))
    blocks: list[Block] = []
    lead: Block | None = None
    for s in found:
        block = block_of(s)
        if (
            lead is not None and lead["section_id"] == block["section_id"] and block["chains"]
            and lead["end_nodes"][1] == block["end_nodes"][0]
        ):
            lead["chains"][0].extend(block["chains"].pop(0))
            lead["end_nodes"] = (lead["end_nodes"][0], block["end_nodes"][1])
            lead["entry_nodes"] = [*lead["entry_nodes"], *block["end_nodes"], *block["entry_nodes"]]
        else:
            lead = block
        blocks.append(block)
    return blocks


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
    level: str


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
            "level": r["level"],
        }
        for r in rows
    ]


def judgment_matches(judgment: Judgment, section: Section) -> bool:
    """Whether a stored judgment belongs to a section: the same level, the
    section has its kind, and each covers at least REATTACH_MIN_SHARE of the
    other's length through the ways they share."""
    if judgment["level"] != section.level or judgment["section_kind"] not in section.kinds:
        return False
    return _covers(judgment["ways"], section.way_lengths)


def _covers(mine: Mapping[int, float], theirs: Mapping[int, float]) -> bool:
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
        INSERT INTO net_section_judgments (judgment, section_kind, factor, source, note, url, created_at, level)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [judgment, section.kind, factor, source, note, url, datetime.now(timezone.utc).isoformat(), section.level],
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
    """Store a bounded factor for a group or a stretch, replacing any earlier
    one at that level. A group's factor applies to all its stretches; a
    stretch's only to itself."""
    if source not in ADJUSTMENT_SOURCES:
        raise ValueError(f"source must be one of {', '.join(ADJUSTMENT_SOURCES)}")
    if source == "research" and not url:
        raise ValueError("a research adjustment needs a url for the source")
    section = get_section(conn, section_id)
    clamped = clamp_factor(factor)
    _store(conn, section, "adjust", clamped, source, note, url)
    return {"section_id": section_id, "level": section.level, "name": section.name, "factor": clamped, "requested_factor": factor, "source": source, "note": note, "url": url}


def exclude_section(conn: sqlite3.Connection, section_id: int, note: str | None) -> dict:
    """Keep a stretch's, or all of a group's, ways out of every route search
    until undone. A way leaves the graph whole, so a way that runs past a
    stretch's end leaves with it."""
    section = get_section(conn, section_id)
    _store(conn, section, "exclude", None, "athlete", note, None)
    return {"section_id": section_id, "level": section.level, "name": section.name, "excluded": True, "note": note}


def clear_section_adjustment(conn: sqlite3.Connection, section_id: int, exclusion: bool = False) -> dict:
    """Remove a section's stored adjustment; with exclusion=True, its exclusion too."""
    section = get_section(conn, section_id)
    _drop_matching(conn, section, "adjust")
    if exclusion:
        _drop_matching(conn, section, "exclude")
    conn.commit()
    return {"section_id": section_id, "level": section.level, "name": section.name, "adjustment_cleared": True, "exclusion_cleared": exclusion}
