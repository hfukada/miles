"""GPX parsing and per-route derived math: distance, elevation smoothing,
resampling, climb segmentation, and grade-band histograms.

Untrusted uploads, so parsing goes through defusedxml rather than the stdlib
XML modules. GPX 1.0 and 1.1 declare different namespaces for the same
elements, so every lookup matches by local tag name (ignoring the namespace
URI) rather than a hardcoded namespace map.

Elevation gain/loss is a centered moving average followed by a hysteresis
threshold: raw point-to-point deltas count GPS/DEM jitter as climbing.
"""

import math
from typing import Literal
from typing_extensions import TypedDict

import defusedxml.ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import Element, ParseError

from .routes_spatial import equirect_distance_m

# Elevation deltas smaller than this, measured from the last point that
# itself cleared the threshold, are GPS/barometric noise rather than real
# climb or descent -- without a floor, jitter on flat ground sums to a
# large fake gain over a long track. Distance-based smoothing (below)
# already removes most of this noise before it reaches gain_loss_m, so the
# floor only needs to catch what survives smoothing.
ELEVATION_NOISE_M = 1.0

# Half-width, in meters of along-route distance, of the centered moving
# average applied before the hysteresis threshold above (the window spans
# every point within this distance on either side of the point being
# smoothed). A distance, not a point count: point spacing varies widely
# between GPX sources -- dense GPS tracks sample every few meters,
# route-planner/DEM exports every twenty-plus -- so a fixed point count
# smooths over a different physical distance depending on the file, while a
# distance window gives the same physical smoothing regardless of sampling
# density.
ELEVATION_SMOOTHING_HALF_WINDOW_M = 50.0

# Noise floor used only when pairing turning points into climb/descent
# segments (segment_climbs). Kept separate from ELEVATION_NOISE_M so a
# tighter gain/loss noise floor doesn't fragment one sustained climb into
# many small segments.
CLIMB_SEGMENTATION_NOISE_M = 3.0

# Target spacing for the resampled route_points table.
RESAMPLE_SPACING_M = 20.0

# Even reference spacing profile_roughness_m resamples onto before measuring
# second differences, so the estimate reflects a profile's own smoothness
# rather than how densely its source happened to sample the ground (dense
# GPS tracks vs. sparse route-planner exports).
ROUGHNESS_GRID_SPACING_M = 20.0

# Roughness (see profile_roughness_m) at or below this is a smooth,
# terrain-model-like profile -- real short rollers change slope gradually
# from one grid step to the next, so little or no smoothing is needed to
# keep their gain. At or above ROUGHNESS_NOISY_M it's GPS/DEM-jitter-grade
# noise, needing the full window this project has always smoothed consumer
# GPX with; between the two the window scales linearly. See
# adr/0002-routes-raw-gpx-ground-truth.md's 2026-09-27 amendment.
ROUGHNESS_SMOOTH_M = 0.3
ROUGHNESS_NOISY_M = 1.5

# Minimum |elevation change| for a turning-point pair to count as a
# reported climb/descent segment -- well above ELEVATION_NOISE_M, which
# only filters point-to-point jitter. A sustained climb is a much bigger
# feature than a single noisy step.
CLIMB_MIN_GAIN_M = 30.0

# (lo, hi, label) grade bands, grade expressed as a fraction (0.10 = 10%).
# Bounds are lo <= grade < hi except the last band, which is closed at hi.
GRADE_BANDS: list[tuple[float, float, str]] = [
    (float("-inf"), -0.10, "<-10"),
    (-0.10, -0.05, "-10..-5"),
    (-0.05, -0.02, "-5..-2"),
    (-0.02, 0.02, "flat"),
    (0.02, 0.05, "2..5"),
    (0.05, 0.10, "5..10"),
    (0.10, float("inf"), ">10"),
]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# The GPX 1.1 default namespace -- required on a root element for the file
# to validate against the schema most consumers (Strava included) expect.
GPX_NAMESPACE = "http://www.topografix.com/GPX/1/1"

# lat, lng, elevation in meters (None when the point carries no <ele>)
GpxPoint = tuple[float, float, float | None]


class GpxTrack(TypedDict):
    kind: Literal["trk", "rte"]
    track_index: int
    gpx_name: str | None
    points: list[GpxPoint]


class GpxWaypoint(TypedDict):
    name: str | None
    desc: str | None
    lat: float
    lng: float


class GpxDocument(TypedDict):
    creator: str | None
    tracks: list[GpxTrack]
    waypoints: list[GpxWaypoint]


class ClimbSegment(TypedDict):
    kind: Literal["climb", "descent"]
    start_m: float
    end_m: float
    gain_m: float
    avg_grade: float


