import json
import sqlite3

import pytest
from helpers import ORIGIN_LAT, ORIGIN_LNG, WayBuilder, by_deg_lat, by_deg_lng, flat_sampler

from miles import network, sections, terrain


def pt(north_m: float, east_m: float) -> tuple[float, float]:
    """A point this many meters north and east of the test origin."""
    return (ORIGIN_LAT + by_deg_lat(north_m), ORIGIN_LNG + by_deg_lng(ORIGIN_LAT, east_m))


class Map:
    """A synthetic map around the origin: ways laid out in meters, plus the
    scenery elements a tile payload carries."""

    def __init__(self) -> None:
        self.b = WayBuilder()
        self.extra: list[dict] = []
        self._next_extra = 5000
        self._nodes: dict[tuple[float, float], int] = {}

    def node(self, north: float, east: float) -> int:
        """The node at a point, shared by every way that passes through it."""
        if (north, east) not in self._nodes:
            self._nodes[(north, east)] = self.b.node(*pt(north, east))
        return self._nodes[(north, east)]

    def line(self, points_m: list[tuple[float, float]], highway: str, **tags: str) -> int:
        """One way through the given (north, east) points, 20 m resolution."""
        ids = [self.node(*p) for p in points_m]
        return self.b.way(ids, {"highway": highway, **tags})

    def joined(self, a: tuple[float, float], z: tuple[float, float], highway: str, **tags: str) -> int:
        return self.line([a, z], highway, **tags)

    def square(self, north: float, east: float, side: float, highway: str = "path", **tags: str) -> list[int]:
        corners = [(north, east), (north, east + side), (north + side, east + side), (north + side, east)]
        ids = [self.node(*c) for c in corners]
        return [
            self.b.way([ids[i], ids[(i + 1) % 4]], {"highway": highway, **tags}) for i in range(4)
        ]

    def waterway(self, points_m: list[tuple[float, float]], name: str = "Example River", kind: str = "river") -> None:
        self.extra.append({
            "type": "way", "id": self._take(), "nodes": [], "tags": {"waterway": kind, "name": name},
            "geometry": [{"lat": pt(*p)[0], "lon": pt(*p)[1]} for p in points_m],
        })

    def area(self, ring_m: list[tuple[float, float]], name: str, **tags: str) -> None:
        ring = [list(pt(*p)) for p in ring_m]
        lats, lngs = [p[0] for p in ring], [p[1] for p in ring]
        self.extra.append({
            "type": "area", "id": self._take(), "tags": {"name": name, **tags}, "rings": [ring],
            "bounds": [min(lats), min(lngs), max(lats), max(lngs)],
        })

    def poi(self, north: float, east: float, **tags: str) -> None:
        lat, lng = pt(north, east)
        self.extra.append({"type": "node", "id": self._take(), "lat": lat, "lon": lng, "tags": tags})

    def route(self, name: str, way_ids: list[int]) -> None:
        self.extra.append({
            "type": "relation", "id": self._take(), "tags": {"type": "route", "route": "hiking", "name": name},
            "members": way_ids, "bounds": [ORIGIN_LAT, ORIGIN_LNG, ORIGIN_LAT + 0.1, ORIGIN_LNG + 0.1],
        })

    def _take(self) -> int:
        self._next_extra += 1
        return self._next_extra

    def build(self, conn: sqlite3.Connection, area_id: int = 1, sampler=None) -> None:
        network.build_network(conn, area_id, [{"elements": [*self.b.elements, *self.extra]}], sampler or flat_sampler())


def kinds_of(conn: sqlite3.Connection, area_id: int = 1) -> dict[str, list[sections.Section]]:
    out: dict[str, list[sections.Section]] = {}
    for s in sections.load_sections(conn, area_id, level=sections.GROUP):
        out.setdefault(s.kind, []).append(s)
    return out


def test_a_trail_system_is_a_connected_component_of_trail_edges_and_paved_paths_and_short_pieces_are_not(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 300, name="Ridge Loop")  # 1200 m of trail
    m.square(2000, 0, 300, name="Second Loop")
    m.square(0, 2000, 300, surface="asphalt")  # a paved path: not a trail
    m.joined((4000, 0), (4100, 0), "path")  # 100 m: under the minimum
    m.build(conn)

    found = kinds_of(conn)

    assert sorted(s.name for s in found["trail_system"]) == ["Ridge Loop", "Second Loop"]
    assert all(s.length_m == pytest.approx(1200, rel=0.02) for s in found["trail_system"])
    assert [s.data["surface_m"] for s in found["trail_system"]][0].keys() == {"singletrack"}


def test_a_very_large_trail_component_is_split_into_units_seeded_by_named_ways(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sections, "TRAIL_SYSTEM_SPLIT_M", 2500.0)
    monkeypatch.setattr(sections, "TRAIL_UNIT_SEED_M", 1000.0)
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", name="West Ridge Trail")
    m.line([(0, 1500), (0, 3000)], "path", name="East Ridge Trail")
    m.line([(300, 1500), (0, 1500)], "path")  # an unnamed spur joins whichever unit is nearest
    m.build(conn)

    names = sorted(s.name for s in kinds_of(conn)["trail_system"])

    assert names == ["East Ridge Trail area", "West Ridge Trail area"]
    assert sum(s.length_m for s in kinds_of(conn)["trail_system"]) == pytest.approx(3300, rel=0.02)


def test_a_large_component_with_no_named_ways_is_cut_into_contiguous_chunks(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sections, "TRAIL_SYSTEM_SPLIT_M", 2500.0)
    monkeypatch.setattr(sections, "UNIT_TARGET_M", 1500.0)
    m = Map()
    for i in range(6):
        m.line([(0, 600 * i), (0, 600 * (i + 1))], "path")
    m.build(conn)

    found = kinds_of(conn)["trail_system"]

    assert len(found) >= 2
    assert {s.name for s in found} == {"Unnamed trail system"}
    assert sum(s.length_m for s in found) == pytest.approx(3600, rel=0.02)


