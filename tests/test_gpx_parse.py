import math
import random

import pytest
from helpers import trkpt as _trkpt

from miles import gpx_parse


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


def test_parse_tracks_and_trksegs():
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

    a = _track_xml(_line_points(3), "Segment A")
    b = _track_xml(_line_points(3, lat0=41.0), "Segment B")
    doc = gpx_parse.parse_gpx_bytes(_gpx(a + b))
    assert [t["track_index"] for t in doc["tracks"]] == [0, 1]
    assert [t["gpx_name"] for t in doc["tracks"]] == ["Segment A", "Segment B"]

    seg1 = "".join(_trkpt(*p) for p in _line_points(3))
    seg2 = "".join(_trkpt(*p) for p in _line_points(3, lat0=41.0))
    doc = gpx_parse.parse_gpx_bytes(_gpx(f"<trk><name>T</name><trkseg>{seg1}</trkseg><trkseg>{seg2}</trkseg></trk>"))
    assert len(doc["tracks"]) == 1
    assert len(doc["tracks"][0]["points"]) == 6  # trksegs flatten into one track


def test_parse_routes_and_file_level_waypoints():
    wpt = '<wpt lat="40.0005" lon="-105.0"><name>Aid Station</name><desc>Mile 3</desc></wpt>'
    rte = "<rte><name>A Route</name>" + "".join(f'<rtept lat="{40.0 + i * 0.001}" lon="-105.0"/>' for i in range(4)) + "</rte>"
    doc = gpx_parse.parse_gpx_bytes(_gpx(wpt + rte))
    assert len(doc["tracks"]) == 1
    assert doc["tracks"][0]["kind"] == "rte"
    assert doc["tracks"][0]["gpx_name"] == "A Route"
    assert len(doc["tracks"][0]["points"]) == 4
    assert len(doc["waypoints"]) == 1
    assert doc["waypoints"][0]["name"] == "Aid Station"
    assert doc["waypoints"][0]["desc"] == "Mile 3"


@pytest.mark.parametrize(
    "points",
    [_line_points(4), [(40.0, -105.0, 100.0), (40.001, -105.0, None), (40.002, -105.0, 102.0)]],
    ids=["no elevation", "partial elevation"],
)
def test_a_track_without_elevation_on_every_point_counts_as_having_none(points):
    doc = gpx_parse.parse_gpx_bytes(_gpx(_track_xml(points)))
    assert not gpx_parse.has_full_elevation(doc["tracks"][0]["points"])


def test_parse_drops_tracks_with_fewer_than_two_points():
    body = _track_xml([(40.0, -105.0, None)]) + _track_xml(_line_points(3))
    doc = gpx_parse.parse_gpx_bytes(_gpx(body))
    assert len(doc["tracks"]) == 1


@pytest.mark.parametrize("data", [b"not xml at all <<<", _gpx('<wpt lat="1" lon="2"/>')], ids=["invalid xml", "no tracks"])
def test_parse_rejects_a_file_with_no_usable_track(data: bytes):
    with pytest.raises(gpx_parse.GpxParseError):
        gpx_parse.parse_gpx_bytes(data)


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


@pytest.mark.parametrize("with_elevation", [True, False])
def test_resample_carries_elevation_only_when_every_point_has_it(with_elevation: bool):
    points = _line_points(10, step_deg=0.0002, ele=[100.0 + i for i in range(10)] if with_elevation else None)
    dist = gpx_parse.cumulative_distances_m(points)
    resampled = gpx_parse.resample_points(points, dist)
    assert all((p[3] is not None) is with_elevation for p in resampled)


def test_segment_climbs_finds_sustained_climb_and_descent_but_not_small_bumps():
    dist = [float(i * 20) for i in range(60)]
    ele = [100.0 + 2.0 * i for i in range(30)] + [100.0 + 2.0 * 29 - 2.0 * i for i in range(30)]
    segments = gpx_parse.segment_climbs(dist, ele, min_gain_m=30.0)
    kinds = [s["kind"] for s in segments]
    assert kinds == ["climb", "descent"]
    assert segments[0]["gain_m"] == pytest.approx(58.0, abs=1.0)
    assert segments[1]["gain_m"] == pytest.approx(58.0, abs=1.0)

    bump = [100.0] * 5 + [110.0] * 5 + [100.0] * 10  # 10 m, well under min_gain_m
    assert gpx_parse.segment_climbs(dist[:20], bump, min_gain_m=30.0) == []


