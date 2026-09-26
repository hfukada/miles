import sqlite3

import pytest

from miles import db, routes as routes_service


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _trkpt(lat: float, lng: float, ele: float | None) -> str:
    if ele is None:
        return f'<trkpt lat="{lat}" lon="{lng}"/>'
    return f'<trkpt lat="{lat}" lon="{lng}"><ele>{ele}</ele></trkpt>'


def _gpx(points: list[tuple[float, float, float | None]], name: str = "T") -> bytes:
    body = f"<trk><name>{name}</name><trkseg>{''.join(_trkpt(*p) for p in points)}</trkseg></trk>"
    return f"""<?xml version="1.0"?>
<gpx version="1.1" creator="Test" xmlns="http://www.topografix.com/GPX/1/1">{body}</gpx>""".encode("utf-8")


def _straight_route(conn: sqlite3.Connection, lat0: float, lng0: float, n: int = 60, step_deg: float = 0.0002, ele0: float | None = 100.0, name: str = "R") -> int:
    points = [(lat0 + i * step_deg, lng0, None if ele0 is None else ele0 + 1.0 * i) for i in range(n)]
    route_ids = routes_service.upload_gpx(conn, _gpx(points, name), f"{name}.gpx", name=name)
    return route_ids[0]


def test_compose_single_leg_partial_span():
    conn = _conn()
    route_id = _straight_route(conn, 40.0, -105.0)
    detail = routes_service.get_route(conn, route_id)
    half = detail["distance_m"] / 2
    result = routes_service.compose_route(conn, [{"route_id": route_id, "from_m": 0.0, "to_m": half}])
    assert result["distance_m"] == pytest.approx(half, rel=0.05)
    assert result["warnings"] == []
    assert result["gain_m"] is not None and result["gain_m"] > 0


def test_compose_reversed_leg():
    conn = _conn()
    route_id = _straight_route(conn, 40.0, -105.0)
    detail = routes_service.get_route(conn, route_id)
    total = detail["distance_m"]
    forward = routes_service.compose_route(conn, [{"route_id": route_id, "from_m": 0.0, "to_m": total}])
    reversed_result = routes_service.compose_route(conn, [{"route_id": route_id, "from_m": total, "to_m": 0.0}])
    assert reversed_result["distance_m"] == pytest.approx(forward["distance_m"], rel=0.01)
    # A climbing route run backwards should descend, not climb.
    assert reversed_result["loss_m"] == pytest.approx(forward["gain_m"], rel=0.1)


def test_compose_two_legs_with_gap_warns():
    conn = _conn()
    route_a = _straight_route(conn, 40.0, -105.0, name="A")
    route_b = _straight_route(conn, 41.0, -105.0, name="B")  # ~111km away
    detail_a = routes_service.get_route(conn, route_a)
    detail_b = routes_service.get_route(conn, route_b)
    result = routes_service.compose_route(
        conn,
        [
            {"route_id": route_a, "from_m": 0.0, "to_m": detail_a["distance_m"]},
            {"route_id": route_b, "from_m": 0.0, "to_m": detail_b["distance_m"]},
        ],
    )
    assert any("gap" in w for w in result["warnings"])


def test_compose_connector_leg_adds_distance_and_optional_gain():
    conn = _conn()
    route_a = _straight_route(conn, 40.0, -105.0, name="A")
    detail_a = routes_service.get_route(conn, route_a)
    result = routes_service.compose_route(
        conn,
        [
            {"route_id": route_a, "from_m": 0.0, "to_m": detail_a["distance_m"]},
            {"kind": "connector", "distance_m": 500.0, "gain_m": 10.0, "note": "road bit"},
        ],
    )
    assert result["distance_m"] == pytest.approx(detail_a["distance_m"] + 500.0, rel=0.01)
    assert result["legs"][-1]["kind"] == "connector"
    assert result["legs"][-1]["note"] == "road bit"


def test_compose_no_elevation_route_leaves_gain_partial_with_warning():
    conn = _conn()
    route_id = _straight_route(conn, 40.0, -105.0, ele0=None)
    detail = routes_service.get_route(conn, route_id)
    result = routes_service.compose_route(conn, [{"route_id": route_id, "from_m": 0.0, "to_m": detail["distance_m"]}])
    assert result["gain_m"] is None
    assert any("no elevation" in w for w in result["warnings"])


def test_compose_save_creates_a_new_route_with_legs():
    conn = _conn()
    route_id = _straight_route(conn, 40.0, -105.0, name="Base")
    detail = routes_service.get_route(conn, route_id)
    result = routes_service.compose_route(
        conn,
        [{"route_id": route_id, "from_m": 0.0, "to_m": detail["distance_m"]}],
        save=True,
        name="My Composed Run",
    )
    assert "route_id" in result
    saved = routes_service.get_route(conn, result["route_id"])
    assert saved["source"] == "composed"
    assert saved["name"] == "My Composed Run"
    assert saved["legs"] is not None and len(saved["legs"]) == 1
    assert saved["legs"][0]["route_id"] == route_id


def test_compose_out_of_range_leg_warns_and_is_skipped():
    conn = _conn()
    route_id = _straight_route(conn, 40.0, -105.0)
    detail = routes_service.get_route(conn, route_id)
    total = detail["distance_m"]
    result = routes_service.compose_route(conn, [{"route_id": route_id, "from_m": total + 1000, "to_m": total + 2000}])
    assert result["distance_m"] == 0.0
    assert any("out of range" in w for w in result["warnings"])
