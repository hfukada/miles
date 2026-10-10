"""A small in-process metrics registry that renders the Prometheus text format.

Counters and gauges live in this process's memory: the API server, the stdio
MCP server, and each CLI run count separately, and a counter restarts at zero
with its process. State that must be visible across processes (outbound request
totals, sync results, backfill queue sizes) is read from the database at scrape time by
metrics_api.py rather than counted here.

Recording is always on and costs a dict update under a lock; only the
/metrics endpoint is gated (MILES_METRICS).
"""

import math
import os
import threading
from collections.abc import Iterable

ENABLE_ENV = "MILES_METRICS"

Labels = tuple[tuple[str, str], ...]

_lock = threading.Lock()
_families: dict[str, tuple[str, str]] = {}  # name -> (type, help)
_values: dict[str, dict[Labels, float]] = {}


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def declare(name: str, kind: str, help_text: str) -> None:
    """Register a metric family so it renders with HELP/TYPE even before its
    first sample. kind is "counter" or "gauge"."""
    with _lock:
        _families[name] = (kind, help_text)
        _values.setdefault(name, {})


def _key(labels: dict[str, object]) -> Labels:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def inc(name: str, amount: float = 1.0, /, **labels: object) -> None:
    with _lock:
        series = _values.setdefault(name, {})
        key = _key(labels)
        series[key] = series.get(key, 0.0) + amount


def set_gauge(name: str, value: float, /, **labels: object) -> None:
    with _lock:
        _values.setdefault(name, {})[_key(labels)] = float(value)


def get(name: str, /, **labels: object) -> float:
    with _lock:
        return _values.get(name, {}).get(_key(labels), 0.0)


def reset() -> None:
    """Zero every series; families stay declared. For tests."""
    with _lock:
        for series in _values.values():
            series.clear()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_value(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def render(extra: Iterable[tuple[str, str, str, Labels, float]] = ()) -> str:
    """The registry in Prometheus text exposition format. `extra` carries
    samples computed at scrape time as (name, kind, help, labels, value)."""
    with _lock:
        families = dict(_families)
        values = {name: dict(series) for name, series in _values.items()}
    samples: dict[str, dict[Labels, float]] = {name: dict(series) for name, series in values.items()}
    for name, kind, help_text, labels, value in extra:
        families.setdefault(name, (kind, help_text))
        samples.setdefault(name, {})[labels] = value

    lines: list[str] = []
    for name in sorted(set(families) | set(samples)):
        kind, help_text = families.get(name, ("untyped", ""))
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in sorted(samples.get(name, {}).items()):
            if labels:
                rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                lines.append(f"{name}{{{rendered}}} {_format_value(value)}")
            else:
                lines.append(f"{name} {_format_value(value)}")
    return "\n".join(lines) + "\n"


HTTP_RETRIES = "miles_http_retries_total"
HTTP_WAIT_SECONDS = "miles_http_wait_seconds_total"
CACHE_REQUESTS = "miles_cache_requests_total"

declare(HTTP_RETRIES, "counter", "Outbound HTTP attempts that were retried, by service (this process).")
declare(HTTP_WAIT_SECONDS, "counter", "Seconds spent waiting on a service's throttle interval or backoff, by service (this process).")
declare(CACHE_REQUESTS, "counter", "Cache lookups by cache and result (hit, miss, not_modified) (this process).")


def cache(name: str, result: str, amount: float = 1.0) -> None:
    if amount:
        inc(CACHE_REQUESTS, amount, cache=name, result=result)