class GpxParseError(ValueError):
    """Raised when a file isn't parseable XML or has no usable tracks/routes."""


def _local_tag(elem: Element) -> str:
    tag = elem.tag
    return tag.split("}", 1)[1] if "}" in tag else tag


def _child_text(elem: Element, tag: str) -> str | None:
    for child in elem:
        if _local_tag(child) == tag:
            text = (child.text or "").strip()
            return text or None
    return None


def _parse_ele(point_elem: Element) -> float | None:
    for child in point_elem:
        if _local_tag(child) == "ele" and child.text:
            try:
                return float(child.text)
            except ValueError:
                return None
    return None


def _parse_point(elem: Element) -> GpxPoint | None:
    lat = elem.get("lat")
    lon = elem.get("lon")
    if lat is None or lon is None:
        return None
    return (float(lat), float(lon), _parse_ele(elem))


def parse_gpx_bytes(data: bytes) -> GpxDocument:
    """Parse raw GPX bytes into every trk/rte (as one entry per track, in
    document order) and every file-level wpt. Multiple trkseg within one trk
    are flattened into a single point list -- segment breaks aren't tracked.
    Tracks/routes with fewer than two points are dropped as unusable.
    """
    try:
        root = DefusedET.fromstring(data)
    except (ParseError, DefusedXmlException) as exc:
        raise GpxParseError(f"Not valid GPX/XML: {exc}") from exc
    if not isinstance(root, Element):
        raise GpxParseError("Not valid GPX/XML: unexpected document root")

    tracks: list[GpxTrack] = []
    for child in root:
        tag = _local_tag(child)
        if tag == "trk":
            points: list[GpxPoint] = []
            for seg in child:
                if _local_tag(seg) != "trkseg":
                    continue
                for trkpt in seg:
                    if _local_tag(trkpt) != "trkpt":
                        continue
                    p = _parse_point(trkpt)
                    if p is not None:
                        points.append(p)
            if len(points) >= 2:
                tracks.append(
                    {
                        "kind": "trk",
                        "track_index": len(tracks),
                        "gpx_name": _child_text(child, "name"),
                        "points": points,
                    }
                )
        elif tag == "rte":
            points = []
            for rtept in child:
                if _local_tag(rtept) != "rtept":
                    continue
                p = _parse_point(rtept)
                if p is not None:
                    points.append(p)
            if len(points) >= 2:
                tracks.append(
                    {
                        "kind": "rte",
                        "track_index": len(tracks),
                        "gpx_name": _child_text(child, "name"),
                        "points": points,
                    }
                )
    if not tracks:
        raise GpxParseError("No trk or rte with at least two points")

    waypoints: list[GpxWaypoint] = []
    for child in root:
        if _local_tag(child) != "wpt":
            continue
        p = _parse_point(child)
        if p is None:
            continue
        waypoints.append(
            {
                "name": _child_text(child, "name"),
                "desc": _child_text(child, "desc"),
                "lat": p[0],
                "lng": p[1],
            }
        )

    return {"creator": root.get("creator"), "tracks": tracks, "waypoints": waypoints}


def cumulative_distances_m(points: list[GpxPoint]) -> list[float]:
    """Cumulative equirectangular distance along points, dist[0] == 0.0."""
    dist = [0.0]
    for i in range(1, len(points)):
        d = equirect_distance_m(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])
        dist.append(dist[-1] + d)
    return dist


def has_full_elevation(points: list[GpxPoint]) -> bool:
    """True only when every point carries <ele>. A track with some points
    missing elevation is treated the same as one with none -- a partial
    profile can't support gain/grade stats any more honestly than no
    profile at all, and mixing real and absent values would silently bias
    the smoothing window."""
    return all(p[2] is not None for p in points)


def smoothed_elevations(
    elevations: list[float], dist_m: list[float], half_window_m: float = ELEVATION_SMOOTHING_HALF_WINDOW_M
) -> list[float]:
    """Centered moving average over a +/-half_window_m window of cumulative
    distance; edge points average over whatever falls inside the series.
    `dist_m` must be the cumulative distance for each point in `elevations`
    (same length, non-decreasing). half_window_m of 0 is a no-op smoothing
    pass (each point averages only itself and any co-located duplicates).

    Run before the hysteresis threshold in gain_loss_m, not after: at
    typical consumer GPX point spacing, per-point elevation jitter is on
    the same scale as ELEVATION_NOISE_M itself, so hysteresis alone would
    double count a real climb that arrives as a staircase of small steps.
    Averaging first removes the jitter's amplitude while leaving the
    climb's shape (which unfolds over many points) intact.

    Two-pointer sliding window: as i advances, both window edges are
    non-decreasing in cumulative distance, so each edge only ever moves
    forward, giving O(n) instead of O(n * window size).
    """
    n = len(elevations)
    half = half_window_m
    out = [0.0] * n
    lo = 0
    hi = -1
    total = 0.0
    count = 0
    for i in range(n):
        while hi + 1 < n and dist_m[hi + 1] - dist_m[i] <= half:
            hi += 1
            total += elevations[hi]
            count += 1
        while dist_m[i] - dist_m[lo] > half:
            total -= elevations[lo]
            count -= 1
            lo += 1
        out[i] = total / count
    return out