def test_a_path_along_water_is_a_water_section_with_the_water_scenery_and_a_distant_path_is_not(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", surface="asphalt")
    m.waterway([(40, -100), (40, 1600)], name="Example River")
    m.line([(2000, 0), (2000, 1500)], "path", surface="asphalt")
    m.build(conn)

    water = kinds_of(conn)["water_path"]

    assert len(water) == 1
    assert water[0].name == "Path along Example River"
    scenery = water[0].data["scenery"]
    assert "near_water" in scenery["tags"] and scenery["near_water_share"] > 0.9
    assert scenery["water_names"] == ["Example River"]
    assert water[0].centroid[0] < pt(1000, 0)[0]


def test_a_water_body_counts_as_water_by_its_shore(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", surface="asphalt")
    m.area([(30, -200), (30, 1700), (800, 1700), (800, -200), (30, -200)], "Example Lake", natural="water")
    m.build(conn)

    assert "near_water" in kinds_of(conn)["water_path"][0].data["scenery"]["tags"]


def test_a_walking_route_relation_makes_a_named_route_section_of_its_member_ways(conn: sqlite3.Connection):
    m = Map()
    first = m.line([(0, 0), (0, 700)], "path", surface="asphalt")
    second = m.line([(0, 700), (0, 1400)], "path", surface="asphalt")
    m.line([(500, 0), (500, 700)], "path", surface="asphalt")  # not in the relation
    m.route("Example Ridge Route", [first, second])
    m.build(conn)

    routes = kinds_of(conn)["named_route"]

    assert [s.name for s in routes] == ["Example Ridge Route"]
    assert routes[0].length_m == pytest.approx(1400, rel=0.02)
    assert "named_route" in routes[0].data["scenery"]["tags"]
    assert routes[0].data["scenery"]["route_names"] == ["Example Ridge Route"]


def test_paths_mostly_inside_a_park_are_park_paths_and_protected_areas_are_tagged_separately(conn: sqlite3.Connection):
    m = Map()
    m.line([(100, 100), (100, 1300)], "path", surface="asphalt")
    m.line([(3000, 100), (3000, 1300)], "path", surface="asphalt")
    m.area([(0, 0), (0, 1500), (400, 1500), (400, 0), (0, 0)], "Example Park", leisure="park")
    m.area([(2900, 0), (2900, 1500), (3400, 1500), (3400, 0), (2900, 0)], "Example Reserve", leisure="nature_reserve")
    m.build(conn)

    paths = {s.name: s for s in kinds_of(conn)["park_paths"]}

    assert set(paths) == {"Paths in Example Park", "Paths in Example Reserve"}
    park = paths["Paths in Example Park"].data["scenery"]
    assert "in_park" in park["tags"] and "protected_area" not in park["tags"]
    assert "protected_area" in paths["Paths in Example Reserve"].data["scenery"]["tags"]


def test_summits_and_viewpoints_near_a_section_are_counted_and_distant_ones_are_not(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", name="Summit Trail")
    m.poi(60, 700, natural="peak", name="Example Peak")
    m.poi(50, 300, tourism="viewpoint")
    m.poi(900, 300, tourism="viewpoint")
    m.build(conn)

    scenery = kinds_of(conn)["trail_system"][0].data["scenery"]

    assert (scenery["summits"], scenery["viewpoints"]) == (1, 1)
    assert scenery["summit_names"] == ["Example Peak"]
    assert {"summit", "viewpoint"} <= set(scenery["tags"])


def test_a_comfortable_street_with_few_junctions_is_a_quiet_road_and_a_fast_or_busy_one_is_not(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 700), (0, 1400)], "residential", name="Example Lane", maxspeed="25 mph")
    m.line([(500, 0), (500, 1400)], "residential", name="Fast Street", maxspeed="45 mph")
    # A street broken by a side street every 100 m.
    for i in range(15):
        m.line([(1000, 100 * i), (1000, 100 * (i + 1))], "residential", name="Block Street", maxspeed="25 mph")
        m.line([(1000, 100 * i), (1100, 100 * i)], "residential", name=f"Cross {i}", maxspeed="25 mph")
    m.build(conn)

    roads = kinds_of(conn)["quiet_road"]

    assert [s.name for s in roads] == ["Example Lane"]
    assert roads[0].data["junctions"] <= 1


def test_entry_nodes_are_where_a_section_meets_the_rest_of_the_network(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", name="Spur Trail")
    m.line([(-400, 0), (0, 0)], "residential", name="Access Road", maxspeed="25 mph")
    m.build(conn)

    section = kinds_of(conn)["trail_system"][0]
    nodes = {r["node_id"]: r for r in conn.execute("SELECT * FROM net_nodes WHERE area_id = 1")}

    entries = [nodes[n] for n in section.data["entry_nodes"]]
    assert len(entries) == 1
    assert entries[0]["lat"] == pytest.approx(pt(0, 0)[0], abs=1e-5)


def test_the_grade_histogram_counts_meters_per_terrain_grade_bin(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", name="Climb Trail")
    m.build(conn, sampler=terrain_slope_sampler(0.13))
    section = kinds_of(conn)["trail_system"][0]
    hist = section.data["grade_hist_m"]

    assert len(hist) == terrain.N_GRADE_BINS
    assert sum(hist) == pytest.approx(section.length_m, rel=0.05)
    assert hist[terrain.grade_bin(13.0)] > 0.8 * sum(hist)
    assert section.data["gain_m"] == pytest.approx(0.13 * 1500, rel=0.1)
    assert section.data["loss_m"] < 0.05 * section.data["gain_m"]


def terrain_slope_sampler(slope: float):
    """Elevation rising with distance east of the origin at the given grade."""
    from helpers import FakeSampler

    def elevation_at(i: int, lat: float, lng: float) -> float:
        east_m = (lng - ORIGIN_LNG) / by_deg_lng(ORIGIN_LAT, 1.0)
        return 100.0 + slope * east_m

    return FakeSampler(elevation_at)


def test_a_rebuild_replaces_sections_with_new_ids_and_clears_what_was_matched_against_them(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 300, name="Ridge Loop")
    m.build(conn)
    first = kinds_of(conn)["trail_system"][0]
    conn.execute("INSERT INTO net_section_visits (section_id, activity_id, covered_m) VALUES (?, 1, 500)", [first.section_id])
    conn.execute("INSERT INTO net_section_matched (area_id, activity_id, built_at) VALUES (1, 1, 'x')")
    conn.commit()

    m.build(conn)

    second = kinds_of(conn)["trail_system"][0]
    assert second.section_id != first.section_id
    assert conn.execute("SELECT COUNT(*) FROM net_section_visits").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM net_section_matched").fetchone()[0] == 0
    with pytest.raises(sections.SectionNotFoundError, match="survey the area again"):
        sections.get_section(conn, first.section_id)


def test_a_failed_build_keeps_the_previous_sections(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    m = Map()
    m.square(0, 0, 300, name="Ridge Loop")
    m.build(conn)
    before = [s.section_id for s in sections.load_sections(conn, 1, level=sections.GROUP)]

    def boom(*args, **kwargs):
        raise RuntimeError("sections failed")

    monkeypatch.setattr(sections, "_describe", boom)
    with pytest.raises(RuntimeError):
        m.build(conn)

    assert [s.section_id for s in sections.load_sections(conn, 1, level=sections.GROUP)] == before


def test_adjustments_clamp_to_the_bounds_and_research_needs_a_link(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 300, name="Ridge Loop")
    m.build(conn)
    section_id = kinds_of(conn)["trail_system"][0].section_id

    stored = sections.adjust_section(conn, section_id, 5.0, "scenic")
    assert (stored["factor"], stored["requested_factor"]) == (2.0, 5.0)
    assert sections.adjust_section(conn, section_id, 0.1, "dull")["factor"] == 0.5
    with pytest.raises(ValueError, match="url"):
        sections.adjust_section(conn, section_id, 1.2, "read it", source="research")
    with pytest.raises(ValueError, match="source"):
        sections.adjust_section(conn, section_id, 1.2, "x", source="agent")
    sections.adjust_section(conn, section_id, 1.2, "read it", source="research", url="https://example.org/trail")

    only = sections.judgments_for(conn, sections.load_sections(conn, 1, level=sections.GROUP))[section_id]["adjustment"]
    assert only == {"factor": 1.2, "source": "research", "note": "read it", "url": "https://example.org/trail"}
    assert conn.execute("SELECT COUNT(*) FROM net_section_judgments").fetchone()[0] == 1  # replaced, not stacked


def test_a_stored_adjustment_reattaches_to_the_rebuilt_section_that_shares_its_ways_even_when_ids_are_renumbered(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 300, name="Ridge Loop")
    m.square(2000, 0, 300, name="Second Loop")
    m.build(conn)
    by_name = {s.name: s for s in sections.load_sections(conn, 1, level=sections.GROUP)}
    sections.adjust_section(conn, by_name["Second Loop"].section_id, 1.5, "lovely", source="athlete")
    sections.exclude_section(conn, by_name["Ridge Loop"].section_id, "too busy")
    old_ids = {s.section_id for s in by_name.values()}

    m.build(conn)  # same map, new section ids

    now = {s.name: s for s in sections.load_sections(conn, 1, level=sections.GROUP)}
    assert not old_ids & {s.section_id for s in now.values()}
    judged = sections.judgments_for(conn, list(now.values()))
    assert judged[now["Second Loop"].section_id]["adjustment"]["factor"] == 1.5  # type: ignore[index]
    assert judged[now["Second Loop"].section_id]["exclusion"] is None
    assert judged[now["Ridge Loop"].section_id]["exclusion"] is not None
    assert judged[now["Ridge Loop"].section_id]["adjustment"] is None


def test_a_judgment_does_not_attach_to_a_section_that_shares_only_one_of_its_ways(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 300, name="Ridge Loop")  # ways 1-4
    m.build(conn)
    sections.adjust_section(conn, kinds_of(conn)["trail_system"][0].section_id, 1.5, "lovely")

    other = Map()
    other.line([(0, 0), (0, 900)], "path", name="Different Trail")  # way 1 again, now the whole of a new section
    other.build(conn)

    new = sections.load_sections(conn, 1, level=sections.GROUP)
    assert len(new) == 1
    assert sections.judgments_for(conn, new)[new[0].section_id]["adjustment"] is None


def test_clearing_an_adjustment_leaves_an_exclusion_unless_asked(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 300, name="Ridge Loop")
    m.build(conn)
    section_id = kinds_of(conn)["trail_system"][0].section_id
    sections.adjust_section(conn, section_id, 1.5, "lovely")
    sections.exclude_section(conn, section_id, "closed")

    sections.clear_section_adjustment(conn, section_id)
    stored = sections.judgments_for(conn, sections.load_sections(conn, 1, level=sections.GROUP))[section_id]
    assert stored["adjustment"] is None and stored["exclusion"] is not None

    sections.clear_section_adjustment(conn, section_id, exclusion=True)
    assert sections.excluded_way_ids(conn) == set()


def test_section_geometry_is_simplified_and_bounded(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 600, name="Ridge Loop")
    m.build(conn)
    section = kinds_of(conn)["trail_system"][0]

    lines = sections.section_geometry(conn, section, max_points=12)

    assert 1 <= sum(len(line) for line in lines) <= 12
    assert all(len(p) == 2 for line in lines for p in line)
    json.dumps(lines)


def only_section(conn: sqlite3.Connection) -> sections.Section:
    """The area's single group."""
    found = sections.load_sections(conn, 1, level=sections.GROUP)
    assert len(found) == 1, [(s.kind, s.name) for s in found]
    return found[0]


def test_drafts_of_different_kinds_over_one_stretch_become_one_section_with_every_kind_and_the_best_name(conn: sqlite3.Connection):
    m = Map()
    first = m.line([(0, 0), (0, 700)], "path", name="Bank Path")
    second = m.line([(0, 700), (0, 1400)], "path", name="Bank Path")
    m.waterway([(40, -100), (40, 1500)])
    m.route("Example Greenway", [first, second])
    m.build(conn)

    merged = only_section(conn)

    assert set(merged.kinds) == {"trail_system", "water_path", "named_route"}
    assert merged.kind == merged.kinds[0]
    assert merged.name == "Example Greenway"
    assert merged.data["other_names"] and merged.length_m == pytest.approx(1400, rel=0.02)
    assert merged.data["scenery"]["route_names"] == ["Example Greenway"]
    assert "near_water" in merged.data["scenery"]["tags"]


def test_a_stretch_mostly_inside_a_much_larger_one_joins_its_group(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 1500, name="Big Loop")
    m.waterway([(40, -100), (40, 1600)])  # beside one side of the loop
    m.build(conn)

    group = only_section(conn)

    assert sorted(group.kinds) == ["trail_system", "water_path"]
    assert group.length_m == pytest.approx(6000, rel=0.02)


def test_drafts_join_a_group_through_a_third_that_overlaps_both(conn: sqlite3.Connection):
    m = Map()
    a = m.line([(0, 0), (0, 1000)], "path", name="West Trail", surface="asphalt")
    b = m.line([(0, 1000), (0, 2000)], "path", name="East Trail", surface="asphalt")
    m.route("West Route", [a])
    m.route("East Route", [b])
    m.route("Whole Route", [a, b])
    m.build(conn)

    group = only_section(conn)

    assert group.kinds == ["named_route"] and group.length_m == pytest.approx(2000, rel=0.02)
    assert group.name == "Whole Route" and set(group.data["other_names"]) == {"East Route", "West Route"}


def test_two_stretches_that_barely_touch_stay_in_separate_groups(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 400, name="West Loop")
    m.square(0, 400, 400, name="East Loop")  # shares one side's corner only through a node
    m.line([(2000, 0), (2000, 1500)], "path", name="Far Trail")
    m.build(conn)

    groups = sections.load_sections(conn, 1, level=sections.GROUP)

    assert len(groups) >= 2


def test_the_union_of_overlapping_sections_is_kept_when_each_has_edges_the_other_lacks(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1000)], "path", name="Trail A")  # trail system continues past the water
    m.line([(0, 1000), (0, 1500)], "path", name="Trail A")
    m.waterway([(40, -100), (40, 1000)])
    m.build(conn)

    merged = only_section(conn)

    assert set(merged.kinds) == {"trail_system", "water_path"}
    assert merged.length_m == pytest.approx(1500, rel=0.02)


def test_parallel_same_named_paths_on_two_banks_are_noted_and_counted_once_in_the_single_path_length(conn: sqlite3.Connection):
    m = Map()
    m.line([(-60, 0), (-60, 1500)], "path", name="Bank Path")
    m.line([(60, 0), (60, 1500)], "path", name="Bank Path")
    m.line([(-60, 0), (60, 0)], "path")
    m.line([(-60, 1500), (60, 1500)], "path")
    m.waterway([(0, -100), (0, 1600)], name="Example River")
    m.build(conn)

    merged = only_section(conn)

    assert set(merged.kinds) == {"trail_system", "water_path"}
    assert merged.data["parallel_m"] == pytest.approx(1500, rel=0.03)
    assert merged.single_path_m == pytest.approx(merged.length_m - 1500, rel=0.03)


def test_a_single_path_or_two_far_apart_same_named_paths_are_not_parallel(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", name="Ridge Trail")
    m.line([(0, 1500), (3000, 1500)], "path", name="Ridge Trail")  # same name, end to end
    m.build(conn)

    assert only_section(conn).data["parallel_m"] == 0.0


def test_pieces_of_one_route_that_meet_within_the_gap_are_one_section_and_farther_ones_are_not(conn: sqlite3.Connection):
    m = Map()
    a = m.line([(0, 0), (0, 1000)], "path", surface="asphalt")
    m.line([(0, 1000), (0, 1100)], "path", surface="asphalt")  # a gap in the relation, bridged
    b = m.line([(0, 1100), (0, 2100)], "path", surface="asphalt")
    c = m.line([(500, 0), (500, 1000)], "path", surface="asphalt")
    d = m.line([(500, 1400), (500, 2400)], "path", surface="asphalt")  # 400 m of nothing before it
    m.route("Gap Route", [a, b])
    m.route("Far Route", [c, d])
    m.build(conn)

    found = {s.name: s for s in sections.load_sections(conn, 1, level=sections.GROUP)}

    assert found["Gap Route"].length_m == pytest.approx(2100, rel=0.02)
    assert {"Far Route (part 1)", "Far Route (part 2)"} <= set(found)


def test_a_judgment_on_a_section_that_later_merges_reattaches_to_the_merged_section(conn: sqlite3.Connection):
    m = Map()
    first = m.line([(0, 0), (0, 700)], "path", surface="asphalt")
    second = m.line([(0, 700), (0, 1400)], "path", surface="asphalt")
    m.route("Example Greenway", [first, second])
    m.build(conn)
    route = only_section(conn)
    assert route.kinds == ["named_route"]
    sections.adjust_section(conn, route.section_id, 1.5, "lovely", source="athlete")
    sections.exclude_section(conn, route.section_id, "closed")

    m.waterway([(40, -100), (40, 1500)])  # the same stretch is now also a water path
    m.build(conn)

    merged = only_section(conn)
    assert set(merged.kinds) == {"named_route", "water_path"}
    judged = sections.judgments_for(conn, [merged])[merged.section_id]
    assert judged["adjustment"] is not None and judged["adjustment"]["factor"] == 1.5
    assert judged["exclusion"] is not None


def test_a_judgment_on_the_lead_kind_of_a_merged_section_stays_attached_and_one_of_another_kind_does_not(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1400)], "path", surface="asphalt")
    m.waterway([(40, -100), (40, 1500)])
    m.build(conn)
    water = only_section(conn)
    sections.adjust_section(conn, water.section_id, 1.5, "lovely")

    m.route("Example Greenway", [1])
    m.build(conn)

    merged = only_section(conn)
    assert set(merged.kinds) == {"water_path", "named_route"}
    assert sections.judgments_for(conn, [merged])[merged.section_id]["adjustment"] is not None
    other_kind: sections.Judgment = {
        "judgment_id": 0, "judgment": "adjust", "section_kind": "quiet_road", "factor": 1.5, "source": "athlete",
        "note": None, "url": None, "ways": merged.way_lengths, "level": "group",
    }
    assert not sections.judgment_matches(other_kind, merged)


@pytest.mark.parametrize(
    "highway, tags, expected",
    [
        ("path", {}, "singletrack"),
        ("footway", {"surface": "dirt"}, "singletrack"),
        ("bridleway", {}, "singletrack"),
        ("path", {"width": "3"}, "track"),
        ("path", {"width": "1 m"}, "singletrack"),
        ("path", {"tracktype": "grade3"}, "track"),
        ("path", {"motor_vehicle": "agricultural"}, "track"),
        ("path", {"bicycle": "designated", "surface": "compacted"}, "track"),
        ("path", {"bicycle": "designated", "surface": "dirt"}, "singletrack"),
        ("path", {"bicycle": "designated", "surface": "compacted", "mtb:scale": "1"}, "singletrack"),
        ("cycleway", {"surface": "gravel"}, "track"),
        ("track", {"surface": "gravel"}, "track"),
        ("track", {"tracktype": "grade5", "sac_scale": "hiking"}, "singletrack"),
        ("track", {"trail_visibility": "bad"}, "singletrack"),
        ("track", {"width": "4", "sac_scale": "hiking"}, "track"),
    ],
)
def test_trail_ways_are_classified_as_singletrack_or_track_from_their_tags(highway: str, tags: dict, expected: str):
    surface_class = network._surface_class(highway, tags)

    assert network._trail_kind(highway, surface_class, tags) == expected


def test_paved_ways_and_roads_are_not_classified_as_trail():
    assert network._trail_kind("path", network._surface_class("path", {"surface": "asphalt"}), {"surface": "asphalt"}) is None
    assert network._trail_kind("residential", "unpaved_road", {}) is None
    assert network._trail_kind("track", network._surface_class("track", {"surface": "asphalt"}), {"surface": "asphalt"}) is None


def test_a_section_reports_singletrack_and_track_apart(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1000)], "path", name="Footpath")
    m.line([(0, 1000), (0, 2000)], "track", name="Fire Road", tracktype="grade2")
    m.build(conn)

    surface = only_section(conn).data["surface_m"]

    assert surface == {"singletrack": pytest.approx(1000, rel=0.02), "track": pytest.approx(1000, rel=0.02)}


