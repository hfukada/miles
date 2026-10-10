"""MCP tools for route search (docs/design-terrain-and-route-search.md,
section 5). Miles/feet are converted to and from route_search's meters only
here."""

import json
from typing import cast

from mcp.server.fastmcp import FastMCP

from . import db, route_search
from .errors import reporting
from .fitness import MILE_M
from .offload import threaded

FT_PER_M = 3.28084


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
    @reporting(ValueError)
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
        starts: list[route_search.Start] | None = None,
        seed: int | None = None,
    ) -> str:
        """
        Find candidate running routes over an imported area's network graph
        (import_area first if the area isn't ready). Give the start(s) one way:
        - start_lat/start_lng alone: a single start.
        - start_lat/start_lng plus max_drive_minutes (at most 90): fans out over
          the trailheads and parking find_trailheads returns (best drive_k,
          default 8) plus the point itself. Drive times are straight-line
          estimates, flagged drive_time_estimated=true, when OSRM is down; tell
          the athlete and re-run later.
        - starts: a list of {"lat", "lng", "label"?, "drive_s"?} objects.

        distance_mi_min/distance_mi_max bound the length. shape is "loop",
        "out_and_back", "lollipop", or "any". min_trail_mi and min_runnable_pct
        (run plus grind, i.e. not hiking) are soft floors; gain_ft_min/
        gain_ft_max bound climbing. avoid_way_ids/include_way_ids are lists of
        OSM way ids; a route touching an avoided way is never returned.

        Returns up to n candidates (default 5): start, distance, trail/road
        mix, runnability, road exposure, and a candidate_id for
        get_route_candidate/save_route_candidate. When nothing meets every
        constraint, the closest few come back with near_miss=true, each listing
        what it misses. The same seed, request, and network reproduce the same
        candidates; the seed used is always returned.
        """
        with db.connection() as conn:
            start_list = route_search.resolve_starts(
                conn,
                area_id,
                starts=starts,
                origin=(start_lat, start_lng) if start_lat is not None and start_lng is not None else None,
                max_drive_minutes=max_drive_minutes,
                drive_k=drive_k,
            )
            if distance_mi_min is None or distance_mi_max is None:
                raise ValueError("find_routes needs distance_mi_min and distance_mi_max")
            if shape not in ("loop", "out_and_back", "lollipop", "any"):
                raise ValueError(f"invalid shape: {shape!r}")

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
                "avoid_way_ids": avoid_way_ids,
                "include_way_ids": include_way_ids,
                "seed": seed,
            }
            result = route_search.find_routes(conn, request)

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
        Full breakdown and geometry for a find_routes candidate. Candidates
        expire a day after their search.
        """
        with db.connection() as conn:
            return json.dumps(route_search.get_route_candidate(conn, candidate_id))

    @mcp.tool()
    @reporting(route_search.CandidateNotFoundError)
    def save_route_candidate(candidate_id: str, name: str) -> str:
        """
        Save a candidate as a route (source "generated") with a GPX built from
        its geometry; it then works like an uploaded route.
        """
        with db.connection() as conn:
            return json.dumps(route_search.save_route_candidate(conn, candidate_id, name))