def gain_loss_m(smoothed: list[float]) -> tuple[float, float]:
    """Cumulative gain/loss in meters over a smoothed elevation series. A
    delta only counts against the last point that itself cleared
    ELEVATION_NOISE_M, not the immediately preceding point -- otherwise a
    staircase of sub-threshold jitter would never accumulate into a real
    climb, and small noise on flat ground would never get filtered."""
    if len(smoothed) < 2:
        return 0.0, 0.0
    gain = 0.0
    loss = 0.0
    last = smoothed[0]
    for ele in smoothed[1:]:
        delta = ele - last
        if abs(delta) < ELEVATION_NOISE_M:
            continue
        if delta > 0:
            gain += delta
        else:
            loss += -delta
        last = ele
    return gain, loss


def _even_targets(total: float, spacing_m: float) -> list[float]:
    targets: list[float] = []
    t = 0.0
    while t < total:
        targets.append(t)
        t += spacing_m
    targets.append(total)
    return targets


def _linear_resample(dist_m: list[float], series: list[float], targets: list[float]) -> list[float]:
    """Linearly interpolate `series` (aligned with dist_m) onto `targets`,
    a non-decreasing list of cumulative distances starting at dist_m[0] or
    later. No smoothing."""
    n = len(dist_m)
    out: list[float] = []
    j = 0
    for target in targets:
        while j < n - 2 and dist_m[j + 1] < target:
            j += 1
        d0, d1 = dist_m[j], dist_m[j + 1] if j + 1 < n else dist_m[j]
        frac = 0.0 if d1 <= d0 else (target - d0) / (d1 - d0)
        frac = min(1.0, max(0.0, frac))
        v0, v1 = series[j], series[j + 1] if j + 1 < n else series[j]
        out.append(v0 + (v1 - v0) * frac)
    return out


def profile_roughness_m(dist_m: list[float], ele_m: list[float]) -> float:
    """A profile's own smoothness, independent of its source or point
    density: elevation is linearly resampled onto an even
    ROUGHNESS_GRID_SPACING_M grid (no smoothing), then this is the RMS
    second difference across that grid -- how much the slope changes from
    one grid step to the next. Real terrain features (a roller, a
    switchback) change slope gradually over many grid steps and leave a
    small value; GPS/DEM jitter reverses direction almost every step and
    leaves a large one, regardless of how densely the source happened to
    sample the ground.
    """
    if len(dist_m) < 3:
        return 0.0
    origin = dist_m[0]
    total = dist_m[-1] - origin
    if total <= 0:
        return 0.0
    shifted = [d - origin for d in dist_m]
    targets = _even_targets(total, ROUGHNESS_GRID_SPACING_M)
    grid_ele = _linear_resample(shifted, ele_m, targets)
    if len(grid_ele) < 3:
        return 0.0
    second_diffs = [grid_ele[i + 1] - 2 * grid_ele[i] + grid_ele[i - 1] for i in range(1, len(grid_ele) - 1)]
    n = len(second_diffs)
    mean = sum(second_diffs) / n
    variance = sum((x - mean) ** 2 for x in second_diffs) / n
    return math.sqrt(variance)


def smoothing_half_window_for(roughness_m: float) -> float:
    """Smoothing half-window (see smoothed_elevations) chosen from a
    profile's own roughness rather than its source label -- a smooth
    terrain-model-like profile needs little or none to keep real short
    rollers; a GPS-jitter-like profile needs the full window this project
    has always used. Linear ramp between the two thresholds."""
    if roughness_m <= ROUGHNESS_SMOOTH_M:
        return 0.0
    if roughness_m >= ROUGHNESS_NOISY_M:
        return ELEVATION_SMOOTHING_HALF_WINDOW_M
    frac = (roughness_m - ROUGHNESS_SMOOTH_M) / (ROUGHNESS_NOISY_M - ROUGHNESS_SMOOTH_M)
    return frac * ELEVATION_SMOOTHING_HALF_WINDOW_M


