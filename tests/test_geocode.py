import json
import sqlite3
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from miles import db, geocode, http_client


def _age(conn: sqlite3.Connection, table: str, days: float) -> None:
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute(f"UPDATE {table} SET fetched_at = ?", (when,))
    conn.commit()


def _nominatim(monkeypatch: pytest.MonkeyPatch, answer: object) -> list[str]:
    """Answer geocode's Nominatim lookups with `answer` (a result tuple, or an
    exception to raise); returns the queries asked."""
    asked: list[str] = []

    def fetch(query: str):
        asked.append(query)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(geocode, "_fetch_nominatim", fetch)
    return asked


def test_a_place_is_fetched_once_and_cached_for_good(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    asked = _nominatim(monkeypatch, (1.0, 2.0, "Somewhere, Region"))

    assert geocode.geocode(conn, "Somewhere") == (1.0, 2.0)
    row = conn.execute("SELECT * FROM geocode_cache").fetchone()
    assert (row["query"], row["lat"], row["lng"]) == ("somewhere", 1.0, 2.0)

    _age(conn, "geocode_cache", 5000)
    assert geocode.geocode(conn, "Somewhere") == (1.0, 2.0)
    assert geocode.lookup(conn, "somewhere") == {
        "lat": 1.0, "lng": 2.0, "display_name": "Somewhere, Region", "attribution": "© OpenStreetMap contributors, ODbL",
    }
    assert len(asked) == 1


@pytest.mark.parametrize(
    "stored, asked_as",
    [
        ("Cached Place", "  Cached   Place "),
        ("Cached Place", "CACHED PLACE"),
        ("Cached Place", "Cached\tPlace\n"),
        ("Créteil", "Créteil"),
    ],
)
def test_spellings_that_differ_in_case_spacing_or_accent_encoding_share_an_entry(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, stored: str, asked_as: str
):
    asked = _nominatim(monkeypatch, (1.0, 2.0, "x"))
    geocode.geocode(conn, stored)
    assert geocode.geocode(conn, asked_as) == (1.0, 2.0)
    assert len(asked) == 1


def test_an_entry_cached_under_its_raw_text_is_still_found(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    conn.execute(
        "INSERT INTO geocode_cache (query, lat, lng, display_name, fetched_at) VALUES ('Old Style', 3.0, 4.0, 'x', 'now')"
    )
    conn.commit()
    asked = _nominatim(monkeypatch, (9.0, 9.0, "y"))
    assert geocode.geocode(conn, "Old Style") == (3.0, 4.0)
    assert asked == []


def test_a_refresh_asks_again_only_after_a_day(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _nominatim(monkeypatch, (1.0, 2.0, "x"))
    geocode.geocode(conn, "Moved Place")

    asked = _nominatim(monkeypatch, (9.0, 9.0, "y"))
    result = geocode.lookup(conn, "Moved Place", refresh=True)
    assert (result["lat"], result["lng"]) == (1.0, 2.0)
    assert "less than a day ago" in result["note"]
    assert asked == []

    _age(conn, "geocode_cache", 2)
    assert geocode.geocode(conn, "Moved Place", refresh=True) == (9.0, 9.0)
    assert geocode.geocode(conn, "Moved Place") == (9.0, 9.0)


@pytest.mark.parametrize(
    "failure, note",
    [(geocode.GeocodeError("network down"), "network down"), (geocode.NoMatchError("no match"), "no longer finds")],
)
def test_a_refresh_that_fails_or_finds_nothing_keeps_the_earlier_answer_and_uses_up_the_day(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, failure: Exception, note: str
):
    _nominatim(monkeypatch, (1.0, 2.0, "x"))
    geocode.geocode(conn, "Renamed")
    _age(conn, "geocode_cache", 2)

    asked = _nominatim(monkeypatch, failure)
    first = geocode.lookup(conn, "Renamed", refresh=True)
    assert (first["lat"], first["lng"]) == (1.0, 2.0)
    assert note in first["note"]
    assert len(asked) == 1

    if isinstance(failure, geocode.NoMatchError):
        again = geocode.lookup(conn, "Renamed", refresh=True)
        assert "less than a day ago" in again["note"]
        assert len(asked) == 1
    assert geocode.geocode(conn, "Renamed") == (1.0, 2.0)


def test_geocode_raises_with_no_cache_and_failed_fetch(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _nominatim(monkeypatch, geocode.GeocodeError("network down"))
    with pytest.raises(geocode.GeocodeError):
        geocode.geocode(conn, "Nowhere")
    assert conn.execute("SELECT COUNT(*) FROM geocode_misses").fetchone()[0] == 0  # a failure is not a "no match"


def test_no_match_is_cached_until_someone_asks_again(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    calls = _nominatim(monkeypatch, geocode.NoMatchError("no match"))
    for _ in range(3):
        with pytest.raises(geocode.NoMatchError):
            geocode.geocode(conn, "Atlantis")
    assert len(calls) == 1

    with pytest.raises(geocode.NoMatchError, match="less than a day ago"):
        geocode.geocode(conn, "Atlantis", refresh=True)
    assert len(calls) == 1

    _age(conn, "geocode_misses", 2)
    _nominatim(monkeypatch, (1.0, 2.0, "Atlantis, Sea"))
    assert geocode.geocode(conn, "Atlantis", refresh=True) == (1.0, 2.0)
    assert conn.execute("SELECT COUNT(*) FROM geocode_misses").fetchone()[0] == 0


def test_concurrent_identical_misses_ask_nominatim_once(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def slow_fetch(query: str):
        calls.append(query)
        time.sleep(0.05)
        return (1.0, 2.0, "x")

    monkeypatch.setattr(geocode, "_fetch_nominatim", slow_fetch)
    results: list[tuple[float, float]] = []

    def worker(text: str) -> None:
        connection = db.connect()
        results.append(geocode.geocode(connection, text))
        connection.close()

    threads = [threading.Thread(target=worker, args=(text,)) for text in ("Same Place", " same  place", "SAME PLACE")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1
    assert results == [(1.0, 2.0)] * 3


def test_the_public_instance_is_refused_without_a_contact(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(geocode, "NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
    monkeypatch.delenv("MILES_CONTACT", raising=False)
    monkeypatch.setattr(http_client, "request", lambda *a, **k: pytest.fail("nothing may be sent"))
    with pytest.raises(geocode.GeocodeError, match="MILES_CONTACT"):
        geocode.geocode(conn, "Anywhere")


@pytest.mark.parametrize(
    "url, contact",
    [("https://nominatim.openstreetmap.org/search", "ops@example.com"), ("http://nominatim.internal:8080/search", None)],
)
def test_the_public_instance_needs_a_contact_and_another_instance_does_not(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, url: str, contact: str | None
):
    monkeypatch.setattr(geocode, "NOMINATIM_URL", url)
    if contact is None:
        monkeypatch.delenv("MILES_CONTACT", raising=False)
    else:
        monkeypatch.setenv("MILES_CONTACT", contact)
    sent: list[str] = []
    monkeypatch.setattr(
        http_client, "request",
        lambda policy, url, **k: sent.append(url) or json.dumps([{"lat": "1.5", "lon": "2.5", "display_name": "A"}]).encode(),
    )
    assert geocode.geocode(conn, "Anywhere") == (1.5, 2.5)
    assert len(sent) == 1


@pytest.mark.parametrize(
    "body",
    [b"<html>rate limited</html>", b"{}", b'[{"lat": "x", "lon": "1"}]', b'[{"lon": "1"}]', b'["string"]', b"null"],
)
def test_an_unusable_response_is_a_geocode_error_and_is_not_cached(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, body: bytes):
    monkeypatch.setattr(geocode, "NOMINATIM_URL", "http://nominatim.internal/search")
    monkeypatch.setattr(http_client, "request", lambda policy, url, **k: body)
    with pytest.raises(geocode.GeocodeError) as raised:
        geocode.geocode(conn, "Anywhere")
    if body not in (b"{}", b"null"):
        assert not isinstance(raised.value, geocode.NoMatchError)
    assert conn.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0] == 0


def test_an_empty_result_list_is_a_no_match(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(geocode, "NOMINATIM_URL", "http://nominatim.internal/search")
    monkeypatch.setattr(http_client, "request", lambda policy, url, **k: b"[]")
    with pytest.raises(geocode.NoMatchError):
        geocode.geocode(conn, "Atlantis")


@pytest.mark.parametrize(
    "failure, message",
    [
        (http_client.ServiceBackoffError("nominatim asked us to stay away"), "stay away"),
        (urllib.error.URLError("down"), "request failed"),
    ],
)
def test_a_backoff_or_request_error_is_a_geocode_error(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, failure: Exception, message: str):
    monkeypatch.setattr(geocode, "NOMINATIM_URL", "http://nominatim.internal/search")

    def fails(policy, url, **k):
        raise failure

    monkeypatch.setattr(http_client, "request", fails)
    with pytest.raises(geocode.GeocodeError, match=message):
        geocode.geocode(conn, "Anywhere")
