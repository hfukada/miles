import math

import pytest

from miles import gpx_parse
from miles.routes_spatial import equirect_distance_m


def _trkpt(lat: float, lng: float, ele: float | None) -> str:
    if ele is None:
        return f'<trkpt lat="{lat}" lon="{lng}"/>'
    return f'<trkpt lat="{lat}" lon="{lng}"><ele>{ele}</ele></trkpt>'


def _gpx(body: str, creator: str = "Test") -> bytes:
    return f"""<?xml version="1.0"?>
<gpx version="1.1" creator="{creator}" xmlns="http://www.topografix.com/GPX/1/1">
{body}
</gpx>""".encode("utf-8")


def _line_points(n: int, lat0: float = 40.0, step_deg: float = 0.0002, ele: list[float] | None = None) -> list[tuple[float, float, float | None]]:
    return [(lat0 + i * step_deg, -105.0, ele[i] if ele else None) for i in range(n)]


def _track_xml(points: list[tuple[float, float, float | None]], name: str | None = None) -> str:
    name_xml = f"<name>{name}</name>" if name else ""
    pts = "\n".join(_trkpt(*p) for p in points)
    return f"<trk>{name_xml}<trkseg>{pts}</trkseg></trk>"


def test_parse_single_track_with_elevation():
    points = _line_points(5, ele=[100.0, 101.0, 102.0, 103.0, 104.0])
    data = _gpx(_track_xml(points, "My Track"), creator="AllTrails.com")
    doc = gpx_parse.parse_gpx_bytes(data)
    assert doc["creator"] == "AllTrails.com"
    assert len(doc["tracks"]) == 1
    track = doc["tracks"][0]
    assert track["kind"] == "trk"
    assert track["track_index"] == 0
    assert track["gpx_name"] == "My Track"
    assert len(track["points"]) == 5
    assert gpx_parse.has_full_elevation(track["points"])


def test_parse_multiple_tracks_get_sequential_indices():
    a = _track_xml(_line_points(3), "Segment A")
    b = _track_xml(_line_points(3, lat0=41.0), "Segment B")
    doc = gpx_parse.parse_gpx_bytes(_gpx(a + b))
    assert [t["track_index"] for t in doc["tracks"]] == [0, 1]
    assert [t["gpx_name"] for t in doc["tracks"]] == ["Segment A", "Segment B"]


def test_parse_flattens_multiple_trksegs_in_one_track():
    pts1 = _line_points(3)
    pts2 = _line_points(3, lat0=41.0)
    seg1 = "".join(_trkpt(*p) for p in pts1)
    seg2 = "".join(_trkpt(*p) for p in pts2)
    body = f"<trk><name>T</name><trkseg>{seg1}</trkseg><trkseg>{seg2}</trkseg></trk>"
    doc = gpx_parse.parse_gpx_bytes(_gpx(body))
    assert len(doc["tracks"]) == 1
    assert len(doc["tracks"][0]["points"]) == 6


def test_parse_rte_rtept():
    body = "<rte><name>A Route</name>" + "".join(
        f'<rtept lat="{40.0 + i * 0.001}" lon="-105.0"/>' for i in range(4)
    ) + "</rte>"
    doc = gpx_parse.parse_gpx_bytes(_gpx(body))
    assert len(doc["tracks"]) == 1
    assert doc["tracks"][0]["kind"] == "rte"
    assert doc["tracks"][0]["gpx_name"] == "A Route"
    assert len(doc["tracks"][0]["points"]) == 4


def test_parse_no_elevation_track():
    points = _line_points(4)  # ele=None for every point
    doc = gpx_parse.parse_gpx_bytes(_gpx(_track_xml(points)))
    assert not gpx_parse.has_full_elevation(doc["tracks"][0]["points"])


def test_parse_partial_elevation_counts_as_no_elevation():
    points = [(40.0, -105.0, 100.0), (40.001, -105.0, None), (40.002, -105.0, 102.0)]
    doc = gpx_parse.parse_gpx_bytes(_gpx(_track_xml(points)))
    assert not gpx_parse.has_full_elevation(doc["tracks"][0]["points"])


def test_parse_file_level_waypoints():
    wpt = '<wpt lat="40.0005" lon="-105.0"><name>Aid Station</name><desc>Mile 3</desc></wpt>'
    doc = gpx_parse.parse_gpx_bytes(_gpx(wpt + _track_xml(_line_points(3))))
    assert len(doc["waypoints"]) == 1
    assert doc["waypoints"][0]["name"] == "Aid Station"
    assert doc["waypoints"][0]["desc"] == "Mile 3"


def test_parse_drops_tracks_with_fewer_than_two_points():
    body = _track_xml([(40.0, -105.0, None)]) + _track_xml(_line_points(3))
    doc = gpx_parse.parse_gpx_bytes(_gpx(body))
    assert len(doc["tracks"]) == 1


def test_parse_invalid_xml_raises():
    with pytest.raises(gpx_parse.GpxParseError):
        gpx_parse.parse_gpx_bytes(b"not xml at all <<<")