def test_parallel_paths_are_found_by_geometry_whatever_they_are_named(conn: sqlite3.Connection):
    m = Map()
    m.line([(-60, 0), (-60, 1500)], "path", name="South Bank Trail")
    m.line([(60, 0), (60, 1500)], "path", name="North Bank Trail")
    m.line([(-60, 0), (60, 0)], "path")
    m.line([(-60, 1500), (60, 1500)], "path")
    m.waterway([(0, -100), (0, 1600)], name="Example River")
    m.build(conn)

    group = only_section(conn)
    (stretch,) = stretches_of(conn)

    assert group.data["parallel_m"] == pytest.approx(1500, rel=0.03)
    assert group.single_path_m == pytest.approx(group.length_m - 1500, rel=0.03)
    assert sorted(stretch.data["sides"]) == ["north", "south"]
    assert group.group_id is None and group.level == sections.GROUP


def test_paths_farther_apart_than_the_parallel_distance_are_not_parallel(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 1500)], "path", name="West Trail")
    m.line([(600, 0), (600, 1500)], "path", name="East Trail")
    m.line([(0, 0), (600, 0)], "path")
    m.build(conn)

    assert only_section(conn).data["parallel_m"] == 0.0


def test_a_judgment_on_a_stretch_stays_on_that_stretch_and_one_on_a_group_covers_all_its_stretches(conn: sqlite3.Connection):
    m = Map()
    long_trail(m, 4000)
    m.line([(0, 2000), (0, 2060)], "path", name="Test Bridge", bridge="yes")
    m.build(conn)
    group = only_section(conn)
    first, second = sections.stretches_of(conn, group.section_id)

    sections.adjust_section(conn, first.section_id, 1.5, "lovely")
    sections.adjust_section(conn, group.section_id, 0.8, "busy")
    sections.exclude_section(conn, first.section_id, "closed")

    judged = sections.judgments_for(conn, [group, first, second])
    assert judged[first.section_id]["adjustment"]["factor"] == 1.5  # type: ignore[index]
    assert judged[second.section_id]["adjustment"] is None
    assert judged[group.section_id]["adjustment"]["factor"] == 0.8  # type: ignore[index]
    assert judged[group.section_id]["exclusion"] is None
    excluded = sections.excluded_way_ids(conn)
    assert excluded == set(first.way_lengths)
    assert excluded < set(group.way_lengths)

    sections.exclude_section(conn, group.section_id, "closed")
    assert sections.excluded_way_ids(conn) == set(group.way_lengths)


