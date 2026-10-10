import pytest

from miles.format import fmt_pace, fmt_time


@pytest.mark.parametrize(
    "minutes, text",
    [
        (6.56, "6:34"),
        # 10.999 minutes -> 10 min + 59.94 sec, which rounds to 60 sec and must
        # carry into the next minute rather than displaying "10:60".
        (10.999, "11:00"),
        (0.0, "0:00"),
    ],
)
def test_fmt_pace(minutes: float, text: str):
    assert fmt_pace(minutes) == text


@pytest.mark.parametrize("seconds, text", [(None, "—"), (754, "0:12:34"), (9994, "2:46:34")])
def test_fmt_time(seconds, text: str):
    assert fmt_time(seconds) == text
