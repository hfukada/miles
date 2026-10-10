"""Distance-specific build analysis: /api/marathons and /api/marathon-weeks
(see api.py) generalized to 5K, 10K, half, marathon, 50K, and everything else.
"""

from datetime import date, timedelta
from typing import Literal, cast

from typing_extensions import TypedDict

from fastapi import APIRouter

from .build_stats import BuildStat, WeekPoint, build_stat, week_offset_points
from .derive import connect_derived as _conn
from .periods import monday_of
from .races import race_rows

router = APIRouter()

Bucket = Literal["5K", "10K", "Half", "Marathon", "50K", "Other"]

# Build-window length in weeks per bucket.
_BUILD_WEEKS: dict[Bucket, int] = {
    "5K": 8, "10K": 8, "Half": 10, "Marathon": 12, "50K": 12, "Other": 8,
}
# races.py's classify_race_distance() categories for each bucket (lowercase
# "half"/"marathon"; "Other" covers 15K/10M/30K/unmatched distances, which
# race_rows() lumps together as "other").
_DISTANCE_CATEGORY: dict[Bucket, str] = {
    "5K": "5K", "10K": "10K", "Half": "half",
    "Marathon": "marathon", "50K": "50K", "Other": "other",
}


def _build_start(race_date: str, build_weeks: int) -> date:
    """Monday-aligned build start, so a build spans whole weeks."""
    return monday_of(date.fromisoformat(race_date)) - timedelta(weeks=build_weeks)


class DistanceBuildRow(TypedDict):
    name: str | None
    date: str
    finish_time_s: int | None
    finish_time: str
    distance_miles: float | None
    pace_min_per_mile: float | None
    # 'raced'/'hard'/'casual'. Lets callers pick the fastest raced result
    # rather than the fastest time on record.
    effort: str | None
    build: BuildStat
    build_weeks: int


@router.get("/api/distance-builds")
def get_distance_builds(bucket: Bucket) -> list[DistanceBuildRow]:
    """
    All races at the given distance bucket with stats for the bucket's build
    window preceding each one, sorted by date ascending.
    """
    build_weeks = _BUILD_WEEKS[bucket]
    conn = _conn()

    builds: list[DistanceBuildRow] = []
    for race in race_rows(conn, distance_category=_DISTANCE_CATEGORY[bucket]):
        race_date = cast(str, race["date"])
        builds.append(DistanceBuildRow(
            name=cast("str | None", race["name"]),
            date=race_date,
            finish_time_s=cast("int | None", race["finish_time_s"]),
            finish_time=cast(str, race["finish_time"]),
            distance_miles=cast("float | None", race["distance_miles"]),
            pace_min_per_mile=cast("float | None", race["pace_min_per_mile"]),
            effort=cast("str | None", race["effort"]),
            build=build_stat(conn, _build_start(race_date, build_weeks).isoformat(), race_date, build_weeks),
            build_weeks=build_weeks,
        ))
    return builds


class DistanceBuildWeeks(TypedDict):
    name: str | None
    date: str
    finish_time_s: int | None
    finish_time: str
    weeks: list[WeekPoint]
    build_weeks: int


@router.get("/api/distance-build-weeks")
def get_distance_build_weeks(bucket: Bucket) -> list[DistanceBuildWeeks]:
    """
    Weekly mileage for each build at the given distance bucket, with each
    week expressed as an offset from race week (0 = race week).
    """
    build_weeks = _BUILD_WEEKS[bucket]
    conn = _conn()

    builds: list[DistanceBuildWeeks] = []
    for race in race_rows(conn, distance_category=_DISTANCE_CATEGORY[bucket]):
        race_date = cast(str, race["date"])
        builds.append(DistanceBuildWeeks(
            name=cast("str | None", race["name"]),
            date=race_date,
            finish_time_s=cast("int | None", race["finish_time_s"]),
            finish_time=cast(str, race["finish_time"]),
            weeks=week_offset_points(conn, _build_start(race_date, build_weeks).isoformat(), race_date, build_weeks),
            build_weeks=build_weeks,
        ))
    return builds
