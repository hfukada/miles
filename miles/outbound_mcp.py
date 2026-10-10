"""MCP tool for the outbound-request ledger."""

import json

from mcp.server.fastmcp import FastMCP

from . import db, events


def register_outbound_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_outbound_requests(days: int = 30) -> str:
        """
        Requests made to each outside service per day over the last `days`
        days (default 30, today included): one row per day, service, host,
        endpoint kind, and status (HTTP code, or "error" for a connection
        failure). Services: strava (kind activities, detail, laps, streams,
        token, other), nominatim, geofabrik, osrm, terrarium, open-meteo.
        Counts cover every miles process since recording began.
        """
        with db.connection() as conn:
            return json.dumps(events.daily_counts(conn, days))
