import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from helpers import call_tool, line_points, make_gpx

from miles import api, db, geocode, http_client, mcp_server, route_locations, routes as routes_service

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

EXAMPLE_TOWN = {"town": "Example Town", "county": "Example County", "state": "Colorado", "country": "United States"}


def _reverse_answer(address: dict | None = None) -> bytes:
    if address is None:
        return json.dumps({"error": "Unable to geocode"}).encode()
    return json.dumps({"name": "x", "address": address}).encode()


class _Nominatim:
    """Answers reverse requests with `body` (or raises it) and records each (policy, url)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, body: bytes | Exception) -> None:
        self.body = body
        self.sent: list[tuple[object, str]] = []
        monkeypatch.setattr(geocode, "NOMINATIM_URL", NOMINATIM_SEARCH_URL)
        monkeypatch.setattr(http_client, "request", self)

    def __call__(self, policy, url, **kwargs):
        self.sent.append((policy, url))
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.fixture
def contact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MILES_CONTACT", "ops@example.com")


def _route(conn: sqlite3.Connection, lat0: float = 40.0, name: str = "Ridge Loop") -> int:
    gpx = make_gpx(line_points(40, lat0=lat0), name=name)
    return routes_service.upload_gpx(conn, gpx, f"{name}.gpx", name=name)[0]


def _area(conn: sqlite3.Connection, label: str, radius_km: float = 10.0, status: str = "ready", lat: float = 40.0) -> None:
    conn.execute(
        "INSERT INTO areas (label, center_lat, center_lng, radius_km, pinned, status, created_at, last_used_at) "
        "VALUES (?, ?, -105.0, ?, 0, ?, '2026-01-01', '2026-01-01')",
        (label, lat, radius_km, status),
    )
    conn.commit()


@pytest.mark.parametrize(
    "address, label",
    [
        ({"national_park": "Example National Park", "town": "Example Town", "state": "Colorado"}, "Example National Park, Colorado"),
        ({"nature_reserve": "Example Reserve", "county": "Example County", "state": "Colorado"}, "Example Reserve, Colorado"),
        (EXAMPLE_TOWN, "Example Town, Colorado"),
        ({"village": "Example Village", "region": "Example Region", "country": "Exampleland"}, "Example Village, Example Region"),
        ({"county": "Example County", "state": "Colorado"}, "Example County, Colorado"),
        ({"country": "Exampleland"}, "Exampleland"),
        ({"road": "Example Road"}, None),
        ({"town": "  ", "state": "Colorado"}, "Colorado"),
    ],
)
def test_a_label_is_a_park_or_locality_with_its_region(address: dict, label: str | None):
    assert geocode.format_location(address) == label


def test_a_reverse_lookup_asks_once_per_rounded_point_and_caches_a_missing_answer(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, contact: None
):
    nominatim = _Nominatim(monkeypatch, _reverse_answer(EXAMPLE_TOWN))

    assert geocode.reverse(conn, 40.00012, -105.00041) == "Example Town, Colorado"
    assert geocode.reverse(conn, 40.00049, -104.99961) == "Example Town, Colorado"  # same point to three decimals
    assert len(nominatim.sent) == 1
    policy, url = nominatim.sent[0]
    assert policy is http_client.NOMINATIM
    assert url.startswith("https://nominatim.openstreetmap.org/reverse?") and "lat=40.00012" in url

    nominatim.body = _reverse_answer(None)
    assert geocode.reverse(conn, 41.0, -105.0) is None
    assert geocode.reverse(conn, 41.0, -105.0) is None
    assert len(nominatim.sent) == 2


def test_a_failed_reverse_lookup_is_raised_and_not_cached(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, contact: None):
    nominatim = _Nominatim(monkeypatch, TimeoutError("slow"))
    with pytest.raises(geocode.GeocodeError):
        geocode.reverse(conn, 40.0, -105.0)

    nominatim.body = _reverse_answer(EXAMPLE_TOWN)
    assert geocode.reverse(conn, 40.0, -105.0) == "Example Town, Colorado"
    assert len(nominatim.sent) == 2


def test_no_contact_means_no_request(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MILES_CONTACT", raising=False)
    nominatim = _Nominatim(monkeypatch, _reverse_answer(EXAMPLE_TOWN))
    route_id = _route(conn)

    assert route_locations.resolve_location(conn, route_id) is None
    assert nominatim.sent == []


def test_resolving_fills_the_location_once_and_marks_it_automatic(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, contact: None):
    nominatim = _Nominatim(monkeypatch, _reverse_answer(EXAMPLE_TOWN))
    route_id = _route(conn)

    assert route_locations.resolve_location(conn, route_id) == "Example Town, Colorado"
    assert route_locations.resolve_location(conn, route_id) == "Example Town, Colorado"
    row = conn.execute("SELECT location, location_source FROM routes").fetchone()
    assert (row["location"], row["location_source"]) == ("Example Town, Colorado", "auto")
    assert len(nominatim.sent) == 1


def test_a_second_route_starting_at_the_same_rounded_point_uses_the_cache(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, contact: None):
    nominatim = _Nominatim(monkeypatch, _reverse_answer(EXAMPLE_TOWN))
    first, second = _route(conn, name="First"), _route(conn, lat0=40.0001, name="Second")

    assert route_locations.resolve_location(conn, first) == route_locations.resolve_location(conn, second) == "Example Town, Colorado"
    assert len(nominatim.sent) == 1


def test_an_athlete_set_location_is_never_replaced_or_looked_up(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, contact: None):
    nominatim = _Nominatim(monkeypatch, _reverse_answer(EXAMPLE_TOWN))
    route_id = _route(conn)

    detail = routes_service.update_route(conn, route_id, location="Home trails")
    assert detail["location"] == "Home trails"
    assert route_locations.resolve_location(conn, route_id) == "Home trails"
    assert nominatim.sent == []
    assert conn.execute("SELECT location_source FROM routes").fetchone()[0] == "athlete"

    routes_service.update_route(conn, route_id, location="")
    row = conn.execute("SELECT location, location_source FROM routes").fetchone()
    assert (row["location"], row["location_source"]) == (None, None)
    assert route_locations.resolve_location(conn, route_id) == "Example Town, Colorado"


def test_an_area_label_stands_in_without_being_stored_when_nominatim_cannot_answer(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, contact: None
):
    nominatim = _Nominatim(monkeypatch, ConnectionError("down"))
    route_id = _route(conn)
    _area(conn, "Example Area", radius_km=50.0)
    _area(conn, "Smaller Area", radius_km=5.0)
    _area(conn, "Unfinished Area", radius_km=1.0, status="importing")

    assert route_locations.resolve_location(conn, route_id) == "Smaller Area"
    assert conn.execute("SELECT location FROM routes").fetchone()[0] is None

    nominatim.body = _reverse_answer(EXAMPLE_TOWN)
    assert route_locations.resolve_location(conn, route_id) == "Example Town, Colorado"


def test_an_area_elsewhere_does_not_label_a_route(conn: sqlite3.Connection):
    route_id = _route(conn, lat0=45.0)
    _area(conn, "Example Area")

    assert routes_service.get_route(conn, route_id)["location"] is None


def test_listing_routes_never_asks_nominatim(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, contact: None):
    nominatim = _Nominatim(monkeypatch, _reverse_answer(EXAMPLE_TOWN))
    stored, covered, bare = _route(conn, name="A"), _route(conn, lat0=41.0, name="B"), _route(conn, lat0=45.0, name="C")
    conn.execute("UPDATE routes SET location = 'Stored Place', location_source = 'athlete' WHERE route_id = ?", (stored,))
    _area(conn, "Covering Area", lat=41.0)

    listed = {r["route_id"]: r["location"] for r in routes_service.list_routes(conn)}

    assert listed == {stored: "Stored Place", covered: "Covering Area", bare: None}
    assert nominatim.sent == []


def test_the_location_endpoint_fills_once_then_serves_from_the_database(monkeypatch: pytest.MonkeyPatch, contact: None):
    nominatim = _Nominatim(monkeypatch, _reverse_answer(EXAMPLE_TOWN))
    with db.connection() as conn:
        route_id = _route(conn)
    client = TestClient(api.app)

    assert client.get(f"/api/routes/{route_id}/location").json() == {"location": "Example Town, Colorado"}
    assert client.get(f"/api/routes/{route_id}/location").json() == {"location": "Example Town, Colorado"}
    assert len(nominatim.sent) == 1
    assert client.get(f"/api/routes/{route_id}").json()["location"] == "Example Town, Colorado"
    assert client.get("/api/routes/999/location").status_code == 404


def test_the_patch_endpoint_sets_and_clears_the_location():
    with db.connection() as conn:
        route_id = _route(conn)
    client = TestClient(api.app)

    assert client.patch(f"/api/routes/{route_id}", json={"location": "Home trails"}).json()["location"] == "Home trails"
    assert client.patch(f"/api/routes/{route_id}", json={"location": ""}).json()["location"] is None


def test_the_get_route_tool_fills_the_location_and_update_route_edits_it(monkeypatch: pytest.MonkeyPatch, contact: None):
    nominatim = _Nominatim(monkeypatch, _reverse_answer(EXAMPLE_TOWN))
    with db.connection() as conn:
        route_id = _route(conn)

    assert call_tool(mcp_server.mcp, "get_route", {"route_id": route_id})["location"] == "Example Town, Colorado"
    assert call_tool(mcp_server.mcp, "get_route", {"route_id": route_id})["location"] == "Example Town, Colorado"
    assert len(nominatim.sent) == 1
    assert call_tool(mcp_server.mcp, "update_route", {"route_id": route_id, "location": "Home trails"})["location"] == "Home trails"
    assert call_tool(mcp_server.mcp, "list_routes")[0]["location"] == "Home trails"
