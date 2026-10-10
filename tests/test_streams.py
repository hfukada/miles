import gzip
import json

import pandas as pd
import pytest
from stravalib.model import Stream

from miles import streams
from miles.streams import (
    _build_dataframe,
    cache_streams,
    has_cached_streams,
    compute_gap_pace_s_per_m,
    fetch_and_cache_streams,
    get_stream_dataframe,
    load_raw_streams,
    median_per_window,
    min_median_max_per_window,
    minetti_cost_ratio,
)


def _stream(data: list, stream_type: str = "time", series_type: str = "time") -> Stream:
    # cache_streams takes the real stravalib model -- it's
    # what strava_client hands them.
    return Stream(type=stream_type, data=data, series_type=series_type, original_size=len(data), resolution="high")


def _entry(data: list, stream_type: str = "time", series_type: str = "time") -> dict:
    # _build_dataframe operates on the plain-dict shape a raw file actually
    # stores and loads -- what cache_streams would have written for the
    # same stream.
    return {"type": stream_type, "data": data, "series_type": series_type, "original_size": len(data), "resolution": "high"}


def test_gap_pace_equals_real_pace_on_the_flat_and_is_faster_uphill():
    assert minetti_cost_ratio(0.0) == pytest.approx(1.0)
    assert minetti_cost_ratio(10.0) > 1.0 > minetti_cost_ratio(-10.0) > 0  # uphill costs more than flat, downhill less but never nothing
    assert minetti_cost_ratio(45.0) == pytest.approx(minetti_cost_ratio(1000.0))  # steeper than +/-45% clips instead of extrapolating

    flat = pd.DataFrame({"velocity_smooth_mps": [3.0] * 10, "grade_smooth": [0.0] * 10})
    assert compute_gap_pace_s_per_m(flat).iloc[5] == pytest.approx(1.0 / 3.0, rel=1e-6)

    # Same speed while climbing means more effort than flat ground needs, so
    # GAP reads as a faster flat-equivalent pace (fewer seconds per meter),
    # the convention Strava's own grade-adjusted pace follows.
    uphill = pd.DataFrame({"velocity_smooth_mps": [3.0] * 10, "grade_smooth": [10.0] * 10})  # percent, as Strava reports it
    assert compute_gap_pace_s_per_m(uphill).iloc[5] < 1.0 / 3.0


def test_compute_gap_pace_zero_velocity_does_not_divide_by_zero():
    df = pd.DataFrame({
        "velocity_smooth_mps": [0.0, 3.0, 0.0],
        "grade_smooth": [0.0, 0.0, 0.0],
    })
    gap = compute_gap_pace_s_per_m(df)
    assert pd.isna(gap.iloc[0])
    assert pd.isna(gap.iloc[2])
    assert not pd.isna(gap.iloc[1])


def test_median_per_window_buckets_samples_and_drops_nan_before_the_median():
    df = pd.DataFrame({"time_s": [0, 1, 2, 3, 4, 5], "heartrate": [100, 110, 120, 140, 150, 160]})
    # windows: [0,2) -> {100,110}, [2,4) -> {120,140}, [4,6) -> {150,160}
    assert median_per_window(df, [0, 2, 4, 6], "heartrate") == [pytest.approx(105), pytest.approx(130), pytest.approx(155)]

    with_gap = pd.DataFrame({"time_s": [0, 1, 2], "heartrate": [100, None, 120]})
    assert median_per_window(with_gap, [0, 3], "heartrate") == [pytest.approx(110)]


def test_windows_without_samples_are_none_and_a_missing_column_or_boundary_gives_nothing():
    df = pd.DataFrame({"time_s": [0, 10], "heartrate": [100, 200]})
    assert median_per_window(df, [0, 1, 2, 11], "heartrate")[1] is None  # window [1,2) holds no samples
    assert min_median_max_per_window(df, [0, 1, 2, 11], "heartrate")[1] == {"min": None, "median": None, "max": None}

    assert median_per_window(df, [0, 1, 2], "no_such_column") == []
    assert median_per_window(df, [0], "heartrate") == []

    speeds = pd.DataFrame({"time_s": [0, 1, 2, 3], "velocity_smooth_mps": [0.01, 4.0, 3.0, 5.0]})
    summary = min_median_max_per_window(speeds, [0, 4], "velocity_smooth_mps", min_value=1.0)[0]  # a paused watch's near-zero sample is excluded
    assert (summary["min"], summary["median"], summary["max"]) == (pytest.approx(3.0), pytest.approx(4.0), pytest.approx(5.0))
    assert min_median_max_per_window(speeds, [0, 4], "velocity_smooth_mps")[0]["min"] == pytest.approx(0.01)
    assert min_median_max_per_window(speeds, [0, 4], "velocity_smooth_mps", min_value=10.0)[0] == {"min": None, "median": None, "max": None}


# ---------------------------------------------------------------------------
# _build_dataframe (raw dict -> derived DataFrame)
# ---------------------------------------------------------------------------


