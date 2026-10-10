"""HTTP API for the route network's preference profile and way overrides.
Mounted into miles/api.py alongside the other routers. No auth, matching
every other endpoint in this app.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, network

router = APIRouter()


@router.get("/api/network/preferences")
def get_preferences() -> dict:
    conn = db.connect()
    return network.get_route_preferences(conn)


class PreferencesPatchBody(BaseModel):
    patch: dict


@router.patch("/api/network/preferences")
def patch_preferences(body: PreferencesPatchBody) -> dict:
    conn = db.connect()
    try:
        return network.set_route_preferences(conn, body.patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class WayOverrideBody(BaseModel):
    way_id: int
    verdict: str
    note: str | None = None


@router.post("/api/network/way-overrides")
def post_way_override(body: WayOverrideBody) -> dict:
    conn = db.connect()
    try:
        return network.set_way_override(conn, body.way_id, body.verdict, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
