import gzip
import json
import sqlite3
from contextlib import contextmanager

import pytest
from helpers import ORIGIN_LAT, ORIGIN_LNG, call_tool, insert_activity
from mcp.server.fastmcp import FastMCP
from test_sections import Map, kinds_of, pt, terrain_slope_sampler

from miles import familiarity, network, route_search, routing_mcp, sections, streams, survey, terrain

START = pt(0, 0)


def two_squares(busy_west: bool = True) -> Map:
    """Two equal trail squares 400 m from the start: one over a quiet
    street, the other over a busy road."""
    m = Map()
    m.line([(0, 0), (0, 400)], "residential", name="Quiet Street", maxspeed="25 mph")
    m.square(0, 400, 400, name="Quiet Loop")
    m.line([(0, 0), (0, -400)], "secondary" if busy_west else "residential", name="Busy Road", maxspeed="45 mph")
    m.square(0, -800, 400, name="Busy Loop")
    return m


def by_name(result: dict) -> dict[str, dict]:
    return {s["name"]: s for s in result["sections"]}


def test_a_section_behind_a_busy_road_ranks_below_an_equal_one_reached_quietly(conn: sqlite3.Connection):
    two_squares().build(conn)

    result = survey.survey(conn, 1, *START)

    rows = by_name(result)
    assert rows["Quiet Loop"]["rank"] == 1 and rows["Busy Loop"]["rank"] == 2
    quiet, busy = rows["Quiet Loop"], rows["Busy Loop"]
    assert busy["terms"]["access_cost"] > 3 * quiet["terms"]["access_cost"]
    assert busy["terms"]["quality"] < quiet["terms"]["quality"]  # the busy road also counts as a crossing
    assert quiet["access"]["approach_m"] == pytest.approx(400, rel=0.05)
    assert quiet["access"]["uncomfortable_m"] == 0.0
    assert busy["access"]["uncomfortable_m"] == pytest.approx(400, rel=0.05)
    assert quiet["score"] == pytest.approx(quiet["terms"]["adjusted_quality"] - quiet["terms"]["access_cost"])


def test_every_term_of_the_score_is_reported_and_the_start_on_a_section_costs_nothing(conn: sqlite3.Connection):
    two_squares().build(conn)

    on_it = survey.survey(conn, 1, *pt(0, 600))  # on the quiet square's south side

    row = by_name(on_it)["Quiet Loop"]
    assert row["access"]["approach_m"] == 0.0 and row["terms"]["access_cost"] == 0.0
    assert set(row["terms"]) == {
        "usable_m", "continuity", "scenery_bonus", "runnability", "quality", "adjustment", "adjusted_quality", "access_cost",
    }
    assert row["score"] == pytest.approx(row["terms"]["adjusted_quality"])


def test_a_section_whose_approach_has_too_much_continuous_uncomfortable_road_is_ruled_out_not_ranked(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 400)], "residential", name="Quiet Street", maxspeed="25 mph")
    m.square(0, 400, 400, name="Quiet Loop")
    m.line([(0, 0), (0, -1000)], "secondary", name="Long Busy Road", maxspeed="45 mph")
    m.square(0, -1400, 400, name="Far Loop")
    m.build(conn)

    result = survey.survey(conn, 1, *START)

    assert [s["name"] for s in result["sections"]] == ["Quiet Loop"]
    assert result["ruled_out_count"] == 1
    assert result["ruled_out"][0]["name"] == "Far Loop" and "uncomfortable" in result["ruled_out"][0]["reason"]


def test_the_distance_bounds_the_approach_and_how_much_of_a_section_counts(conn: sqlite3.Connection):
    two_squares(busy_west=False).build(conn)

    short = survey.survey(conn, 1, *START, distance_m=1500.0)  # approach budget 375 m: both are 400 m away
    long = survey.survey(conn, 1, *START, distance_m=3000.0)

    assert short["sections"] == [] and short["ruled_out_count"] == 2
    row = by_name(long)["Quiet Loop"]
    assert row["terms"]["usable_m"] == pytest.approx(1600 - 0, abs=1)  # 1600 m of section, 3000 - 2 x 400 available
    tight = survey.survey(conn, 1, *START, distance_m=2200.0)
    assert by_name(tight)["Quiet Loop"]["terms"]["usable_m"] == pytest.approx(2200 - 2 * 400, rel=0.05)


