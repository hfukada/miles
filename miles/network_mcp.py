"""MCP tools for the route network's preference profile and way overrides,
which route search and scoring read."""

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db, network
from .errors import reporting


def register_network_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_route_preferences() -> str:
        """
        The route-scoring preference profile: road-comfort thresholds, maximum
        continuous uncomfortable-road distance. Defaults apply until
        set_route_preferences changes them.
        """
        with db.connection() as conn:
            return json.dumps(network.get_route_preferences(conn))

    @mcp.tool()
    @reporting(ValueError)
    def set_route_preferences(patch: dict[str, Any]) -> str:
        """
        Update the preference profile. patch is a JSON object with any subset
        of get_route_preferences' top-level keys; dict-valued keys are merged
        into the existing value. Returns the full profile.
        """
        with db.connection() as conn:
            return json.dumps(network.set_route_preferences(conn, patch))

    @mcp.tool()
    @reporting(ValueError)
    def set_way_override(way_id: int, verdict: str, note: str | None = None) -> str:
        """
        Mark an OSM way "comfortable" or "avoid" regardless of its tags; survives
        network rebuilds.
        """
        with db.connection() as conn:
            return json.dumps(network.set_way_override(conn, way_id, verdict, note))
