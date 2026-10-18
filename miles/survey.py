"""Area survey (adr/0004-section-first-route-search.md): rank an area's
sections for one athlete starting from one point, before any route exists.

Everything stored about a section depends on the map alone (sections.py).
Everything here depends on the athlete or the start: runnability reads the
stored grade histogram through the current terrain model, familiarity reads
the matched activity tracks (familiarity.py), and access cost comes from one
preference-weighted shortest-path pass over the area graph.

Score, in quality-miles:

    score = quality x adjustment - access_cost

    quality     = usable_mi x continuity x (1 + scenery) x runnability
                  x surface
    usable_mi   = min(single-path length, run distance - 2 x approach
                  distance), the run distance being the request's
                  distance_mi, or DEFAULT_DISTANCE_MI; the single-path length
                  counts a path that runs beside a same-named one once
    continuity  = 1 / (1 + CONTINUITY_PER_X x x_per_mi), where
                  x_per_mi = (busy-road crossings
                              + JUNCTION_WEIGHT x junctions) per mile
    scenery     = SCENERY_WEIGHTS applied to the share of the section beside
                  water, in a park, in a protected area, plus a named route
                  and the summits and viewpoints along it, capped at
                  SCENERY_CAP
    runnability = RUNNABILITY_FLOOR + (1 - RUNNABILITY_FLOOR) x
                  (run + GRIND_VALUE x grind + HIKE_VALUE x hike), the shares
                  taken from the section's grade histogram through the
                  athlete's terrain model
    surface     = the section's singletrack, track, and other shares of its
                  length, each weighted by SURFACE_WEIGHTS for the request's
                  surface_preference
    access_cost = ACCESS_COST_PER_WEIGHTED_MI x the one-way approach in
                  weighted miles: each stretch of the shortest approach counts
                  its length times its weight, which is higher on tolerable
                  and uncomfortable road tiers, a sidewalk, and steep grades,
                  plus a flat charge per uncomfortable-road edge

A group is cut into stretches (sections.py): the detail view lists a group's
stretches in position order, each scored like a section of its own (its
single-path length, stored traits, matched familiarity and approach to the
nearest of its entry nodes), with its group's adjustment applied. An overview
lists a group with its stretches' roles; a group's approach is its best
stretch's.

A section shorter than the fragment limit (FRAGMENT_MIN_M, or
FRAGMENT_RUN_SHARE of the run distance if more) is left out of the ranking
unless include_fragments is set; the result counts those it left out.

A section is ruled out, not ranked, when its approach is longer than
APPROACH_BUDGET_SHARE of the run distance, cannot be reached, or runs more
continuous uncomfortable road than the route preferences allow. That check
reads the approach that minimizes the weighted cost, not the best one
satisfying the limit.

The adjustment is a bounded factor on quality (sections.FACTOR_MIN to
FACTOR_MAX): stored by the athlete or from research, or passed for this one
request. Excluded sections are listed, marked, and never ranked.
"""

import heapq
import math
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Mapping, Sequence, cast
from typing_extensions import NotRequired, TypedDict

from . import familiarity, network, route_search, sections, terrain
from .route_search import GraphEdge, SearchGraph

MILE_M = 1609.344

DEFAULT_DISTANCE_MI = 8.0
APPROACH_BUDGET_SHARE = 0.25
DEFAULT_N = 10
MAX_N = 25
LISTED_EXCLUDED = 5
LISTED_RULED_OUT = 5
DETAIL_LEVELS = ("overview", "detail")
# Stretches listed under a group.
MAX_INCLUDES = 8

ACCESS_COST_PER_WEIGHTED_MI = 0.75
TIER_WEIGHT = {"comfortable": 1.0, "tolerable": 1.6, "uncomfortable": 5.0}
SIDEWALK_WEIGHT = 1.2
UNCOMFORTABLE_EDGE_CHARGE_M = 50.0
HIKE_GRADE_WEIGHT = 1.5
STEEP_DESCENT_WEIGHT = 1.2
PITCH_WINDOW_M = 80.0

CONTINUITY_PER_X = 0.15
JUNCTION_WEIGHT = 0.35
SCENERY_WEIGHTS = {"water": 0.30, "park": 0.15, "protected": 0.15, "route": 0.15, "poi_each": 0.04, "poi_cap": 0.12}
SCENERY_CAP = 0.8
RUNNABILITY_FLOOR = 0.3
GRIND_VALUE = 0.6
HIKE_VALUE = 0.15
NEUTRAL_RUNNABILITY = 0.65

