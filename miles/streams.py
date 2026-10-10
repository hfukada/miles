"""Per-second Strava stream data: fetch, store the raw response as ground
truth, and derive GAP/lap medians and a DataFrame on read.

Raw streams are stored verbatim -- type/data/series_type/original_size/
resolution per stream, gzipped JSON, one file per activity under
STREAMS_DIR/raw/<activity_id>.json.gz -- the way activities.raw_json and
routes' raw GPX (adr/0002) are ground truth. A raw file is written once,
atomically, and replaced only by a refetch. Every consumer (GAP/lap medians, terrain
segmentation, activity-detail charts) derives its DataFrame from that file
on read; there is no second stored form to go stale or need invalidating.
STREAM_DERIVE_VERSION exists so a change to that derivation is a rebuild for
anything that caches a result keyed to it (terrain.model_version_tag()),
not a re-fetch from Strava.

A *.parquet file from before raw storage existed is left on disk -- it's the
athlete's data -- but ignored: needs_streams_fetch only checks for the raw
file, since a legacy parquet discarded per-stream metadata and a shorter
stream's true length, so it can't stand in for a real fetch.
"""

import gzip
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pandas as pd
from stravalib.model import Stream
from typing_extensions import TypedDict

from . import strava_budget, strava_client

STREAMS_DIR = Path(os.environ.get("MILES_STREAMS_DIR", Path(__file__).parent.parent / "data" / "streams"))

# Bump when _build_dataframe's alignment/parsing logic changes -- a
# consumer that caches a derived result keyed to this version (see
# terrain.model_version_tag()) knows to re-derive from the raw file
# rather than re-fetch it from Strava.
STREAM_DERIVE_VERSION = 1

_STREAM_COLUMNS = [
    "time_s", "distance_m", "altitude_m", "velocity_smooth_mps",
    "heartrate", "cadence", "watts", "temp_c", "grade_smooth", "moving",
    "lat", "lng",
]

_STRAVA_TO_COLUMN = {
    "time": "time_s",
    "distance": "distance_m",
    "altitude": "altitude_m",
    "velocity_smooth": "velocity_smooth_mps",
    "heartrate": "heartrate",
    "cadence": "cadence",
    "watts": "watts",
    "temp": "temp_c",
    "grade_smooth": "grade_smooth",
    "moving": "moving",
}


def _raw_path(activity_id: int) -> Path:
    return STREAMS_DIR / "raw" / f"{activity_id}.json.gz"


def has_cached_streams(activity_id: int) -> bool:
    return _raw_path(activity_id).exists()


def discard_raw_streams(activity_id: int) -> None:
    """Forget the cached streams, so the next backfill fetches them again."""
    _raw_path(activity_id).unlink(missing_ok=True)