# ---------------------------------------------------------------------------
# Stretches
# ---------------------------------------------------------------------------


def long_trail(m: Map, length: int = 4000, name: str = "Long Trail", at: int = 0) -> None:
    """A trail along the east axis, one way per 500 m so that its nodes are graph nodes."""
    for start in range(0, length, 500):
        m.line([(at, start), (at, start + 500)], "path", name=name)


def stretches_of(conn: sqlite3.Connection, area_id: int = 1) -> list[sections.Section]:
    groups = sections.load_sections(conn, area_id, level=sections.GROUP)
    assert len(groups) == 1
    return sections.stretches_of(conn, groups[0].section_id)


def edge_ends(conn: sqlite3.Connection, edge_id: int) -> set[int]:
    row = conn.execute("SELECT from_node_id, to_node_id FROM net_edges WHERE edge_id = ?", [edge_id]).fetchone()
    return {row["from_node_id"], row["to_node_id"]}


def chain_m(conn: sqlite3.Connection, stretch: sections.Section) -> float:
    """The length of a stretch's primary chain."""
    ids = stretch.data["chains"][0]
    marks = ",".join("?" for _ in ids)
    return conn.execute(f"SELECT SUM(length_m) FROM net_edges WHERE edge_id IN ({marks})", ids).fetchone()[0]