def _synthetic_profile(
    n: int, spacing_m: float, period_m: float, amplitude_m: float, grade: float,
    jitter_m: float = 0.0, seed: int = 0,
) -> tuple[list[float], list[float]]:
    """A steady grade plus a sinusoidal roller, optionally with uniform
    per-point jitter layered on top to stand in for GPS noise. Returns
    (dist_m, ele_m); the jitter-free version is the "truth" a jittered
    profile's smoothing is judged against."""
    dist = [i * spacing_m for i in range(n)]
    truth = [100.0 + d * grade + amplitude_m * math.sin(2 * math.pi * d / period_m) for d in dist]
    if jitter_m <= 0:
        return dist, truth
    rng = random.Random(seed)
    return dist, [e + rng.uniform(-jitter_m, jitter_m) for e in truth]


def _smoothed_gain(raw: list[float], dist: list[float], *half_window: int) -> float:
    gain, _ = gpx_parse.gain_loss_m(gpx_parse.smoothed_elevations(raw, dist, *half_window))
    return gain


def test_gain_and_loss_ignore_sub_threshold_jitter_and_sum_a_real_climb_then_descent():
    flat = [100.0 + 0.5 * math.sin(i) for i in range(40)]  # jitter well under ELEVATION_NOISE_M
    gain, loss = gpx_parse.gain_loss_m(gpx_parse.smoothed_elevations(flat, [float(i * 7) for i in range(40)]))
    assert gain < 1.0 and loss < 1.0

    up = [100.0 + 2.0 * i for i in range(26)]
    down = [up[-1] - 2.0 * i for i in range(1, 26)]
    raw = [100.0] * 10 + up + [up[-1]] * 15 + down + [down[-1]] * 10  # a 50 m climb, a plateau, a 50 m descent
    gain, loss = gpx_parse.gain_loss_m(gpx_parse.smoothed_elevations(raw, [float(i * 7) for i in range(len(raw))]))
    assert gain == pytest.approx(50.0, abs=5.0)
    assert loss == pytest.approx(50.0, abs=5.0)


def test_smoothing_recovers_true_gain_through_jitter_and_across_sample_spacings():
    rng = random.Random(1234)
    n = 200
    raw = [100.0 + 0.5 * i + rng.uniform(-2.0, 2.0) for i in range(n)]  # a steady climb with +/-2 m jitter
    assert _smoothed_gain(raw, [i * 7.0 for i in range(n)]) == pytest.approx(0.5 * (n - 1), rel=0.10)

    # Short steep pitches on a flat course, sampled at two spacings: a fixed
    # distance window smooths the same physical width either way, a fixed
    # point-count window would flatten the pitches at the sparser spacing.
    def pitches(spacing_m: float, pitch_len: float = 60.0, pitch_gain: float = 18.0, period: float = 200.0) -> tuple[list[float], list[float]]:
        dist = [i * spacing_m for i in range(int((8 * period + 200.0) / spacing_m) + 1)]
        ele = []
        for d in dist:
            pos = (d - 100.0) % period if d >= 100.0 else period
            ele.append(100.0 + pitch_gain * (1 - abs(2 * pos / pitch_len - 1)) if pos < pitch_len else 100.0)
        return dist, ele

    dense = _smoothed_gain(*pitches(7.0)[::-1])
    sparse = _smoothed_gain(*pitches(25.0)[::-1])
    assert dense > 0
    assert sparse == pytest.approx(dense, rel=0.10)


@pytest.mark.parametrize(
    "n, spacing_m, period_m, amplitude_m, grade, jitter_m, seed",
    [(500, 20.0, 250.0, 3.0, 0.03, 0.0, 0), (1500, 7.0, 500.0, 5.0, 0.02, 3.0, 7)],
    ids=["short rollers keep their gain", "gps jitter is suppressed"],
)
def test_roughness_based_smoothing_keeps_real_rollers_and_suppresses_jitter(n, spacing_m, period_m, amplitude_m, grade, jitter_m, seed):
    dist, truth = _synthetic_profile(n, spacing_m, period_m, amplitude_m, grade)
    _, ele = _synthetic_profile(n, spacing_m, period_m, amplitude_m, grade, jitter_m, seed)
    truth_gain, _ = gpx_parse.gain_loss_m(truth)
    raw_gain = sum(max(0.0, ele[i + 1] - ele[i]) for i in range(len(ele) - 1))
    half_window = gpx_parse.smoothing_half_window_for(gpx_parse.profile_roughness_m(dist, ele))
    smoothed_gain = _smoothed_gain(ele, dist, half_window)
    assert smoothed_gain == pytest.approx(truth_gain, rel=0.1)
    if jitter_m:
        assert smoothed_gain < raw_gain * 0.5
