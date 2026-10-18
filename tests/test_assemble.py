import itertools
import sqlite3

import pytest
from test_sections import Map, pt

from miles import assemble, route_search, sections, survey

START = pt(0, 0)


@pytest.fixture(autouse=True)
def no_fragment_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(survey, "FRAGMENT_MIN_M", 0.0)
    monkeypatch.setattr(survey, "FRAGMENT_RUN_SHARE", 0.0)


def request(start: tuple[float, float], dmin: float, dmax: float, **extra) -> route_search.RouteSearchRequest:
    req: route_search.RouteSearchRequest = {
        "area_id": 1,
        "starts": [{"lat": start[0], "lng": start[1]}],
        "distance_m_min": dmin,
        "distance_m_max": dmax,
        "n": 10,
        "time_budget_s": 20.0,
        "seed": 1,
    }
    req.update(extra)  # type: ignore[typeddict-item]
    return req


def group_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {s.name: s.section_id for s in sections.load_sections(conn, 1, level=sections.GROUP)}


def used(candidate: route_search.RouteCandidateOut) -> set[str]:
    """The names of the sections a candidate runs, a stretch's name being its section's."""
    return {u["name"].split(":")[0] for u in candidate["sections_used"]}


def named(result: route_search.RouteSearchResult, shape: str | None = None) -> list[route_search.RouteCandidateOut]:
    return [c for c in result["candidates"] if shape is None or c["shape"] == shape]


def ring_with_street() -> Map:
    """A 2400 m trail square reached by a 300 m quiet street."""
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="Access Street", maxspeed="25 mph")
    m.square(0, 300, 600, name="Ridge Loop")
    return m


def test_an_out_and_back_along_a_block_turns_where_the_distance_fits(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="Access Street", maxspeed="25 mph")
    m.line([(0, 300), (0, 4300)], "path", name="Long Trail")
    m.build(conn)

    result = route_search.find_routes(conn, request(START, 2800, 3200, shape="out_and_back"))

    assert result["candidates"]
    for c in result["candidates"]:
        assert c["built_from"] == "sections" and c["shape"] == "out_and_back"
        assert 2800 <= c["distance_m"] <= 3200  # turned partway along 4000 m of trail, not at its end
        assert used(c) == {"Long Trail"}
        assert sum(u["length_m"] for u in c["sections_used"]) == pytest.approx(c["distance_m"] - 600, rel=0.02)
        assert not c["missed_constraints"]


def test_a_loop_through_a_block_comes_back_by_a_different_path(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 600, name="Ridge Loop")
    m.build(conn)

    result = route_search.find_routes(conn, request(START, 2200, 2600, shape="loop"))

    assert result["candidates"]
    best = result["candidates"][0]
    assert best["built_from"] == "sections" and best["shape"] == "loop"
    by_way: dict[str, float] = {}
    for w in best["ways"]:
        by_way[w["name"]] = by_way.get(w["name"], 0.0) + w["length_m"]
    assert by_way == {"Ridge Loop": pytest.approx(2400, rel=0.05)}  # every side exactly once
    assert 2200 <= best["distance_m"] <= 2600


def test_a_return_that_rejoins_the_approach_is_a_lollipop_not_a_loop(conn: sqlite3.Connection):
    ring_with_street().build(conn)

    result = route_search.find_routes(conn, request(START, 2700, 3300))

    assert named(result, "lollipop") and not named(result, "loop")
    by_way: dict[str, float] = {}
    for w in named(result, "lollipop")[0]["ways"]:
        by_way[w["name"]] = by_way.get(w["name"], 0.0) + w["length_m"]
    assert by_way["Access Street"] == pytest.approx(600, rel=0.05)  # in and out once each
    assert by_way["Ridge Loop"] == pytest.approx(2400, rel=0.05)


def test_a_lollipop_runs_a_stem_and_a_loop_inside_the_block(conn: sqlite3.Connection):
    ring_with_street().build(conn)

    result = route_search.find_routes(conn, request(START, 2700, 3300, shape="lollipop"))

    lollipops = named(result, "lollipop")
    assert lollipops and all(c["built_from"] == "sections" for c in lollipops)
    stem = [w for w in lollipops[0]["ways"] if w["name"] == "Access Street"]
    assert sum(w["length_m"] for w in stem) == pytest.approx(600, rel=0.05)


