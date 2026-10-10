from datetime import datetime, timezone

import pytest
import requests
from helpers import strava_fault

from miles import errors, metrics, strava_budget, strava_client


@pytest.fixture(autouse=True)
def _reset():
    metrics.reset()


def _headers(short_usage: int, long_usage: int) -> dict[str, str]:
    return {
        "X-ReadRateLimit-Limit": "100,1000", "X-ReadRateLimit-Usage": f"{short_usage},{long_usage}",
        "X-RateLimit-Limit": "100,1000", "X-RateLimit-Usage": f"{short_usage},{long_usage}",
    }


def _response(status: int = 200, path: str = "/api/v3/activities/5/laps", headers: dict[str, str] | None = None):
    response = requests.Response()
    response.status_code = status
    response.url = f"https://www.strava.com{path}"
    response.headers.update(headers or {})
    response.request = requests.Request("GET", response.url).prepare()
    return response


class _Canned(requests.adapters.BaseAdapter):
    """Answers every request with the next prepared response."""

    def __init__(self, *responses: requests.Response) -> None:
        super().__init__()
        self.responses = list(responses)

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        response = self.responses.pop(0)
        response.request = request
        return response

    def close(self) -> None:
        pass


def _get(*responses: requests.Response) -> None:
    session = strava_client._api_session("token")
    session.mount("https://", _Canned(*responses))
    for response in responses:
        session.get(response.url)


def _at(monkeypatch: pytest.MonkeyPatch, *args: int) -> None:
    moment = datetime(*args, tzinfo=timezone.utc)
    monkeypatch.setattr(strava_client, "_now", lambda: moment)


def _observe(*, short_usage: int, long_usage: int) -> None:
    strava_client._on_response(_response(headers=_headers(short_usage, long_usage)))


# --- the response hook -------------------------------------------------------


def test_daily_remaining_comes_from_the_latest_response_headers(monkeypatch: pytest.MonkeyPatch):
    rates: list[tuple[int, int]] = []
    monkeypatch.setattr(strava_client, "rate_sink", lambda daily, short: rates.append((daily, short)))
    assert strava_client.daily_calls_remaining() is None

    _get(_response(headers=_headers(30, 400)))

    assert strava_client.daily_calls_remaining() == 600
    assert strava_client.short_window_calls_remaining() == 70
    assert rates == [(600, 70)]


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/api/v3/athlete/activities", "activities"),
        ("/api/v3/activities/5", "detail"),
        ("/api/v3/activities/5/laps", "laps"),
        ("/api/v3/activities/5/streams", "streams"),
        ("/api/v3/athlete", "other"),
    ],
)
def test_responses_are_counted_by_endpoint_kind_and_their_own_status(reported, path: str, kind: str):
    _get(_response(200, path), _response(404, path))

    assert reported == [
        ("strava", "www.strava.com", "200", kind),
        ("strava", "www.strava.com", "404", kind),
    ]


def test_a_401_drops_the_access_token_and_is_an_outcome(monkeypatch: pytest.MonkeyPatch, outcomes):
    monkeypatch.setattr(strava_client, "_access_token", "stale")

    _get(_response(401))

    assert strava_client._access_token is None
    assert outcomes == [("strava", 401)]


def test_a_429_is_left_for_the_gate_to_record(outcomes):
    _get(_response(429))
    assert outcomes == []


def test_the_client_carries_a_limiter_that_never_waits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "1")
    monkeypatch.setattr(strava_client, "_current_access_token", lambda: ("token", 4_000_000_000.0))
    client = strava_client._get_client()
    assert client.protocol.rate_limiter.rules == []  # type: ignore[attr-defined]
    assert client.protocol.rsession.hooks["response"] == [strava_client._on_response]


class _Failing:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def get_activity_streams(self, *args: object, **kwargs: object):
        raise self.error


def test_a_body_that_will_not_parse_is_not_also_counted_as_an_error(monkeypatch: pytest.MonkeyPatch, reported):
    monkeypatch.setattr(strava_client, "_get_client", lambda: _Failing(requests.exceptions.JSONDecodeError("bad", "doc", 0)))
    with pytest.raises(errors.StravaUnavailableError):
        strava_client.get_activity_streams(5)
    assert reported == []


def test_no_response_at_all_is_counted_as_an_error_and_names_no_url(monkeypatch: pytest.MonkeyPatch, reported):
    monkeypatch.setattr(
        strava_client, "_get_client", lambda: _Failing(requests.exceptions.ConnectionError("down: https://x/?token=secret"))
    )
    with pytest.raises(errors.StravaUnavailableError) as raised:
        strava_client.get_activity_streams(5)
    assert [r[2:] for r in reported] == [("error", "streams")]
    assert "secret" not in str(raised.value)


# --- the allowance reading ----------------------------------------------------


def test_short_window_usage_resets_after_a_window_boundary(monkeypatch: pytest.MonkeyPatch):
    _at(monkeypatch, 2026, 3, 1, 10, 7)
    _observe(short_usage=95, long_usage=400)
    _at(monkeypatch, 2026, 3, 1, 10, 14)
    assert strava_client.short_window_calls_remaining() == 5
    _at(monkeypatch, 2026, 3, 1, 10, 15)
    assert strava_client.short_window_calls_remaining() == 100
    assert strava_client.daily_calls_remaining() == 600


