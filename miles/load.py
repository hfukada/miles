"""
Running load and injury-risk signals. Daily load is Edwards TRIMP (minutes in
each 10%-of-max-HR band x 1..5), from the same per-run HR time as intensity.py;
a run with no HR is charged the window's median TRIMP per minute. From the
daily series:
- acute:chronic workload ratio: last 7 days vs the 28-day weekly average
  (rolling, coupled);
- monotony (7-day mean / standard deviation of daily load) and strain
  (7-day load x monotony), Foster.
Runs only, matching the rest of the app. Computed on request; nothing is stored.
"""

import sqlite3
import statistics
from datetime import date, timedelta
from typing import Literal

from typing_extensions import TypedDict

from . import db
from .intensity import hr_pct_seconds, runs_between
from .races import run_sport_filter

AcwrBand = Literal["low", "optimal", "caution", "high"]

ACWR_LOW_MAX = 0.8
ACWR_OPTIMAL_MAX = 1.3
ACWR_CAUTION_MAX = 1.5
MONOTONY_HIGH = 2.0

ACUTE_DAYS = 7
CHRONIC_DAYS = 28


class DayLoad(TypedDict):
    date: str
    load: float
    acute: float
    chronic_weekly: float | None
    acwr: float | None
    acwr_band: AcwrBand | None
    monotony: float | None
    strain: float | None


class LoadResponse(TypedDict):
    max_hr: int | None
    days: list[DayLoad]
    current: DayLoad | None
    estimated_runs: int  # runs (incl. the 28-day lead-in) charged the median rate (no HR)
    note: str | None


def edwards_weight(pct_max: int) -> int:
    if pct_max < 50:
        return 0
    return min(5, pct_max // 10 - 4)


def acwr_band(ratio: float) -> AcwrBand:
    if ratio < ACWR_LOW_MAX:
        return "low"
    if ratio <= ACWR_OPTIMAL_MAX:
        return "optimal"
    if ratio <= ACWR_CAUTION_MAX:
        return "caution"
    return "high"


def daily_loads(conn: sqlite3.Connection, start: date, end: date, max_hr: int) -> tuple[dict[str, float], int]:
    """TRIMP per calendar day on start..end (days without runs absent), and
    how many runs were charged the median rate for lack of HR."""
    runs = runs_between(conn, start, end)
    pcts = hr_pct_seconds(conn, runs, max_hr)
    per_run: dict[int, float] = {
        aid: sum(secs / 60.0 * edwards_weight(pct) for pct, secs in by_pct.items())
        for aid, by_pct in pcts.items()
    }
    rates = [per_run[r["activity_id"]] / (r["moving_time_s"] / 60.0) for r in runs if r["activity_id"] in per_run]
    median_rate = statistics.median(rates) if rates else 0.0

    out: dict[str, float] = {}
    estimated = 0
    for r in runs:
        load = per_run.get(r["activity_id"])
        if load is None:
            load = median_rate * r["moving_time_s"] / 60.0
            estimated += 1
        out[r["date"]] = out.get(r["date"], 0.0) + load
    return out, estimated


def _first_run_date(conn: sqlite3.Connection) -> date | None:
    clause, params = run_sport_filter()
    row = conn.execute(f"SELECT MIN(DATE(start_date)) AS d FROM activities WHERE {clause}", params).fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


def load_series(conn: sqlite3.Connection, weeks: int, today: date | None = None) -> LoadResponse:
    """Daily load, ACWR and monotony/strain for the last `weeks` x 7 days
    ending today. Ratios are null until 28 days of history precede a day."""
    today = today or date.today()
    athlete = db.get_athlete(conn)
    max_hr = athlete["max_hr"] if athlete else None
    if not max_hr:
        return LoadResponse(
            max_hr=None, days=[], current=None, estimated_runs=0,
            note="No max HR configured; TRIMP load needs it.",
        )

    n_days = weeks * 7
    first_day = today - timedelta(days=n_days - 1)
    lead_start = first_day - timedelta(days=CHRONIC_DAYS - 1)
    loads, estimated = daily_loads(conn, lead_start, today, max_hr)
    first_run = _first_run_date(conn)

    span = [lead_start + timedelta(days=i) for i in range((today - lead_start).days + 1)]
    series = [loads.get(d.isoformat(), 0.0) for d in span]

    days: list[DayLoad] = []
    for i in range(CHRONIC_DAYS - 1, len(span)):
        d = span[i]
        acute_window = series[i - ACUTE_DAYS + 1:i + 1]
        acute = sum(acute_window)
        has_history = first_run is not None and d - timedelta(days=CHRONIC_DAYS - 1) >= first_run
        chronic_weekly = sum(series[i - CHRONIC_DAYS + 1:i + 1]) / (CHRONIC_DAYS / ACUTE_DAYS) if has_history else None
        ratio = acute / chronic_weekly if chronic_weekly else None
        sd = statistics.pstdev(acute_window)
        monotony = (acute / ACUTE_DAYS) / sd if sd > 0 else None
        days.append(DayLoad(
            date=d.isoformat(),
            load=round(series[i], 1),
            acute=round(acute, 1),
            chronic_weekly=round(chronic_weekly, 1) if chronic_weekly is not None else None,
            acwr=round(ratio, 2) if ratio is not None else None,
            acwr_band=acwr_band(ratio) if ratio is not None else None,
            monotony=round(monotony, 2) if monotony is not None else None,
            strain=round(acute * monotony) if monotony is not None else None,
        ))

    return LoadResponse(
        max_hr=max_hr,
        days=days,
        current=days[-1] if days else None,
        estimated_runs=estimated,
        note=None,
    )