def test_two_nearby_blocks_are_linked_into_one_route(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="Access Street", maxspeed="25 mph")
    m.line([(0, 300), (0, 1200)], "path", name="Alder Trail")
    m.line([(0, 1200), (0, 1500)], "residential", name="Link Street", maxspeed="25 mph")
    m.line([(0, 1500), (0, 2400)], "path", name="Birch Trail")
    m.line([(0, 2400), (-300, 2400), (-300, 0), (0, 0)], "residential", name="Return Street", maxspeed="25 mph")
    m.build(conn)

    ids = group_ids(conn)

    result = route_search.find_routes(conn, request(START, 5100, 5700, shape="loop", sections=[ids["Alder Trail"], ids["Birch Trail"]]))

    linked = [c for c in result["candidates"] if used(c) >= {"Alder Trail", "Birch Trail"}]
    assert linked
    assert linked[0]["shape"] == "loop" and 5100 <= linked[0]["distance_m"] <= 5700
    ways = [w["name"] for w in linked[0]["ways"]]
    assert "Return Street" in ways  # home by the other street, not back over the trails


def ring_loop_scores(conn: sqlite3.Connection, **extra) -> dict[str, float]:
    result = route_search.find_routes(conn, request(pt(0, 600), 1500, 1700, **extra))
    best: dict[str, float] = {}
    for c in result["candidates"]:
        best[c["shape"]] = max(best.get(c["shape"], -1e9), c["ranking"]["score"])
    return best


def test_the_loop_bonus_ranks_a_loop_over_an_equal_out_and_back_and_zero_ties_them(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 400, 400, name="Ridge Loop")
    m.build(conn)

    default = route_search.find_routes(conn, request(pt(0, 600), 1500, 1700))
    scores = ring_loop_scores(conn, loop_bonus=0.0)
    bonus = ring_loop_scores(conn, loop_bonus=0.3)

    assert default["candidates"][0]["shape"] == "loop"
    assert scores["loop"] == pytest.approx(scores["out_and_back"], rel=1e-3)
    assert bonus["loop"] == pytest.approx(1.3 * bonus["out_and_back"], rel=1e-3)
    assert default["candidates"][0]["ranking"]["loop"] == pytest.approx(1.0 + route_search.DEFAULT_LOOP_BONUS)


def test_a_better_out_and_back_still_beats_a_lesser_loop(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 200)], "residential", name="West Lane", maxspeed="25 mph")
    m.square(0, 200, 400, name="Plain Loop")
    m.line([(0, 0), (0, -200)], "residential", name="East Lane", maxspeed="25 mph")
    m.line([(0, -200), (0, -1200)], "path", surface="asphalt", name="Scenic Path")
    m.waterway([(40, -1300), (40, -100)])
    m.build(conn)

    result = route_search.find_routes(conn, request(START, 1800, 2600))

    top = result["candidates"][0]
    assert top["shape"] == "out_and_back" and used(top) == {"Scenic Path"}
    loops = [c for c in result["candidates"] if c["shape"] in ("loop", "lollipop")]
    assert loops and used(loops[0]) == {"Plain Loop"}
    assert loops[0]["ranking"]["loop"] > 1.0 and loops[0]["ranking"]["score"] < top["ranking"]["score"]


def test_an_excluded_sections_ways_are_never_used_and_naming_it_is_refused(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="West Street", maxspeed="25 mph")
    m.square(0, 300, 400, name="West Loop")
    m.line([(0, 0), (0, -300)], "residential", name="East Street", maxspeed="25 mph")
    m.square(0, -700, 400, name="East Loop")
    m.build(conn)
    ids = group_ids(conn)
    sections.exclude_section(conn, ids["East Loop"], "closed")

    result = route_search.find_routes(conn, request(START, 1800, 2800))

    assert result["candidates"]
    for c in result["candidates"]:
        assert "East Loop" not in {w["name"] for w in c["ways"]}
    with pytest.raises(ValueError, match="excluded"):
        route_search.find_routes(conn, request(START, 1800, 2800, sections=[ids["East Loop"]]))


def test_naming_sections_restricts_the_search_to_them(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="West Street", maxspeed="25 mph")
    m.square(0, 300, 400, name="West Loop")
    m.line([(0, 0), (0, -300)], "residential", name="East Street", maxspeed="25 mph")
    m.square(0, -700, 400, name="East Loop")
    m.build(conn)
    ids = group_ids(conn)

    result = route_search.find_routes(conn, request(START, 1800, 2800, sections=[ids["East Loop"]]))

    assert result["candidates"]
    for c in result["candidates"]:
        assert c["built_from"] == "sections"
        assert used(c) == {"East Loop"}
        assert "West Loop" not in {w["name"] for w in c["ways"]}
    with pytest.raises(sections.SectionNotFoundError):
        route_search.find_routes(conn, request(START, 1800, 2800, sections=[999999]))


def test_an_unreachable_named_section_gets_a_warning_and_no_walk(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 300)], "residential", name="Access Street", maxspeed="25 mph")
    m.square(0, 300, 400, name="Near Loop")
    m.square(9000, 0, 400, name="Far Loop")
    m.build(conn)

    result = route_search.find_routes(conn, request(START, 1800, 2800, sections=[group_ids(conn)["Far Loop"]]))

    assert result["candidates"] == [] and result["warnings"]


