from xml.etree import ElementTree as ET

import pytest
from helpers import memory_db, upload_line_route as _straight_route

from miles import gpx_parse, routes as routes_service


def test_compose_a_partial_span_and_a_reversed_leg():
    conn = memory_db()
    route_id = _straight_route(conn, 40.0, -105.0)
    total = routes_service.get_route(conn, route_id)["distance_m"]
    half = total / 2
    result = routes_service.compose_route(conn, [{"route_id": route_id, "from_m": 0.0, "to_m": half}])
    assert result["distance_m"] == pytest.approx(half, rel=0.05)
    assert result["warnings"] == []
    assert result["gain_m"] is not None and result["gain_m"] > 0

    forward = routes_service.compose_route(conn, [{"route_id": route_id, "from_m": 0.0, "to_m": total}])
    reversed_result = routes_service.compose_route(conn, [{"route_id": route_id, "from_m": total, "to_m": 0.0}])
    assert reversed_result["distance_m"] == pytest.approx(forward["distance_m"], rel=0.01)
    # A climbing route run backwards should descend, not climb.
    assert reversed_result["loss_m"] == pytest.approx(forward["gain_m"], rel=0.1)


def test_compose_connector_leg_adds_distance_and_optional_gain():
    conn = memory_db()
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


def test_compose_save_creates_a_new_route_with_legs_and_a_namespaced_gpx_that_round_trips():
    conn = memory_db()
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

    filename, gpx_bytes = routes_service.get_route_gpx(conn, result["route_id"])
    assert filename.startswith("composed-")

    root = ET.fromstring(gpx_bytes)
    assert root.tag == f"{{{gpx_parse.GPX_NAMESPACE}}}gpx"

    doc = gpx_parse.parse_gpx_bytes(gpx_bytes)
    assert doc["creator"] == "miles"
    assert len(doc["tracks"]) == 1
    assert doc["tracks"][0]["gpx_name"] == "My Composed Run"
    assert len(doc["tracks"][0]["points"]) > 1


def test_compose_warns_about_a_gap_between_legs_a_route_without_elevation_and_an_out_of_range_leg():
    conn = memory_db()
    route_a = _straight_route(conn, 40.0, -105.0, name="A")
    route_b = _straight_route(conn, 41.0, -105.0, name="B")  # ~111km away
    total_a = routes_service.get_route(conn, route_a)["distance_m"]
    total_b = routes_service.get_route(conn, route_b)["distance_m"]
    gap = routes_service.compose_route(
        conn,
        [{"route_id": route_a, "from_m": 0.0, "to_m": total_a}, {"route_id": route_b, "from_m": 0.0, "to_m": total_b}],
    )
    assert any("gap" in w for w in gap["warnings"])

    flat = _straight_route(conn, 42.0, -105.0, ele0=None)
    flat_total = routes_service.get_route(conn, flat)["distance_m"]
    result = routes_service.compose_route(conn, [{"route_id": flat, "from_m": 0.0, "to_m": flat_total}])
    assert result["gain_m"] is None
    assert any("no elevation" in w for w in result["warnings"])

    result = routes_service.compose_route(conn, [{"route_id": route_a, "from_m": total_a + 1000, "to_m": total_a + 2000}])
    assert result["distance_m"] == 0.0
    assert any("out of range" in w for w in result["warnings"])
