"""
Dump field names/types for stravalib models and a sample Open-Meteo response.
Run with: uv run python3 scripts/inspect_models.py
Results are stable across sessions — stravalib and Open-Meteo schemas change rarely.
"""

import json
import urllib.parse
import urllib.request
from typing import get_type_hints

from stravalib.model import Lap, SummaryActivity


def _model_fields(model: type) -> dict[str, str]:
    try:
        hints = get_type_hints(model)
        return {k: str(v) for k, v in hints.items() if not k.startswith("_")}
    except Exception:
        return {k: str(v.annotation) for k, v in model.model_fields.items()}


def _open_meteo_sample() -> dict[str, object]:
    params = urllib.parse.urlencode({
        "latitude": 37.7749,
        "longitude": -122.4194,
        "start_date": "2024-10-15",
        "end_date": "2024-10-15",
        "hourly": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,windspeed_10m",
        "timezone": "UTC",
        "wind_speed_unit": "kmh",
    })
    url = f"https://archive-api.open-meteo.com/v1/archive?{params}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data: dict[str, object] = json.loads(resp.read())
    # Summarize: show keys and first 3 values of each hourly series
    hourly = data.get("hourly", {})
    assert isinstance(hourly, dict)
    summary: dict[str, object] = {
        "top_level_keys": list(data.keys()),
        "hourly_keys": list(hourly.keys()),
        "hourly_sample": {k: v[:3] if isinstance(v, list) else v for k, v in hourly.items()},
    }
    return summary


if __name__ == "__main__":
    print("=== SummaryActivity fields ===")
    for name, typ in _model_fields(SummaryActivity).items():
        print(f"  {name}: {typ}")

    print("\n=== Lap fields ===")
    for name, typ in _model_fields(Lap).items():
        print(f"  {name}: {typ}")

    print("\n=== Open-Meteo archive response structure ===")
    sample = _open_meteo_sample()
    print(json.dumps(sample, indent=2))

    print("\n=== Key facts ===")
    from stravalib.model import LatLon
    ll = LatLon.model_validate([37.0, -122.0])
    print(f"  LatLon.lat = {ll.lat}, LatLon.lon = {ll.lon}  (access via .lat/.lon, not indexing)")
    print(f"  SummaryActivity.model_dump_json() available: {hasattr(SummaryActivity, 'model_dump_json')}")
    print(f"  Lap.model_dump_json() available: {hasattr(Lap, 'model_dump_json')}")
