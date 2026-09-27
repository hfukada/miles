"""Pure spatial geometry for route matching: distance, a coarse grid index,
junction/overlap detection between two point sequences, shape
classification, and the pointwise slicing compose_route needs to stitch
partial legs together.

Deliberately dependency-light -- equirectangular distance plus a grid index
over lat/lng cells, no GIS library. At the scale of a single hike or race
course (tens of km, thousands of points) the flat-earth approximation this
uses is accurate to well under a meter, and a real projection library would
be a lot of dependency for a problem this local in extent.
"""

import math
from typing_extensions import TypedDict

_EARTH_RADIUS_M = 6371000.0
_METERS_PER_DEG_LAT = 111320.0


class Junction(TypedDict):
    """A brief contact point between two routes -- a crossing, not a
    shared stretch (see Overlap for those)."""
    a_dist_m: float
    b_dist_m: float
    distance_m: float  # actual separation between the two matched points


class Overlap(TypedDict):
    """A run of consecutive points on route A that all lie within
    tolerance of route B -- the two routes running together for a
    stretch (a shared road, a shared section of trail)."""
    a_from_m: float
    a_to_m: float
    b_from_m: float
    b_to_m: float


class JunctionResult(TypedDict):
    junctions: list[Junction]
    overlaps: list[Overlap]


# A run of consecutive matched points shorter than this (along route A) is
# reported as a Junction (a brief contact); at or above it, an Overlap (the
# routes are running together, not just touching).
OVERLAP_MIN_LEN_M = 100.0

# A run is only one overlap while the matched position on B keeps advancing
# by roughly one sample step per step of A, forward or backward alike. A
# step bigger than this many multiples of B's own point spacing means A's
# nearest match jumped to an unrelated stretch of B -- e.g. a loop's
# outbound and return legs both passing within tolerance of the same A
# stretch near a shared trailhead -- and the run must split there rather
# than reporting a shared span all the way from B's first cluster to its
# last.
OVERLAP_MAX_STEP_MULTIPLE = 5.0


