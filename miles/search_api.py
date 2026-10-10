"""HTTP API for route search. Mounted into miles/api.py alongside the other
routers. Works in meters throughout, matching route_search.py itself --
unit conversion for people is search_mcp.py's job, not this one's.
"""

from typing import cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, route_search

router = APIRouter()


class StartBody(BaseModel):
    lat: float
    lng: float
    label: str | None = None
    drive_s: float | None = None


class FindRoutesBody(BaseModel):
    area_id: int
    starts: list[StartBody] | None = None
    drive_origin: StartBody | None = None
    max_drive_minutes: float | None = None
    drive_k: int = 8
    distance_m_min: float
    distance_m_max: float
    shape: route_search.Shape = "any"
    min_trail_m: float | None = None
    min_runnable_pct: float | None = None
    gain_m_min: float | None = None
    gain_m_max: float | None = None
    include_way_ids: list[int] | None = None
    avoid_way_ids: list[int] | None = None
    n: int | None = None
    time_budget_s: float | None = None
    seed: int | None = None


@router.post("/api/network/search")
def find_routes(body: FindRoutesBody) -> route_search.RouteSearchResult:
    conn = db.connect()
    drive_mode = body.max_drive_minutes is not None
    try:
        start_list = route_search.resolve_starts(
            conn,
            body.area_id,
            starts=[cast(route_search.Start, s.model_dump()) for s in body.starts or []],
            origin=(body.drive_origin.lat, body.drive_origin.lng) if body.drive_origin and drive_mode else None,
            max_drive_minutes=body.max_drive_minutes,
            drive_k=body.drive_k,
            origin_field="drive_origin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request: route_search.RouteSearchRequest = {
        "area_id": body.area_id,
        "starts": start_list,
        "distance_m_min": body.distance_m_min,
        "distance_m_max": body.distance_m_max,
        "shape": body.shape,
        "n": body.n or route_search.DEFAULT_N,
        "time_budget_s": body.time_budget_s or route_search.DEFAULT_TIME_BUDGET_S,
        "min_trail_m": body.min_trail_m,
        "min_runnable_pct": body.min_runnable_pct,
        "gain_m_min": body.gain_m_min,
        "gain_m_max": body.gain_m_max,
        "include_way_ids": body.include_way_ids,
        "avoid_way_ids": body.avoid_way_ids,
        "seed": body.seed,
    }
    try:
        return route_search.find_routes(conn, request)
    except (route_search.AreaRebuildingError, route_search.AreaNotReadyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/network/search/candidates/{candidate_id}")
def get_route_candidate(candidate_id: str) -> route_search.RouteCandidateOut:
    conn = db.connect()
    return route_search.get_route_candidate(conn, candidate_id)


class SaveCandidateBody(BaseModel):
    name: str


@router.post("/api/network/search/candidates/{candidate_id}/save")
def save_route_candidate(candidate_id: str, body: SaveCandidateBody) -> dict:
    conn = db.connect()
    return route_search.save_route_candidate(conn, candidate_id, body.name)
