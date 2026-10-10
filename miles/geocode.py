"""Nominatim geocoding, cached by query text so a repeated "I'm staying at
X" never re-hits the endpoint. A cached answer never expires: a place's
coordinates do not change, and every lookup the cache absorbs is one request
fewer to a volunteer-run service. "No match" is cached too, and stays cached
until someone asks again. A caller who suspects a wrong or outdated answer
passes refresh=True to look it up again, at most once a day per place.
Requests go through http_client, which holds Nominatim's one-request-per-
second cap process-wide and sends the identifying User-Agent its usage policy
requires; the public instance is refused until MILES_CONTACT says who is
asking.
"""

import json
import os
import sqlite3
import threading
import unicodedata
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing_extensions import NotRequired, TypedDict

from . import http_client, metrics

NOMINATIM_URL = os.environ.get("MILES_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
PUBLIC_HOST = "nominatim.openstreetmap.org"

ATTRIBUTION = "© OpenStreetMap contributors, ODbL"

# A place already looked up this recently is not asked about again, even on
# refresh.
REFRESH_MIN_INTERVAL = timedelta(days=1)


class GeocodeError(Exception):
    """Nominatim returned no match, or the request itself failed."""


class NoMatchError(GeocodeError):
    """Nominatim answered, and had nothing for the query."""


class GeocodeResult(TypedDict):
    lat: float
    lng: float
    display_name: str
    attribution: str
    note: NotRequired[str]


def normalize_query(query: str) -> str:
    """The cache key: Unicode-composed, trimmed, inner whitespace collapsed,
    case-folded, so spellings that differ only in those ways share an entry."""
    return " ".join(unicodedata.normalize("NFC", query).split()).casefold()


def _require_identification() -> None:
    host = urllib.parse.urlsplit(NOMINATIM_URL).hostname
    if host == PUBLIC_HOST and not os.environ.get(http_client.CONTACT_ENV, "").strip():
        raise GeocodeError(
            f"The public Nominatim instance requires an identifying contact: set {http_client.CONTACT_ENV} "
            "(an email address or URL) in the operator's environment, or point MILES_NOMINATIM_URL at "
            "another instance. Coordinates (lat and lng) work without it."
        )


def _fetch_nominatim(query: str) -> tuple[float, float, str]:
    _require_identification()
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1})
    try:
        body = http_client.request(http_client.NOMINATIM, f"{NOMINATIM_URL}?{params}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise GeocodeError(f"Nominatim request failed: {e}") from e
    try:
        results = json.loads(body)
        if not results:
            raise NoMatchError(f"no match for {query!r}")
        result = results[0]
        return (float(result["lat"]), float(result["lon"]), str(result.get("display_name") or query))
    except (ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
        raise GeocodeError(f"Nominatim returned an unusable response ({type(e).__name__})") from e


_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def _result(lat: float, lng: float, display_name: str | None, query: str, note: str | None = None) -> GeocodeResult:
    result: GeocodeResult = {"lat": lat, "lng": lng, "display_name": display_name or query, "attribution": ATTRIBUTION}
    if note:
        result["note"] = note
    return result


def _recent(fetched_at: str) -> bool:
    return datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at) < REFRESH_MIN_INTERVAL


def _cached_answer(conn: sqlite3.Connection, key: str, query: str, refresh: bool) -> GeocodeResult | None:
    """The answer to give from the cache, or None when Nominatim has to be
    asked. Raises for a cached "no match". Rows written before keys were
    normalized sit under the raw query text and are found by it."""
    hit = conn.execute(
        "SELECT lat, lng, display_name, fetched_at FROM geocode_cache WHERE query IN (?, ?) "
        "ORDER BY query = ? DESC LIMIT 1",
        (key, query, key),
    ).fetchone()
    miss = conn.execute("SELECT fetched_at FROM geocode_misses WHERE query = ?", (key,)).fetchone()
    if hit is not None and not refresh:
        metrics.cache("geocode", "hit")
        return _result(hit["lat"], hit["lng"], hit["display_name"], query)
    if hit is None and miss is not None and not refresh:
        metrics.cache("geocode", "hit")
        raise NoMatchError(f"no match for {query!r} (cached; refresh_geocode asks again)")
    if refresh:
        asked = hit["fetched_at"] if hit is not None else miss["fetched_at"] if miss is not None else None
        if asked is not None and _recent(asked):
            metrics.cache("geocode", "hit")
            note = "looked up less than a day ago; returning that answer"
            if hit is None:
                raise NoMatchError(f"no match for {query!r} ({note})")
            return _result(hit["lat"], hit["lng"], hit["display_name"], query, note)
    return None


def lookup(conn: sqlite3.Connection, query: str, *, refresh: bool = False) -> GeocodeResult:
    """Coordinates, a display name, and the required attribution for a
    free-text place, from cache when there is an entry. refresh=True asks
    Nominatim again and overwrites the entry, unless the place was looked up
    within the last day; if that request fails the cached answer is returned
    with a note rather than an error, since a network hiccup shouldn't break
    an otherwise-cached lookup. Identical lookups running at once ask
    Nominatim once."""
    query = " ".join(query.split())
    key = normalize_query(query)
    if not key:
        raise GeocodeError("no place given")
    answer = _cached_answer(conn, key, query, refresh)
    if answer is not None:
        return answer
    with _lock_for(key):
        answer = _cached_answer(conn, key, query, refresh)
        if answer is not None:
            return answer
        metrics.cache("geocode", "miss")
        return _ask_nominatim(conn, key, query)


def _ask_nominatim(conn: sqlite3.Connection, key: str, query: str) -> GeocodeResult:
    now = datetime.now(timezone.utc).isoformat()
    previous = conn.execute(
        "SELECT lat, lng, display_name FROM geocode_cache WHERE query IN (?, ?) ORDER BY query = ? DESC LIMIT 1",
        (key, query, key),
    ).fetchone()
    try:
        lat, lng, display_name = _fetch_nominatim(query)
    except NoMatchError:
        if previous is not None:
            # The question was asked; the once-a-day limit counts from now.
            conn.execute("UPDATE geocode_cache SET fetched_at = ? WHERE query IN (?, ?)", (now, key, query))
            conn.commit()
            return _result(
                previous["lat"], previous["lng"], previous["display_name"], query,
                "Nominatim no longer finds this place; returning the earlier answer",
            )
        conn.execute(
            "INSERT INTO geocode_misses (query, fetched_at) VALUES (?, ?) "
            "ON CONFLICT(query) DO UPDATE SET fetched_at = excluded.fetched_at",
            (key, now),
        )
        conn.commit()
        raise
    except GeocodeError as e:
        if previous is not None:
            return _result(
                previous["lat"], previous["lng"], previous["display_name"], query,
                f"Nominatim could not be reached ({e}); returning the cached answer",
            )
        raise

    conn.execute(
        """
        INSERT INTO geocode_cache (query, lat, lng, display_name, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(query) DO UPDATE SET
            lat = excluded.lat, lng = excluded.lng,
            display_name = excluded.display_name, fetched_at = excluded.fetched_at
        """,
        (key, lat, lng, display_name, now),
    )
    conn.execute("DELETE FROM geocode_misses WHERE query = ?", (key,))
    conn.commit()
    return _result(lat, lng, display_name, query)


def geocode(conn: sqlite3.Connection, query: str, *, refresh: bool = False) -> tuple[float, float]:
    """(lat, lng) of lookup(): for callers that need only the coordinates."""
    result = lookup(conn, query, refresh=refresh)
    return (result["lat"], result["lng"])