def assert_walkable(conn: sqlite3.Connection, chain: list[int]) -> None:
    for before, after in zip(chain, chain[1:]):
        assert edge_ends(conn, before) & edge_ends(conn, after), (before, after)


def test_a_bridge_cuts_a_trail_into_stretches_named_for_it(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 2000)], "path", name="Long Trail")
    m.line([(0, 2000), (0, 2060)], "path", name="Test Bridge", bridge="yes")
    m.line([(0, 2060), (0, 4000)], "path", name="Long Trail")
    m.build(conn)

    found = stretches_of(conn)

    assert len(found) == 2
    assert found[0].data["role"] == "west end to Test Bridge" and found[1].data["role"] == "Test Bridge to east end"
    assert sum(s.length_m for s in found) == pytest.approx(4060, rel=0.02)
    assert all(s.level == sections.STRETCH and s.group_id for s in found)


def test_a_busy_road_cuts_at_the_crossing_and_a_quiet_street_is_a_junction(conn: sqlite3.Connection):
    m = Map()
    for start in range(0, 6000, 1500):
        m.line([(0, start), (0, start + 1500)], "path", name="Long Trail")
    m.line([(-300, 1500), (0, 1500), (300, 1500)], "secondary", name="Crossing Road", maxspeed="45 mph")
    m.line([(-300, 4500), (0, 4500), (300, 4500)], "residential", name="Quiet Street", maxspeed="25 mph")
    m.build(conn)

    found = stretches_of(conn)

    assert [s.data["role"] for s in found] == [
        "west end to Crossing Road", "between Crossing Road and Quiet Street", "Quiet Street to east end",
    ]


