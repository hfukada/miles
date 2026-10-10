"""HTTP API for drive-to trailhead search -- mounted into miles/api.py
alongside the other routers. No auth, matching every other endpoint in this
app.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, drive_starts

router = APIRouter()


class DriveStartOut(BaseModel):
    lat: float
    lng: float
    label: str
    drive_minutes: float
    kind: str
    osm_node_id: int | None
    trail_miles_nearby: float
    drive_time_estimated: bool


@router.get("/api/areas/{area_id}/trailheads")
def get_trailheads(area_id: int, lat: float, lng: float, max_drive_minutes: float, k: int = 8) -> list[DriveStartOut]:
    try:
        with db.connection() as conn:
            starts = drive_starts.drive_starts(conn, area_id, lat, lng, max_drive_minutes, k=k)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [DriveStartOut(**drive_starts.athlete_view(s)) for s in starts]
