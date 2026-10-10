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


def test_a_failed_fetch_is_not_a_no_match_and_a_no_match_is_cached_until_someone_asks_again(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    _nominatim(monkeypatch, geocode.GeocodeError("network down"))
    with pytest.raises(geocode.GeocodeError):
        geocode.geocode(conn, "Nowhere")
    assert conn.execute("SELECT COUNT(*) FROM geocode_misses").fetchone()[0] == 0

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


def _answer_with(monkeypatch: pytest.MonkeyPatch, url: str, outcome: bytes | Exception) -> list[str]:
    """Point geocode at `url` and answer every request with `outcome` (a body, or an exception to raise)."""
    monkeypatch.setattr(geocode, "NOMINATIM_URL", url)
    sent: list[str] = []

    def request(policy, url, **k):
        sent.append(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(http_client, "request", request)
    return sent


@pytest.mark.parametrize(
    "url, contact, refused",
    [
        ("https://nominatim.openstreetmap.org/search", None, True),
        ("https://nominatim.openstreetmap.org/search", "ops@example.com", False),
        ("http://nominatim.internal:8080/search", None, False),
    ],
    ids=["the public instance without a contact", "the public instance with one", "another instance without one"],
)
def test_the_public_instance_needs_a_contact_and_another_instance_does_not(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, url: str, contact: str | None, refused: bool
):
    if contact is None:
        monkeypatch.delenv("MILES_CONTACT", raising=False)
    else:
        monkeypatch.setenv("MILES_CONTACT", contact)
    sent = _answer_with(monkeypatch, url, json.dumps([{"lat": "1.5", "lon": "2.5", "display_name": "A"}]).encode())
    if refused:
        with pytest.raises(geocode.GeocodeError, match="MILES_CONTACT"):
            geocode.geocode(conn, "Anywhere")
        assert sent == []
    else:
        assert geocode.geocode(conn, "Anywhere") == (1.5, 2.5)
        assert len(sent) == 1


@pytest.mark.parametrize(
    "outcome, error, message",
    [
        (b"<html>rate limited</html>", geocode.GeocodeError, None),
        (b"{}", geocode.NoMatchError, None),
        (b'[{"lat": "x", "lon": "1"}]', geocode.GeocodeError, None),
        (b'[{"lon": "1"}]', geocode.GeocodeError, None),
        (b'["string"]', geocode.GeocodeError, None),
        (b"null", geocode.NoMatchError, None),
        (b"[]", geocode.NoMatchError, None),
        (http_client.ServiceBackoffError("nominatim asked us to stay away"), geocode.GeocodeError, "stay away"),
        (urllib.error.URLError("down"), geocode.GeocodeError, "request failed"),
    ],
    ids=["html", "empty object", "bad lat", "no lat", "string entry", "null", "empty list", "backoff", "request error"],
)
def test_an_unusable_response_or_failed_request_is_a_geocode_error_and_is_not_cached(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, outcome: bytes | Exception, error: type[Exception], message: str | None
):
    _answer_with(monkeypatch, "http://nominatim.internal/search", outcome)
    with pytest.raises(error, match=message) as raised:
        geocode.geocode(conn, "Anywhere")
    assert isinstance(raised.value, geocode.NoMatchError) is issubclass(error, geocode.NoMatchError)
    assert conn.execute("SELECT COUNT(*) FROM geocode_cache").fetchone()[0] == 0
