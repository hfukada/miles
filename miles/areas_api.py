"""HTTP API for area import -- mounted into miles/api.py alongside the
existing routers. No auth, matching every other endpoint in this app.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import areas as areas_service
from . import db

router = APIRouter()


class ImportAreaBody(BaseModel):
    place: str | None = None
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = None
    drive_minutes: float | None = None
    label: str
    pinned: bool | None = None
    region: str | None = None
    refresh: bool = False
    refresh_geocode: bool = False


@router.post("/api/areas")
def post_import_area(body: ImportAreaBody) -> areas_service.AreaStatus:
    conn = db.connect()
    try:
        return areas_service.import_area(
            conn,
            place=body.place,
            lat=body.lat,
            lng=body.lng,
            radius_km=body.radius_km,
            drive_minutes=body.drive_minutes,
            label=body.label,
            pinned=body.pinned,
            region=body.region,
            refresh=body.refresh,
            refresh_geocode=body.refresh_geocode,
        )
    except (areas_service.AreaImportError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class ImportTileBody(BaseModel):
    gx: int
    gy: int
    elements: list[dict]


class ImportTilesBody(BaseModel):
    label: str
    center_lat: float
    center_lng: float
    radius_km: float
    tiles: list[ImportTileBody]
    pinned: bool | None = None
    source: str = "extract:cli"


@router.post("/api/areas/import-tiles")
def post_import_tiles(body: ImportTilesBody) -> areas_service.AreaStatus:
    """Receiving end of miles-area-import: tiles already cut locally from a
    pbf the CLI's caller has but this host doesn't (or can't reach) --
    skips straight to the elevation + network-build tail import_area's
    background job would otherwise run after fetching them itself."""
    conn = db.connect()
    try:
        return areas_service.import_tiles(
            conn,
            label=body.label,
            center_lat=body.center_lat,
            center_lng=body.center_lng,
            radius_km=body.radius_km,
            tile_payloads=[{"gx": t.gx, "gy": t.gy, "elements": t.elements} for t in body.tiles],
            pinned=body.pinned,
            source=body.source,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/areas")
def get_areas() -> list[areas_service.AreaSummary]:
    conn = db.connect()
    return areas_service.list_areas(conn)


@router.get("/api/areas/{area_id}/status")
def get_area_status(area_id: int) -> areas_service.AreaStatus:
    conn = db.connect()
    return areas_service.get_area_status(conn, area_id)


@router.get("/api/areas/{area_id}")
def get_area(area_id: int) -> areas_service.AreaDetail:
    conn = db.connect()
    return areas_service.get_area(conn, area_id)


@router.post("/api/areas/{area_id}/pin")
def post_pin_area(area_id: int) -> areas_service.AreaDetail:
    conn = db.connect()
    return areas_service.pin_area(conn, area_id)


@router.post("/api/areas/{area_id}/unpin")
def post_unpin_area(area_id: int) -> areas_service.AreaDetail:
    conn = db.connect()
    return areas_service.unpin_area(conn, area_id)
