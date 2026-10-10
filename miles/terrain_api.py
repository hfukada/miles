"""HTTP API for the terrain model: profile/trend/analysis/scoring reads and
the stream-backfill job's start/status. No auth, like the rest of the app.
"""

import logging
import threading

from fastapi import APIRouter, HTTPException

from . import db, terrain, terrain_backfill
from .errors import describe_error

router = APIRouter()
_logger = logging.getLogger(__name__)


@router.get("/api/terrain/profile")
def get_terrain_profile(window_days: int = terrain.ROLLING_WINDOW_DAYS) -> terrain.TerrainProfile:
    with db.connection(terrain.connect) as conn:
        return terrain.get_terrain_profile(conn, window_days=window_days)


@router.get("/api/terrain/trend")
def get_terrain_trend(
    metric: str,
    grade_min_pct: float | None = None,
    grade_max_pct: float | None = None,
    since: str | None = None,
) -> list[terrain.TerrainTrendPoint]:
    with db.connection(terrain.connect) as conn:
        grade_range = (grade_min_pct, grade_max_pct) if grade_min_pct is not None and grade_max_pct is not None else None
        try:
            return terrain.get_terrain_trend(conn, metric, grade_range=grade_range, since=since)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/terrain/activities/{activity_id}")
def get_activity_terrain(activity_id: int) -> terrain.ActivityTerrainAnalysis:
    with db.connection(terrain.connect) as conn:
        try:
            return terrain.analyze_activity_terrain(conn, activity_id)
        except terrain.TerrainDataError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/terrain/routes/{route_id}")
def get_route_terrain_score(route_id: int) -> terrain.RouteTerrainScore:
    with db.connection(terrain.connect) as conn:
        return terrain.score_route(conn, route_id)


@router.get("/api/terrain/routes/{route_id}/points")
def get_route_terrain_points(route_id: int) -> list[terrain.RoutePointClass]:
    with db.connection(terrain.connect) as conn:
        return terrain.route_point_classes(conn, route_id)


def _run_terrain_backfill_in_background() -> None:
    """Thread target for /api/terrain/backfill: runs one pass on its own
    connection, persisting progress to terrain_backfill_status."""
    with db.connection() as conn:
        progress = terrain_backfill.BackfillProgress(conn)
        try:
            terrain_backfill.run_backfill(conn, progress)
            progress.finish()
        except Exception as e:
            _logger.exception("Terrain backfill failed")
            progress.fail(describe_error(e))


@router.post("/api/terrain/backfill")
def start_terrain_backfill() -> terrain_backfill.BackfillStatus:
    with db.connection(terrain.connect) as conn:
        progress = terrain_backfill.BackfillProgress(conn)
        if progress.try_start():
            threading.Thread(target=_run_terrain_backfill_in_background, daemon=True).start()
        return progress.snapshot()


@router.get("/api/terrain/backfill/status")
def get_terrain_backfill_status() -> terrain_backfill.BackfillStatus:
    with db.connection(terrain.connect) as conn:
        return terrain_backfill.BackfillProgress(conn).snapshot()
