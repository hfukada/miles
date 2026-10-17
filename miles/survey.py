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
    usable_mi   = min(section length, run distance - 2 x approach distance),
                  the run distance being the request's distance_mi, or
                  DEFAULT_DISTANCE_MI
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
    access_cost = ACCESS_COST_PER_WEIGHTED_MI x the one-way approach in
                  weighted miles: each stretch of the shortest approach counts
                  its length times its weight, which is higher on tolerable
                  and uncomfortable road tiers, a sidewalk, and steep grades,
                  plus a flat charge per uncomfortable-road edge

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
# Another listed section covers this share of a section's ways: reported so
# one stretch seen as a trail system and as a water path is not read as two.
OVERLAP_SHARE = 0.5


class SurveyRow(TypedDict):
    section_id: int
    kind: str
    name: str
    excluded: bool
    rank: int | None
    # Higher-ranked sections in the result that cover half or more of this one's length.
    overlaps: list[int]
    length_m: float
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
class _Approach:
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


class _EdgeCosts:
    """Per-edge approach cost, computed on first use."""

    def __init__(
        self, prefs: dict, model: terrain.TerrainModel, overrides: Mapping[int, str], rows: Mapping[int, dict]
    ) -> None:
        self._prefs, self._model, self._overrides, self._rows = prefs, model, overrides, rows
        self._memo: dict[int, tuple[float, str | None, float]] = {}

    def of(self, edge_key: int) -> tuple[float, str | None, float]:
        """(cost in meters, comfort tier or None off the roads, steepest pitch)."""
        cached = self._memo.get(edge_key)
        if cached is not None:
            return cached
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
        result = (cost, tier, pitch)
        self._memo[edge_key] = result
        return result


def _weighted_paths(
    graph: SearchGraph, source: int, costs: _EdgeCosts, max_cost_m: float
) -> tuple[dict[int, float], dict[int, GraphEdge], dict[int, int]]:
    dist: dict[int, float] = {source: 0.0}
    prev_edge: dict[int, GraphEdge] = {}
    prev_node: dict[int, int] = {}
    done: set[int] = set()
    heap: list[tuple[float, int]] = [(0.0, source)]
    while heap:
        d, node = heapq.heappop(heap)
        if node in done:
            continue
        if d > max_cost_m:
            break
        done.add(node)
        for edge in graph.adjacency.get(node, []):
            step = edge.traverse(node)
            if step is None:
                continue
            nxt = step[0]
            nd = d + costs.of(edge.edge_key)[0]
            if nxt not in dist or nd < dist[nxt]:
                dist[nxt] = nd
                prev_edge[nxt] = edge
                prev_node[nxt] = node
                heapq.heappush(heap, (nd, nxt))
    return dist, prev_edge, prev_node


