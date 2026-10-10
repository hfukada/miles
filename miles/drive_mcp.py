"""MCP tool for drive-to trailhead search; results are in miles/minutes
(drive_starts.athlete_view), while drive_starts.py works in meters/seconds."""

import json

from mcp.server.fastmcp import FastMCP

from . import db, drive_starts
from .offload import threaded


def register_drive_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    @threaded
    def find_trailheads(area_id: int, lat: float, lng: float, max_drive_minutes: float, k: int = 8) -> str:
        """
        Trailheads and trail-adjacent parking in an imported area within
        max_drive_minutes (at most 90) of (lat, lng), best k first by drive
        time and nearby trail. The point itself is always first, as a
        zero-drive-time start.

        Each result has lat/lng, label, drive_minutes, kind
        ("origin"/"trailhead"/"parking"), the OSM id (null for the origin;
        negated way id for an outline), trail_miles_nearby, and
        drive_time_estimated (true when routing was unavailable and the time
        is a straight-line estimate).
        """
        try:
            with db.connection() as conn:
                starts = drive_starts.drive_starts(conn, area_id, lat, lng, max_drive_minutes, k=k)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        return json.dumps([drive_starts.athlete_view(s) for s in starts])
