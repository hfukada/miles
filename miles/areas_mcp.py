"""MCP tools for area import, registered from mcp_server.py with one call."""

import json

from mcp.server.fastmcp import FastMCP

from . import areas as areas_service
from . import db
from .errors import reporting
from .offload import threaded


def register_area_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    @threaded
    @reporting(areas_service.AreaImportError, ValueError)
    def import_area(
        place: str | None = None,
        lat: float | None = None,
        lng: float | None = None,
        radius_km: float | None = None,
        drive_minutes: float | None = None,
        label: str = "",
        pinned: bool | None = None,
        region: str | None = None,
        refresh: bool = False,
        refresh_geocode: bool = False,
    ) -> str:
        """
        Import the map-data and elevation cache for a region, keyed by label;
        the same label reuses the area and its cached data. Map data never
        refreshes on its own: refresh=true re-fetches it (at most once a day
        per region), and status reports data_as_of and refresh_suggested
        (over ~180 days old). refresh_geocode=true re-looks-up `place`.

        Center on `place` (free text, geocoded) or lat/lng. Size with radius_km
        (at most 60) or drive_minutes (rough 1 km/minute, not an isochrone).
        pinned=true keeps the area indefinitely; unpinned areas are collected
        after 60 days unused. When refreshing, leave pinned and region unset
        to keep what the area has.

        The regional extract is chosen automatically (smallest region, or
        several at a border); pass region (Geofabrik path, comma-separated
        for several) only when the athlete names one. Extracts over 2 GB are
        refused with the smaller regions named in the error.

        Returns immediately with status and an estimate while a background
        import runs; poll get_area_status(area_id), not in a tight loop (a
        first import can take minutes). Geocoding is rate limited: reuse
        labels, pass lat/lng once known, and do not retry a failed place in
        a loop.
        """
        with db.connection() as conn:
            return json.dumps(
                areas_service.import_area(
                    conn,
                    place=place,
                    lat=lat,
                    lng=lng,
                    radius_km=radius_km,
                    drive_minutes=drive_minutes,
                    label=label,
                    pinned=pinned,
                    region=region,
                    refresh=refresh,
                    refresh_geocode=refresh_geocode,
                )
            )

    @mcp.tool()
    def list_areas() -> str:
        """List every imported area with label, center, radius, pinned, status,
        and last-used/imported times."""
        with db.connection() as conn:
            return json.dumps(areas_service.list_areas(conn))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError)
    def get_area_status(area_id: int) -> str:
        """Import status of one area: "new", "importing" (phase/done/total),
        "ready", or "error" (with message), plus data_as_of and
        refresh_suggested (re-run import_area with refresh=true)."""
        with db.connection() as conn:
            return json.dumps(areas_service.get_area_status(conn, area_id))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError)
    def pin_area(area_id: int) -> str:
        """Keep an area indefinitely instead of collecting it after 60 days unused."""
        with db.connection() as conn:
            return json.dumps(areas_service.pin_area(conn, area_id))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError)
    def unpin_area(area_id: int) -> str:
        """Return an area to the 60-days-unused collection lifecycle."""
        with db.connection() as conn:
            return json.dumps(areas_service.unpin_area(conn, area_id))
