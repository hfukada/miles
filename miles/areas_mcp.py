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
        Import the open-map-data + elevation cache for a region, keyed by
        label -- calling this again with the same label reuses the area and
        its cached map data as they are. Map data never refreshes on its own:
        pass refresh=true to re-fetch it (a newer Geofabrik extract if one
        exists, at most once a day per region). Area
        status reports data_as_of and sets refresh_suggested once that date
        is more than ~180 days old. Pass refresh_geocode=true to look `place`
        up again instead of trusting its cached coordinates.

        Center the area on either place (free-text geocoded via Nominatim,
        e.g. "Example Town") or an explicit lat/lng. Size it with either
        radius_km or drive_minutes (a rough 1 km/minute heuristic, capped --
        not a real drive-time isochrone). Pass pinned=true for a place kept
        indefinitely (e.g. home); unpinned areas are garbage collected after
        60 days unused. Leave pinned (and region) unset when refreshing an
        existing area to keep whatever it already has.

        Map data is cut from a regional OpenStreetMap extract (Geofabrik),
        downloaded on first use and shared by every area inside it. The
        smallest region containing the area is chosen automatically, or, at
        a border, the smaller regions that together cover it (each
        downloaded and merged); pass region (a Geofabrik path such as
        "continent/country/region", or several comma-separated) only when
        the athlete names one. An unlisted path is refused. radius_km is at
        most 60. An extract over 2 GB is refused with the smaller regions
        that would cover the area named in the error; the operator can allow
        it or upload a cut with miles-area-import --pbf.

        Starts a background import and returns immediately with its status
        and an estimate (source, tile count, expected seconds once an extract
        is registered); poll get_area_status(area_id) for progress, and not
        in a tight loop: a first import downloads a large file and can take
        minutes. place is geocoded with Nominatim
        (https://operations.osmfoundation.org/policies/nominatim/): at most
        one request a second, no bulk or looped lookups, so reuse a label,
        pass lat/lng once known, and do not retry a failed place in a loop.
        """
        conn = db.connect()
        try:
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
        finally:
            conn.close()

    @mcp.tool()
    def list_areas() -> str:
        """List every imported area: label, center, radius, pinned, status,
        and when it was last used or imported."""
        conn = db.connect()
        return json.dumps(areas_service.list_areas(conn))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError)
    def get_area_status(area_id: int) -> str:
        """Current status of one area's import: "new" (created, not yet
        started), "importing" (with phase/done/total progress), "ready", or
        "error" (with the error message). Also
        data_as_of (the date the area's map data is as of) and
        refresh_suggested (true once that is over ~180 days old; re-run
        import_area with refresh=true)."""
        conn = db.connect()
        return json.dumps(areas_service.get_area_status(conn, area_id))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError)
    def pin_area(area_id: int) -> str:
        """Pin an area so it's kept indefinitely instead of garbage
        collected after 60 days unused."""
        conn = db.connect()
        return json.dumps(areas_service.pin_area(conn, area_id))

    @mcp.tool()
    @reporting(areas_service.AreaNotFoundError)
    def unpin_area(area_id: int) -> str:
        """Unpin an area, returning it to the normal 60-day-unused-GC
        lifecycle."""
        conn = db.connect()
        return json.dumps(areas_service.unpin_area(conn, area_id))
