"""Prometheus metrics for /metrics.

Counters live in process memory and restart at zero with it. State that must
span processes (outbound totals, sync results, backfill queue sizes) is read
from the database at scrape time by a collector registered in health_api.py.
Recording is always on; only the /metrics endpoint is gated (MILES_METRICS).
"""

import os

from prometheus_client import CollectorRegistry, Counter, disable_created_metrics

ENABLE_ENV = "MILES_METRICS"

# The per-series creation timestamps would add a second family per counter.
disable_created_metrics()

REGISTRY = CollectorRegistry()

HTTP_RETRIES = Counter(
    "miles_http_retries", "Outbound HTTP attempts that were retried, by service (this process).",
    ["service"], registry=REGISTRY,
)
HTTP_WAIT_SECONDS = Counter(
    "miles_http_wait_seconds",
    "Seconds spent waiting on a service's throttle interval or backoff, by service (this process).",
    ["service"], registry=REGISTRY,
)
CACHE_REQUESTS = Counter(
    "miles_cache_requests", "Cache lookups by cache and result (hit, miss, not_modified) (this process).",
    ["cache", "result"], registry=REGISTRY,
)


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def cache(name: str, result: str, amount: float = 1.0) -> None:
    if amount:
        CACHE_REQUESTS.labels(cache=name, result=result).inc(amount)


def reset() -> None:
    """Zero every series. For tests."""
    for counter in (HTTP_RETRIES, HTTP_WAIT_SECONDS, CACHE_REQUESTS):
        counter.clear()