# A section counts as excluded when this share of its length lies in ways an
# exclusion removed from the graph.
EXCLUDED_SHARE = 0.5
# Sections under this long, or under this share of the run distance if that is
# longer, are connectors and stubs rather than something to run.
FRAGMENT_MIN_M = 1500.0
FRAGMENT_RUN_SHARE = 0.15

# How much of a section's length counts toward quality by what it is on, for
# each surface_preference: singletrack, road-width track (dirt road, canal or
# levee bank, fire road), and every other surface (paved paths and roads
# included). The default weighs a track at 0.8 of singletrack and the rest
# at full value; a stated preference tilts toward its surface.
SURFACE_WEIGHTS: dict[str, dict[str, float]] = {
    "any": {"singletrack": 1.0, "track": 0.8, "other": 1.0},
    "singletrack": {"singletrack": 1.0, "track": 0.5, "other": 0.7},
    "track": {"singletrack": 0.6, "track": 1.0, "other": 0.7},
    "paved": {"singletrack": 0.6, "track": 0.6, "other": 1.0},
}
SURFACE_PREFERENCES = tuple(SURFACE_WEIGHTS)


class SurveyRow(TypedDict):
    section_id: int
    kind: str
    name: str
    # "group" in an overview, "stretch" in a detail survey; a stretch names its group.
    level: str
    group_id: int | None
    excluded: bool
    rank: int | None
    # Every kind the section was formed from, the kind above first.
    kinds: list[str]
    other_names: list[str]
    # Single-path length: the union less the paths that run beside another.
    length_m: float
    union_m: float
    parallel_m: float
    fragment: bool
    # A group's stretches in position order, capped (section_id, role,
    # length_m single-path); stretches_total counts them all.
    stretches: list[dict]
    stretches_total: int
    # A stretch's place in its group: role, the sides it spans, the spurs it
    # includes, and its 1-based position.
    role: str | None
    sides: list[str]
    notes: list[str]
    seq: int | None
    surface_pct: dict[str, float]
    junctions: int
    crossings: int
    scenery: dict
    runnability: dict
    gain_m_per_mile: float | None
    familiarity: dict
    access: dict
    adjustment: dict | None
    exclusion: NotRequired[dict | None]
    terms: dict
    score: float | None


@dataclass
class Approach:
    cost_m: float
    approach_m: float
    uncomfortable_m: float
    max_uncomfortable_run_m: float
    steepest_pitch_pct: float


def steepest_pitch_pct(points: Sequence[tuple[float, float, float, float | None]]) -> float:
    """The steepest grade, in percent (negative for a descent), over any
    stretch of about PITCH_WINDOW_M along an edge's sample points; 0 when it
    has no elevation."""
    if any(p[3] is None for p in points) or len(points) < 2:
        return 0.0
    best = 0.0
    j = 0
    for i in range(len(points)):
        while j < len(points) - 1 and points[j][2] - points[i][2] < PITCH_WINDOW_M:
            j += 1
        run = points[j][2] - points[i][2]
        if run < PITCH_WINDOW_M / 2:
            continue
        grade = (cast(float, points[j][3]) - cast(float, points[i][3])) / run * 100.0
        if abs(grade) > abs(best):
            best = grade
    return best


class EdgeCosts(dict[int, tuple[float, str | None, float]]):
    """Edge key -> (approach cost in meters, comfort tier or None off the
    roads, steepest pitch), computed on first use."""

    def __init__(
        self, prefs: dict, model: terrain.TerrainModel, overrides: Mapping[int, str], rows: Mapping[int, dict]
    ) -> None:
        super().__init__()
        self._prefs, self._model, self._overrides, self._rows = prefs, model, overrides, rows

    def __missing__(self, edge_key: int) -> tuple[float, str | None, float]:
        row = self._rows[edge_key]
        tier: str | None = None
        if row["maxspeed_mph"] is not None:
            tier = network.road_comfort_tier(
                row["highway"], row["surface_class"], row["maxspeed_mph"], row["lanes"], bool(row["has_separation"]),
                self._prefs,
            )
            if self._overrides.get(row["way_id"]) == "comfortable":
                tier = "comfortable"
        weight = TIER_WEIGHT[tier] if tier is not None else (SIDEWALK_WEIGHT if row["surface_class"] == "sidewalk" else 1.0)
        pitch = steepest_pitch_pct(network.unpack_edge_points(bytes(row["points_blob"]))) if row["has_elevation"] else 0.0
        cls = self._model.classify_grade(pitch)
        if cls == "hike" and pitch > 0:
            weight *= HIKE_GRADE_WEIGHT
        elif pitch < 0 and cls == "grind":
            weight *= STEEP_DESCENT_WEIGHT
        cost = row["length_m"] * weight + (UNCOMFORTABLE_EDGE_CHARGE_M if tier == "uncomfortable" else 0.0)
        result = self[edge_key] = (cost, tier, pitch)
        return result


