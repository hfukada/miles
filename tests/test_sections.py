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
    for s in sections.load_sections(conn, area_id):
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
    assert [s.data["surface_m"] for s in found["trail_system"]][0].keys() == {"trail"}


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
    first = m.line([(0, 0), (0, 700)], "path", surface="gravel")
    second = m.line([(0, 700), (0, 1400)], "path", surface="gravel")
    m.line([(500, 0), (500, 700)], "path", surface="gravel")  # not in the relation
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
    before = [s.section_id for s in sections.load_sections(conn, 1)]

    def boom(*args, **kwargs):
        raise RuntimeError("sections failed")

    monkeypatch.setattr(sections, "_describe", boom)
    with pytest.raises(RuntimeError):
        m.build(conn)

    assert [s.section_id for s in sections.load_sections(conn, 1)] == before


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

    only = sections.judgments_for(conn, sections.load_sections(conn, 1))[section_id]["adjustment"]
    assert only == {"factor": 1.2, "source": "research", "note": "read it", "url": "https://example.org/trail"}
    assert conn.execute("SELECT COUNT(*) FROM net_section_judgments").fetchone()[0] == 1  # replaced, not stacked


def test_a_stored_adjustment_reattaches_to_the_rebuilt_section_that_shares_its_ways_even_when_ids_are_renumbered(conn: sqlite3.Connection):
    m = Map()
    m.square(0, 0, 300, name="Ridge Loop")
    m.square(2000, 0, 300, name="Second Loop")
    m.build(conn)
    by_name = {s.name: s for s in sections.load_sections(conn, 1)}
    sections.adjust_section(conn, by_name["Second Loop"].section_id, 1.5, "lovely", source="athlete")
    sections.exclude_section(conn, by_name["Ridge Loop"].section_id, "too busy")
    old_ids = {s.section_id for s in by_name.values()}

    m.build(conn)  # same map, new section ids

    now = {s.name: s for s in sections.load_sections(conn, 1)}
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

    new = sections.load_sections(conn, 1)
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
    stored = sections.judgments_for(conn, sections.load_sections(conn, 1))[section_id]
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
