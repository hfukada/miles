"""MCP tools for the terrain model (docs/design-terrain-and-route-search.md,
section 5)."""

import functools
import json
from collections.abc import Callable
from typing import ParamSpec

from mcp.server.fastmcp import FastMCP

from . import db, terrain
from . import routes as routes_service
from .errors import reporting

P = ParamSpec("P")


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
    def get_terrain_profile(window_days: int = terrain.ROLLING_WINDOW_DAYS) -> str:
        """
        The athlete's terrain model: per-2%-grade-bin median speed,
        grade-adjusted efficiency, walk fraction, and sample count over a
        rolling window (window_days, default 90), plus the
        walk-onset grade, comfortable-descent grade, and a confidence level.
        Bins with too little data use a generic default (sample_count 0).
        """
        with db.connection(terrain.connect) as conn:
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
        A weekly-snapshot series for one terrain-model metric:
        "walk_onset_grade_pct" or "descent_comfort_grade_pct", or
        "median_speed_mps" / "efficiency_median" / "walk_fraction" averaged
        over grade_min_pct..grade_max_pct (both required for those). since is
        an ISO date lower bound.
        """
        grade_range = (grade_min_pct, grade_max_pct) if grade_min_pct is not None and grade_max_pct is not None else None
        with db.connection(terrain.connect) as conn:
            return json.dumps(terrain.get_terrain_trend(conn, metric, grade_range=grade_range, since=since))

    @mcp.tool()
    @_reporting_rebuild
    @reporting(terrain.TerrainDataError)
    def analyze_activity_terrain(activity_id: int) -> str:
        """
        Each climb and descent of one activity against the current terrain
        model: actual vs. expected speed for that grade/fatigue/temperature,
        as a residual and a percentile of the athlete's own history at that
        grade. Needs the activity's streams cached with an altitude stream
        (see the terrain backfill), else returns an error. GPS is not needed.
        """
        with db.connection(terrain.connect) as conn:
            return json.dumps(terrain.analyze_activity_terrain(conn, activity_id))

    @mcp.tool()
    @_reporting_rebuild
    @reporting(routes_service.RouteNotFoundError)
    def score_route(route_id: int) -> str:
        """
        Runnability of a stored route's grade profile under the current
        terrain model: percent run/grind/hike, longest continuous hike,
        steep-descent distance, and a gap analysis (grade histogram weighted
        by the athlete's per-bin efficiency deficit: the grades most worth
        practicing). No elevation data returns has_elevation=false.
        """
        with db.connection(terrain.connect) as conn:
            return json.dumps(terrain.score_route(conn, route_id))
