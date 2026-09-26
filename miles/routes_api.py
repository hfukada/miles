"""HTTP API for the route database -- upload, list/get/patch/delete, and
the spatial/compose/compare tools. Mounted into miles/api.py alongside the
existing routers. No auth, matching every other endpoint in this app: the
API has none, so this doesn't invent any (see /api/workbooks/upload).
"""

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from . import gpx_parse, routes as routes_service

router = APIRouter()


def _parse_tags(tags: str) -> list[str]:
    return [t.strip() for t in tags.split(",") if t.strip()]


def _not_found(exc: routes_service.RouteNotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


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
    conn = routes_service.connect()
    try:
        route_ids = routes_service.upload_gpx(
            conn, data, file.filename or "upload.gpx",
            name=name, source=source, notes=notes, tags=_parse_tags(tags),
        )
    except gpx_parse.GpxParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [routes_service.get_route(conn, rid) for rid in route_ids]  # type: ignore[misc]


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
    conn = routes_service.connect()
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
    conn = routes_service.connect()
    try:
        return routes_service.find_route_junctions(conn, route_a, route_b, tolerance_m=tolerance_m)
    except routes_service.RouteNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/api/routes/near")
def get_routes_near(lat: float, lng: float, radius_m: float = 1000.0) -> list[routes_service.RouteNearby]:
    conn = routes_service.connect()
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
    conn = routes_service.connect()
    legs: list[routes_service.RouteLegInput] = [leg.model_dump() for leg in body.legs]  # type: ignore[misc]
    try:
        return routes_service.compose_route(
            conn, legs, tolerance_m=body.tolerance_m, save=body.save,
            name=body.name, notes=body.notes, tags=body.tags,
        )
    except routes_service.RouteNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/api/routes/compare")
def get_compare(route_id: int, course_route_id: int) -> routes_service.CompareResult:
    conn = routes_service.connect()
    try:
        return routes_service.compare_route_to_course(conn, route_id, course_route_id)
    except routes_service.RouteNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/api/routes/{route_id}")
def get_route(route_id: int) -> routes_service.RouteDetail:
    conn = routes_service.connect()
    try:
        return routes_service.get_route(conn, route_id)
    except routes_service.RouteNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/api/routes/{route_id}/gpx")
def get_route_gpx(route_id: int) -> Response:
    conn = routes_service.connect()
    try:
        filename, data = routes_service.get_route_gpx(conn, route_id)
    except routes_service.RouteNotFoundError as exc:
        raise _not_found(exc) from exc
    return Response(
        content=data,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/routes/{route_id}/profile")
def get_route_profile(route_id: int) -> list[routes_service.RouteProfilePoint]:
    conn = routes_service.connect()
    try:
        return routes_service.get_route_profile(conn, route_id)
    except routes_service.RouteNotFoundError as exc:
        raise _not_found(exc) from exc


@router.get("/api/routes/{route_id}/near")
def get_route_near_routes(route_id: int, radius_m: float = 1000.0) -> list[routes_service.RouteNearby]:
    conn = routes_service.connect()
    try:
        return routes_service.routes_near_route(conn, route_id, radius_m)
    except routes_service.RouteNotFoundError as exc:
        raise _not_found(exc) from exc


class RouteUpdateBody(BaseModel):
    name: str | None = None
    notes: str | None = None
    tags: list[str] | None = None


@router.patch("/api/routes/{route_id}")
def patch_route(route_id: int, body: RouteUpdateBody) -> routes_service.RouteDetail:
    conn = routes_service.connect()
    try:
        return routes_service.update_route(conn, route_id, name=body.name, notes=body.notes, tags=body.tags)
    except routes_service.RouteNotFoundError as exc:
        raise _not_found(exc) from exc


@router.delete("/api/routes/{route_id}", status_code=204)
def delete_route(route_id: int) -> None:
    conn = routes_service.connect()
    try:
        routes_service.delete_route(conn, route_id)
    except routes_service.RouteNotFoundError as exc:
        raise _not_found(exc) from exc
