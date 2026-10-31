import pytest

from miles.elements import Segment, detect_elements


def _expected(grade_pct: float) -> float:
    return max(1.0, 3.0 - 0.15 * grade_pct)


def _run(parts: list[tuple[int, float, float]]) -> list[Segment]:
    """Consecutive 20 m segments from (count, speed m/s, grade %) parts."""
    segments: list[Segment] = []
    for count, speed, grade in parts:
        for _ in range(count):
            segments.append(Segment(20.0 * len(segments), speed, grade, None))
    return segments


def _with_bursts(n: int, burst: tuple[int, float, float], gap: int = 30, lead: int = 200) -> list[Segment]:
    parts = [(lead, 3.0, 0.0)]
    for _ in range(n):
        parts += [burst, (gap, 3.0, 0.0)]
    return _run(parts)


STRIDES = _with_bursts(6, (6, 5.5, 0.0))
HILL_SPRINTS = _with_bursts(5, (4, 3.6, 6.0))


@pytest.mark.parametrize(
    "segments, exclude, expected",
    [
        (STRIDES, [], [("strides", 6, 22)]),
        (HILL_SPRINTS, [], [("hill_sprints", 5, 22)]),
        (_with_bursts(1, (6, 5.5, 0.0)), [], []),
        # the same bursts inside a workout's work laps are reps, not strides
        (STRIDES, [(3900.0, 20000.0)], []),
        # a three-minute tempo is too long for a stride, and the run's own median stays easy
        (_run([(300, 3.0, 0.0), (36, 4.2, 0.0), (300, 3.0, 0.0)]), [], []),
        # a long steady climb at a higher effort than the run's flat running
        (_run([(150, 3.0, 0.0), (200, 3.6, 5.0), (150, 3.0, 0.0)]), [], []),
        # a progression: each stretch faster than the last, never a burst against its neighbours
        (_run([(60, 3.0, 0.0), (60, 3.4, 0.0), (60, 3.8, 0.0), (60, 4.2, 0.0), (60, 4.6, 0.0)]), [], []),
        # a mild surge below the burst ratio
        (_with_bursts(5, (6, 3.6, 0.0)), [], []),
    ],
)
def test_detects_stride_and_hill_sprint_bursts_only(segments, exclude, expected):
    found = detect_elements(segments, _expected, exclude)
    assert [(e["type"], e["count"], e["duration_s"]) for e in found] == expected
    assert all(e["source"] == "stream" for e in found)


def test_where_names_a_span_or_the_distance_from_the_end():
    assert detect_elements(STRIDES, _expected)[0]["where"] == "mi 2.5-4.8"
    assert detect_elements(_with_bursts(6, (6, 5.5, 0.0), lead=400), _expected)[0]["where"] == "last 2.7 mi"