def equirect_distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Flat-earth approximation using the pair's mean latitude as the
    reference for longitude scaling. Accurate to well under a meter over
    the point spacing this module deals with (tens of meters); a full
    haversine buys nothing extra at that scale."""
    ref_lat = math.radians((lat1 + lat2) / 2.0)
    x = math.radians(lng2 - lng1) * math.cos(ref_lat)
    y = math.radians(lat2 - lat1)
    return math.sqrt(x * x + y * y) * _EARTH_RADIUS_M


class Grid:
    """Coarse lat/lng bucket index over a fixed point list, for
    radius queries. Cell size is fixed at construction from a reference
    latitude (the mean of the indexed points) -- good enough since every
    use here spans a single route or a local neighborhood, not enough
    latitude range for the deg-per-meter approximation to drift.
    """

    def __init__(self, points: list[tuple[float, float]], cell_size_m: float):
        self._points = points
        self._cell_size_m = cell_size_m
        ref_lat = math.radians(sum(p[0] for p in points) / len(points)) if points else 0.0
        self._deg_per_m_lat = 1.0 / _METERS_PER_DEG_LAT
        cos_ref = max(math.cos(ref_lat), 1e-6)  # guard the poles; irrelevant at running scale
        self._deg_per_m_lng = 1.0 / (_METERS_PER_DEG_LAT * cos_ref)
        self._cell_lat = cell_size_m * self._deg_per_m_lat
        self._cell_lng = cell_size_m * self._deg_per_m_lng
        self._cells: dict[tuple[int, int], list[int]] = {}
        for i, (lat, lng) in enumerate(points):
            self._cells.setdefault(self._cell_of(lat, lng), []).append(i)

    def _cell_of(self, lat: float, lng: float) -> tuple[int, int]:
        return (int(math.floor(lat / self._cell_lat)), int(math.floor(lng / self._cell_lng)))

    def near(self, lat: float, lng: float, radius_m: float) -> list[int]:
        """Candidate point indices within radius_m -- a superset (cell
        granularity, no exact-distance filtering); callers must re-check
        with equirect_distance_m."""
        if not self._points:
            return []
        cx, cy = self._cell_of(lat, lng)
        reach_x = max(1, math.ceil(radius_m / max(self._cell_size_m, 1e-9)))
        reach_y = reach_x
        out: list[int] = []
        for dx in range(-reach_x, reach_x + 1):
            for dy in range(-reach_y, reach_y + 1):
                out.extend(self._cells.get((cx + dx, cy + dy), []))
        return out


def _typical_spacing_m(dist: list[float]) -> float:
    """Median gap between consecutive entries of a cumulative-distance
    series -- a proxy for that list's own resample spacing, without
    importing gpx_parse's RESAMPLE_SPACING_M (gpx_parse already imports
    from this module, so the reverse import would cycle)."""
    if len(dist) < 2:
        return 0.0
    diffs = sorted(dist[i + 1] - dist[i] for i in range(len(dist) - 1))
    return diffs[len(diffs) // 2]


def _split_run_by_b_continuity(
    run: list[tuple[int, int, float]], dist_b: list[float], max_step_m: float
) -> list[list[tuple[int, int, float]]]:
    """Split a run of A-index-contiguous matches wherever the matched
    position on B jumps by more than max_step_m from one A point to the
    next. B may advance forward or backward within one sub-run (both are a
    genuine shared stretch); a jump either way starts a new one."""
    groups: list[list[tuple[int, int, float]]] = [[run[0]]]
    for prev, cur in zip(run, run[1:]):
        delta = dist_b[cur[1]] - dist_b[prev[1]]
        if abs(delta) > max_step_m:
            groups.append([cur])
        else:
            groups[-1].append(cur)
    return groups


def find_junctions(
    points_a: list[tuple[float, float]],
    dist_a: list[float],
    points_b: list[tuple[float, float]],
    dist_b: list[float],
    tolerance_m: float = 30.0,
) -> JunctionResult:
    """For every point of A, find its nearest point of B within
    tolerance_m (via a grid index over B), collapse consecutive matched
    points of A into runs, then split each run wherever B's matched
    position jumps (see _split_run_by_b_continuity) before classifying
    each piece as a Junction (short contact) or an Overlap (a run at
    least OVERLAP_MIN_LEN_M long, reported as a from/to span on both
    routes).

    Matching is one-directional (A points look up B), which is adequate
    when both point lists are resampled at comparable spacing -- a
    two-sided nearest-neighbor match would double the work for the same
    result at that density.
    """
    if not points_a or not points_b:
        return {"junctions": [], "overlaps": []}

    grid = Grid(points_b, cell_size_m=max(tolerance_m, 15.0))
    matches: list[tuple[int, int, float] | None] = []
    for i, (lat, lng) in enumerate(points_a):
        best: tuple[int, float] | None = None
        for j in grid.near(lat, lng, tolerance_m):
            d = equirect_distance_m(lat, lng, points_b[j][0], points_b[j][1])
            if d <= tolerance_m and (best is None or d < best[1]):
                best = (j, d)
        matches.append((i, best[0], best[1]) if best is not None else None)

    runs: list[list[tuple[int, int, float]]] = []
    current: list[tuple[int, int, float]] = []
    for m in matches:
        if m is not None:
            current.append(m)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    max_step_m = max(
        _typical_spacing_m(dist_a), _typical_spacing_m(dist_b), 0.0
    ) * OVERLAP_MAX_STEP_MULTIPLE
    max_step_m = max(max_step_m, tolerance_m)
    runs = [sub for run in runs for sub in _split_run_by_b_continuity(run, dist_b, max_step_m)]

    junctions: list[Junction] = []
    overlaps: list[Overlap] = []
    for run in runs:
        a_start_idx = run[0][0]
        a_end_idx = run[-1][0]
        length_a = dist_a[a_end_idx] - dist_a[a_start_idx]
        b_indices = [m[1] for m in run]
        if length_a >= OVERLAP_MIN_LEN_M:
            overlaps.append(
                {
                    "a_from_m": dist_a[a_start_idx],
                    "a_to_m": dist_a[a_end_idx],
                    "b_from_m": dist_b[min(b_indices)],
                    "b_to_m": dist_b[max(b_indices)],
                }
            )
        else:
            mid = run[len(run) // 2]
            junctions.append(
                {
                    "a_dist_m": dist_a[mid[0]],
                    "b_dist_m": dist_b[mid[1]],
                    "distance_m": mid[2],
                }
            )
    return {"junctions": junctions, "overlaps": overlaps}


def classify_shape(points: list[tuple[float, float]], dist_m: list[float]) -> str:
    """'loop' | 'out_and_back' | 'point_to_point'. Checked in that order of
    specificity: an out-and-back also starts and ends at (approximately)
    the same point, so the retrace check must run before the plain
    start-meets-end loop check, not after it, or every out-and-back would
    be misread as a loop."""
    total = dist_m[-1]
    if total <= 0 or len(points) < 2:
        return "point_to_point"

    mid_idx = next((i for i, d in enumerate(dist_m) if d >= total / 2.0), len(points) // 2)
    if 1 <= mid_idx < len(points) - 1:
        first_half_pts = points[:mid_idx]
        first_half_dist = dist_m[:mid_idx]
        second_half_pts = points[mid_idx:]
        second_half_dist = dist_m[mid_idx:]
        result = find_junctions(second_half_pts, second_half_dist, first_half_pts, first_half_dist, tolerance_m=30.0)
        overlap_len = sum(o["a_to_m"] - o["a_from_m"] for o in result["overlaps"])
        second_half_len = second_half_dist[-1] - second_half_dist[0]
        if second_half_len > 0 and overlap_len >= 0.6 * second_half_len:
            return "out_and_back"

    end_gap = equirect_distance_m(points[0][0], points[0][1], points[-1][0], points[-1][1])
    loop_tolerance_m = max(60.0, total * 0.02)
    if end_gap <= loop_tolerance_m:
        return "loop"

    return "point_to_point"


def routes_near_point(
    query_lat: float,
    query_lng: float,
    radius_m: float,
    points: list[tuple[float, float]],
) -> float | None:
    """Closest approach in meters from (query_lat, query_lng) to any of
    points, or None if nothing lies within radius_m. Used both for
    routes_near (fixed point vs. many routes) and routes_near_route
    (route vs. route, called once per candidate)."""
    grid = Grid(points, cell_size_m=max(radius_m, 15.0))
    best: float | None = None
    for j in grid.near(query_lat, query_lng, radius_m):
        d = equirect_distance_m(query_lat, query_lng, points[j][0], points[j][1])
        if d <= radius_m and (best is None or d < best):
            best = d
    return best


def slice_by_distance(
    dist_m: list[float], values: list[float | None], from_m: float, to_m: float
) -> tuple[list[float], list[float | None]]:
    """Slice values (parallel to dist_m) between from_m and to_m, re-based
    so the returned distances start at 0. from_m > to_m reverses the leg
    (the returned distance series still runs 0..|to_m-from_m|, increasing).
    Endpoints are linearly interpolated when they don't land exactly on a
    sample -- important for compose_route, where leg boundaries are
    athlete-chosen distances, not sample indices.
    """
    reversed_leg = from_m > to_m
    lo, hi = (to_m, from_m) if reversed_leg else (from_m, to_m)
    lo = max(lo, dist_m[0])
    hi = min(hi, dist_m[-1])
    if hi <= lo:
        return [], []

    n = len(dist_m)
    out_dist: list[float] = []
    out_vals: list[float | None] = []

    # Left endpoint: the sample at or after lo, interpolating from the
    # preceding sample when lo doesn't land exactly on one.
    j = 0
    while j < n - 1 and dist_m[j + 1] < lo:
        j += 1
    if dist_m[j] < lo and j < n - 1:
        frac = (lo - dist_m[j]) / (dist_m[j + 1] - dist_m[j])
        out_dist.append(0.0)
        out_vals.append(_interp(values[j], values[j + 1], frac))
        k = j + 1
    else:
        out_dist.append(dist_m[j] - lo)
        out_vals.append(values[j])
        k = j + 1

    # Interior samples strictly between lo and hi.
    while k < n and dist_m[k] < hi:
        out_dist.append(dist_m[k] - lo)
        out_vals.append(values[k])
        k += 1

    # Right endpoint: exact if a sample lands on hi, else interpolated.
    if k < n and dist_m[k] == hi:
        out_dist.append(dist_m[k] - lo)
        out_vals.append(values[k])
    else:
        frac = (hi - dist_m[k - 1]) / (dist_m[k] - dist_m[k - 1])
        out_dist.append(hi - lo)
        out_vals.append(_interp(values[k - 1], values[k], frac))

    if reversed_leg:
        total = out_dist[-1]
        out_dist = [total - d for d in reversed(out_dist)]
        out_vals = list(reversed(out_vals))
    return out_dist, out_vals


def _interp(a: float | None, b: float | None, frac: float) -> float | None:
    if a is None or b is None:
        return None
    return a + (b - a) * frac
