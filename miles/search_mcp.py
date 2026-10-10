"""MCP tools for route search (docs/design-terrain-and-route-search.md,
section 5). find_routes converts the athlete-facing miles/feet request into
route_search's meters-only RouteSearchRequest, runs it, and reports a
compact per-candidate breakdown back in miles/feet -- the unit conversion
happens only at this boundary, never inside route_search itself.
"""

import json
from typing import cast

from mcp.server.fastmcp import FastMCP

from . import db, route_search
from .errors import reporting
from .fitness import MILE_M
from .offload import threaded

FT_PER_M = 3.28084


def _parse_starts(raw: str | list) -> list[route_search.Start]:
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, list) or not parsed:
        raise ValueError('starts must be a non-empty JSON list of {"lat", "lng"} objects')
    out: list[route_search.Start] = []
    for i, item in enumerate(parsed):
        if (
            not isinstance(item, dict)
            or not _is_number(item.get("lat"))
            or not _is_number(item.get("lng"))
        ):
            raise ValueError(f'starts[{i}] must be an object with numeric "lat" and "lng"')
        out.append(cast(route_search.Start, item))
    return out


def _parse_way_ids(name: str, raw: str | list) -> list[int]:
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in parsed):
        raise ValueError(f"{name} must be a JSON list of integer OSM way ids")
    return parsed


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _start_summary(start: route_search.Start) -> dict:
    drive_s = start.get("drive_s")
    return {
        "lat": start["lat"],
        "lng": start["lng"],
        "label": start.get("label"),
        "drive_minutes": drive_s / 60.0 if drive_s is not None else None,
        "drive_time_estimated": start.get("drive_time_estimated", False),
    }


def _runnability_summary(runnability: dict) -> dict:
    out = {k: v for k, v in runnability.items() if k not in ("longest_hike_m", "steep_descent_m")}
    out["longest_hike_mi"] = runnability["longest_hike_m"] / MILE_M
    out["steep_descent_ft"] = runnability["steep_descent_m"] * FT_PER_M
    return out


def _candidate_summary(candidate: route_search.RouteCandidateOut) -> dict:
    score = candidate["score"]
    gain_m = score.get("gain_m")
    loss_m = score.get("loss_m")
    exposure = score["road_exposure"]
    return {
        "candidate_id": candidate["candidate_id"],
        "shape": candidate["shape"],
        "start": _start_summary(candidate["start"]),
        "distance_mi": candidate["distance_m"] / MILE_M,
        "trail_mi": score["trail_distance_m"] / MILE_M,
        "unpaved_road_mi": score["unpaved_road_distance_m"] / MILE_M,
        "paved_mi": score["paved_distance_m"] / MILE_M,
        "connector_mi": score["connector_distance_m"] / MILE_M,
        "gain_ft": gain_m * FT_PER_M if gain_m is not None else None,
        "loss_ft": loss_m * FT_PER_M if loss_m is not None else None,
        "has_elevation": score["has_elevation"],
        "elevation_coverage_pct": score["elevation_coverage_pct"],
        "runnability": _runnability_summary(score["runnability"]),
        "road_exposure": {
            "max_continuous_uncomfortable_mi": exposure["max_continuous_uncomfortable_m"] / MILE_M,
            "violation_count": len(exposure["violations"]),
            "totals_mi": {k: v / MILE_M for k, v in exposure["totals_m"].items()},
        },
        "missed_constraints": candidate["missed_constraints"],
    }


