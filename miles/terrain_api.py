"""HTTP API for the terrain model -- profile/trend/analysis/scoring reads
and the resumable stream-backfill job's start/status. Mounted into
miles/api.py alongside the route-database router. No auth, matching every
other endpoint in this app.
"""

import logging
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, terrain, terrain_backfill
from .errors import describe_error

router = APIRouter()
_logger = logging.getLogger(__name__)


@router.get("/api/terrain/profile")
def get_terrain_profile(window_days: int | None = None) -> terrain.TerrainProfile:
    conn = terrain.connect()
    return terrain.get_terrain_profile(conn, window_days=window_days)


@router.get("/api/terrain/trend")
def get_terrain_trend(
    metric: str,
    grade_min_pct: float | None = None,
    grade_max_pct: float | None = None,
    since: str | None = None,
) -> list[terrain.TerrainTrendPoint]:
    conn = terrain.connect()
    grade_range = (grade_min_pct, grade_max_pct) if grade_min_pct is not None and grade_max_pct is not None else None
    try:
        return terrain.get_terrain_trend(conn, metric, grade_range=grade_range, since=since)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/terrain/activities/{activity_id}")
def get_activity_terrain(activity_id: int) -> terrain.ActivityTerrainAnalysis:
    conn = terrain.connect()
    try:
        return terrain.analyze_activity_terrain(conn, activity_id)
    except terrain.TerrainDataError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/terrain/routes/{route_id}")
def get_route_terrain_score(route_id: int) -> terrain.RouteTerrainScore:
    conn = terrain.connect()
    return terrain.score_route(conn, route_id)


@router.get("/api/terrain/routes/{route_id}/points")
def get_route_terrain_points(route_id: int) -> list[terrain.RoutePointClass]:
    conn = terrain.connect()
    return terrain.route_point_classes(conn, route_id)


class TerrainSettingsPatch(BaseModel):
    walk_threshold_spm: float | None = None
    rolling_window_days: int | None = None


@router.get("/api/terrain/settings")
def get_terrain_settings() -> terrain.TerrainSettings:
    conn = terrain.connect()
    return terrain.get_settings(conn)


@router.patch("/api/terrain/settings")
def patch_terrain_settings(body: TerrainSettingsPatch) -> terrain.TerrainSettings:
    conn = terrain.connect()
    return terrain.update_settings(
        conn, walk_threshold_spm=body.walk_threshold_spm, rolling_window_days=body.rolling_window_days
    )


def _run_terrain_backfill_in_background() -> None:
    """Thread target for /api/terrain/backfill: opens its own connection
    (sqlite3.Connection isn't safe to share across threads) and runs one
    backfill pass. Progress is persisted to terrain_backfill_status, polled
    by GET /api/terrain/backfill/status."""
    conn = db.connect()
    progress = terrain_backfill.BackfillProgress(conn)
    try:
        terrain_backfill.run_backfill(conn, progress)
        progress.finish()
    except Exception as e:
        _logger.exception("Terrain backfill failed")
        progress.fail(describe_error(e))
    finally:
        conn.close()


@router.post("/api/terrain/backfill")
def start_terrain_backfill() -> terrain_backfill.BackfillStatus:
    conn = terrain.connect()
    progress = terrain_backfill.BackfillProgress(conn)
    if progress.try_start():
        threading.Thread(target=_run_terrain_backfill_in_background, daemon=True).start()
    return progress.snapshot()


@router.get("/api/terrain/backfill/status")
def get_terrain_backfill_status() -> terrain_backfill.BackfillStatus:
    conn = terrain.connect()
    return terrain_backfill.BackfillProgress(conn).snapshot()
