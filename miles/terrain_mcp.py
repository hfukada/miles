"""MCP tools for the terrain model (docs/design-terrain-and-route-search.md,
section 5). A separate module, registered from mcp_server.py with a single
register_terrain_tools(mcp) call, so that file's own edit stays a one-liner."""

import functools
import json
import sqlite3
from collections.abc import Callable
from typing import ParamSpec

from mcp.server.fastmcp import FastMCP

from . import routes as routes_service
from . import terrain
from .errors import reporting

P = ParamSpec("P")


def _terrain_conn() -> sqlite3.Connection:
    return terrain.connect()


def _reporting_rebuild(tool: Callable[P, str]) -> Callable[P, str]:
    """Answer with an error payload, not a protocol failure, while the terrain
    model is being rebuilt."""

    @functools.wraps(tool)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return tool(*args, **kwargs)
        except terrain.TerrainRebuildingError as e:
            return json.dumps({"error": str(e), "status": "rebuilding"})

    return wrapper


def register_terrain_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    @_reporting_rebuild
    def get_terrain_profile(window_days: int | None = None) -> str:
        """
        The athlete's current terrain model: per-2%-grade-bin median speed,
        grade-adjusted efficiency, walk fraction, and sample count over a
        rolling window (window_days, default the athlete's configured
        window), plus the derived walk-onset grade, comfortable-descent
        grade, and an overall confidence level. Bins with too little data
        fall back to a generic default and report sample_count 0.
        """
        conn = _terrain_conn()
        return json.dumps(terrain.get_terrain_profile(conn, window_days=window_days))

    @mcp.tool()
    @_reporting_rebuild
    @reporting(ValueError)
    def get_terrain_trend(
        metric: str,
        grade_min_pct: float | None = None,
        grade_max_pct: float | None = None,
        since: str | None = None,
    ) -> str:
        """
        A weekly-snapshot series for one terrain-model metric: either
        "walk_onset_grade_pct" or "descent_comfort_grade_pct" (no grade
        range needed), or "median_speed_mps" / "efficiency_median" /
        "walk_fraction" averaged over grade_min_pct..grade_max_pct (both
        required for those). since is an ISO date lower bound on the
        snapshot's as_of date.
        """
        conn = _terrain_conn()
        grade_range = (grade_min_pct, grade_max_pct) if grade_min_pct is not None and grade_max_pct is not None else None
        return json.dumps(terrain.get_terrain_trend(conn, metric, grade_range=grade_range, since=since))

    @mcp.tool()
    @_reporting_rebuild
    @reporting(terrain.TerrainDataError)
    def analyze_activity_terrain(activity_id: int) -> str:
        """
        Each climb and descent of one activity (the same segmentation used
        for routes) against the athlete's current terrain model: actual vs.
        expected speed for that grade/fatigue/temperature, as a residual and
        a percentile of the athlete's own history at that grade. Requires
        the activity's streams to already be cached and to include an
        altitude stream (see the terrain backfill job) -- returns an error
        otherwise. GPS is not needed: the analysis reads recorded altitude.
        """
        conn = _terrain_conn()
        return json.dumps(terrain.analyze_activity_terrain(conn, activity_id))

    @mcp.tool()
    @_reporting_rebuild
    @reporting(routes_service.RouteNotFoundError)
    def score_route(route_id: int) -> str:
        """
        Runnability breakdown of a stored route's grade profile, read
        through the athlete's current terrain model: percent run/grind/hike,
        longest continuous hike, steep-descent distance, and a course gap
        analysis (grade histogram weighted by the athlete's own per-bin
        efficiency deficit -- the grades most worth practicing for this
        route). Routes with no elevation data return has_elevation=false.
        """
        conn = _terrain_conn()
        return json.dumps(terrain.score_route(conn, route_id))
