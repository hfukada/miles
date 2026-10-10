"""HTTP receiving end of miles-area-import. No auth, like every other endpoint."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import areas as areas_service
from . import db

router = APIRouter()


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
    """Receiving end of miles-area-import: tiles already cut locally; skips to
    the elevation and network-build tail."""
    try:
        with db.connection() as conn:
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