@dataclass
class Tree:
    """Cheapest paths from one node: the cost and length to each node reached,
    and the edge and node each was reached from."""
    cost: dict[int, float]
    length: dict[int, float]
    prev_edge: dict[int, GraphEdge]
    prev_node: dict[int, int]

    def steps(self, node: int) -> route_search.Steps:
        return route_search._reconstruct(node, self.prev_edge, self.prev_node)


# An edge in a tree's `penalized` set costs this many times its usual cost.
REUSE_COST_FACTOR = 4.0


def weighted_tree(
    graph: SearchGraph, source: int, costs: EdgeCosts, max_cost: float, penalized: Collection[int] = frozenset(),
    targets: Collection[int] = (),
) -> Tree:
    """The cheapest paths from `source` out to max_cost, an edge in `penalized`
    costing REUSE_COST_FACTOR times its base cost. With `targets`, the search
    stops once all of them are settled, so only they (and nodes settled on
    the way) have final costs. Traversal is inlined from GraphEdge.traverse:
    this is the search's hot loop."""
    pending = set(targets)
    cost: dict[int, float] = {source: 0.0}
    length: dict[int, float] = {source: 0.0}
    prev_edge: dict[int, GraphEdge] = {}
    prev_node: dict[int, int] = {}
    heap: list[tuple[float, int]] = [(0.0, source)]
    done: set[int] = set()
    adjacency = graph.adjacency
    while heap:
        d, node = heapq.heappop(heap)
        if node in done:
            continue
        if d > max_cost:
            break
        done.add(node)
        if pending:
            pending.discard(node)
            if not pending:
                break
        for edge in adjacency.get(node, ()):
            if node == edge.a:
                if edge.allowed == "b_to_a":
                    continue
                nxt = edge.b
            else:
                if edge.allowed == "a_to_b":
                    continue
                nxt = edge.a
            step = costs[edge.edge_key][0]
            if penalized and edge.edge_key in penalized:
                step *= REUSE_COST_FACTOR
            nd = d + step
            if nxt not in cost or nd < cost[nxt]:
                cost[nxt] = nd
                length[nxt] = length[node] + edge.length_m
                prev_edge[nxt] = edge
                prev_node[nxt] = node
                heapq.heappush(heap, (nd, nxt))
    return Tree(cost, length, prev_edge, prev_node)


def approach_to(tree: Tree, node: int, costs: EdgeCosts, rows: Mapping[int, dict]) -> Approach:
    """The path from the tree's source to `node`, as the survey reads it."""
    length = uncomfortable = run = longest_run = 0.0
    steepest = 0.0
    cursor = node
    while cursor in tree.prev_edge:
        edge = tree.prev_edge[cursor]
        _cost, tier, pitch = costs[edge.edge_key]
        length += rows[edge.edge_key]["length_m"]
        if tier == "uncomfortable":
            uncomfortable += rows[edge.edge_key]["length_m"]
            run += rows[edge.edge_key]["length_m"]
            longest_run = max(longest_run, run)
        else:
            run = 0.0
        if abs(pitch) > abs(steepest):
            steepest = pitch
        cursor = tree.prev_node[cursor]
    return Approach(tree.cost[node], length, uncomfortable, longest_run, steepest)