def test_kinds_filter_and_n_limit_the_result_and_unknown_kinds_are_refused(conn: sqlite3.Connection):
    m = two_squares(busy_west=False)
    m.line([(0, 0), (-1500, 0)], "residential", name="Long Lane", maxspeed="25 mph")
    m.build(conn)

    only_roads = survey.survey(conn, 1, *START, kinds=["quiet_road"])
    top_one = survey.survey(conn, 1, *START, n=1)

    assert {s["kind"] for s in only_roads["sections"]} == {"quiet_road"}
    assert len([s for s in top_one["sections"] if not s["excluded"]]) == 1
    with pytest.raises(ValueError, match="unknown kinds"):
        survey.survey(conn, 1, *START, kinds=["scenic"])


def test_runnability_comes_from_the_histogram_through_the_current_model_and_feeds_the_score(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", name="Climb Trail")
    m.build(conn, sampler=terrain_slope_sampler(0.18))

    model = terrain.default_model()
    model.walk_onset_grade_pct = 15.0
    monkeypatch.setattr(terrain, "current_model", lambda conn: model)
    steep = by_name(survey.survey(conn, 1, *pt(0, 0)))["Climb Trail"]
    model.walk_onset_grade_pct = 25.0
    tolerant = by_name(survey.survey(conn, 1, *pt(0, 0)))["Climb Trail"]

    assert steep["runnability"]["hike_pct"] > 90 and tolerant["runnability"]["hike_pct"] == 0
    assert tolerant["terms"]["runnability"] > steep["terms"]["runnability"]
    assert tolerant["score"] > steep["score"]
    assert steep["gain_m_per_mile"] == pytest.approx(0.18 * 1609.344, rel=0.1)


def test_scenery_raises_quality_and_continuity_lowers_it(conn: sqlite3.Connection):
    plain = Map()
    plain.line([(0, 0), (0, 1500)], "path", name="Plain Trail")
    plain.line([(1500, 0), (1500, 1500)], "path", surface="asphalt", name="Riverside Path")
    plain.waterway([(1540, -100), (1540, 1600)])
    plain.line([(1500, 0), (0, 0)], "residential", name="Link", maxspeed="25 mph")
    plain.build(conn)

    rows = by_name(survey.survey(conn, 1, *pt(0, 0), distance_m=8000.0))

    assert rows["Riverside Path"]["terms"]["scenery_bonus"] > 0.25
    assert rows["Plain Trail"]["terms"]["scenery_bonus"] == 0.0

    crossings = Map()
    crossings.line([(0, east) for east in range(0, 1501, 100)], "path", name="Cut Path")
    for east in range(100, 1500, 100):
        crossings.line([(-200, east), (0, east)], "secondary", maxspeed="45 mph")
    crossings.build(conn, area_id=2)
    cut = by_name(survey.survey(conn, 2, *pt(0, 0)))["Cut Path"]
    assert cut["crossings"] >= 10
    assert cut["terms"]["continuity"] < 0.5


def write_stream(activity_id: int, points: list[tuple[float, float]]) -> None:
    path = streams._raw_path(activity_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps({"latlng": {"data": [list(p) for p in points]}}).encode()))


def track_around(north: float, east: float, side: float, step: float = 10.0) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    corners = [(north, east), (north, east + side), (north + side, east + side), (north + side, east), (north, east)]
    for (n0, e0), (n1, e1) in zip(corners, corners[1:]):
        count = int(max(abs(n1 - n0), abs(e1 - e0)) / step)
        pts += [pt(n0 + (n1 - n0) * i / count, e0 + (e1 - e0) * i / count) for i in range(count)]
    return pts


