"""Route searches run in a background thread, so a tool call that starts one
returns at once instead of racing the client's timeout. Jobs live in this
process only: a restart loses the running ones, and a finished job is kept for
JOB_TTL_S. The candidates a search finds are persisted by route_search as
usual, so they outlive the job."""

import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from . import db, route_search
from .errors import describe_error

JOB_TTL_S = 3600.0
MAX_JOBS = 50
# A wait must end well inside a client's tool-call timeout.
MAX_WAIT_S = 45.0
# How long cancel() waits for the search to hand back what it found.
CANCEL_WAIT_S = 5.0


class UnknownSearchError(LookupError):
    pass


@dataclass
class _Job:
    request: route_search.RouteSearchRequest
    submitted_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    progress: route_search.SearchProgress = field(default_factory=route_search.SearchProgress)
    done: threading.Event = field(default_factory=threading.Event)
    result: route_search.RouteSearchResult | None = None
    error: str | None = None


_jobs: OrderedDict[str, _Job] = OrderedDict()
_jobs_lock = threading.Lock()


def _prune() -> None:
    """Drop finished jobs past their TTL, then the oldest finished ones over MAX_JOBS."""
    now = time.monotonic()
    for search_id, job in list(_jobs.items()):
        if job.finished_at is not None and (now - job.finished_at > JOB_TTL_S or len(_jobs) > MAX_JOBS):
            del _jobs[search_id]


def _run(job: _Job) -> None:
    try:
        with db.connection() as conn:
            job.result = route_search.find_routes(conn, job.request, job.progress)
    except Exception as e:
        job.error = describe_error(e)
    finally:
        job.finished_at = time.monotonic()
        job.done.set()


def start(conn: sqlite3.Connection, request: route_search.RouteSearchRequest) -> dict:
    """Validate `request` (ValueError as find_routes raises it), then search in
    the background. Returns the new job's get() view."""
    request = route_search.validate_request(conn, request)
    job = _Job(request)
    search_id = str(uuid.uuid4())
    with _jobs_lock:
        _prune()
        _jobs[search_id] = job
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return _view(search_id, job)


def _job(search_id: str) -> _Job:
    with _jobs_lock:
        job = _jobs.get(search_id)
    if job is None:
        raise UnknownSearchError(f"unknown or expired search {search_id}; start a new one with find_routes")
    return job


def get(search_id: str, wait_s: float = 0.0) -> dict:
    """The job's state, after waiting up to wait_s (capped at MAX_WAIT_S) for it
    to finish; candidates are the best so far while it runs."""
    job = _job(search_id)
    job.done.wait(min(wait_s, MAX_WAIT_S))
    return _view(search_id, job)


def cancel(search_id: str) -> dict:
    """Ask the search to stop, wait briefly for it to hand back what it has
    found, and return its get() view."""
    job = _job(search_id)
    job.progress.cancelled.set()
    return get(search_id, wait_s=CANCEL_WAIT_S)


def _view(search_id: str, job: _Job) -> dict:
    progress = job.progress
    finished = job.done.is_set()
    if job.error is not None:
        status = "error"
    elif progress.cancelled.is_set():
        status = "cancelled"
    elif finished:
        status = "done"
    elif not progress.started and route_search.search_lock_held():
        status = "queued"
    else:
        status = "running"
    candidates: list[route_search.RouteCandidateOut] = []
    if job.result is not None:
        candidates = job.result["candidates"]
    elif not finished and progress.snapshot is not None:
        with db.connection() as conn:
            candidates = progress.snapshot(conn)
            conn.commit()
    return {
        "search_id": search_id,
        "purpose": job.request.get("purpose"),
        "status": status,
        "elapsed_s": (job.finished_at or time.monotonic()) - job.submitted_at,
        "qualifying": progress.qualifying,
        "candidates": candidates,
        "result": job.result,
        "error": job.error,
    }
