"""HTTP API for the route database -- upload, list/get/patch/delete, and
the spatial/compose/compare tools. Mounted into miles/api.py alongside the
existing routers. No auth, matching every other endpoint in this app: the
API has none, so this doesn't invent any (see /api/workbooks/upload).
"""

import anyio.to_thread
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from . import db, elevation, gpx_parse, routes as routes_service

router = APIRouter()


def _parse_tags(tags: str) -> list[str]:
    return [t.strip() for t in tags.split(",") if t.strip()]


@router.post("/api/routes/upload")
async def upload_route(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    source: str | None = Form(None),
    notes: str | None = Form(None),
    tags: str = Form(""),
) -> list[routes_service.RouteSummary]:
    data = await file.read()
    if len(data) > gpx_parse.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {gpx_parse.MAX_UPLOAD_BYTES} bytes")
    parsed_tags = _parse_tags(tags)

    def store() -> list[routes_service.RouteSummary]:
        with db.connection(routes_service.connect) as conn:
            route_ids = routes_service.upload_gpx(
                conn, data, file.filename or "upload.gpx", name=name, source=source, notes=notes, tags=parsed_tags,
            )
            return [routes_service.get_route(conn, rid) for rid in route_ids]  # type: ignore[misc]

    try:
        return await anyio.to_thread.run_sync(store)
    except gpx_parse.GpxParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/routes")
def list_routes(
    min_distance_m: float | None = None,
    max_distance_m: float | None = None,
    min_ft_per_mi: float | None = None,
    max_ft_per_mi: float | None = None,
    tag: str | None = None,
    near_lat: float | None = None,
    near_lng: float | None = None,
    near_radius_m: float = 5000.0,
) -> list[routes_service.RouteSummary]:
    with db.connection(routes_service.connect) as conn:
        return routes_service.list_routes(
            conn,
            min_distance_m=min_distance_m,
            max_distance_m=max_distance_m,
            min_ft_per_mi=min_ft_per_mi,
            max_ft_per_mi=max_ft_per_mi,
            tag=tag,
            near_lat=near_lat,
            near_lng=near_lng,
            near_radius_m=near_radius_m,
        )


@router.get("/api/routes/junctions")
def get_junctions(route_a: int, route_b: int, tolerance_m: float = 30.0) -> routes_service.FindJunctionsResult:
    with db.connection(routes_service.connect) as conn:
        return routes_service.find_route_junctions(conn, route_a, route_b, tolerance_m=tolerance_m)


@router.get("/api/routes/near")
def get_routes_near(lat: float, lng: float, radius_m: float = 1000.0) -> list[routes_service.RouteNearby]:
    with db.connection(routes_service.connect) as conn:
        return routes_service.routes_near(conn, lat, lng, radius_m)


class ComposeLegBody(BaseModel):
    kind: str = "route"
    route_id: int | None = None
    from_m: float | None = None
    to_m: float | None = None
    distance_m: float | None = None
    gain_m: float | None = None
    note: str | None = None


class ComposeRequestBody(BaseModel):
    legs: list[ComposeLegBody]
    tolerance_m: float = 30.0
    save: bool = False
    name: str | None = None
    notes: str | None = None
    tags: list[str] | None = None


@router.post("/api/routes/compose")
def post_compose_route(body: ComposeRequestBody) -> routes_service.ComposeResult:
    with db.connection(routes_service.connect) as conn:
        legs: list[routes_service.RouteLegInput] = [leg.model_dump() for leg in body.legs]  # type: ignore[misc]
        return routes_service.compose_route(
            conn, legs, tolerance_m=body.tolerance_m, save=body.save,
            name=body.name, notes=body.notes, tags=body.tags,
        )


@router.get("/api/routes/compare")
def get_compare(route_id: int, course_route_id: int) -> routes_service.CompareResult:
    with db.connection(routes_service.connect) as conn:
        return routes_service.compare_route_to_course(conn, route_id, course_route_id)


@router.get("/api/routes/{route_id}")
def get_route(route_id: int) -> routes_service.RouteDetail:
    with db.connection(routes_service.connect) as conn:
        return routes_service.get_route(conn, route_id)


@router.get("/api/routes/{route_id}/gpx")
def get_route_gpx(route_id: int) -> Response:
    with db.connection(routes_service.connect) as conn:
        filename, data = routes_service.get_route_gpx(conn, route_id)
        return Response(
            content=data,
            media_type="application/gpx+xml",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


@router.get("/api/routes/{route_id}/profile")
def get_route_profile(route_id: int) -> list[routes_service.RouteProfilePoint]:
    with db.connection(routes_service.connect) as conn:
        return routes_service.get_route_profile(conn, route_id)


@router.get("/api/routes/{route_id}/near")
def get_route_near_routes(route_id: int, radius_m: float = 1000.0) -> list[routes_service.RouteNearby]:
    with db.connection(routes_service.connect) as conn:
        return routes_service.routes_near_route(conn, route_id, radius_m)


class RouteUpdateBody(BaseModel):
    name: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    elevation_source: str | None = None


@router.patch("/api/routes/{route_id}")
def patch_route(route_id: int, body: RouteUpdateBody) -> routes_service.RouteDetail:
    with db.connection(routes_service.connect) as conn:
        try:
            return routes_service.update_route(
                conn, route_id, name=body.name, notes=body.notes, tags=body.tags,
                elevation_source=body.elevation_source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/routes/{route_id}/redrape")
def post_redrape_route(route_id: int) -> routes_service.RouteDetail:
    """Re-derive this route's elevation with network fetches allowed --
    unlike every other derive path, which only reads the elevation tile
    cache. Use after an area import has cached tiles the route needed, or
    to retry a pending drape immediately."""
    with db.connection(routes_service.connect) as conn:
        try:
            return routes_service.redrape_route(conn, route_id)
        except elevation.ElevationFetchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/api/routes/{route_id}", status_code=204)
def delete_route(route_id: int) -> None:
    with db.connection(routes_service.connect) as conn:
        routes_service.delete_route(conn, route_id)
