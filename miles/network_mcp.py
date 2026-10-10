"""MCP tools for the route network's preference profile and way overrides,
which route search and scoring read."""

import json

from mcp.server.fastmcp import FastMCP

from . import db, network


def register_network_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_route_preferences() -> str:
        """
        The route-scoring preference profile: road-comfort thresholds, maximum
        continuous uncomfortable-road distance, surface and technicality
        preferences, and the highway-alongside distance and penalty. Defaults
        apply until set_route_preferences changes them.
        """
        with db.connection() as conn:
            return json.dumps(network.get_route_preferences(conn))

    @mcp.tool()
    def set_route_preferences(patch: str) -> str:
        """
        Update the preference profile. patch is a JSON object with any subset
        of get_route_preferences' top-level keys; dict-valued keys are merged
        into the existing value. Returns the full profile.
        """
        try:
            parsed = json.loads(patch)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"patch must be a JSON object: {e}"})
        if not isinstance(parsed, dict):
            return json.dumps({"error": "patch must be a JSON object"})
        try:
            with db.connection() as conn:
                return json.dumps(network.set_route_preferences(conn, parsed))
        except ValueError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    def set_way_override(way_id: int, verdict: str, note: str | None = None) -> str:
        """
        Mark an OSM way "comfortable" or "avoid" regardless of its tags; survives
        network rebuilds.
        """
        try:
            with db.connection() as conn:
                return json.dumps(network.set_way_override(conn, way_id, verdict, note))
        except ValueError as e:
            return json.dumps({"error": str(e)})
