"""MCP tool for drive-to trailhead search, registered from mcp_server.py
with one call. Results are reported in miles/minutes (drive_starts.athlete_view);
drive_starts.py itself works in meters/seconds like the rest of the network stack.
"""

import json

from mcp.server.fastmcp import FastMCP

from . import db, drive_starts
from .offload import threaded


def register_drive_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    @threaded
    def find_trailheads(area_id: int, lat: float, lng: float, max_drive_minutes: float, k: int = 8) -> str:
        """
        Trailheads and trail-adjacent parking in an imported area reachable
        within max_drive_minutes of (lat, lng), best K first by a blend of
        drive time and how much trail each start puts within reach. The
        point itself is always included first as an "out the door" start
        with zero drive time.

        Each result carries lat/lng, a label, drive_minutes, kind
        ("origin"/"trailhead"/"parking"), the OSM id (null for the origin;
        the node id, or the negated way id for a start mapped as an
        outline), trail_miles_nearby, and drive_time_estimated (true when the
        routing service was unavailable and the time is a straight-line
        estimate). max_drive_minutes is at most 90.
        """
        conn = db.connect()
        try:
            starts = drive_starts.drive_starts(conn, area_id, lat, lng, max_drive_minutes, k=k)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        finally:
            conn.close()
        return json.dumps([drive_starts.athlete_view(s) for s in starts])