def needs_streams_fetch(activity_id: int) -> bool:
    """True when there's no raw stream file for this activity yet."""
    return not has_cached_streams(activity_id)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _raw_to_dict(raw: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {
        key: cast(dict[str, object], stream.model_dump(mode="json"))
        for key, stream in raw.items()
        if isinstance(stream, Stream) and stream.data is not None
    }


def store_raw_streams(activity_id: int, raw: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """Persist Strava's stream response verbatim, gzipped, written
    atomically (temp file + rename); an existing file is replaced whole, so
    callers fetch only when needs_streams_fetch says there is none.
    Returns the plain-dict form actually written, so a caller that just
    fetched doesn't need to read it back off disk to derive a DataFrame."""
    raw_dict = _raw_to_dict(raw)
    _atomic_write_bytes(_raw_path(activity_id), gzip.compress(json.dumps(raw_dict).encode("utf-8")))
    return raw_dict


def load_raw_streams(activity_id: int) -> dict[str, dict[str, object]]:
    with gzip.open(_raw_path(activity_id), "rt", encoding="utf-8") as f:
        return cast(dict[str, dict[str, object]], json.load(f))


def _aligned(data: list[object], n: int) -> list[object]:
    """Positionally align one stream's data to the reference stream's
    length n. Streams are index-aligned per activity at the same
    resolution, but a sensor gap (an HR strap dropping out mid-run) makes
    Strava omit samples rather than null them, so a stream can come back
    shorter than the rest. Pad a shorter tail with None and truncate a
    longer one -- never shift values -- so every kept sample stays matched
    to its real time/distance."""
    if len(data) == n:
        return data
    if len(data) < n:
        return data + [None] * (n - len(data))
    return data[:n]


def _build_dataframe(raw: Mapping[str, Mapping[str, object]]) -> pd.DataFrame:
    """Derive the per-second stream DataFrame from raw stream data, as
    stored by store_raw_streams / loaded by load_raw_streams. time is the
    reference stream (everything else aligns to its length) when present;
    distance stands in for the rare activity Strava returns without one."""
    reference_key, reference_column = "time", "time_s"
    ref = raw.get(reference_key)
    if ref is None or ref.get("data") is None:
        reference_key, reference_column = "distance", "distance_m"
        ref = raw.get(reference_key)
    if ref is None or ref.get("data") is None:
        return pd.DataFrame(columns=_STREAM_COLUMNS)

    reference_data = cast(list[object], ref["data"])
    n = len(reference_data)
    df = pd.DataFrame({reference_column: reference_data})

    for strava_key, column in _STRAVA_TO_COLUMN.items():
        if strava_key == reference_key:
            continue
        entry = raw.get(strava_key)
        if entry is None or entry.get("data") is None:
            continue
        df[column] = _aligned(cast(list[object], entry["data"]), n)

    latlng = raw.get("latlng")
    if latlng is not None and latlng.get("data") is not None:
        pairs = _aligned(cast(list[object], latlng["data"]), n)
        df["lat"] = [p[0] if isinstance(p, list) else None for p in pairs]
        df["lng"] = [p[1] if isinstance(p, list) else None for p in pairs]

    for column in _STREAM_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA

    return cast(pd.DataFrame, df[_STREAM_COLUMNS])


def get_stream_dataframe(activity_id: int) -> pd.DataFrame:
    """The derived DataFrame for an activity whose raw stream file is
    already on disk -- callers check needs_streams_fetch/has_cached_streams
    first."""
    return _build_dataframe(load_raw_streams(activity_id))


def cache_streams(activity_id: int, raw: Mapping[str, object]) -> pd.DataFrame:
    """Store already-fetched raw streams and derive a DataFrame from the
    same data in memory, without reading it back off disk. Split out from
    fetch_and_cache_streams so a batch fetcher (the terrain backfill job)
    can store each activity as it arrives without a redundant per-activity
    API call."""
    raw_dict = store_raw_streams(activity_id, raw)
    return _build_dataframe(raw_dict)


def fetch_and_cache_streams(activity_id: int) -> pd.DataFrame:
    if has_cached_streams(activity_id):
        return get_stream_dataframe(activity_id)
    raw = strava_budget.Gate().call("streams", lambda: strava_client.get_activity_streams(activity_id), interactive=True)
    return cache_streams(activity_id, raw)


# Minetti et al. (2002) energy cost of running on gradients, as a polynomial
# in grade fraction (not percent). Cost is in J/(kg*m); GAP = pace divided by
# the ratio of graded cost to flat-ground cost, holding effort constant —
# uphill (higher cost) yields a faster GAP, downhill (lower cost) a slower one.
def _minetti_cost(grade_fraction: pd.Series) -> pd.Series:
    i = grade_fraction.clip(-0.45, 0.45)
    cost = (
        155.4 * i**5
        - 30.4 * i**4
        - 43.3 * i**3
        + 46.3 * i**2
        + 19.5 * i
        + 3.6
    )
    return cast(pd.Series, cost)


_FLAT_COST: float = float(cast(float, _minetti_cost(pd.Series([0.0])).iloc[0]))


def minetti_cost_ratio(grade_pct: float) -> float:
    """Energy cost of grade relative to flat ground, at a single grade in
    percent (not fraction) — the same Minetti curve compute_gap_pace_s_per_m
    applies to a stream, exposed as a scalar for the terrain model's
    grade-adjusted efficiency and expected-speed calculations."""
    return float(cast(float, _minetti_cost(pd.Series([grade_pct / 100.0])).iloc[0]) / _FLAT_COST)


def compute_gap_pace_s_per_m(df: pd.DataFrame) -> pd.Series:
    """Grade-adjusted pace (seconds per meter) from velocity_smooth + grade_smooth.

    grade_smooth is smoothed again here (short centered rolling mean) before
    the Minetti cost function — the nonlinear cost curve biases the mean
    upward under sample-to-sample GPS/altitude noise, so smoothing must
    happen on the input, not on the resulting GAP values (which would blur
    per-lap medians).
    """
    velocity = df["velocity_smooth_mps"]
    grade = df["grade_smooth"].astype(float).rolling(window=7, center=True, min_periods=1).mean()
    cost_ratio = _minetti_cost(grade / 100.0) / _FLAT_COST
    real_pace_s_per_m = 1.0 / velocity.replace(0, pd.NA)
    return real_pace_s_per_m / cost_ratio


def median_per_window(
    df: pd.DataFrame,
    boundaries_s: list[float],
    value_column: str,
) -> list[float | None]:
    """Median of value_column within each [boundaries_s[i], boundaries_s[i+1])
    window. Returns len(boundaries_s) - 1 values; empty windows -> None."""
    if value_column not in df.columns or len(boundaries_s) < 2:
        return []
    results: list[float | None] = []
    for start, end in zip(boundaries_s[:-1], boundaries_s[1:]):
        mask = (df["time_s"] >= start) & (df["time_s"] < end)
        window = cast(pd.Series, df.loc[mask, value_column]).dropna()
        results.append(float(cast(float, window.median())) if len(window) else None)
    return results


class WindowStats(TypedDict):
    min: float | None
    median: float | None
    max: float | None


def min_median_max_per_window(
    df: pd.DataFrame,
    boundaries_s: list[float],
    value_column: str,
    min_value: float | None = None,
) -> list[WindowStats]:
    """Min/median/max of value_column within each [boundaries_s[i], boundaries_s[i+1])
    window. Returns len(boundaries_s) - 1 entries; empty windows -> all None.

    min_value excludes samples below it before computing stats — for
    velocity_smooth_mps, near-zero readings from pauses/GPS glitches would
    otherwise blow out max pace (min speed) to absurd values.
    """
    if value_column not in df.columns or len(boundaries_s) < 2:
        return []
    results: list[WindowStats] = []
    for start, end in zip(boundaries_s[:-1], boundaries_s[1:]):
        mask = (df["time_s"] >= start) & (df["time_s"] < end)
        window = cast(pd.Series, df.loc[mask, value_column]).dropna()
        if min_value is not None:
            window = cast(pd.Series, window[window >= min_value])
        if len(window):
            results.append(WindowStats(
                min=float(cast(float, window.min())),
                median=float(cast(float, window.median())),
                max=float(cast(float, window.max())),
            ))
        else:
            results.append(WindowStats(min=None, median=None, max=None))
    return results
