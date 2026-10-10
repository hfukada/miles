"""/api/health and /metrics, mounted into miles/api.py."""

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from . import db, events, health, metrics, sync
from .errors import describe_error
from .metrics import Labels

router = APIRouter()

_TEXT_FORMAT = "text/plain; version=0.0.4; charset=utf-8"


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


Sample = tuple[str, str, str, Labels, float]


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


def database_samples(conn: sqlite3.Connection) -> list[Sample]:
    """Series read from the database at scrape time, so they describe work done
    by any process (the sync CLI included), unlike the in-process counters."""
    samples: list[Sample] = []
    for name, help_text, value in [
        ("miles_sync_last_success_timestamp_seconds", "Unix time of the last successful sync.", _parsed_meta(conn, sync.META_LAST_SUCCESS, _timestamp)),
        ("miles_sync_last_failure_timestamp_seconds", "Unix time of the last failed sync.", _parsed_meta(conn, sync.META_LAST_FAILURE, _timestamp)),
        ("miles_sync_last_duration_seconds", "Duration of the last successful sync.", _parsed_meta(conn, sync.META_LAST_DURATION, float)),
        ("miles_sync_last_new_activities", "Activities fetched by the last successful sync.", _parsed_meta(conn, sync.META_LAST_NEW, float)),
    ]:
        if value is not None:
            samples.append((name, "gauge", help_text, (), value))

    help_text = "Outbound requests since records began (see events.py), by service, host, endpoint kind, and status."
    for row in events.totals(conn):
        labels: Labels = (
            ("host", row["host"]), ("kind", row["kind"]), ("service", row["service"]), ("status", row["status"]),
        )
        samples.append(("miles_outbound_requests_total", "counter", help_text, labels, float(row["count"])))

    rate = _parsed_meta(conn, events.STRAVA_RATE_META, _rate)
    if rate is not None:
        samples.append((
            "miles_strava_daily_calls_remaining", "gauge",
            "Strava daily calls left, per the latest response any process saw.", (), float(rate["daily"]),
        ))
        samples.append((
            "miles_strava_short_window_calls_remaining", "gauge",
            "Strava 15-minute-window calls left, per the latest response any process saw.", (), float(rate["short"]),
        ))

    streams_left = conn.execute("SELECT COUNT(*) FROM terrain_backfill_queue").fetchone()[0]
    laps_left = len(sync.remaining_lap_ids(conn))
    help_text = "Historical activities still waiting for their Strava detail, by kind."
    samples.append(("miles_backfill_remaining", "gauge", help_text, (("kind", "streams"),), float(streams_left)))
    samples.append(("miles_backfill_remaining", "gauge", help_text, (("kind", "laps"),), float(laps_left)))
    return samples


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
    with db.connection() as conn:
        body = metrics.render(database_samples(conn))
    return PlainTextResponse(body, media_type=_TEXT_FORMAT)