def insert_area(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO areas (area_id, label, center_lat, center_lng, radius_km, status, created_at, last_used_at) "
        "VALUES (1, 'test', ?, ?, 5.0, 'ready', 'now', 'now')",
        [ORIGIN_LAT, ORIGIN_LNG],
    )
    conn.commit()


def test_familiarity_counts_the_activities_that_ran_a_section_and_unfamiliar_only_drops_them(conn: sqlite3.Connection):
    two_squares(busy_west=False).build(conn)
    insert_area(conn)
    for activity_id in (1, 2):
        insert_activity(conn, activity_id, start_lat=ORIGIN_LAT, start_lng=ORIGIN_LNG, start_date=f"2026-09-0{activity_id}T08:00:00")
        write_stream(activity_id, track_around(0, 400, 400))
    insert_activity(conn, 3, start_lat=ORIGIN_LAT, start_lng=ORIGIN_LNG)  # no streams: waiting for a backfill
    insert_activity(conn, 4, start_lat=ORIGIN_LAT + 5.0, start_lng=ORIGIN_LNG)  # far from the area
    write_stream(4, track_around(0, 400, 400))

    assert familiarity.status(conn, 1) == familiarity.FamiliarityStatus(0, 2)
    done = familiarity.update(conn, 1)

    assert (done.matched, done.pending) == (2, 0)
    result = survey.survey(conn, 1, *START)
    rows = by_name(result)
    assert rows["Quiet Loop"]["familiarity"] == {"runs": 2, "last_run": "2026-09-02T08:00:00", "new_to_you": False}
    assert rows["Busy Loop"]["familiarity"]["new_to_you"] is True
    assert result["familiarity"] == {"activities_matched": 2, "activities_pending": 0}
    fresh = survey.survey(conn, 1, *START, unfamiliar_only=True)
    assert [s["name"] for s in fresh["sections"]] == ["Busy Loop"]


def test_only_activities_not_yet_matched_are_matched_and_a_rebuild_matches_them_all_again(conn: sqlite3.Connection):
    two_squares(busy_west=False).build(conn)
    insert_area(conn)
    insert_activity(conn, 1, start_lat=ORIGIN_LAT, start_lng=ORIGIN_LNG)
    write_stream(1, track_around(0, 400, 400))
    familiarity.update(conn, 1)
    insert_activity(conn, 2, start_lat=ORIGIN_LAT, start_lng=ORIGIN_LNG)
    write_stream(2, track_around(0, 400, 400))

    assert familiarity.status(conn, 1).pending == 1
    familiarity.update(conn, 1, max_activities=1)
    assert familiarity.status(conn, 1) == familiarity.FamiliarityStatus(2, 0)

    two_squares(busy_west=False).build(conn)
    assert familiarity.status(conn, 1) == familiarity.FamiliarityStatus(0, 2)


