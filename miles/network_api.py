"""HTTP API for the route network's preference profile and way overrides."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, network

router = APIRouter()


@router.get("/api/network/preferences")
def get_preferences() -> dict:
    with db.connection() as conn:
        return network.get_route_preferences(conn)


class PreferencesPatchBody(BaseModel):
    patch: dict


@router.patch("/api/network/preferences")
def patch_preferences(body: PreferencesPatchBody) -> dict:
    try:
        with db.connection() as conn:
            return network.set_route_preferences(conn, body.patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class WayOverrideBody(BaseModel):
    way_id: int
    verdict: str
    note: str | None = None


@router.post("/api/network/way-overrides")
def post_way_override(body: WayOverrideBody) -> dict:
    try:
        with db.connection() as conn:
            return network.set_way_override(conn, body.way_id, body.verdict, body.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
