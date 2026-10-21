"""HTTP face of the background route search (search_jobs), behind
create-route.html: start one, follow it, cancel it. Candidates come back as
the cards candidates.html draws. No auth, like the rest of the app."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import candidate_sets, db, route_search, search_jobs
from .fitness import MILE_M

router = APIRouter()


class SearchBody(BaseModel):
    area_id: int
    start_lat: float
    start_lng: float
    distance_mi_min: float
    distance_mi_max: float
    shape: Literal["any", "loop", "out_and_back", "lollipop"] = "any"
    purpose: str | None = None
    repeat_value: float = route_search.DEFAULT_REPEAT_VALUE
    surface_preference: str | None = None
    n: int = route_search.DEFAULT_N
    seed: int | None = None


def _reply(view: dict) -> dict:
    out = {
        "search_id": view["search_id"],
        "status": view["status"],
        "elapsed_s": view["elapsed_s"],
        "qualifying": view["qualifying"],
        "candidates": [candidate_sets.card(c, None) for c in view["candidates"]],
        "error": view["error"],
    }
    if view["result"] is not None:
        out["near_miss"] = view["result"]["near_miss"]
        out["budget_exceeded"] = view["result"]["budget_exceeded"]
    return out


@router.post("/api/searches")
def start_search(body: SearchBody) -> dict:
    request: route_search.RouteSearchRequest = {
        "area_id": body.area_id,
        "starts": [{"lat": body.start_lat, "lng": body.start_lng}],
        "distance_m_min": body.distance_mi_min * MILE_M,
        "distance_m_max": body.distance_mi_max * MILE_M,
        "shape": body.shape,
        "n": body.n,
        "seed": body.seed,
        "repeat_value": body.repeat_value,
        "surface_preference": body.surface_preference,
        "purpose": body.purpose,
    }
    try:
        with db.connection() as conn:
            return _reply(search_jobs.start(conn, request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/searches/{search_id}")
def get_search(search_id: str, wait_s: float = 0.0) -> dict:
    try:
        return _reply(search_jobs.get(search_id, wait_s))
    except search_jobs.UnknownSearchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/searches/{search_id}/cancel")
def cancel_search(search_id: str) -> dict:
    try:
        return _reply(search_jobs.cancel(search_id))
    except search_jobs.UnknownSearchError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