def test_a_survey_with_activities_waiting_says_so_and_matches_them_in_the_background(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    two_squares(busy_west=False).build(conn)
    insert_area(conn)
    insert_activity(conn, 1, start_lat=ORIGIN_LAT, start_lng=ORIGIN_LNG)
    write_stream(1, track_around(0, 400, 400))
    started: list[int] = []
    monkeypatch.setattr(familiarity, "start_background_update", lambda area_id: started.append(area_id) or True)

    result = survey.survey(conn, 1, *START)

    assert started == [1]
    assert result["familiarity"]["activities_pending"] == 1
    assert any("still being matched" in w for w in result["warnings"])


def test_a_per_request_adjustment_multiplies_quality_and_is_not_stored_and_a_stored_one_shows_its_source(conn: sqlite3.Connection):
    two_squares(busy_west=False).build(conn)
    base = by_name(survey.survey(conn, 1, *START))
    quiet = base["Quiet Loop"]["section_id"]
    busy = base["Busy Loop"]["section_id"]
    assert base["Quiet Loop"]["adjustment"] is None

    boosted = by_name(survey.survey(conn, 1, *START, adjustments={str(busy): 9.0}))

    assert boosted["Busy Loop"]["adjustment"] == {"factor": 2.0, "source": "request", "note": None, "url": None}
    assert boosted["Busy Loop"]["terms"]["adjusted_quality"] == pytest.approx(2.0 * base["Busy Loop"]["terms"]["quality"])
    assert conn.execute("SELECT COUNT(*) FROM net_section_judgments").fetchone()[0] == 0

    sections.adjust_section(conn, quiet, 0.5, "dull", source="research", url="https://example.org/loop")
    stored = by_name(survey.survey(conn, 1, *START, adjustments={quiet: 1.5}))
    assert stored["Quiet Loop"]["adjustment"]["source"] == "request"
    assert stored["Quiet Loop"]["adjustment"]["replaces_stored_factor"] == 0.5
    plain = by_name(survey.survey(conn, 1, *START))["Quiet Loop"]["adjustment"]
    assert plain == {"factor": 0.5, "source": "research", "note": "dull", "url": "https://example.org/loop"}
    with pytest.raises(ValueError, match="section ids"):
        survey.survey(conn, 1, *START, adjustments={"loop": 1.2})


def test_an_excluded_section_is_listed_marked_and_never_ranked(conn: sqlite3.Connection):
    two_squares(busy_west=False).build(conn)
    base = by_name(survey.survey(conn, 1, *START))
    sections.exclude_section(conn, base["Quiet Loop"]["section_id"], "closed for restoration")

    result = survey.survey(conn, 1, *START)

    rows = by_name(result)
    assert rows["Quiet Loop"]["excluded"] is True and rows["Quiet Loop"]["rank"] is None
    assert rows["Quiet Loop"]["exclusion"] == {"note": "closed for restoration", "source": "athlete"}
    assert rows["Busy Loop"]["excluded"] is False and rows["Busy Loop"]["rank"] == 1
    assert result["sections"][-1]["name"] == "Quiet Loop"


def test_an_excluded_sections_ways_leave_route_search_alongside_avoided_ones(conn: sqlite3.Connection):
    m = Map()
    squares = {}
    for name, (north, east) in {"north": (300, 0), "east": (0, 300), "south": (-1100, 0)}.items():
        squares[name] = m.square(north, east, 600, name=f"{name} loop")
    m.line([(0, 0), (300, 0)], "residential", name="hub north", maxspeed="25 mph")
    m.line([(0, 0), (0, 300)], "residential", name="hub east", maxspeed="25 mph")
    m.line([(0, 0), (-500, 0)], "residential", name="hub south", maxspeed="25 mph")
    m.build(conn)
    by_section = {s.name: s for s in sections.load_sections(conn, 1) if s.kind == "trail_system"}
    sections.exclude_section(conn, by_section["north loop"].section_id, "athlete said so")

    def used_ways(**extra: object) -> set[int]:
        request: route_search.RouteSearchRequest = {
            "area_id": 1, "starts": [{"lat": START[0], "lng": START[1]}],
            "distance_m_min": 100.0, "distance_m_max": 3000.0, "n": 5, "seed": 1, "time_budget_s": 5.0, **extra,  # type: ignore[typeddict-item]
        }
        result = route_search.find_routes(conn, request)
        return {w["way_id"] for c in result["candidates"] for w in c["ways"]}

    ways = used_ways()
    assert ways and not ways & set(squares["north"])
    assert ways & set(squares["east"]) or ways & set(squares["south"])

    ways = used_ways(avoid_way_ids=squares["east"])
    assert not ways & (set(squares["north"]) | set(squares["east"]))
    assert ways & set(squares["south"])

    network.set_way_override(conn, squares["south"][0], "avoid")
    network.set_way_override(conn, squares["south"][1], "avoid")
    assert not used_ways() & (set(squares["north"]) | set(squares["south"][:2]))

    sections.clear_section_adjustment(conn, by_section["north loop"].section_id, exclusion=True)
    assert used_ways(avoid_way_ids=squares["east"] + squares["south"]) & set(squares["north"])


def test_the_survey_response_stays_small_however_many_sections_the_area_has(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 700 * i) for i in range(41)], "residential", name="Spine", maxspeed="25 mph")
    for i in range(40):
        m.square(100, 700 * i, 300, name=f"Loop {i}")
        m.joined((0, 700 * i), (100, 700 * i), "residential", maxspeed="25 mph", name=f"Lane {i}")
    m.build(conn)

    result = survey.survey(conn, 1, *START, distance_m=120000.0)
    view = routing_mcp._survey_view(result)

    assert result["ranked_count"] > 25 and len(result["sections"]) == survey.DEFAULT_N
    assert len(json.dumps(view)) < 16000
    assert "geometry" not in json.dumps(view)


