import http.client
import threading
import urllib.error

import pytest
from helpers import FakeClock, FakeResponse, http_error, urlopen_sequence

from miles import http_client


def test_user_agent_has_no_contact_by_default_and_appends_the_configured_one(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MILES_CONTACT", raising=False)
    ua = http_client.user_agent()
    assert ua.startswith("miles/")
    assert "contact" not in ua

    monkeypatch.setenv("MILES_CONTACT", "ops@example.org")
    assert "contact: ops@example.org" in http_client.user_agent()


def test_request_sends_the_user_agent(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    calls = urlopen_sequence(monkeypatch, [FakeResponse(b"hello")])
    assert http_client.request(http_client.OSRM, "http://x") == b"hello"
    assert calls[0].get_header("User-agent") == http_client.user_agent()


def test_requests_to_one_service_are_spaced_by_its_interval_and_services_are_independent(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    urlopen_sequence(monkeypatch, [FakeResponse()])
    starts: list[float] = []
    real = http_client.urllib.request.urlopen

    def stamping(req, timeout=None):
        starts.append(clock.now)
        return real(req, timeout=timeout)

    monkeypatch.setattr(http_client.urllib.request, "urlopen", stamping)
    for _ in range(3):
        http_client.request(http_client.NOMINATIM, "http://x")
    assert starts[1] - starts[0] >= http_client.NOMINATIM.min_interval_s
    assert starts[2] - starts[1] >= http_client.NOMINATIM.min_interval_s

    clock.sleeps.clear()
    http_client.request(http_client.OSRM, "http://x")
    assert clock.sleeps == []


def test_retries_on_429_503_504_with_exponential_backoff(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    policy = http_client.ServicePolicy("t-retry", max_attempts=4, backoff_base_s=2.0)
    calls = urlopen_sequence(monkeypatch, [http_error(429), http_error(503), http_error(504), FakeResponse(b"ok")])
    assert http_client.request(policy, "http://x") == b"ok"
    assert len(calls) == 4
    assert clock.sleeps == [2.0, 4.0, 8.0]


def test_retries_on_connection_errors(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    policy = http_client.ServicePolicy("t-conn", max_attempts=3, backoff_base_s=1.0)
    calls = urlopen_sequence(monkeypatch, [urllib.error.URLError("refused"), TimeoutError(), FakeResponse(b"ok")])
    assert http_client.request(policy, "http://x") == b"ok"
    assert len(calls) == 3


def test_gives_up_after_max_attempts_and_raises_the_last_error(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    policy = http_client.ServicePolicy("t-giveup", max_attempts=3, backoff_base_s=1.0)
    calls = urlopen_sequence(monkeypatch, [http_error(503)])
    with pytest.raises(urllib.error.HTTPError):
        http_client.request(policy, "http://x")
    assert len(calls) == 3


def test_a_body_cut_short_on_the_last_attempt_surfaces_as_a_url_error(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    policy = http_client.ServicePolicy("t-incomplete", max_attempts=2, backoff_base_s=1.0)
    calls = urlopen_sequence(monkeypatch, [http.client.IncompleteRead(b"ab", 10)])
    with pytest.raises(urllib.error.URLError) as caught:
        http_client.request(policy, "http://x")
    assert isinstance(caught.value.__cause__, http.client.IncompleteRead)
    assert len(calls) == 2


def test_does_not_retry_other_statuses(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    calls = urlopen_sequence(monkeypatch, [http_error(400)])
    with pytest.raises(urllib.error.HTTPError):
        http_client.request(http_client.OSRM, "http://x")
    assert len(calls) == 1


def test_retry_after_is_honored_when_longer_than_the_backoff(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    policy = http_client.ServicePolicy("t-retry-after", max_attempts=2, backoff_base_s=1.0)
    urlopen_sequence(monkeypatch, [http_error(429, {"Retry-After": "42"}), FakeResponse()])
    http_client.request(policy, "http://x")
    assert clock.sleeps == [42.0]


def test_a_retry_after_longer_than_the_cap_gives_up_instead_of_retrying(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    policy = http_client.ServicePolicy("t-long-retry-after", max_attempts=3, backoff_base_s=1.0)
    huge = str(int(http_client._MAX_RETRY_AFTER_S) + 1)
    calls = urlopen_sequence(monkeypatch, [http_error(429, {"Retry-After": huge}), FakeResponse()])
    with pytest.raises(urllib.error.HTTPError):
        http_client.request(policy, "http://x")
    assert len(calls) == 1
    assert clock.sleeps == []


def test_a_block_longer_than_a_request_is_worth_waiting_for_fails_fast(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    urlopen_sequence(monkeypatch, [http_error(403), FakeResponse()])
    with pytest.raises(urllib.error.HTTPError):
        http_client.request(http_client.NOMINATIM, "http://x")
    clock.sleeps.clear()
    with pytest.raises(http_client.ServiceBackoffError, match="stay away"):
        http_client.request(http_client.NOMINATIM, "http://x")
    assert clock.sleeps == []


def test_open_meteo_waits_a_minute_after_a_429_before_retrying(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    urlopen_sequence(monkeypatch, [http_error(429), FakeResponse(b"ok")])
    assert http_client.request(http_client.OPEN_METEO, "http://x") == b"ok"
    assert max(clock.sleeps) >= 60.0


def test_a_short_pause_is_waited_out_by_the_next_caller_and_the_refusal_is_not_retried(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    calls = urlopen_sequence(monkeypatch, [http_error(429), FakeResponse()])
    with pytest.raises(urllib.error.HTTPError):
        http_client.request(http_client.NOMINATIM, "http://x")
    assert len(calls) == 1
    clock.sleeps.clear()
    http_client.request(http_client.NOMINATIM, "http://x")
    assert clock.sleeps and clock.sleeps[0] >= 60.0


def test_a_pause_after_429_also_holds_back_other_callers(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    policy = http_client.ServicePolicy("t-shared-pause", min_pause_s={429: 30.0}, max_attempts=1)
    urlopen_sequence(monkeypatch, [http_error(429), FakeResponse()])
    with pytest.raises(urllib.error.HTTPError):
        http_client.request(policy, "http://x")
    clock.sleeps.clear()
    http_client.request(policy, "http://x")
    assert clock.sleeps and clock.sleeps[0] >= 30.0


def test_a_pause_set_while_a_caller_waits_its_turn_is_honored(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    policy = http_client.ServicePolicy("t-late-pause", min_interval_s=1.0)
    starts: list[float] = []

    def stamping(req, timeout=None):
        starts.append(clock.now)
        return FakeResponse()

    monkeypatch.setattr(http_client.urllib.request, "urlopen", stamping)
    http_client.request(policy, "http://x")

    paused = False

    def sleep_then_get_paused(seconds: float) -> None:
        nonlocal paused
        clock.sleep(seconds)
        if not paused:
            paused = True
            http_client._state(policy).stay_quiet_until(clock.now + 30.0)  # another caller just saw a 429

    monkeypatch.setattr(http_client.time, "sleep", sleep_then_get_paused)
    http_client.request(policy, "http://x")

    assert starts[1] - starts[0] >= 30.0


@pytest.mark.parametrize(
    "policy", [http_client.OSRM, http_client.NOMINATIM, http_client.OPEN_METEO], ids=lambda p: p.name
)
def test_published_request_rates_are_kept_by_the_client(clock: FakeClock, monkeypatch: pytest.MonkeyPatch, policy):
    starts: list[float] = []

    def stamping(req, timeout=None):
        starts.append(clock.now)
        return FakeResponse()

    monkeypatch.setattr(http_client.urllib.request, "urlopen", stamping)
    for _ in range(4):
        http_client.request(policy, "http://x")

    floor = 1.0
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert gaps and all(g >= floor for g in gaps)


@pytest.mark.parametrize("policy", [http_client.GEOFABRIK, http_client.NOMINATIM], ids=lambda p: p.name)
def test_concurrent_callers_of_an_exclusive_service_run_one_at_a_time_under_the_real_policy(monkeypatch: pytest.MonkeyPatch, policy):
    state_lock = threading.Lock()
    now = [1000.0]
    real_sleep = http_client.time.sleep

    def monotonic() -> float:
        with state_lock:
            return now[0]

    def sleep(seconds: float) -> None:
        with state_lock:
            now[0] += seconds
        real_sleep(0.001)

    monkeypatch.setattr(http_client.time, "monotonic", monotonic)
    monkeypatch.setattr(http_client.time, "sleep", sleep)
    active = 0
    max_active = 0
    intervals: list[tuple[float, float]] = []

    def fake_urlopen(req, timeout=None):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
            began = now[0]
        real_sleep(0.02)
        with state_lock:
            active -= 1
            intervals.append((began, now[0]))
        return FakeResponse()

    monkeypatch.setattr(http_client.urllib.request, "urlopen", fake_urlopen)
    threads = [threading.Thread(target=http_client.request, args=(policy, "http://x")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_active == 1
    starts = sorted(began for began, _ in intervals)
    assert all(b - a >= policy.min_interval_s for a, b in zip(starts, starts[1:]))


def test_a_429_that_a_retry_gets_past_is_not_an_outcome(clock: FakeClock, monkeypatch: pytest.MonkeyPatch, outcomes):
    policy = http_client.ServicePolicy("t-past-429", max_attempts=3, backoff_base_s=1.0)
    urlopen_sequence(monkeypatch, [http_error(429), FakeResponse(b"ok")])
    assert http_client.request(policy, "http://x") == b"ok"
    assert outcomes == []


def test_a_429_on_the_last_attempt_is_an_outcome(clock: FakeClock, monkeypatch: pytest.MonkeyPatch, outcomes):
    policy = http_client.ServicePolicy("t-last-429", max_attempts=2, backoff_base_s=1.0)
    urlopen_sequence(monkeypatch, [http_error(429)])
    with pytest.raises(urllib.error.HTTPError):
        http_client.request(policy, "http://x")
    assert outcomes == [("t-last-429", 429)]


def test_a_response_is_reported_after_its_body_is_read(clock: FakeClock, monkeypatch: pytest.MonkeyPatch, reported):
    seen = reported
    policy = http_client.ServicePolicy("t-body", max_attempts=1)
    urlopen_sequence(monkeypatch, [FakeResponse(b"ok")])

    def dying_reader(resp: object) -> bytes:
        raise ConnectionResetError("reset while reading")

    with pytest.raises(ConnectionResetError):
        http_client.request(policy, "http://x", reader=dying_reader)
    assert [s[2] for s in seen] == ["error"]


def test_a_request_can_name_its_method(clock: FakeClock, monkeypatch: pytest.MonkeyPatch):
    calls = urlopen_sequence(monkeypatch, [FakeResponse(b""), FakeResponse(b"")])
    policy = http_client.ServicePolicy("t-method", max_attempts=1)

    http_client.request(policy, "http://x", method="HEAD")
    http_client.request(policy, "http://x")

    assert [c.get_method() for c in calls] == ["HEAD", "GET"]