def test_build_dataframe_maps_streams_to_columns():
    raw = {
        "time": _entry([0, 1, 2]),
        "heartrate": _entry([100, 110, 120], "heartrate"),
        "velocity_smooth": _entry([3.0, 3.1, 3.2], "velocity_smooth"),
        "latlng": _entry([[40.0, -105.0], [40.001, -105.0], [40.002, -105.0]], "latlng"),
        "temp": _entry([18, 18, 19], "temp"),
    }
    df = _build_dataframe(raw)
    assert list(df["time_s"]) == [0, 1, 2]
    assert list(df["heartrate"]) == [100, 110, 120]
    assert list(df["velocity_smooth_mps"]) == [3.0, 3.1, 3.2]
    assert list(df["lat"]) == [40.0, 40.001, 40.002]
    assert list(df["lng"]) == [-105.0, -105.0, -105.0]
    assert list(df["temp_c"]) == [18, 18, 19]
    assert df["cadence"].isna().all()  # a stream that was not recorded becomes all-NA

    # A stream shorter than the reference aligns from the start and is padded; a longer one is cut to fit.
    ragged = _build_dataframe({"time": _entry([0, 1, 2, 3]), "heartrate": _entry([100, 110], "heartrate")})
    assert len(ragged) == 4
    assert list(ragged["heartrate"][:2]) == [100, 110]
    assert ragged["heartrate"][2:].isna().all()
    assert list(_build_dataframe({"time": _entry([0, 1, 2]), "heartrate": _entry([100, 110, 120, 130], "heartrate")})["heartrate"]) == [100, 110, 120]


def test_build_dataframe_missing_time_stream_falls_back_to_distance_and_neither_returns_empty():
    df = _build_dataframe({"distance": _entry([0.0, 5.0, 10.0], "distance", "distance")})
    assert list(df["distance_m"]) == [0.0, 5.0, 10.0]
    assert df["time_s"].isna().all()

    assert _build_dataframe({"heartrate": _entry([100, 110], "heartrate")}).empty


# ---------------------------------------------------------------------------
# Raw storage: staleness, atomic write, round-trip
# ---------------------------------------------------------------------------


def _legacy_parquet(activity_id: int) -> None:
    # A parquet cache from before raw storage existed doesn't count -- it
    # discarded per-stream metadata and a shorter stream's true length, so
    # it can't stand in for a real fetch.
    streams.STREAMS_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"time_s": [0, 1], "distance_m": [0.0, 5.0]}).to_parquet(streams.STREAMS_DIR / f"{activity_id}.parquet", index=False)


def test_a_fetch_is_needed_until_raw_streams_are_stored_and_a_legacy_parquet_does_not_count():
    assert has_cached_streams(123) is False
    _legacy_parquet(123)
    assert has_cached_streams(123) is False
    cache_streams(123, {"time": _stream([0, 1, 2])})
    assert has_cached_streams(123) is True


def test_cache_streams_round_trips_as_gzip_json_and_drops_streams_with_no_data():
    raw = {
        "time": _stream([0, 1, 2]),
        "distance": _stream([0.0, 5.0, 10.0], "distance", "distance"),
        "heartrate": _stream([140, 145, 150], "heartrate"),
        "watts": Stream(type="watts", data=None, series_type="time", original_size=0, resolution="high"),
    }
    cache_streams(777, raw)
    loaded = load_raw_streams(777)
    assert "watts" not in loaded
    assert loaded["heartrate"]["data"] == [140, 145, 150]
    assert loaded["heartrate"]["original_size"] == 3
    assert loaded["heartrate"]["resolution"] == "high"

    with gzip.open(streams.STREAMS_DIR / "raw" / "777.json.gz", "rt", encoding="utf-8") as f:
        assert json.load(f)["time"]["data"] == [0, 1, 2]


def test_cache_streams_atomic_write_leaves_original_on_failure(monkeypatch):
    cache_streams(1, {"time": _stream([0, 1, 2])})
    original = load_raw_streams(1)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(streams.os, "replace", boom)
    with pytest.raises(OSError):
        cache_streams(1, {"time": _stream([9, 9, 9])})

    # The failed write must not clobber the existing raw file, and must not
    # leave a stray temp file behind.
    assert load_raw_streams(1) == original
    assert list((streams.STREAMS_DIR / "raw").glob("*.tmp")) == []


def test_cache_streams_stores_the_raw_file_and_get_stream_dataframe_reads_the_same_frame():
    raw = {
        "time": _stream([0, 1, 2]),
        "latlng": _stream([[40.0, -105.0], [40.001, -105.0], [40.002, -105.0]], "latlng"),
        "heartrate": _stream([140, 150, 160], "heartrate"),
    }
    written_df = cache_streams(42, raw)
    assert has_cached_streams(42) is True
    assert "lat" in written_df.columns
    pd.testing.assert_frame_equal(written_df.reset_index(drop=True), get_stream_dataframe(42).reset_index(drop=True))


@pytest.mark.parametrize("legacy_parquet_present", [False, True])
def test_fetch_and_cache_streams_fetches_once_when_the_raw_file_is_missing(monkeypatch, legacy_parquet_present: bool):
    if legacy_parquet_present:
        _legacy_parquet(777)
    calls = []

    def fake_get_raw(activity_id: int):
        calls.append(activity_id)
        return {"time": _stream([0, 1]), "distance": _stream([0.0, 5.0], "distance", "distance")}

    monkeypatch.setattr(streams.strava_client, "get_activity_streams", fake_get_raw)
    df = fetch_and_cache_streams(777)
    assert calls == [777]
    assert list(df["distance_m"]) == [0.0, 5.0]

    # A second call must not refetch -- the raw file is now on disk.
    fetch_and_cache_streams(777)
    assert calls == [777]