def _runnability(section: sections.Section, classes: Sequence[str], speeds: Sequence[float]) -> dict:
    hist = section.data["grade_hist_m"]
    covered = sum(hist)
    if covered <= 0:
        return {"run_pct": None, "grind_pct": None, "hike_pct": None, "est_pace_s_per_mi": None, "elevation_coverage_pct": 0.0}
    shares = {"run": 0.0, "grind": 0.0, "hike": 0.0}
    seconds = 0.0
    for meters, cls, speed in zip(hist, classes, speeds):
        shares[cls] += meters
        seconds += meters / speed
    return {
        "run_pct": 100.0 * shares["run"] / covered,
        "grind_pct": 100.0 * shares["grind"] / covered,
        "hike_pct": 100.0 * shares["hike"] / covered,
        "est_pace_s_per_mi": seconds / covered * MILE_M,
        "elevation_coverage_pct": 100.0 * min(1.0, covered / section.length_m),
    }


def _runnability_factor(runnability: dict) -> float:
    if runnability["run_pct"] is None:
        return NEUTRAL_RUNNABILITY
    value = (runnability["run_pct"] + GRIND_VALUE * runnability["grind_pct"] + HIKE_VALUE * runnability["hike_pct"]) / 100.0
    return RUNNABILITY_FLOOR + (1.0 - RUNNABILITY_FLOOR) * value


def _scenery_bonus(scenery: dict) -> float:
    w = SCENERY_WEIGHTS
    bonus = (
        w["water"] * scenery["near_water_share"] + w["park"] * scenery["in_park_share"]
        + w["protected"] * scenery["protected_share"] + (w["route"] if scenery["route_names"] else 0.0)
        + min(w["poi_cap"], w["poi_each"] * (scenery["summits"] + scenery["viewpoints"]))
    )
    return min(SCENERY_CAP, bonus)


def _continuity_factor(section: sections.Section) -> float:
    miles = max(section.length_m / MILE_M, 0.25)
    x_per_mi = (section.data["crossings"] + JUNCTION_WEIGHT * section.data["junctions"]) / miles
    return 1.0 / (1.0 + CONTINUITY_PER_X * x_per_mi)


def _surface_factor(section: sections.Section, preference: str) -> float:
    surface = section.data["surface_m"]
    total = sum(surface.values())
    if total <= 0:
        return 1.0
    weights = SURFACE_WEIGHTS[preference]
    return sum(meters * weights.get(key, weights["other"]) for key, meters in surface.items()) / total


def _excluded_share(section: sections.Section, excluded_ways: set[int]) -> float:
    if not excluded_ways or section.length_m <= 0:
        return 0.0
    return sum(length for way, length in section.way_lengths.items() if way in excluded_ways) / section.length_m


def _request_factors(adjustments: Mapping[str, float] | Mapping[int, float] | None) -> dict[int, float]:
    factors: dict[int, float] = {}
    for key, value in (adjustments or {}).items():
        try:
            section_id = int(key)
        except (TypeError, ValueError):
            raise ValueError(f"adjustments keys must be section ids, got {key!r}") from None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"adjustment for section {section_id} must be a number, got {value!r}")
        factors[section_id] = sections.clamp_factor(float(value))
    return factors


def _wanted(section: sections.Section, kinds: Sequence[str] | None) -> bool:
    return not kinds or any(k in kinds for k in section.kinds)


def grade_classes(model: terrain.TerrainModel) -> tuple[list[str], list[float]]:
    """The terrain model's class and speed for each grade bin."""
    classes = [model.classify_grade(terrain.bin_center(b)) for b in range(terrain.N_GRADE_BINS)]
    speeds = [max(model.expected_speed_mps(terrain.bin_center(b)), 0.3) for b in range(terrain.N_GRADE_BINS)]
    return classes, speeds


@dataclass
class Density:
    """The terms of a section's quality per mile."""
    continuity: float
    scenery_bonus: float
    runnability_factor: float
    surface: float
    runnability: dict

    @property
    def value(self) -> float:
        return self.continuity * (1.0 + self.scenery_bonus) * self.runnability_factor * self.surface


def density_terms(section: sections.Section, classes: Sequence[str], speeds: Sequence[float], preference: str) -> Density:
    runnability = _runnability(section, classes, speeds)
    return Density(
        _continuity_factor(section), _scenery_bonus(section.data["scenery"]), _runnability_factor(runnability),
        _surface_factor(section, preference), runnability,
    )


def density(section: sections.Section, model: terrain.TerrainModel, preference: str) -> float:
    """A section's quality per usable mile, before any adjustment: continuity
    x (1 + scenery) x runnability x surface."""
    classes, speeds = grade_classes(model)
    return density_terms(section, classes, speeds, preference).value


