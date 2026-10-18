"""HTTP API behind candidates.html: a shared set of route candidates, one
candidate's classed geometry, and saving one as a route. No auth, like the
rest of the app."""

from fastapi import APIRouter, HTTPException

from . import candidate_sets, db, route_search, terrain
from .fitness import MILE_M

router = APIRouter()


@router.get("/api/candidate-sets/{set_id}")
def get_candidate_set(set_id: str) -> candidate_sets.CandidateSetView:
    with db.connection() as conn:
        try:
            return candidate_sets.get_set(conn, set_id)
        except (candidate_sets.CandidateSetNotFoundError, route_search.CandidateNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/candidates/{candidate_id}")
def get_candidate_points(candidate_id: str) -> list[terrain.RoutePointClass]:
    with db.connection() as conn:
        try:
            candidate = route_search.get_route_candidate(conn, candidate_id)
        except route_search.CandidateNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return terrain.geometry_point_classes(conn, candidate["geometry"])


@router.post("/api/candidates/{candidate_id}/save")
def save_candidate(candidate_id: str) -> dict:
    """Saves the candidate as a route named for its shape and distance."""
    with db.connection() as conn:
        try:
            candidate = route_search.get_route_candidate(conn, candidate_id)
            name = f"{candidate['shape'].replace('_', ' ').capitalize()} {candidate['distance_m'] / MILE_M:.1f} mi"
            return route_search.save_route_candidate(conn, candidate_id, name)
        except route_search.CandidateNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