def test_a_trailhead_cuts_a_trail(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 2000)], "path", name="Long Trail")
    m.line([(0, 2000), (0, 4000)], "path", name="Long Trail")
    m.b.node(*pt(30, 2000), {"highway": "trailhead", "name": "Quarry Trailhead"})
    m.build(conn)

    found = stretches_of(conn)

    assert len(found) == 2 and found[0].data["role"].endswith("to Quarry Trailhead")


def test_a_name_change_cuts_a_trail(conn: sqlite3.Connection):
    m = Map()
    long_trail(m, 2000, name="Upper Trail")
    m.line([(0, 2000), (0, 4000)], "path", name="Lower Trail")
    m.build(conn)

    found = stretches_of(conn)

    assert len(found) == 2 and found[0].data["role"].endswith("to Lower Trail")


def test_a_short_interval_is_merged_into_a_neighbor_dropping_the_lower_priority_cut(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 3000)], "path", name="Long Trail")
    m.line([(0, 3000), (0, 3060)], "path", name="Test Bridge", bridge="yes")
    m.line([(0, 3060), (0, 3600)], "path", name="Long Trail")
    m.line([(0, 3600), (0, 6000)], "path", name="Long Trail")
    m.line([(-300, 3600), (0, 3600), (300, 3600)], "secondary", name="Crossing Road", maxspeed="45 mph")
    m.build(conn)

    found = stretches_of(conn)

    assert len(found) == 2 and found[0].data["role"].endswith("to Test Bridge")
    assert all(chain_m(conn, s) >= sections.STRETCH_MIN_M for s in found)


def test_a_long_interval_is_split_at_the_junction_nearest_its_middle_and_at_midway_nodes_when_there_is_none(conn: sqlite3.Connection):
    m = Map()
    long_trail(m, 14_000)
    m.line([(0, 6500), (300, 6500)], "path", name="Spur Trail")
    m.build(conn)

    found = stretches_of(conn)

    junction = next(i for i, s in enumerate(found) if s.data["role"].endswith("and junction with Spur Trail"))
    assert sum(chain_m(conn, s) for s in found[:junction + 1]) == pytest.approx(6500, abs=1)
    assert all(chain_m(conn, s) <= sections.STRETCH_MAX_M for s in found) and len(found) >= 3
    assert any(s.data["role"].startswith("west end to mile ") for s in found)
    assert sum(s.length_m for s in found) == pytest.approx(14_300, rel=0.02)


def test_a_loop_is_one_stretch_walked_from_its_end_node_back_to_it(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 1400, name="Round Trail")
    m.build(conn)

    (only,) = stretches_of(conn)

    assert only.data["role"] == "whole"
    assert only.data["end_nodes"][0] == only.data["end_nodes"][1]
    assert len(only.data["chains"]) == 1 and len(only.data["chains"][0]) == 4
    assert_walkable(conn, only.data["chains"][0])


def banks_with_bridge(m: Map, length: int = 6000) -> None:
    half = length // 2
    for name_at in (-60, 60):
        m.line([(name_at, 0), (name_at, half), (name_at, length)], "path", name="Bank Path")
    m.line([(-60, half), (60, half)], "path", name="Middle Bridge", bridge="yes")
    m.waterway([(0, -100), (0, length + 100)], name="Example River")


def test_a_stretch_of_a_river_spans_both_banks_between_the_same_bridges_and_counts_one_bank(conn: sqlite3.Connection):
    m = Map()
    banks_with_bridge(m)
    m.build(conn)

    found = stretches_of(conn)
    (group,) = sections.load_sections(conn, 1, level=sections.GROUP)

    assert len(found) == 2
    for stretch in found:
        assert len(stretch.data["chains"]) == 2 and set(stretch.data["sides"]) == {"north", "south"}
        assert stretch.single_path_m == pytest.approx(3000, rel=0.05)
        assert stretch.length_m > 1.9 * stretch.single_path_m
        for chain in stretch.data["chains"]:
            assert_walkable(conn, chain)
    assert group.single_path_m == pytest.approx(sum(s.single_path_m for s in found), abs=1)
    assert group.single_path_m == pytest.approx(6000, rel=0.05)


def test_banks_joined_only_by_a_street_are_cut_together(conn: sqlite3.Connection):
    m = Map()
    banks = [m.line([(at, 0), (at, 2000), (at, 4000)], "path", name="Bank Path") for at in (-60, 60)]
    street = m.line([(-60, 0), (60, 0)], "residential", name="Joining Street", maxspeed="25 mph")
    m.route("Example Greenway", [*banks, street])
    m.line([(-300, 2000), (-60, 2000)], "secondary", name="Crossing Road", maxspeed="45 mph")
    m.line([(300, 2000), (60, 2000)], "secondary", name="Crossing Road", maxspeed="45 mph")
    m.build(conn)

    found = stretches_of(conn)

    assert len(found) == 2
    assert all(len(s.data["chains"]) == 2 and len(s.data["sides"]) == 2 for s in found)
    assert sum(s.single_path_m for s in found) == pytest.approx(4000 + 120, rel=0.03)  # one bank and the street


def test_building_blocks_are_the_stretches_in_order_with_walkable_chains(conn: sqlite3.Connection):
    m = Map()
    banks_with_bridge(m)
    m.build(conn)
    (group,) = sections.load_sections(conn, 1, level=sections.GROUP)
    stretches = sections.stretches_of(conn, group.section_id)

    blocks = sections.building_blocks(conn, 1)

    assert [b["block_id"] for b in blocks] == [s.section_id for s in stretches]
    for block, stretch in zip(blocks, stretches):
        assert block["section_id"] == group.section_id and block["name"] == stretch.name
        assert block["edge_ids"] == stretch.edge_ids
        for chain in block["chains"]:
            assert_walkable(conn, chain)
    assert sections.building_blocks(conn, 1, [group.section_id]) == blocks
    assert sections.building_blocks(conn, 1, [stretches[1].section_id])[0]["block_id"] == stretches[1].section_id
    assert sections.building_blocks(conn, 1, []) == []
    with pytest.raises(sections.SectionNotFoundError):
        sections.building_blocks(conn, 1, [999_999])
    with pytest.raises(ValueError, match="area"):
        sections.building_blocks(conn, 2, [group.section_id])