@pytest.fixture
def tool(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    server = FastMCP("test")
    routing_mcp.register_routing_tools(server)

    @contextmanager
    def lend(opener=None):
        yield conn

    monkeypatch.setattr(routing_mcp.db, "connection", lend)
    return lambda name, args: call_tool(server, name, args)


def test_the_section_tools_survey_in_miles_adjust_within_bounds_exclude_and_clear(conn: sqlite3.Connection, tool):
    two_squares(busy_west=False).build(conn)

    surveyed = tool("survey_area", {"area_id": 1, "start_lat": START[0], "start_lng": START[1], "distance_mi": 5})
    quiet = next(s for s in surveyed["sections"] if s["name"] == "Quiet Loop")
    assert quiet["length_mi"] == pytest.approx(1.0, rel=0.02)
    assert quiet["access"]["approach_mi"] == pytest.approx(0.25, abs=0.02)
    assert surveyed["distance_mi"] == 5.0 and "score_formula" in surveyed

    detail = tool("get_section", {"section_id": quiet["section_id"]})
    assert {w["name"] for w in detail["ways"]} == {"Quiet Loop"} and len(detail["ways"]) == 4
    assert detail["geometry"] and detail["length_mi"] == quiet["length_mi"]

    stored = tool("adjust_section", {"section_id": quiet["section_id"], "factor": 7, "note": "lovely"})
    assert stored["factor"] == 2.0
    assert "url" in tool("adjust_section", {"section_id": quiet["section_id"], "factor": 1.2, "note": "x", "source": "research"})["error"]
    assert tool("exclude_section", {"section_id": quiet["section_id"], "note": "athlete asked"})["excluded"] is True
    after = tool("survey_area", {"area_id": 1, "start_lat": START[0], "start_lng": START[1]})
    row = next(s for s in after["sections"] if s["name"] == "Quiet Loop")
    assert row["excluded"] and row["adjustment"]["factor"] == 2.0
    assert tool("clear_section_adjustment", {"section_id": quiet["section_id"]})["exclusion_cleared"] is False
    assert tool("clear_section_adjustment", {"section_id": quiet["section_id"], "exclusion": True})["exclusion_cleared"] is True
    assert "error" in tool("get_section", {"section_id": 999999})


def test_the_routing_tools_listed_are_the_old_ones_plus_the_section_tools():
    server = FastMCP("test")
    routing_mcp.register_routing_tools(server)

    import asyncio

    names = {t.name for t in asyncio.run(server.list_tools())}

    assert {"survey_area", "get_section", "adjust_section", "exclude_section", "clear_section_adjustment"} <= names
    assert {"find_routes", "find_trailheads", "get_route_candidate", "save_route_candidate", "set_way_override"} <= names


def test_a_stretch_listed_as_two_kinds_points_at_the_higher_ranked_one(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", name="River Trail")
    m.waterway([(40, -100), (40, 1600)])
    m.build(conn)

    rows = sorted(survey.survey(conn, 1, *START)["sections"], key=lambda r: r["rank"])

    assert {r["kind"] for r in rows} == {"trail_system", "water_path"}
    assert rows[0]["overlaps"] == [] and rows[1]["overlaps"] == [rows[0]["section_id"]]