def resample_points(
    points: list[GpxPoint],
    dist_m: list[float],
    spacing_m: float = RESAMPLE_SPACING_M,
    half_window_m: float = ELEVATION_SMOOTHING_HALF_WINDOW_M,
) -> list[tuple[float, float, float, float | None]]:
    """Resample onto an even ~spacing_m grid along cumulative distance,
    linearly interpolating lat/lng and (when present) a smoothed elevation
    series (smoothed over +/-half_window_m -- see smoothed_elevations).
    Returns (lat, lng, dist_m, ele_m) tuples; the first and last raw points
    are always included.
    """
    total = dist_m[-1]
    if total <= 0:
        lat, lng, ele = points[0]
        return [(lat, lng, 0.0, ele)]

    elevations = [p[2] for p in points]
    has_ele = all(e is not None for e in elevations)
    smoothed = smoothed_elevations([e for e in elevations if e is not None], dist_m, half_window_m) if has_ele else []

    targets = _even_targets(total, spacing_m)

    lats = _linear_resample(dist_m, [p[0] for p in points], targets)
    lngs = _linear_resample(dist_m, [p[1] for p in points], targets)
    eles: list[float | None] = list(_linear_resample(dist_m, smoothed, targets)) if has_ele else [None] * len(targets)
    return list(zip(lats, lngs, targets, eles))


def bbox(points: list[GpxPoint]) -> tuple[float, float, float, float]:
    """(south, west, north, east)."""
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    return (min(lats), min(lngs), max(lats), max(lngs))


def _turning_point_indices(ele: list[float], noise_m: float) -> list[int]:
    """Indices of ele forming the smallest set of alternating peaks/troughs
    that reproduce gain_loss_m's hysteresis: the same reversal-from-the-
    running-extreme test, but recording *where* each confirmed extreme was
    instead of only accumulating its magnitude."""
    n = len(ele)
    if n < 2:
        return list(range(n))
    turning = [0]
    direction = 0  # 0 unknown, 1 climbing toward extreme_idx, -1 descending
    extreme_idx = 0
    for i in range(1, n):
        if direction >= 0 and ele[i] >= ele[extreme_idx]:
            extreme_idx = i
            direction = 1
        elif direction <= 0 and ele[i] <= ele[extreme_idx]:
            extreme_idx = i
            direction = -1
        else:
            reversal = ele[extreme_idx] - ele[i] if direction == 1 else ele[i] - ele[extreme_idx]
            if reversal >= noise_m:
                turning.append(extreme_idx)
                extreme_idx = i
                direction = -direction
    if turning[-1] != extreme_idx:
        turning.append(extreme_idx)
    if turning[-1] != n - 1:
        turning.append(n - 1)
    return turning


def segment_climbs(
    dist_m: list[float],
    ele_m: list[float],
    min_gain_m: float = CLIMB_MIN_GAIN_M,
    noise_m: float = CLIMB_SEGMENTATION_NOISE_M,
) -> list[ClimbSegment]:
    """Sustained climbs and descents: pair up consecutive turning points
    from the (already smoothed) elevation series and keep only pairs whose
    |elevation change| clears min_gain_m. Ordered along the route."""
    if len(ele_m) < 2:
        return []
    turning = _turning_point_indices(ele_m, noise_m)
    segments: list[ClimbSegment] = []
    for a, b in zip(turning, turning[1:]):
        delta = ele_m[b] - ele_m[a]
        magnitude = abs(delta)
        if magnitude < min_gain_m:
            continue
        seg_dist = dist_m[b] - dist_m[a]
        segments.append(
            {
                "kind": "climb" if delta > 0 else "descent",
                "start_m": dist_m[a],
                "end_m": dist_m[b],
                "gain_m": magnitude,
                "avg_grade": (delta / seg_dist) if seg_dist > 0 else 0.0,
            }
        )
    return segments


def grade_band_histogram(dist_m: list[float], ele_m: list[float]) -> dict[str, float]:
    """Share of distance (0..1, summing to ~1.0) falling in each GRADE_BANDS
    bucket, computed point-to-point over the (already smoothed) profile."""
    totals = {label: 0.0 for _, _, label in GRADE_BANDS}
    total_dist = 0.0
    for i in range(len(dist_m) - 1):
        seg = dist_m[i + 1] - dist_m[i]
        if seg <= 0:
            continue
        grade = (ele_m[i + 1] - ele_m[i]) / seg
        total_dist += seg
        for lo, hi, label in GRADE_BANDS:
            if lo <= grade < hi:
                totals[label] += seg
                break
        else:
            totals[GRADE_BANDS[-1][2]] += seg
    if total_dist <= 0:
        return {label: 0.0 for label in totals}
    return {label: v / total_dist for label, v in totals.items()}
