"""MCP tool for the outbound-request ledger, registered from mcp_server.py
with one call."""

import json

from mcp.server.fastmcp import FastMCP

from . import db, events


def register_outbound_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_outbound_requests(days: int = 30) -> str:
        """
        How many requests miles made to each outside service, per day, over
        the last `days` days (default 30, today included). One row per day,
        service, host, endpoint kind, and status; status is an HTTP code, or
        "error" for a connection failure. Services: strava (kind is
        activities, detail, laps, streams, token, or other), nominatim, geofabrik,
        osrm, terrarium, open-meteo. host is the hostname actually requested,
        so a self-hosted override shows up as its own host. Counts cover every
        miles process (the sync CLI included) from the day recording began.
        """
        conn = db.connect()
        try:
            return json.dumps(events.daily_counts(conn, days))
        finally:
            conn.close()
