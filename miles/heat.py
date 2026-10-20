"""
Heat adjustment for run paces: the runner's temperature + dew point rule (sum
of both in °F maps to a percent slowdown), from the run's synced weather.
Cold is never adjusted; a run with no weather row stays raw.
"""

import math
import sqlite3

# (temp °F + dew point °F, % slowdown) knots, interpolated linearly between and
# clamped at both ends. The rule stops at 180 ("hard running not advised").
_SLOWDOWN_KNOTS: list[tuple[float, float]] = [
    (100.0, 0.0),
    (110.0, 0.5),
    (120.0, 1.0),
    (130.0, 2.0),
    (140.0, 3.0),
    (150.0, 4.5),
    (160.0, 6.0),
    (170.0, 8.0),
    (180.0, 10.0),
]

# Magnus coefficients for dew point over water.
_MAGNUS_A = 17.62
_MAGNUS_B = 243.12


def c_to_f(temp_c: float) -> float:
    return temp_c * 9.0 / 5.0 + 32.0


def dew_point_c(temp_c: float, humidity_pct: float) -> float:
    """Dew point (°C) from air temperature and relative humidity (Magnus)."""
    rh = min(max(humidity_pct, 1.0), 100.0)
    gamma = math.log(rh / 100.0) + _MAGNUS_A * temp_c / (_MAGNUS_B + temp_c)
    return _MAGNUS_B * gamma / (_MAGNUS_A - gamma)


def slowdown_pct(temp_c: float, humidity_pct: float) -> float:
    """Expected percent slowdown versus cool conditions."""
    combined_f = c_to_f(temp_c) + c_to_f(dew_point_c(temp_c, humidity_pct))
    lo_x, lo_y = _SLOWDOWN_KNOTS[0]
    if combined_f <= lo_x:
        return lo_y
    for hi_x, hi_y in _SLOWDOWN_KNOTS[1:]:
        if combined_f <= hi_x:
            return lo_y + (hi_y - lo_y) * (combined_f - lo_x) / (hi_x - lo_x)
        lo_x, lo_y = hi_x, hi_y
    return _SLOWDOWN_KNOTS[-1][1]


def heat_factor(temp_c: float | None, humidity_pct: float | None) -> float:
    """Multiplier from a hot-weather pace to its cool-weather equivalent:
    adjusted_time = time / factor, adjusted_speed = speed * factor. 1.0 when
    weather is missing."""
    if temp_c is None or humidity_pct is None:
        return 1.0
    return 1.0 + slowdown_pct(temp_c, humidity_pct) / 100.0


def activity_heat_factors(conn: sqlite3.Connection, activity_ids: list[int]) -> dict[int, float]:
    """heat_factor for each activity that has a weather row; absent ids have
    no weather (callers treat them as 1.0)."""
    out: dict[int, float] = {}
    for i in range(0, len(activity_ids), 500):
        chunk = activity_ids[i:i + 500]
        rows = conn.execute(f"""
            SELECT activity_id, temp_c_avg, humidity_avg FROM weather
            WHERE activity_id IN ({','.join('?' * len(chunk))})
              AND temp_c_avg IS NOT NULL AND humidity_avg IS NOT NULL
        """, chunk).fetchall()
        for r in rows:
            out[int(r["activity_id"])] = heat_factor(float(r["temp_c_avg"]), float(r["humidity_avg"]))
    return out
