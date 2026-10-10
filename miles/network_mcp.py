"""MCP tools for the route network's preference profile and way overrides --
the athlete-facing knobs over network.py's build/score pipeline. Route
search (search_mcp.py) and route scoring read the profile and overrides set
here; neither graph browsing nor path scoring is exposed in this module.
"""

import json

from mcp.server.fastmcp import FastMCP

from . import db, network


def register_network_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_route_preferences() -> str:
        """
        The stored route-scoring preference profile: road-comfort speed and
        class thresholds, the maximum continuous distance tolerated on an
        uncomfortable road, surface and technicality preferences, and the
        trail-alongside-a-highway distance and penalty. Defaults apply until
        set_route_preferences changes them.
        """
        conn = db.connect()
        return json.dumps(network.get_route_preferences(conn))

    @mcp.tool()
    def set_route_preferences(patch: str) -> str:
        """
        Update the route-scoring preference profile. patch is a JSON object
        with any subset of the top-level keys returned by
        get_route_preferences (road_comfort, max_continuous_uncomfortable_m,
        surface_weight, technicality_tolerance, highway_alongside);
        dict-valued keys are merged into the existing profile rather than
        replaced whole. Returns the full profile after the update.
        """
        conn = db.connect()
        try:
            parsed = json.loads(patch)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"patch must be a JSON object: {e}"})
        if not isinstance(parsed, dict):
            return json.dumps({"error": "patch must be a JSON object"})
        try:
            return json.dumps(network.set_route_preferences(conn, parsed))
        except ValueError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    def set_way_override(way_id: int, verdict: str, note: str | None = None) -> str:
        """
        Mark a specific OSM way "comfortable" or "avoid" regardless of what
        its tags would otherwise score -- athlete judgment that survives a
        network rebuild. verdict must be "comfortable" or "avoid".
        """
        conn = db.connect()
        try:
            return json.dumps(network.set_way_override(conn, way_id, verdict, note))
        except ValueError as e:
            return json.dumps({"error": str(e)})