def test_the_daily_figure_is_dropped_after_the_utc_date_changes_or_a_window_ends(monkeypatch: pytest.MonkeyPatch):
    _at(monkeypatch, 2026, 3, 1, 23, 58)
    _observe(short_usage=1, long_usage=990)
    assert strava_client.daily_calls_remaining() == 10
    _at(monkeypatch, 2026, 3, 2, 0, 1)
    assert strava_client.daily_calls_remaining() is None
    strava_budget.Gate(12).acquire("detail")

    # A waited-out window does not carry yesterday's daily figure forward either.
    _at(monkeypatch, 2026, 3, 1, 23, 50)
    _observe(short_usage=95, long_usage=990)
    _at(monkeypatch, 2026, 3, 2, 0, 5)
    assert strava_client.short_window_calls_remaining() == 100
    assert strava_client.daily_calls_remaining() is None


# --- the gate -------------------------------------------------------------------


def test_the_daily_reserve_refuses_a_request(strava_allowance):
    gate = strava_budget.Gate(12)
    strava_allowance(daily=13)
    gate.acquire("laps")
    strava_allowance(daily=12)
    with pytest.raises(errors.StravaAllowanceLowError, match="reserve"):
        gate.acquire("laps")


def test_the_reserve_defaults_to_the_environment_and_falls_back_to_the_default_on_junk(monkeypatch: pytest.MonkeyPatch, strava_allowance):
    monkeypatch.delenv(strava_budget.RESERVE_ENV, raising=False)
    assert strava_budget.default_reserve() == strava_budget.DEFAULT_RESERVE == 12

    monkeypatch.setenv(strava_budget.RESERVE_ENV, "40")
    strava_allowance(daily=40)
    assert strava_budget.default_reserve() == 40
    with pytest.raises(errors.StravaAllowanceLowError):
        strava_budget.Gate().acquire("streams")

    for junk in ("bogus", "-4"):
        monkeypatch.setenv(strava_budget.RESERVE_ENV, junk)
        assert strava_budget.default_reserve() == strava_budget.DEFAULT_RESERVE


def test_an_unknown_allowance_does_not_block_and_the_activity_listing_is_never_held_back(strava_allowance):
    strava_budget.Gate(12).acquire("laps")

    strava_allowance(daily=0, short=0)
    strava_budget.Gate(12).acquire("activities")


def test_a_nearly_spent_window_is_waited_out_then_resets_the_reading(slept: list[float], monkeypatch: pytest.MonkeyPatch):
    _at(monkeypatch, 2026, 3, 1, 10, 5)
    _observe(short_usage=95, long_usage=100)
    monkeypatch.setattr(
        strava_budget.time, "sleep", lambda seconds: (slept.append(seconds), _at(monkeypatch, 2026, 3, 1, 10, 15))
    )
    waits: list[str] = []

    strava_budget.Gate(12, on_wait=waits.append).acquire("laps")

    assert len(slept) == 1 and len(waits) == 1
    assert strava_client.short_window_calls_remaining() == 100
    assert strava_client.daily_calls_remaining() == 900


def test_an_interactive_request_is_refused_not_held_for_a_window(slept: list[float]):
    _observe(short_usage=95, long_usage=100)
    with pytest.raises(errors.StravaAllowanceLowError, match="15-minute"):
        strava_budget.Gate(12).acquire("laps", interactive=True)
    assert slept == []


class _Flaky:
    """Raises each queued status in turn, then succeeds."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.statuses:
            raise strava_fault(self.statuses.pop(0))
        return "ok"


def test_a_429_waits_one_window_and_repeats_the_request_but_a_second_with_no_success_between_is_final(slept: list[float], outcomes):
    gate = strava_budget.Gate(12)
    request = _Flaky(429)

    assert gate.call("streams", request) == "ok"

    assert request.calls == 2 and len(slept) == 1
    assert outcomes == []

    assert gate.call("streams", _Flaky(429)) == "ok"  # a success between two 429s earns another wait
    assert len(slept) == 2
    assert outcomes == []

    with pytest.raises(errors.StravaAllowanceLowError, match="daily limit reached; resets 00:00 UTC"):
        strava_budget.Gate(12).call("streams", _Flaky(429, 429))
    assert len(slept) == 3
    assert outcomes == [("strava", 429)]


def test_the_activity_listing_keeps_waiting_while_the_daily_limit_has_room(slept: list[float], outcomes):
    request = _Flaky(429, 429, 429)

    assert strava_budget.Gate(12).call("activities", request) == "ok"

    assert len(slept) == 3
    assert outcomes == []


def test_a_429_with_the_daily_limit_spent_or_on_an_interactive_request_is_final_without_waiting(slept: list[float], outcomes):
    _observe(short_usage=3, long_usage=1000)

    with pytest.raises(errors.StravaAllowanceLowError, match="daily limit reached"):
        strava_budget.Gate(0).call("activities", _Flaky(429))

    assert slept == []
    assert outcomes == [("strava", 429)]

    strava_client._observation = None  # the daily figure is no longer spent
    interactive = _Flaky(429)
    with pytest.raises(errors.StravaAllowanceLowError, match="try again"):
        strava_budget.Gate(12).call("laps", interactive, interactive=True)

    assert interactive.calls == 1 and slept == []
    assert outcomes == [("strava", 429), ("strava", 429)]


