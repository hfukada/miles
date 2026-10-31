"""Strides and hill sprints found in a run's 20 m terrain segments, for runs
whose laps don't show them. Detections are proposals: nothing here writes notes."""

import sqlite3
import statistics
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import NamedTuple
from typing_extensions import TypedDict

from . import terrain

M_PER_MILE = 1609.34
SEGMENT_M = 20.0
# A burst is a stretch of segments whose grade-adjusted speed is this multiple
# of the run's own median; strides and sprints are far above easy running, a
# tempo or progression stays below it or runs too long.
BURST_RATIO = 1.3
STRIDE_S = (10.0, 40.0)
HILL_SPRINT_S = (8.0, 25.0)
HILL_GRADE_PCT = 4.0
MIN_BURSTS = 2


class Segment(NamedTuple):
    dist_m: float
    speed_mps: float
    grade_pct: float
    heartrate: float | None


class Element(TypedDict):
    type: str
    count: int
    duration_s: int
    where: str
    source: str


def detect_elements(
    segments: Sequence[Segment],
    expected_speed: Callable[[float], float],
    exclude_m: Sequence[tuple[float, float]] = (),
) -> list[Element]:
    """Bursts of stride or hill-sprint length well above the run's median effort
    (speed divided by the model's speed for the grade), skipping any that overlap
    an `exclude_m` distance span (a workout's work laps)."""
    if len(segments) < 2:
        return []
    effort = [s.speed_mps / expected_speed(s.grade_pct) for s in segments]
    threshold = BURST_RATIO * statistics.median(effort)

    bursts: list[tuple[float, float, float, float]] = []  # start_m, end_m, seconds, mean grade
    i = 0
    while i < len(segments):
        if effort[i] < threshold:
            i += 1
            continue
        j = i
        while j < len(segments) and effort[j] >= threshold:
            j += 1
        start, end = segments[i].dist_m, segments[j - 1].dist_m + SEGMENT_M
        seconds = sum(SEGMENT_M / segments[k].speed_mps for k in range(i, j))
        grade = sum(segments[k].grade_pct for k in range(i, j)) / (j - i)
        if not any(start < hi and end > lo for lo, hi in exclude_m):
            bursts.append((start, end, seconds, grade))
        i = j

    total_mi = (segments[-1].dist_m + SEGMENT_M) / M_PER_MILE
    found: list[Element] = []
    for kind, (lo, hi), uphill in (("strides", STRIDE_S, False), ("hill_sprints", HILL_SPRINT_S, True)):
        group = [b for b in bursts if lo <= b[2] <= hi and (b[3] >= HILL_GRADE_PCT) == uphill]
        if len(group) < MIN_BURSTS:
            continue
        first_mi, last_mi = group[0][0] / M_PER_MILE, group[-1][1] / M_PER_MILE
        where = f"last {total_mi - first_mi:.1f} mi" if first_mi >= 0.6 * total_mi else f"mi {first_mi:.1f}-{last_mi:.1f}"
        found.append({
            "type": kind,
            "count": len(group),
            "duration_s": round(statistics.median(b[2] for b in group)),
            "where": where,
            "source": "stream",
        })
    return found


def load_segments(conn: sqlite3.Connection, activity_ids: Sequence[int]) -> dict[int, list[Segment]]:
    """Moving segments with a speed, per activity, in order; absent for runs with no stored stream."""
    out: dict[int, list[Segment]] = defaultdict(list)
    rows = conn.execute(
        f"""
        SELECT activity_id, dist_m, speed_cms, grade_dpct, heartrate FROM terrain_segments
        WHERE activity_id IN ({','.join('?' * len(activity_ids))}) AND speed_cms > 0 ORDER BY activity_id, idx
        """,
        list(activity_ids),
    )
    for r in rows:
        out[r["activity_id"]].append(Segment(r["dist_m"], r["speed_cms"] / 100.0, r["grade_dpct"] / 10.0, r["heartrate"]))
    return out


def work_lap_spans(conn: sqlite3.Connection, activity_ids: Sequence[int]) -> dict[int, list[tuple[float, float]]]:
    """Distance spans (m from the start) of each activity's work laps."""
    spans: dict[int, list[tuple[float, float]]] = defaultdict(list)
    covered: dict[int, float] = defaultdict(float)
    rows = conn.execute(
        f"SELECT activity_id, distance_m, lap_type FROM laps WHERE activity_id IN ({','.join('?' * len(activity_ids))}) "
        "ORDER BY activity_id, lap_index",
        list(activity_ids),
    )
    for r in rows:
        start = covered[r["activity_id"]]
        covered[r["activity_id"]] = start + (r["distance_m"] or 0.0)
        if r["lap_type"] == "work":
            spans[r["activity_id"]].append((start, covered[r["activity_id"]]))
    return spans


def detect_for_activities(conn: sqlite3.Connection, activity_ids: Sequence[int]) -> dict[int, list[Element]]:
    """Detections per activity that has stored segments (possibly none), against generic grade-speed expectations."""
    if not activity_ids:
        return {}
    expected = terrain.default_model().expected_speed_mps
    segments, spans = load_segments(conn, activity_ids), work_lap_spans(conn, activity_ids)
    return {a: detect_elements(segs, expected, spans.get(a, ())) for a, segs in segments.items()}
