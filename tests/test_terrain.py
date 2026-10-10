import json
import math
import sqlite3
import threading

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from helpers import activity_row, call_tool, flat_sampler, insert_activity, latitude_sampler, line_points, make_gpx
from mcp.server.fastmcp import FastMCP

from miles import api, db, routes as routes_service, streams, terrain, terrain_mcp


def _gpx_climb(n: int = 30) -> bytes:
    """~22 m per point climbing 6 m per point: a ~27% grade, well above any
    default walk-onset threshold."""
    return make_gpx(line_points(n, step_deg=0.0002, gain_per_point=6.0), name="Climb")


def _flat_gpx() -> bytes:
    return make_gpx([(40.0, -105.0, None), (40.001, -105.0, None)], name="Flat")


def _synthetic_stream_df(max_dist: float = 1000.0, step: float = 5.0) -> pd.DataFrame:
    """A run that climbs at a constant 10% grade to 500m (50m of gain, clear
    of gpx_parse's climb-segmentation floor) then descends at a constant 5%
    grade, switching from a running cadence to a walking one at the same
    point, with one stretch flagged not-moving (well clear of either grade
    change, so it tests dropping in isolation) to verify it gets dropped."""
    dists = [i * step for i in range(int(max_dist / step) + 1)]
    rows = []
    for d in dists:
        if d < 500:
            alt = d * 0.10
            cadence_single_leg = 80.0  # -> 160 spm, running
        else:
            alt = 50.0 - (d - 500) * 0.05
            cadence_single_leg = 50.0  # -> 100 spm, walking
        moving = not (240 <= d <= 260)
        rows.append({
            "time_s": d / 2.5,
            "distance_m": d,
            "altitude_m": alt,
            "velocity_smooth_mps": 2.5,
            "heartrate": 150.0,
            "cadence": cadence_single_leg,
            "grade_smooth": 0.0,
            "moving": moving,
            "lat": 40.0,
            "lng": -105.0,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# TerrainModel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grades, expected",
    [
        ([0.0] * 50, {"run_pct": 100.0, "hike_pct": 0.0, "longest_hike_m": 0.0}),
        (lambda m: [m.walk_onset_grade_pct + 5.0] * 10 + [0.0] * 10, {"hike_pct": 50.0, "longest_hike_m": 200.0}),
        (lambda m: [m.descent_comfort_grade_pct - 5.0] * 5, {"steep_descent_m": 100.0, "hike_pct": 0.0}),
    ],
    ids=["all flat", "hiking then flat", "steep descent, tracked apart from hiking"],
)
def test_score_profile(grades, expected):
    model = terrain.default_model()
    result = model.score_profile(grades(model) if callable(grades) else grades, segment_m=20.0)
    assert {k: result[k] for k in expected} == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def test_segment_and_store_activity_drops_non_moving_and_classifies_walking(conn: sqlite3.Connection):
    n = terrain.segment_and_store_activity(conn, 1, _synthetic_stream_df(), "2024-06-01", 15.0, walk_threshold_spm=130.0)
    assert n > 0

    rows = conn.execute("SELECT * FROM terrain_segments WHERE activity_id = 1 ORDER BY dist_m").fetchall()
    assert len(rows) == n
    # The intervals starting at 240m/260m sit inside the not-moving stretch
    # and must not appear at all.
    assert all(r["dist_m"] not in (240.0, 260.0) for r in rows)

    first, last = rows[0], rows[-1]
    assert first["dist_m"] < 500
    assert first["grade_dpct"] > 0
    assert bool(first["is_walking"]) is False

    assert last["dist_m"] > 500
    assert last["grade_dpct"] < 0
    assert bool(last["is_walking"]) is True

    assert all(r["ele_source"] == terrain.ELE_SOURCE_RECORDED for r in rows)
    assert all(r["temp_dc"] == 150 for r in rows)


def test_segmenting_without_elevation_stores_nothing_and_a_rerun_replaces_rather_than_appends(conn: sqlite3.Connection):
    no_elevation = pd.DataFrame({
        "time_s": [0, 1, 2], "distance_m": [0.0, 5.0, 10.0], "altitude_m": [None, None, None],
        "velocity_smooth_mps": [2.5, 2.5, 2.5], "heartrate": [140, 140, 140], "cadence": [80, 80, 80],
        "grade_smooth": [0, 0, 0], "moving": [True, True, True], "lat": [40.0] * 3, "lng": [-105.0] * 3,
    })
    assert terrain.segment_and_store_activity(conn, 2, no_elevation, "2024-06-01", None, 130.0) == 0
    assert conn.execute("SELECT COUNT(*) FROM terrain_segments WHERE activity_id = 2").fetchone()[0] == 0

    df = _synthetic_stream_df()
    n1 = terrain.segment_and_store_activity(conn, 1, df, "2024-06-01", None, 130.0)
    n2 = terrain.segment_and_store_activity(conn, 1, df, "2024-06-01", None, 130.0)
    assert n1 == n2
    assert conn.execute("SELECT COUNT(*) FROM terrain_segments WHERE activity_id = 1").fetchone()[0] == n2


# ---------------------------------------------------------------------------
# DEM draping
# ---------------------------------------------------------------------------


def _track_stream_df(n: int = 50, step: float = 20.0, lat_step: float = 0.0002, grade: float = 0.05) -> pd.DataFrame:
    """A straight, steadily-climbing track (recorded altitude) with real
    lat/lng movement, so a fake DEM sampler keyed on latitude has something
    to disagree with."""
    rows = []
    for i in range(n):
        d = i * step
        rows.append({
            "time_s": d / 2.5, "distance_m": d, "altitude_m": d * grade,
            "velocity_smooth_mps": 2.5, "heartrate": 150.0, "cadence": 80.0,
            "grade_smooth": 0.0, "moving": True,
            "lat": 40.0 + i * lat_step, "lng": -105.0,
        })
    return pd.DataFrame(rows)


def _sources(conn: sqlite3.Connection) -> list[str]:
    return [r["ele_source"] for r in conn.execute("SELECT ele_source FROM terrain_segments WHERE activity_id = 1 ORDER BY dist_m")]


def test_dem_draping_per_segment_falls_back_to_recorded_altitude(conn: sqlite3.Connection):
    terrain.segment_and_store_activity(conn, 1, _track_stream_df(), "2024-06-01", None, 130.0, dem_sampler=None)
    assert set(_sources(conn)) == {terrain.ELE_SOURCE_RECORDED}

    full = latitude_sampler(base=200.0)
    terrain.segment_and_store_activity(conn, 1, _track_stream_df(), "2024-06-01", None, 130.0, dem_sampler=full)
    assert set(_sources(conn)) == {terrain.ELE_SOURCE_DEM}
    assert len(full.calls) == 1  # one drape call for the whole activity, not per point

    gap = latitude_sampler(base=200.0, no_coverage=frozenset(range(20, 30)))
    terrain.segment_and_store_activity(conn, 1, _track_stream_df(), "2024-06-01", None, 130.0, dem_sampler=gap)
    assert set(_sources(conn)) == {terrain.ELE_SOURCE_DEM, terrain.ELE_SOURCE_RECORDED}


def test_a_missing_gps_fix_is_never_sampled_as_null_island(conn: sqlite3.Connection):
    all_missing = _synthetic_stream_df()
    all_missing["lat"] = float("nan")
    all_missing["lng"] = float("nan")
    sampler = flat_sampler()
    terrain.segment_and_store_activity(conn, 1, all_missing, "2024-06-01", None, 130.0, dem_sampler=sampler)
    assert sampler.points == []
    assert set(_sources(conn)) == {terrain.ELE_SOURCE_RECORDED}

    partly_missing = _synthetic_stream_df()
    partly_missing.loc[partly_missing["distance_m"] < 200, ["lat", "lng"]] = float("nan")
    sampler = flat_sampler()
    terrain.segment_and_store_activity(conn, 1, partly_missing, "2024-06-01", None, 130.0, dem_sampler=sampler)
    assert sampler.points
    assert all(not math.isnan(lat) and not math.isnan(lng) for lat, lng in sampler.points)


# ---------------------------------------------------------------------------
# current_model
# ---------------------------------------------------------------------------


def test_current_model_falls_back_to_defaults_with_no_segments_or_only_old_ones(conn: sqlite3.Connection):
    model = terrain.current_model(conn)
    assert model.confidence == "low"
    assert all(b.sample_count == 0 for b in model.bins)

    terrain.segment_and_store_activity(conn, 1, _synthetic_stream_df(), "2020-01-01", 15.0, 130.0)
    assert all(b.sample_count == 0 for b in terrain.current_model(conn, window_days=30).bins)


def test_current_model_uses_real_bins_when_samples_meet_threshold(conn: sqlite3.Connection):
    terrain.segment_and_store_activity(conn, 1, _synthetic_stream_df(), "2024-06-01", 15.0, 130.0)
    model = terrain.current_model(conn, window_days=3650)
    assert model.confidence == "low"  # only ~19 samples total, well under the "high"/"medium" floors

    real_bins = [b for b in model.bins if b.sample_count > 0]
    assert len(real_bins) >= 2
    uphill_bin = next(b for b in real_bins if b.lo_pct >= 0)
    assert uphill_bin.median_speed_mps == pytest.approx(2.5, abs=0.1)
    assert uphill_bin.walk_fraction == pytest.approx(0.0)

    downhill_bin = next(b for b in real_bins if b.hi_pct <= 0)
    assert downhill_bin.walk_fraction == pytest.approx(1.0)

    # Only the uphill bin has real data and its walk_fraction never reaches
    # 0.5, so onset stays the generic default rather than a spurious value.
    assert model.walk_onset_grade_pct == terrain._DEFAULT_WALK_ONSET_PCT
    # descent comfort is derived from the one real downhill bin.
    assert model.descent_comfort_grade_pct == pytest.approx(downhill_bin.lo_pct)


# ---------------------------------------------------------------------------
# Snapshots / trend / schema / rebuild version gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("metric", ["median_speed_mps", "not_a_real_metric"])
def test_a_trend_needs_a_known_metric_and_a_grade_range_for_bin_metrics(conn: sqlite3.Connection, metric: str):
    with pytest.raises(ValueError):
        terrain.get_terrain_trend(conn, metric)


def test_ensure_terrain_derived_stamps_the_current_version(conn: sqlite3.Connection):
    terrain.ensure_terrain_derived(conn)
    row = conn.execute("SELECT value FROM meta WHERE key = 'terrain_model_version'").fetchone()
    assert row["value"] == terrain.model_version_tag()


# ---------------------------------------------------------------------------
# score_route / route_point_classes
# ---------------------------------------------------------------------------


def test_score_route_without_elevation_reports_unavailable(conn: sqlite3.Connection):
    route_id = routes_service.upload_gpx(conn, _flat_gpx(), "flat.gpx")[0]
    result = terrain.score_route(conn, route_id)
    assert result["has_elevation"] is False
    assert result["runnability"] is None

    points = terrain.route_point_classes(conn, route_id)
    assert points
    assert all(p["cls"] is None and p["grade_pct"] is None for p in points)


def test_a_steep_climb_scores_mostly_hike_and_classifies_its_middle_as_hike(conn: sqlite3.Connection):
    route_id = routes_service.upload_gpx(conn, _gpx_climb(), "climb.gpx")[0]

    result = terrain.score_route(conn, route_id)
    assert result["has_elevation"] is True
    assert result["runnability"]["hike_pct"] > 50.0
    assert result["gap_analysis"] is not None

    points = terrain.route_point_classes(conn, route_id)
    assert len(points) == len(routes_service.get_route_profile(conn, route_id))
    # Smoothing softens the grade at each end, so only the sustained middle
    # is guaranteed to read as steep enough to hike; the last point repeats
    # the prior segment's class instead of being left unclassified.
    assert all(p["cls"] in ("grind", "hike") for p in points)
    assert points[len(points) // 2]["cls"] == "hike"


@pytest.mark.parametrize("function", [terrain.score_route, terrain.route_point_classes])
def test_an_unknown_route_raises(conn: sqlite3.Connection, function):
    with pytest.raises(routes_service.RouteNotFoundError):
        function(conn, 999999)


# ---------------------------------------------------------------------------
# analyze_activity_terrain
# ---------------------------------------------------------------------------


def test_analyze_activity_terrain_requires_cached_streams(conn: sqlite3.Connection, monkeypatch):
    monkeypatch.setattr(streams, "has_cached_streams", lambda activity_id: False)
    with pytest.raises(terrain.TerrainDataError):
        terrain.analyze_activity_terrain(conn, 1)


def test_analyze_activity_terrain_reports_climbs(conn: sqlite3.Connection, monkeypatch):
    df = _synthetic_stream_df()
    monkeypatch.setattr(streams, "has_cached_streams", lambda activity_id: True)
    monkeypatch.setattr(streams, "fetch_and_cache_streams", lambda activity_id: df)

    analysis = terrain.analyze_activity_terrain(conn, 1)

    assert analysis["activity_id"] == 1
    climb = analysis["climbs"][0]
    assert climb["kind"] == "climb"
    assert climb["expected_speed_mps"] > 0


# ---------------------------------------------------------------------------
# rebuild_terrain
# ---------------------------------------------------------------------------


def _segmented(conn: sqlite3.Connection) -> set[int]:
    return {r["activity_id"] for r in conn.execute("SELECT DISTINCT activity_id FROM terrain_segments")}


def test_virtual_runs_are_not_segmented(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    insert_activity(conn, 1, sport_type="TrailRun")
    insert_activity(conn, 2, sport_type="VirtualRun")
    monkeypatch.setattr(streams, "has_cached_streams", lambda aid: True)
    monkeypatch.setattr(streams, "get_stream_dataframe", lambda aid: _synthetic_stream_df())

    terrain.rebuild_terrain(conn, full=True)

    assert _segmented(conn) == {1}


def test_one_unreadable_stream_file_does_not_fail_the_rebuild_and_is_discarded_for_a_refetch(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    insert_activity(conn, 1)
    insert_activity(conn, 2)
    monkeypatch.setattr(streams, "has_cached_streams", lambda aid: True)
    discarded: list[int] = []
    monkeypatch.setattr(streams, "discard_raw_streams", discarded.append)

    def fetch(aid: int):
        if aid == 1:
            raise OSError("stream file is truncated")
        return _synthetic_stream_df()

    monkeypatch.setattr(streams, "get_stream_dataframe", fetch)

    terrain.rebuild_terrain(conn, full=True)

    assert _segmented(conn) == {2}
    assert discarded == [1]
    failure = conn.execute("SELECT status_code, error, model_version FROM terrain_backfill_failures WHERE activity_id = 1").fetchone()
    assert failure is not None and failure["status_code"] is None and failure["model_version"] is None
    assert "truncated" in failure["error"]
    assert terrain.segmentation_failed_ids(conn) == set()
    assert terrain.model_is_current(conn)


def test_a_segmentation_failure_keeps_the_file_and_is_not_retried_until_the_model_version_changes(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
):
    insert_activity(conn, 1)
    monkeypatch.setattr(streams, "has_cached_streams", lambda aid: True)
    monkeypatch.setattr(streams, "get_stream_dataframe", lambda aid: _synthetic_stream_df())
    discarded: list[int] = []
    monkeypatch.setattr(streams, "discard_raw_streams", discarded.append)
    attempts: list[int] = []

    def failing(conn, activity_id, *args, **kwargs):
        attempts.append(activity_id)
        raise ValueError("bad segment")

    monkeypatch.setattr(terrain, "segment_and_store_activity", failing)

    terrain.rebuild_terrain(conn, full=True)
    terrain.rebuild_terrain(conn, full=True)
    terrain.rebuild_terrain(conn)
    assert attempts == [1]
    assert discarded == []
    assert terrain.segmentation_failed_ids(conn) == {1}
    assert terrain.activities_to_segment(conn) == []

    monkeypatch.setattr(terrain, "TERRAIN_MODEL_VERSION", terrain.TERRAIN_MODEL_VERSION + 1)
    terrain.rebuild_terrain(conn)
    assert attempts == [1, 1]


def test_rebuild_drops_segments_of_activities_that_no_longer_qualify(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    insert_activity(conn, 2, sport_type="VirtualRun")
    terrain.segment_and_store_activity(conn, 2, _synthetic_stream_df(), "2024-06-01", 15.0, 130.0)
    monkeypatch.setattr(streams, "has_cached_streams", lambda aid: False)

    terrain.rebuild_terrain(conn, full=True)

    assert conn.execute("SELECT COUNT(*) FROM terrain_segments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM terrain_segment_dates").fetchone()[0] == 0


def _snapshot(conn: sqlite3.Connection, as_of: str, window_days: int, bins: list[dict]) -> None:
    conn.execute(
        "INSERT INTO terrain_snapshots (as_of, window_days, walk_onset_grade_pct, descent_comfort_grade_pct, "
        "confidence, bins_json, created_at) VALUES (?, ?, 15, -12, 'low', ?, 'now')",
        [as_of, window_days, json.dumps(bins)],
    )
    conn.commit()


def _bin(lo: float, hi: float, speed: float, samples: int) -> dict:
    return {
        "lo_pct": lo, "hi_pct": hi, "median_speed_mps": speed, "efficiency_median": None,
        "walk_fraction": None, "sample_count": samples,
    }


def test_trend_returns_one_point_per_date_for_the_current_window_and_ignores_bins_with_too_few_samples(conn: sqlite3.Connection):
    _snapshot(conn, "2024-06-01", 90, [_bin(0, 2, 3.0, 50), _bin(2, 4, 9.0, 1)])
    _snapshot(conn, "2024-06-01", 30, [_bin(0, 2, 5.0, 50)])
    _snapshot(conn, "2024-07-01", 90, [_bin(0, 2, 3.0, terrain.MIN_SAMPLES_FOR_BIN - 1)])

    trend = terrain.get_terrain_trend(conn, "median_speed_mps", grade_range=(0.0, 4.0))
    assert trend == [{"as_of": "2024-06-01", "value": 3.0}, {"as_of": "2024-07-01", "value": None}]
    assert terrain.get_terrain_trend(conn, "median_speed_mps", grade_range=(0.0, 4.0), window_days=30)[0]["value"] == 5.0


# ---------------------------------------------------------------------------
# connect: background rebuilds
# ---------------------------------------------------------------------------


def test_connect_during_a_stale_model_starts_one_background_rebuild_and_reports_rebuilding(monkeypatch):
    seed = db.connect()
    insert_activity(seed, 1)
    seed.close()
    release = threading.Event()
    started = threading.Event()
    rebuilds: list[str] = []

    def slow_rebuild(c, *, full=False):
        rebuilds.append("rebuild")
        started.set()
        release.wait(timeout=10)
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('terrain_model_version', ?)", [terrain.model_version_tag()])
        c.commit()
        return {"activities_segmented": 0, "snapshots": 0}

    monkeypatch.setattr(terrain, "rebuild_terrain", slow_rebuild)
    monkeypatch.setattr(terrain, "_rebuild_thread", None)

    with pytest.raises(terrain.TerrainRebuildingError):
        terrain.connect()
    assert started.wait(timeout=5)
    with pytest.raises(terrain.TerrainRebuildingError):
        terrain.connect()
    assert rebuilds == ["rebuild"]  # the second request did not start another

    release.set()
    terrain._rebuild_thread.join(timeout=10)
    terrain.connect().close()  # current now


def _raise_rebuilding():
    raise terrain.TerrainRebuildingError("being rebuilt")


def test_terrain_tools_and_endpoints_report_rebuilding_instead_of_failing(monkeypatch):
    monkeypatch.setattr(terrain, "connect", _raise_rebuilding)
    server = FastMCP("t")
    terrain_mcp.register_terrain_tools(server)

    assert call_tool(server, "get_terrain_profile")["status"] == "rebuilding"
    response = TestClient(api.app).get("/api/terrain/profile")
    assert response.status_code == 503
    assert response.headers["retry-after"]


def test_a_rebuild_commits_as_it_goes_instead_of_holding_one_transaction(monkeypatch):
    conn = db.connect()
    for activity_id in (1, 2, 3):
        db.upsert_activities(conn, [activity_row(activity_id, f"2026-01-0{activity_id}T08:00:00")])
    observer = db.connect()
    visible_to_others: list[int] = []

    def segment(conn_, activity_id, *args, **kwargs):
        visible_to_others.append(observer.execute("SELECT COUNT(*) FROM terrain_segment_dates").fetchone()[0])
        conn_.execute(
            "INSERT INTO terrain_segment_dates (activity_id, start_date, segment_count, needs_dem) VALUES (?, '2026-01-01', 0, 0)",
            (activity_id,),
        )
        return 0

    monkeypatch.setattr(terrain, "segment_and_store_activity", segment)
    monkeypatch.setattr(streams, "has_cached_streams", lambda aid: True)
    monkeypatch.setattr(streams, "get_stream_dataframe", lambda aid: pd.DataFrame())
    monkeypatch.setattr(terrain, "activity_temp_c", lambda *a: None)
    monkeypatch.setattr(terrain, "backfill_snapshots", lambda *a, **k: 0)

    terrain.rebuild_terrain(conn, full=True)

    assert visible_to_others == [0, 1, 2]


def test_a_request_during_a_rebuild_is_turned_away_not_queued(conn: sqlite3.Connection):
    assert terrain._rebuild_lock.acquire(blocking=False)
    try:
        with pytest.raises(terrain.TerrainRebuildingError):
            terrain.ensure_terrain_derived(conn)
    finally:
        terrain._rebuild_lock.release()