def _own_adjustment(
    section: sections.Section, judgments: Mapping[int, sections.SectionJudgments], request_factors: Mapping[int, float]
) -> tuple[float, dict | None]:
    """A section's own adjustment: the request's if given, else the stored."""
    stored = judgments[section.section_id]["adjustment"]
    if section.section_id in request_factors:
        adjustment: dict = {"factor": request_factors[section.section_id], "source": "request", "note": None, "url": None}
        if stored is not None:
            adjustment["replaces_stored_factor"] = stored["factor"]
        return request_factors[section.section_id], adjustment
    return (stored["factor"] if stored else 1.0), (dict(stored) if stored else None)


def section_factor(
    section: sections.Section, parent: sections.Section | None, judgments: Mapping[int, sections.SectionJudgments],
    request_factors: Mapping[int, float] | None = None,
) -> tuple[float, dict | None]:
    """The factor on a section's quality and the adjustment behind it: its
    own times its group's (`parent`, for a stretch), bounded."""
    requested = request_factors or {}
    own, adjustment = _own_adjustment(section, judgments, requested)
    if parent is None:
        return own, adjustment
    group_factor, group_adjustment = _own_adjustment(parent, judgments, requested)
    factor = max(sections.FACTOR_MIN, min(sections.FACTOR_MAX, own * group_factor))
    if group_adjustment is not None and group_factor != 1.0:
        adjustment = {**(adjustment or group_adjustment), "factor": factor, "group_factor": group_factor}
    elif adjustment is not None:
        adjustment["factor"] = factor
    return factor, adjustment


