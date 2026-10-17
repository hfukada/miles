"""Human-readable place labels for routes ("Example Town, Colorado").

A route's location is filled the first time it is asked for: one reverse
geocode of the route's start point, cached by rounded coordinates in
geocode.py and stored on the route with source 'auto'. An athlete-set
location (source 'athlete') is never replaced. Listing routes never asks
Nominatim. Where it cannot be asked, or fails, the label of an imported area
containing the start point stands in; that stand-in is not stored, so a later
request tries Nominatim again.
"""

import sqlite3

from . import geocode, tiles

SOURCE_AUTO = "auto"
SOURCE_ATHLETE = "athlete"


def area_label_at(conn: sqlite3.Connection, lat: float | None, lng: float | None) -> str | None:
    """The label of the smallest ready area whose bounding box holds the point."""
    if lat is None or lng is None:
        return None
    best: tuple[float, str] | None = None
    for area in conn.execute("SELECT label, center_lat, center_lng, radius_km FROM areas WHERE status = 'ready'"):
        south, west, north, east = tiles.bbox_for_center_radius(area["center_lat"], area["center_lng"], area["radius_km"])
        if south <= lat <= north and west <= lng <= east and (best is None or area["radius_km"] < best[0]):
            best = (area["radius_km"], area["label"])
    return best[1] if best else None


def stored_or_area_label(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """The location a listing shows: the stored one, else an area's label."""
    return row["location"] or area_label_at(conn, row["start_lat"], row["start_lng"])


def resolve_location(conn: sqlite3.Connection, route_id: int) -> str | None:
    """The route's location, reverse geocoding its start point once when none
    is stored. Does not raise for a Nominatim failure or a missing
    MILES_CONTACT."""
    row = conn.execute(
        "SELECT location, location_source, start_lat, start_lng FROM routes WHERE route_id = ?", (route_id,)
    ).fetchone()
    if row is None:
        return None
    if row["location"]:
        return row["location"]
    lat, lng = row["start_lat"], row["start_lng"]
    if lat is None or lng is None:
        return None
    try:
        label = geocode.reverse(conn, lat, lng)
    except geocode.GeocodeError:
        label = None
    if label and row["location_source"] != SOURCE_ATHLETE:
        conn.execute(
            "UPDATE routes SET location = ?, location_source = ? WHERE route_id = ? AND location IS NULL",
            (label, SOURCE_AUTO, route_id),
        )
        conn.commit()
        return label
    return area_label_at(conn, lat, lng)