def test_a_chain_runs_on_through_the_following_stretches_that_continue_it(conn: sqlite3.Connection):
    m = Map()
    banks_with_bridge(m)
    m.build(conn)
    (group,) = sections.load_sections(conn, 1, level=sections.GROUP)
    first, second = sections.stretches_of(conn, group.section_id)

    both = sections.building_blocks(conn, 1, [group.section_id])
    alone = sections.building_blocks(conn, 1, [second.section_id])

    assert len(both[0]["chains"][0]) == len(first.data["chains"][0]) + len(second.data["chains"][0])
    assert both[0]["end_nodes"] == (first.data["end_nodes"][0], second.data["end_nodes"][1])
    assert both[0]["end_nodes"][0] in edge_ends(conn, both[0]["chains"][0][0])
    assert both[0]["end_nodes"][1] in edge_ends(conn, both[0]["chains"][0][-1])
    assert second.data["end_nodes"][0] in both[0]["entry_nodes"]
    assert len(both[1]["chains"]) == len(second.data["chains"]) - 1
    assert alone[0]["chains"] == second.data["chains"]
    assert_walkable(conn, both[0]["chains"][0])


def test_building_block_ids_are_the_area_graph_ids(conn: sqlite3.Connection):
    from miles import route_search

    m = Map()
    m.line([(0, 0), (0, 2000)], "path", name="Long Trail")
    m.line([(0, 2000), (0, 2060)], "path", name="Test Bridge", bridge="yes")
    m.line([(0, 2060), (0, 4000)], "path", name="Long Trail")
    m.build(conn)

    graph, rows = route_search.load_search_graph(conn, 1)
    blocks = sections.building_blocks(conn, 1)

    assert blocks
    for block in blocks:
        for node in (*block["end_nodes"], *block["entry_nodes"]):
            assert node in graph.nodes
        for chain in block["chains"]:
            for edge_id in chain:
                assert edge_id in rows and rows[edge_id]["length_m"] > 0


def test_a_stretch_judgment_reattaches_after_a_rebuild_and_only_to_its_stretch(conn: sqlite3.Connection):
    def build() -> None:
        m = Map()
        m.line([(0, 0), (0, 2000)], "path", name="Long Trail")
        m.line([(0, 2000), (0, 2060)], "path", name="Test Bridge", bridge="yes")
        m.line([(0, 2060), (0, 4000)], "path", name="Long Trail")
        m.build(conn)

    build()
    first, second = stretches_of(conn)
    sections.adjust_section(conn, first.section_id, 1.5, "lovely")
    sections.exclude_section(conn, second.section_id, "closed")

    build()
    new_first, new_second = stretches_of(conn)
    judged = sections.judgments_for(conn, [new_first, new_second])

    assert new_first.section_id != first.section_id
    adjustment = judged[new_first.section_id]["adjustment"]
    assert adjustment is not None and adjustment["factor"] == 1.5
    assert judged[new_first.section_id]["exclusion"] is None
    assert judged[new_second.section_id]["adjustment"] is None and judged[new_second.section_id]["exclusion"] is not None


def test_a_group_reports_the_sum_of_its_stretches_single_path_lengths(conn: sqlite3.Connection):
    m = Map()
    m.line([(-60, 0), (-60, 1500)], "path", name="Bank Path")
    m.line([(60, 0), (60, 1500)], "path", name="Bank Path")
    m.line([(-60, 0), (60, 0)], "path")
    m.line([(-60, 1500), (60, 1500)], "path")
    m.waterway([(0, -100), (0, 1600)], name="Example River")
    m.build(conn)

    found = stretches_of(conn)
    (group,) = sections.load_sections(conn, 1, level=sections.GROUP)

    assert len(found) == 1 and found[0].data["role"] == "whole"
    assert group.single_path_m == pytest.approx(sum(s.single_path_m for s in found), abs=1)
    assert group.single_path_m == pytest.approx(1500 + 120, rel=0.1)


@pytest.mark.parametrize(
    "tags, expected",
    [({"bridge": "yes"}, "bridge"), ({"bridge": "boardwalk"}, "bridge"), ({"tunnel": "culvert"}, "tunnel"),
     ({"bridge": "no"}, None), ({}, None)],
)
def test_a_way_is_marked_a_bridge_or_tunnel_from_its_tags(tags: dict, expected: str | None):
    assert network._structure(tags) == expected


def test_the_graph_keeps_which_edges_are_bridges(conn: sqlite3.Connection):
    m = Map()
    m.line([(0, 0), (0, 100)], "path", name="Test Bridge", bridge="yes")
    m.line([(0, 100), (0, 200)], "path")
    m.build(conn)

    rows = conn.execute("SELECT way_name, structure FROM net_edges ORDER BY edge_id").fetchall()

    assert {(r["way_name"], r["structure"]) for r in rows} == {("Test Bridge", "bridge"), (None, None)}


def test_a_long_branch_is_a_unit_of_its_own_and_a_short_spur_is_noted_on_its_stretch(conn: sqlite3.Connection):
    m = Map()
    long_trail(m, 8000)
    m.line([(0, 4000), (3200, 4000)], "path", name="Side Trail")
    m.line([(0, 1000), (200, 1000)], "path", name="Spur Trail")
    m.build(conn)

    found = stretches_of(conn)
    (group,) = sections.load_sections(conn, 1, level=sections.GROUP)

    branch = [s for s in found if "branch" in s.data["role"]]
    main = [s for s in found if "branch" not in s.data["role"]]
    assert [s.data["role"] for s in branch] == ["north branch off Long Trail"]
    assert sum(s.length_m for s in branch) == pytest.approx(3200, rel=0.03)
    assert sum(s.length_m for s in main) == pytest.approx(8000 + 200, rel=0.03)  # the spur counts toward its stretch
    assert sum(s.length_m for s in found) == pytest.approx(group.length_m, rel=0.001)
    assert max(s.single_path_m for s in found) <= sections.STRETCH_MAX_M
    noted = [s for s in main if s.data["notes"]]
    assert len(noted) == 1 and noted[0].data["notes"] == ["includes spur to Spur Trail (0.1 mi)"]
    assert branch[0].data["notes"] == []