def test_the_walk_fills_in_when_the_area_has_no_sections(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 100, name="Tiny Loop")  # under the minimum section length
    m.build(conn)

    result = route_search.find_routes(conn, request(pt(0, 50), 300, 500, shape="loop"))

    assert result["candidates"]
    assert {c["built_from"] for c in result["candidates"]} == {"walk"}
    assert all(c["sections_used"] == [] for c in result["candidates"])


def test_the_assembly_runs_inside_the_search_slot_and_stops_at_the_deadline(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    ring_with_street().build(conn)
    held: list[bool] = []
    real = assemble.Assembly.candidates

    def watching(self, deadline):
        held.append(route_search._search_lock.locked())
        yield from real(self, deadline)

    monkeypatch.setattr(assemble.Assembly, "candidates", watching)
    ticks = itertools.count()
    monkeypatch.setattr(assemble.time, "monotonic", lambda: 10.0 * next(ticks))

    result = route_search.find_routes(conn, request(START, 2700, 3300, n=10, time_budget_s=3.0))

    assert held == [True]
    assert result["budget_exceeded"] is True
    assert result["candidates"] == []


def test_a_loop_bonus_outside_its_bounds_is_refused(conn: sqlite3.Connection):
    ring_with_street().build(conn)

    with pytest.raises(ValueError, match="loop_bonus"):
        route_search.find_routes(conn, request(START, 2700, 3300, loop_bonus=0.9))
    with pytest.raises(ValueError, match="surface_preference"):
        route_search.find_routes(conn, request(START, 2700, 3300, surface_preference="gravel"))


def three_regions() -> Map:
    """Three 3.2 km loops of one trail system, each touching the next at a corner."""
    m = Map()
    for k in range(3):
        m.square(k * 800, k * 800, 800, name=f"Loop {k}")
    return m


def blocks_used(candidate: route_search.RouteCandidateOut) -> set[int]:
    return {u["block_id"] for u in candidate["sections_used"]}


def test_a_loop_chains_regions_of_a_trail_system_none_of_which_fills_the_distance(conn: sqlite3.Connection):
    three_regions().build(conn)

    two = route_search.find_routes(conn, request(START, 5800, 7000, shape="loop"))
    three = route_search.find_routes(conn, request(START, 8800, 10400, shape="loop"))

    assert any(len(blocks_used(c)) == 2 for c in two["candidates"])
    assert any(len(blocks_used(c)) == 3 for c in three["candidates"])
    for result, (low, high) in ((two, (5800, 7000)), (three, (8800, 10400))):
        for c in result["candidates"]:
            assert c["built_from"] == "sections" and c["shape"] == "loop" and low <= c["distance_m"] <= high
            assert not c["missed_constraints"]


def test_a_chained_loop_is_ranked_like_any_other_candidate(conn: sqlite3.Connection):
    three_regions().build(conn)

    result = route_search.find_routes(conn, request(START, 5800, 7000, shape="loop", loop_bonus=0.2))

    chained = next(c for c in result["candidates"] if len(blocks_used(c)) == 2)
    ranking = chained["ranking"]
    assert ranking["loop"] == pytest.approx(1.2) and ranking["fit"] == pytest.approx(1.0)
    assert ranking["score"] == pytest.approx(ranking["quality_mi"] * 1.2 - ranking["access_cost"])
    assert sum(u["length_m"] for u in chained["sections_used"]) == pytest.approx(chained["distance_m"], rel=0.02)


def test_a_trail_mesh_far_from_the_start_gives_lollipops_that_chain_its_regions(conn: sqlite3.Connection):
    from test_sections import mesh

    m = Map()
    m.line([(0, -2500), (0, 0)], "residential", name="Access Street", maxspeed="25 mph")
    mesh(m)  # 30 km of trail, several regions
    m.build(conn)
    far = pt(0, -2500)

    for low, high in ((16_000, 18_000), (20_000, 22_000), (26_000, 29_000)):
        result = route_search.find_routes(conn, request(far, low, high))

        lollipops = named(result, "lollipop")
        assert lollipops, (low, high)
        for c in lollipops:
            assert low <= c["distance_m"] <= high and not c["missed_constraints"]
            by_way: dict[str, float] = {}
            for w in c["ways"]:
                by_way[w["name"]] = by_way.get(w["name"], 0.0) + w["length_m"]
            assert by_way["Access Street"] == pytest.approx(5000, rel=0.05)  # the stem, out and back
        assert any(len(blocks_used(c)) >= 2 for c in lollipops)