def register_search_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    @threaded
    def find_routes(
        area_id: int,
        start_lat: float | None = None,
        start_lng: float | None = None,
        max_drive_minutes: float | None = None,
        drive_k: int = 8,
        distance_mi_min: float | None = None,
        distance_mi_max: float | None = None,
        shape: str = "any",
        min_trail_mi: float | None = None,
        min_runnable_pct: float | None = None,
        gain_ft_min: float | None = None,
        gain_ft_max: float | None = None,
        avoid_way_ids: list[int] | None = None,
        include_way_ids: list[int] | None = None,
        n: int = 5,
        starts: list[dict[str, object]] | None = None,
        seed: int | None = None,
    ) -> str:
        """
        Find candidate running routes over an imported area's network graph
        (import_area first if the area isn't ready). Exactly one of these
        gives the start(s):
        - start_lat/start_lng alone: a single out-the-door start.
        - start_lat/start_lng plus max_drive_minutes: drive mode -- fans out
          over trailheads and trail-adjacent parking within that drive time
          of (start_lat, start_lng) (best drive_k, default 8, first), the
          same set find_trailheads returns, plus the point itself as a
          zero-drive-time start.
        - starts: a list of {"lat", "lng", "label"?, "drive_s"?} objects,
          for a caller that already has its own start set.

        distance_mi_min/distance_mi_max bound the route length. shape is
        "loop", "out_and_back", "lollipop", or "any" (default). min_trail_mi
        and min_runnable_pct (percent of the route that's comfortably
        runnable or grind -- runnable but slow, not steep enough to count as
        hiking) are soft floors; gain_ft_min/gain_ft_max bounds total
        climbing. avoid_way_ids/include_way_ids take lists of OSM way
        ids -- a route touching an avoided way is never returned, even as a
        near miss.

        In drive mode max_drive_minutes is at most 90. The drive times come
        from one OSRM request; if that service is down they are straight-line
        estimates and each start says so with drive_time_estimated=true
        (say that to the athlete, and re-run later for real times).

        Returns up to n candidates (default 5), each with its start (label
        and drive_minutes included) and a compact breakdown -- distance,
        trail/road mix, runnability, road-exposure summary -- plus a
        candidate_id for get_route_candidate/save_route_candidate. When
        nothing meets every constraint, returns the closest few instead
        (near_miss=true), each listing what it misses so the constraints
        can be relaxed deliberately.

        seed makes the search reproducible: the same seed, request, and
        network give the same candidates. The seed used is always returned,
        so a good result can be regenerated.
        """
        conn = db.connect()
        try:
            start_list = route_search.resolve_starts(
                conn,
                area_id,
                starts=_parse_starts(starts) if starts else None,
                origin=(start_lat, start_lng) if start_lat is not None and start_lng is not None else None,
                max_drive_minutes=max_drive_minutes,
                drive_k=drive_k,
            )
            if distance_mi_min is None or distance_mi_max is None:
                return json.dumps({"error": "find_routes needs distance_mi_min and distance_mi_max"})
            if shape not in ("loop", "out_and_back", "lollipop", "any"):
                return json.dumps({"error": f"invalid shape: {shape!r}"})

            request: route_search.RouteSearchRequest = {
                "area_id": area_id,
                "starts": start_list,
                "distance_m_min": distance_mi_min * MILE_M,
                "distance_m_max": distance_mi_max * MILE_M,
                "shape": cast(route_search.Shape, shape),
                "n": n,
                "min_trail_m": None if min_trail_mi is None else min_trail_mi * MILE_M,
                "min_runnable_pct": min_runnable_pct,
                "gain_m_min": None if gain_ft_min is None else gain_ft_min / FT_PER_M,
                "gain_m_max": None if gain_ft_max is None else gain_ft_max / FT_PER_M,
                "avoid_way_ids": _parse_way_ids("avoid_way_ids", avoid_way_ids) if avoid_way_ids else None,
                "include_way_ids": _parse_way_ids("include_way_ids", include_way_ids) if include_way_ids else None,
                "seed": seed,
            }
            result = route_search.find_routes(conn, request)
        except (ValueError, json.JSONDecodeError) as e:
            return json.dumps({"error": str(e)})
        finally:
            conn.close()

        return json.dumps(
            {
                "candidates": [_candidate_summary(c) for c in result["candidates"]],
                "near_miss": result["near_miss"],
                "budget_exceeded": result["budget_exceeded"],
                "seed": result["seed"],
            }
        )

    @mcp.tool()
    @reporting(route_search.CandidateNotFoundError)
    def get_route_candidate(candidate_id: str) -> str:
        """
        Full breakdown and geometry for one search candidate, by the
        candidate_id find_routes returned. Candidates are ephemeral and
        expire a day after the search that produced them.
        """
        conn = db.connect()
        try:
            return json.dumps(route_search.get_route_candidate(conn, candidate_id))
        finally:
            conn.close()

    @mcp.tool()
    @reporting(route_search.CandidateNotFoundError)
    def save_route_candidate(candidate_id: str, name: str) -> str:
        """
        Save a search candidate as a normal route (source "generated"),
        generating its GPX from the candidate's stored geometry. After this,
        compare_route_to_course, find_route_junctions, compose_route, and
        GPX export all apply to it exactly like an uploaded route.
        """
        conn = db.connect()
        try:
            return json.dumps(route_search.save_route_candidate(conn, candidate_id, name))
        finally:
            conn.close()