def test_a_branch_shorter_than_the_branch_minimum_is_folded_into_the_stretch_it_hangs_from(conn: sqlite3.Connection):
    m = Map()
    long_trail(m, 8000)
    m.line([(0, 4000), (2400, 4000)], "path")
    m.build(conn)

    found = stretches_of(conn)

    assert not any("branch" in s.data["role"] for s in found)
    assert [n for s in found for n in s.data["notes"]] == ["includes spur to the north (1.5 mi)"]
    assert sum(s.length_m for s in found) == pytest.approx(8000 + 2400, rel=0.03)


def test_a_cut_is_named_for_a_road_then_a_bridge_then_a_trailhead_and_never_for_its_own_trail(conn: sqlite3.Connection):
    m = Map()
    for start in range(0, 12_000, 1500):
        m.line([(0, start), (0, start + 1500)], "path", name="Long Trail")
    m.line([(-300, 3000), (0, 3000)], "residential", name="Quiet Street", maxspeed="25 mph")
    m.line([(0, 4500), (0, 4560)], "path", name="Test Bridge", bridge="yes")
    m.line([(-300, 4500), (0, 4500)], "secondary", name="Crossing Road", maxspeed="45 mph")  # road and bridge at one node
    m.b.node(*pt(30, 7500), {"highway": "trailhead", "name": "Quarry Trailhead"})
    m.line([(300, 9000), (0, 9000)], "path", name="Long Trail")  # the trail's own name joining it
    m.build(conn)

    roles = [s.data["role"] for s in stretches_of(conn)]

    joined = " | ".join(roles)
    assert "Crossing Road" in joined and "Test Bridge" not in joined  # the road outranks the bridge at one node
    assert "Quarry Trailhead" in joined and "Quiet Street" in joined
    assert "junction with Long Trail" not in joined
    for role in roles:
        parts = role.removeprefix("between ").split(" and ") if role.startswith("between ") else []
        assert len(set(parts)) == len(parts)


def test_a_stretch_between_two_crossings_of_one_road_does_not_read_as_between_x_and_x(conn: sqlite3.Connection):
    m = Map()
    for start in range(0, 9000, 1500):
        m.line([(0, start), (0, start + 1500)], "path", name="Long Trail")
    for at in (1500, 4500):
        m.line([(-300, at), (0, at), (300, at)], "secondary", name="Crossing Road", maxspeed="45 mph")
    m.build(conn)

    roles = [s.data["role"] for s in stretches_of(conn)]

    assert roles[1] == "between Crossing Road at mile 0.9 and Crossing Road at mile 2.8"


def test_an_unnamed_cut_is_placed_by_the_distance_along_the_path(conn: sqlite3.Connection):
    m = Map()
    long_trail(m, 12_000)
    m.build(conn)

    roles = [s.data["role"] for s in stretches_of(conn)]

    assert roles[0].startswith("west end to ") and roles[-1].endswith(" to east end")
    assert any("midpoint" in r or "mile " in r for r in roles) and "None" not in " ".join(roles)


def mesh(m: Map, lines: int = 6, step: int = 500) -> None:
    """A square grid of trails, one way per block so that every crossing is a node."""
    for i in range(lines):
        for j in range(lines - 1):
            m.line([(i * step, j * step), (i * step, (j + 1) * step)], "path", name="Grid Trail")
            m.line([(j * step, i * step), ((j + 1) * step, i * step)], "path", name="Grid Trail")


def test_a_mesh_of_trails_is_divided_into_a_few_regions_that_tile_it(conn: sqlite3.Connection):
    m = Map()
    mesh(m)  # 30 km of trail
    m.b.node(*pt(0, 0), {"highway": "trailhead", "name": "Corner Trailhead"})
    m.build(conn)

    found = stretches_of(conn)
    (group,) = sections.load_sections(conn, 1, level=sections.GROUP)

    assert 3 <= len(found) <= 6
    assert all(sections.REGION_MIN_M <= s.length_m <= sections.REGION_MAX_M for s in found)
    assert sorted(e for s in found for e in s.edge_ids) == sorted(group.edge_ids)  # every edge in exactly one region
    assert sum(s.single_path_m for s in found) == pytest.approx(group.single_path_m, abs=1)
    assert len({s.data["role"] for s in found}) == len(found)
    for region in found:
        assert region.data["sides"] == [] and region.data["chains"]
        covered = set()
        for chain in region.data["chains"]:
            assert_walkable(conn, chain)
            covered |= set(chain)
        assert covered <= set(region.edge_ids) and len(covered) == len(region.edge_ids)
        primary = region.data["chains"][0]
        assert max(len(c) for c in region.data["chains"]) == len(primary)


def test_a_small_mesh_is_one_region_and_a_trail_with_few_branches_is_not_a_mesh(conn: sqlite3.Connection):
    m = Map()
    mesh(m, lines=3)  # 6 km
    m.build(conn)

    (only,) = stretches_of(conn)

    assert only.data["role"] == "whole" and only.length_m == pytest.approx(6000, rel=0.02)


def test_regions_are_named_for_their_main_trails_or_their_access_point(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sections, "TRAIL_SYSTEM_SPLIT_M", 100_000.0)
    m = Map()
    for i in range(6):
        for j in range(5):
            m.line([(i * 500, j * 500), (i * 500, (j + 1) * 500)], "path", name="Ridge Trail" if i < 3 else "Creek Trail")
            m.line([(j * 500, i * 500), ((j + 1) * 500, i * 500)], "path")
    m.b.node(*pt(0, 0), {"highway": "trailhead", "name": "Corner Trailhead"})
    m.build(conn)

    roles = [s.data["role"] for s in stretches_of(conn)]

    assert any(r.startswith("Ridge Trail") for r in roles) and any(r.startswith("Creek Trail") for r in roles)
    assert all(r.endswith("area") or "area (" in r or r.endswith("part") or r == "whole" for r in roles)