def test_parse_no_usable_tracks_raises():
    with pytest.raises(gpx_parse.GpxParseError):
        gpx_parse.parse_gpx_bytes(_gpx("<wpt lat=\"1\" lon=\"2\"/>"))


def test_cumulative_distances_matches_equirect_sum():
    points = _line_points(4)
    dist = gpx_parse.cumulative_distances_m(points)
    assert dist[0] == 0.0
    expected_total = sum(
        equirect_distance_m(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        for i in range(len(points) - 1)
    )
    assert dist[-1] == pytest.approx(expected_total)


def test_smoothed_elevations_reduces_jitter_amplitude():
    # A flat line with alternating +/-1m jitter every point -- smoothing
    # should collapse this toward the mean.
    raw = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(21)]
    smoothed = gpx_parse.smoothed_elevations(raw)
    assert max(smoothed) - min(smoothed) < max(raw) - min(raw)


def test_gain_loss_ignores_sub_threshold_jitter():
    # Flat ground with jitter well under ELEVATION_NOISE_M should read ~0 gain/loss.
    raw = [100.0 + 0.5 * math.sin(i) for i in range(40)]
    smoothed = gpx_parse.smoothed_elevations(raw)
    gain, loss = gpx_parse.gain_loss_m(smoothed)
    assert gain < 1.0
    assert loss < 1.0


def test_gain_loss_sums_a_real_climb_then_descent():
    # Flat lead-in/tail (long enough to clear the smoothing window without
    # being biased by the ramp itself), a 50m climb, a flat plateau, a 50m
    # descent, then a flat tail.
    lead_in = [100.0] * 10
    up = [100.0 + 2.0 * i for i in range(26)]
    plateau = [up[-1]] * 15
    down = [up[-1] - 2.0 * i for i in range(1, 26)]
    tail = [down[-1]] * 10
    smoothed = gpx_parse.smoothed_elevations(lead_in + up + plateau + down + tail)
    gain, loss = gpx_parse.gain_loss_m(smoothed)
    assert gain == pytest.approx(50.0, abs=5.0)
    assert loss == pytest.approx(50.0, abs=5.0)


def test_resample_points_hits_target_spacing():
    # ~500m straight line (about 0.0045 deg lat), resampled at 20m.
    points = _line_points(50, step_deg=0.00009)
    dist = gpx_parse.cumulative_distances_m(points)
    total = dist[-1]
    resampled = gpx_parse.resample_points(points, dist, spacing_m=20.0)
    assert resampled[0][2] == 0.0
    assert resampled[-1][2] == pytest.approx(total)
    # Interior spacing should be close to 20m (last gap may be shorter).
    gaps = [resampled[i + 1][2] - resampled[i][2] for i in range(len(resampled) - 2)]
    assert all(g == pytest.approx(20.0, abs=0.5) for g in gaps)


def test_resample_carries_elevation_when_present():
    points = _line_points(10, step_deg=0.0002, ele=[100.0 + i for i in range(10)])
    dist = gpx_parse.cumulative_distances_m(points)
    resampled = gpx_parse.resample_points(points, dist)
    assert all(p[3] is not None for p in resampled)


def test_resample_ele_is_none_without_full_elevation():
    points = _line_points(10, step_deg=0.0002)
    dist = gpx_parse.cumulative_distances_m(points)
    resampled = gpx_parse.resample_points(points, dist)
    assert all(p[3] is None for p in resampled)


def test_segment_climbs_finds_sustained_climb_and_descent():
    dist = [float(i * 20) for i in range(60)]
    ele = [100.0 + 2.0 * i for i in range(30)] + [100.0 + 2.0 * 29 - 2.0 * i for i in range(30)]
    segments = gpx_parse.segment_climbs(dist, ele, min_gain_m=30.0)
    kinds = [s["kind"] for s in segments]
    assert kinds == ["climb", "descent"]
    assert segments[0]["gain_m"] == pytest.approx(58.0, abs=1.0)
    assert segments[1]["gain_m"] == pytest.approx(58.0, abs=1.0)


def test_segment_climbs_drops_small_bumps():
    dist = [float(i * 20) for i in range(20)]
    # A 10m bump well under the 30m min_gain_m default.
    ele = [100.0] * 5 + [110.0] * 5 + [100.0] * 10
    segments = gpx_parse.segment_climbs(dist, ele, min_gain_m=30.0)
    assert segments == []


def test_grade_band_histogram_buckets_a_constant_grade_and_sums_to_one():
    # Constant +7% grade for the whole profile -- lands in the 5..10 band.
    dist = [float(i * 20) for i in range(20)]
    ele = [100.0 + 0.07 * d for d in dist]
    bands = gpx_parse.grade_band_histogram(dist, ele)
    assert bands["5..10"] == pytest.approx(1.0)
    assert sum(bands.values()) == pytest.approx(1.0)


def test_grade_band_histogram_flat_ground():
    dist = [float(i * 20) for i in range(20)]
    ele = [100.0] * 20
    bands = gpx_parse.grade_band_histogram(dist, ele)
    assert bands["flat"] == pytest.approx(1.0)
