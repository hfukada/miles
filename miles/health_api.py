"""/api/health and /metrics, mounted into miles/api.py."""

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import TypeVar

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from . import db, events, health, metrics, sync
from .errors import describe_error

router = APIRouter()

@router.get("/api/health")
def get_health(verbose: bool = False) -> JSONResponse:
    """200 {"status": "ok"}, or 503 {"status": "attention", "reasons": [...]};
    verbose=1 adds the underlying values."""
    result: health.Health
    try:
        with db.connection() as conn:
            result = health.check(conn, verbose=verbose)
    except sqlite3.Error as e:
        result = {"status": "attention", "reasons": [f"database unreadable: {describe_error(e)}"]}
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)


T = TypeVar("T")


def _parsed_meta(conn: sqlite3.Connection, key: str, parse: Callable[[str], T]) -> T | None:
    """The meta value run through `parse`; None when it is absent or malformed,
    so one bad value costs its own series and not the whole scrape."""
    value = db.get_meta(conn, key)
    if value is None:
        return None
    try:
        return parse(value)
    except (ValueError, KeyError, TypeError):
        return None


def _timestamp(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def _rate(text: str) -> dict[str, int]:
    parsed = json.loads(text)
    return {"daily": int(parsed["daily"]), "short": int(parsed["short"])}


def _gauge(name: str, help_text: str, value: float | None) -> Iterator[GaugeMetricFamily]:
    if value is not None:
        family = GaugeMetricFamily(name, help_text)
        family.add_metric([], value)
        yield family


class DatabaseCollector(Collector):
    """Series read from the database at scrape time, so they describe work done
    by any process (the sync CLI included), unlike the in-process counters."""

    def describe(self) -> list[Metric]:
        return []

    def collect(self) -> Iterator[Metric]:
        with db.connection() as conn:
            yield from self._collect(conn)

    @staticmethod
    def _collect(conn: sqlite3.Connection) -> Iterator[Metric]:
        yield from _gauge(
            "miles_sync_last_success_timestamp_seconds", "Unix time of the last successful sync.",
            _parsed_meta(conn, sync.META_LAST_SUCCESS, _timestamp),
        )
        yield from _gauge(
            "miles_sync_last_failure_timestamp_seconds", "Unix time of the last failed sync.",
            _parsed_meta(conn, sync.META_LAST_FAILURE, _timestamp),
        )
        yield from _gauge(
            "miles_sync_last_duration_seconds", "Duration of the last successful sync.",
            _parsed_meta(conn, sync.META_LAST_DURATION, float),
        )
        yield from _gauge(
            "miles_sync_last_new_activities", "Activities fetched by the last successful sync.",
            _parsed_meta(conn, sync.META_LAST_NEW, float),
        )

        outbound = CounterMetricFamily(
            "miles_outbound_requests",
            "Outbound requests since records began (see events.py), by service, host, endpoint kind, and status.",
            labels=["host", "kind", "service", "status"],
        )
        for row in events.totals(conn):
            outbound.add_metric([row["host"], row["kind"], row["service"], row["status"]], float(row["count"]))
        yield outbound

        rate = _parsed_meta(conn, events.STRAVA_RATE_META, _rate)
        if rate is not None:
            yield from _gauge(
                "miles_strava_daily_calls_remaining",
                "Strava daily calls left, per the latest response any process saw.", float(rate["daily"]),
            )
            yield from _gauge(
                "miles_strava_short_window_calls_remaining",
                "Strava 15-minute-window calls left, per the latest response any process saw.", float(rate["short"]),
            )

        backfill = GaugeMetricFamily(
            "miles_backfill_remaining", "Historical activities still waiting for their Strava detail, by kind.",
            labels=["kind"],
        )
        backfill.add_metric(["streams"], conn.execute("SELECT COUNT(*) FROM terrain_backfill_queue").fetchone()[0])
        backfill.add_metric(["laps"], len(sync.remaining_lap_ids(conn)))
        yield backfill


metrics.REGISTRY.register(DatabaseCollector())


@router.get("/api/outbound")
def get_outbound(days: int = 30) -> list[dict[str, object]]:
    """Per-day counts of outbound requests by service, host, endpoint kind,
    and status over the last `days` days."""
    with db.connection() as conn:
        return events.daily_counts(conn, days)


@router.get("/metrics")
def get_metrics() -> PlainTextResponse:
    """Prometheus text format; 404 unless MILES_METRICS is set. Retry, wait, and
    cache series are this process's; the rest come from the database."""
    if not metrics.enabled():
        raise HTTPException(status_code=404)
    return PlainTextResponse(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)
