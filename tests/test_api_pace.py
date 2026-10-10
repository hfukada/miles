import pytest

from miles.api import _speed_to_pace


@pytest.mark.parametrize(
    "speed_mps, pace",
    [
        # 26.8224 is the complete m/s -> min/mile conversion constant (1609.34 / 60).
        (1.0, pytest.approx(26.8224)),
        # ~3.0 m/s is roughly an 8:56/mi pace.
        (3.0, pytest.approx(8.9408, rel=1e-4)),
        # No speed, or a non-positive one, would be an infinite pace: guarded rather than raising.
        (None, None),
        (0.0, None),
        (-1.0, None),
    ],
)
def test_speed_to_pace(speed_mps, pace):
    assert _speed_to_pace(speed_mps) == pace