def survey(
    conn: sqlite3.Connection,
    area_id: int,
    start_lat: float,
    start_lng: float,
    *,
    distance_m: float | None = None,
    kinds: Sequence[str] | None = None,
    unfamiliar_only: bool = False,
    adjustments: Mapping[str, float] | Mapping[int, float] | None = None,
    n: int = DEFAULT_N,
    surface_preference: str | None = None,
    include_fragments: bool = False,
    detail: str = "overview",
    within: int | None = None,
) -> dict:
    """The area's sections around a start, best first: its groups, or with
    detail="detail" its stretches (those of one group, in position order, when
    `within` names it). Lengths are meters; see the module docstring for the
    score."""
    if kinds:
        unknown = [k for k in kinds if k not in sections.SECTION_KINDS]
        if unknown:
            raise ValueError(f"unknown kinds {unknown}; kinds are {', '.join(sections.SECTION_KINDS)}")
    preference = surface_preference or "any"
    if preference not in SURFACE_WEIGHTS:
        raise ValueError(f"unknown surface_preference {surface_preference!r}; choose from {', '.join(SURFACE_PREFERENCES)}")
    if detail not in DETAIL_LEVELS:
        raise ValueError(f"unknown detail {detail!r}; choose from {', '.join(DETAIL_LEVELS)}")
    if within is not None:
        detail = "detail"
    if distance_m is not None and distance_m <= 0:
        raise ValueError("distance_mi must be positive")
    n = max(1, min(int(n), MAX_N))
    request_factors = _request_factors(adjustments)
    run_m = distance_m if distance_m is not None else DEFAULT_DISTANCE_MI * MILE_M
    max_approach_m = APPROACH_BUDGET_SHARE * run_m
    fragment_m = max(FRAGMENT_MIN_M, FRAGMENT_RUN_SHARE * run_m)

    route_search.require_searchable(conn, area_id)
    prefs = network.get_route_preferences(conn)
    model = terrain.current_model(conn)
    everything = sections.load_sections(conn, area_id)
    if not everything:
        raise ValueError(f"area {area_id} has no sections; check get_area_status and survey again when it is ready")
    groups = {s.section_id: s for s in everything if s.level == sections.GROUP}
    stretches_by_group: dict[int, list[sections.Section]] = {}
    for s in everything:
        if s.group_id is not None and s.level == sections.STRETCH:
            stretches_by_group.setdefault(s.group_id, []).append(s)
    seq_of = {st.section_id: i for sts in stretches_by_group.values() for i, st in enumerate(sts, start=1)}
    if within is not None:
        if within not in groups:
            raise ValueError(f"within must be a group id from an overview survey of area {area_id}, got {within}")
        candidates = stretches_by_group.get(within, [])
    elif detail == "detail":
        candidates = [st for sts in stretches_by_group.values() for st in sts]
    else:
        candidates = list(groups.values())
    judgments = sections.judgments_for(conn, everything)
    excluded_ways = sections.excluded_way_ids(conn)
    avoid_ways = network.avoided_way_ids(conn) | excluded_ways
    overrides = {
        r["way_id"]: r["verdict"] for r in conn.execute("SELECT way_id, verdict FROM net_way_overrides").fetchall()
    }
    loaded = route_search.load_search_graph(conn, area_id)

    # Access is measured to stretches; a group takes its best stretch's.
    to_reach = [st for st in candidates if st.level == sections.STRETCH] or [
        st for g in candidates for st in stretches_by_group.get(g.section_id, [])
    ]
    with route_search.search_slot():
        graph, rows, start_node = route_search.start_graph(
            loaded, start_lat, start_lng, max_approach_m + route_search.START_SNAP_RADIUS_M, avoid_ways
        )
        if start_node is None:
            raise ValueError("no network found near the start")
        costs = EdgeCosts(prefs, model, overrides, rows)
        tree = weighted_tree(graph, start_node, costs, 2.0 * max_approach_m)
        start_ways = {rows[e.edge_key]["way_id"] for e in graph.adjacency.get(start_node, []) if e.edge_key in rows}
        stretch_approach: dict[int, Approach | None] = {}
        for stretch in to_reach:
            if start_ways & stretch.way_lengths.keys():
                stretch_approach[stretch.section_id] = Approach(0.0, 0.0, 0.0, 0.0, 0.0)
                continue
            reached = [e for e in stretch.data["entry_nodes"] if e in tree.cost]
            if not reached:
                stretch_approach[stretch.section_id] = None
                continue
            best = min(reached, key=lambda e: (tree.cost[e], e))
            stretch_approach[stretch.section_id] = approach_to(tree, best, costs, rows)
    approaches: dict[int, Approach | None] = {}
    for section in candidates:
        if section.level != sections.GROUP:
            approaches[section.section_id] = stretch_approach.get(section.section_id)
            continue
        options = [
            (a.cost_m, st.section_id) for st in stretches_by_group.get(section.section_id, [])
            if (a := stretch_approach.get(st.section_id)) is not None
        ]
        approaches[section.section_id] = stretch_approach[min(options)[1]] if options else None

    classes, speeds = grade_classes(model)
    visit_counts = familiarity.visits(conn, area_id)
    fam_status = familiarity.status(conn, area_id)
    warnings: list[str] = []
    if fam_status.pending:
        familiarity.start_background_update(area_id)
        warnings.append(
            f"{fam_status.pending} activities are still being matched to sections; "
            "familiarity may undercount until that finishes"
        )
    if unfamiliar_only and fam_status.matched == 0:
        warnings.append("no activity tracks have been matched to this area, so unfamiliar_only filtered nothing out")

    ranked: list[SurveyRow] = []
    excluded_rows: list[SurveyRow] = []
    ruled_out: list[dict] = []
    ruled_out_count = 0
    counts: dict[str, int] = {}
    fragments_hidden = 0
    for section in candidates:
        if not _wanted(section, kinds):
            continue
        is_fragment = section.single_path_m < fragment_m
        listing_a_group_in_full = within is not None
        if is_fragment and not include_fragments and not listing_a_group_in_full:
            fragments_hidden += 1
            continue
        counts[section.kind] = counts.get(section.kind, 0) + 1
        is_group = section.level == sections.GROUP
        parent = groups.get(section.group_id) if section.group_id is not None else None
        stored = judgments[section.section_id]
        parent_exclusion = judgments[parent.section_id]["exclusion"] if parent else None
        exclusion = stored["exclusion"] or parent_exclusion
        is_excluded = exclusion is not None or _excluded_share(section, excluded_ways) >= EXCLUDED_SHARE
        visit = visit_counts.get(section.section_id)
        runs = visit.runs if visit else 0
        if unfamiliar_only and runs > 0 and not is_excluded:
            continue

        approach = approaches[section.section_id]
        reason: str | None = None
        if approach is None:
            reason = "too far to reach within the approach budget"
        elif approach.approach_m > max_approach_m:
            reason = "approach longer than the budget for this distance"
        elif approach.max_uncomfortable_run_m > prefs["max_continuous_uncomfortable_m"]:
            reason = "approach has more continuous uncomfortable road than the preferences allow"
        if reason is not None and not is_excluded:
            ruled_out_count += 1
            if len(ruled_out) < LISTED_RULED_OUT:
                ruled_out.append({"section_id": section.section_id, "kind": section.kind, "name": section.name, "reason": reason})
            continue
        approach = approach or Approach(math.inf, math.inf, 0.0, 0.0, 0.0)

        terms = density_terms(section, classes, speeds, preference)
        usable_m = max(0.0, min(section.single_path_m, run_m - 2.0 * approach.approach_m)) if math.isfinite(approach.approach_m) else 0.0
        quality = usable_m / MILE_M * terms.value
        factor, adjustment = section_factor(section, parent, judgments, request_factors)
        access_cost = ACCESS_COST_PER_WEIGHTED_MI * approach.cost_m / MILE_M if math.isfinite(approach.cost_m) else math.inf
        score = quality * factor - access_cost if math.isfinite(access_cost) else None

        group_stretches = stretches_by_group.get(section.section_id, []) if is_group else []
        elevated = section.data["elevated_m"]
        surface = section.data["surface_m"]
        surface_total = sum(surface.values())
        row: SurveyRow = {
            "section_id": section.section_id,
            "kind": section.kind,
            "name": section.name,
            "level": section.level,
            "group_id": section.group_id,
            "kinds": section.kinds,
            "other_names": section.data.get("other_names", []),
            "excluded": is_excluded,
            "rank": None,
            "length_m": section.single_path_m,
            "union_m": section.length_m,
            "parallel_m": section.data.get("parallel_m", 0.0),
            "fragment": is_fragment,
            "stretches": [
                {"section_id": st.section_id, "role": st.data.get("role"), "length_m": st.single_path_m}
                for st in group_stretches[:MAX_INCLUDES]
            ],
            "stretches_total": len(group_stretches),
            "role": section.data.get("role") if not is_group else None,
            "sides": section.data.get("sides", []),
            "notes": section.data.get("notes", []),
            "seq": seq_of.get(section.section_id),
            "surface_pct": {k: 100.0 * v / surface_total for k, v in sorted(surface.items())} if surface_total else {},
            "junctions": section.data["junctions"],
            "crossings": section.data["crossings"],
            "scenery": section.data["scenery"],
            "runnability": terms.runnability,
            "gain_m_per_mile": section.data["gain_m"] / (elevated / MILE_M) if elevated > 0 else None,
            "familiarity": {"runs": runs, "last_run": visit.last_run if visit else None, "new_to_you": runs == 0},
            "access": {
                "approach_m": approach.approach_m if math.isfinite(approach.approach_m) else None,
                "weighted_approach_m": approach.cost_m if math.isfinite(approach.cost_m) else None,
                "uncomfortable_m": approach.uncomfortable_m,
                "steepest_pitch_pct": approach.steepest_pitch_pct,
                "ruled_out": reason,
            },
            "adjustment": adjustment,
            "terms": {
                "usable_m": usable_m,
                "continuity": terms.continuity,
                "scenery_bonus": terms.scenery_bonus,
                "runnability": terms.runnability_factor,
                "surface": terms.surface,
                "quality": quality,
                "adjustment": factor,
                "adjusted_quality": quality * factor,
                "access_cost": access_cost if math.isfinite(access_cost) else None,
            },
            "score": score,
        }
        if exclusion is not None:
            row["exclusion"] = exclusion
        (excluded_rows if is_excluded else ranked).append(row)

    ranked.sort(key=lambda r: (-(r["score"] or 0.0), -r["length_m"], r["section_id"]))
    top = ranked[:n]
    for i, row in enumerate(top, start=1):
        row["rank"] = i
    excluded_rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0.0), r["section_id"]))
    if within is not None:
        top.sort(key=lambda r: r["section_id"])
    return {
        "area_id": area_id,
        "start": {"lat": start_lat, "lng": start_lng},
        "distance_m": distance_m,
        "run_distance_m": run_m,
        "detail": detail,
        "within": within,
        "sections": [*top, *excluded_rows[:LISTED_EXCLUDED]],
        "ranked_count": len(ranked),
        "counts_by_kind": counts,
        "surface_preference": preference,
        "fragments_hidden": fragments_hidden,
        "fragment_limit_m": fragment_m,
        "ruled_out_count": ruled_out_count,
        "ruled_out": ruled_out,
        "familiarity": {"activities_matched": fam_status.matched, "activities_pending": fam_status.pending},
        "warnings": warnings,
    }