def _approach_to(
    node: int,
    dist: Mapping[int, float],
    prev_edge: Mapping[int, GraphEdge],
    prev_node: Mapping[int, int],
    costs: _EdgeCosts,
    rows: Mapping[int, dict],
) -> _Approach:
    length = uncomfortable = run = longest_run = 0.0
    steepest = 0.0
    cursor = node
    while cursor in prev_edge:
        edge = prev_edge[cursor]
        _cost, tier, pitch = costs.of(edge.edge_key)
        length += rows[edge.edge_key]["length_m"]
        if tier == "uncomfortable":
            uncomfortable += rows[edge.edge_key]["length_m"]
            run += rows[edge.edge_key]["length_m"]
            longest_run = max(longest_run, run)
        else:
            run = 0.0
        if abs(pitch) > abs(steepest):
            steepest = pitch
        cursor = prev_node[cursor]
    return _Approach(dist[node], length, uncomfortable, longest_run, steepest)


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
) -> dict:
    """The area's sections around a start, best first. Lengths are meters;
    see the module docstring for the score."""
    if kinds:
        unknown = [k for k in kinds if k not in sections.SECTION_KINDS]
        if unknown:
            raise ValueError(f"unknown kinds {unknown}; kinds are {', '.join(sections.SECTION_KINDS)}")
    if distance_m is not None and distance_m <= 0:
        raise ValueError("distance_mi must be positive")
    n = max(1, min(int(n), MAX_N))
    request_factors = _request_factors(adjustments)
    run_m = distance_m if distance_m is not None else DEFAULT_DISTANCE_MI * MILE_M
    max_approach_m = APPROACH_BUDGET_SHARE * run_m

    route_search.require_searchable(conn, area_id)
    prefs = network.get_route_preferences(conn)
    model = terrain.current_model(conn)
    all_sections = sections.load_sections(conn, area_id)
    if not all_sections:
        raise ValueError(f"area {area_id} has no sections; check get_area_status and survey again when it is ready")
    judgments = sections.judgments_for(conn, all_sections)
    excluded_ways = sections.excluded_way_ids(conn)
    avoid_ways = network.avoided_way_ids(conn) | excluded_ways
    overrides = {
        r["way_id"]: r["verdict"] for r in conn.execute("SELECT way_id, verdict FROM net_way_overrides").fetchall()
    }
    loaded = route_search.load_search_graph(conn, area_id)

    with route_search.search_slot():
        graph, rows, start_node = route_search.start_graph(
            loaded, start_lat, start_lng, max_approach_m + route_search.START_SNAP_RADIUS_M, avoid_ways
        )
        if start_node is None:
            raise ValueError("no network found near the start")
        costs = _EdgeCosts(prefs, model, overrides, rows)
        dist, prev_edge, prev_node = _weighted_paths(graph, start_node, costs, 2.0 * max_approach_m)
        start_ways = {rows[e.edge_key]["way_id"] for e in graph.adjacency.get(start_node, []) if e.edge_key in rows}
        approaches: dict[int, _Approach | None] = {}
        for section in all_sections:
            if kinds and section.kind not in kinds:
                continue
            if start_ways & section.way_lengths.keys():
                approaches[section.section_id] = _Approach(0.0, 0.0, 0.0, 0.0, 0.0)
                continue
            reached = [e for e in section.data["entry_nodes"] if e in dist]
            if not reached:
                approaches[section.section_id] = None
                continue
            best = min(reached, key=lambda e: (dist[e], e))
            approaches[section.section_id] = _approach_to(best, dist, prev_edge, prev_node, costs, rows)

    classes = [model.classify_grade(terrain.bin_center(b)) for b in range(terrain.N_GRADE_BINS)]
    speeds = [max(model.expected_speed_mps(terrain.bin_center(b)), 0.3) for b in range(terrain.N_GRADE_BINS)]
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
    for section in all_sections:
        if kinds and section.kind not in kinds:
            continue
        counts[section.kind] = counts.get(section.kind, 0) + 1
        stored = judgments[section.section_id]
        is_excluded = stored["exclusion"] is not None or _excluded_share(section, excluded_ways) >= EXCLUDED_SHARE
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
        approach = approach or _Approach(math.inf, math.inf, 0.0, 0.0, 0.0)

        runnability = _runnability(section, classes, speeds)
        scenery = section.data["scenery"]
        usable_m = max(0.0, min(section.length_m, run_m - 2.0 * approach.approach_m)) if math.isfinite(approach.approach_m) else 0.0
        continuity = _continuity_factor(section)
        scenery_bonus = _scenery_bonus(scenery)
        runnability_factor = _runnability_factor(runnability)
        quality = usable_m / MILE_M * continuity * (1.0 + scenery_bonus) * runnability_factor

        adjustment: dict | None = None
        factor = 1.0
        if stored["adjustment"] is not None:
            adjustment = {**stored["adjustment"]}
            factor = adjustment["factor"]
        if section.section_id in request_factors:
            replaced = adjustment["factor"] if adjustment else None
            factor = request_factors[section.section_id]
            adjustment = {"factor": factor, "source": "request", "note": None, "url": None}
            if replaced is not None:
                adjustment["replaces_stored_factor"] = replaced
        access_cost = ACCESS_COST_PER_WEIGHTED_MI * approach.cost_m / MILE_M if math.isfinite(approach.cost_m) else math.inf
        score = quality * factor - access_cost if math.isfinite(access_cost) else None

        elevated = section.data["elevated_m"]
        surface = section.data["surface_m"]
        row: SurveyRow = {
            "section_id": section.section_id,
            "kind": section.kind,
            "name": section.name,
            "excluded": is_excluded,
            "rank": None,
            "overlaps": [],
            "length_m": section.length_m,
            "surface_pct": {k: 100.0 * v / section.length_m for k, v in sorted(surface.items())} if section.length_m else {},
            "junctions": section.data["junctions"],
            "crossings": section.data["crossings"],
            "scenery": scenery,
            "runnability": runnability,
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
                "continuity": continuity,
                "scenery_bonus": scenery_bonus,
                "runnability": runnability_factor,
                "quality": quality,
                "adjustment": factor,
                "adjusted_quality": quality * factor,
                "access_cost": access_cost if math.isfinite(access_cost) else None,
            },
            "score": score,
        }
        if stored["exclusion"] is not None:
            row["exclusion"] = stored["exclusion"]
        (excluded_rows if is_excluded else ranked).append(row)

    ranked.sort(key=lambda r: (-(r["score"] or 0.0), -r["length_m"], r["section_id"]))
    top = ranked[:n]
    by_id = {s.section_id: s for s in all_sections}
    for i, row in enumerate(top, start=1):
        row["rank"] = i
        mine = by_id[row["section_id"]].way_lengths
        for earlier in top[:i - 1]:
            theirs = by_id[earlier["section_id"]].way_lengths
            if row["length_m"] > 0 and sum(length for way, length in mine.items() if way in theirs) / row["length_m"] >= OVERLAP_SHARE:
                row["overlaps"].append(earlier["section_id"])
    excluded_rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0.0), r["section_id"]))
    return {
        "area_id": area_id,
        "start": {"lat": start_lat, "lng": start_lng},
        "distance_m": distance_m,
        "run_distance_m": run_m,
        "sections": [*top, *excluded_rows[:LISTED_EXCLUDED]],
        "ranked_count": len(ranked),
        "counts_by_kind": counts,
        "ruled_out_count": ruled_out_count,
        "ruled_out": ruled_out,
        "familiarity": {"activities_matched": fam_status.matched, "activities_pending": fam_status.pending},
        "warnings": warnings,
    }

