from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from helpers import insert_activity
from stravalib import model

from miles import api, db, errors, strava_budget, strava_client, streams


def _detail(description: str | None, laps: list[dict[str, object]] | None) -> model.DetailedActivity:
    payload: dict[str, object] = {"id": 5, "description": description}
    if laps is not None:
        payload["laps"] = laps
    return model.DetailedActivity.model_validate(payload)


_LAP = {"id": 1, "lap_index": 1, "distance": 1609.0, "moving_time": 300, "elapsed_time": 301, "average_speed": 5.3}


def test_a_detailed_activity_carries_both_description_and_laps_and_no_description_reads_as_empty(monkeypatch: pytest.MonkeyPatch):
    client = SimpleNamespace(get_activity=lambda aid: _detail("steady", [_LAP]))
    monkeypatch.setattr(strava_client, "_get_client", lambda: client)

    description, laps = strava_client.get_activity_detail(5)

    assert description == "steady"
    assert laps is not None and laps[0]["lap_id"] == 1 and laps[0]["activity_id"] == 5

    client.get_activity = lambda aid: _detail(None, None)
    assert strava_client.get_activity_detail(5) == ("", None)


@pytest.fixture
def at_the_reserve(monkeypatch: pytest.MonkeyPatch, strava_allowance) -> None:
    strava_allowance(daily=5)
    monkeypatch.delenv(strava_budget.RESERVE_ENV, raising=False)


def test_interactive_fetches_are_refused_at_the_reserve_and_allowed_above_it(strava_allowance, at_the_reserve):
    gate = strava_budget.Gate()
    with pytest.raises(errors.StravaAllowanceLowError):
        gate.acquire("laps", interactive=True)
    strava_allowance(daily=500)
    gate.acquire("laps", interactive=True)
    strava_allowance(daily=500, short=3)
    with pytest.raises(errors.StravaAllowanceLowError):
        gate.acquire("laps", interactive=True)


@pytest.fixture
def one_run() -> None:
    conn = db.connect()
    insert_activity(conn, 1, start_date="2026-01-05T08:00:00", strava_url="https://example.test/a/1")
    conn.close()


def test_a_fetched_empty_description_is_cached(monkeypatch: pytest.MonkeyPatch, one_run):
    calls: list[int] = []
    monkeypatch.setattr(strava_client, "get_activity_detail", lambda aid: (calls.append(aid), ("", None))[1])
    client = TestClient(api.app)
    client.get("/api/activity-detail", params={"id": 1})
    client.get("/api/activity-detail", params={"id": 1})
    assert calls == [1]


def test_the_activity_pages_spend_no_call_at_the_reserve(monkeypatch: pytest.MonkeyPatch, one_run, at_the_reserve):
    for name in ("get_activity_laps", "get_activity_detail", "get_activity_streams"):
        monkeypatch.setattr(strava_client, name, lambda aid, name=name: pytest.fail(f"spent {name} at the reserve"))
    client = TestClient(api.app)

    body = client.get("/api/activity-detail", params={"id": 1}).json()
    assert body["description"] is None
    assert db.connect().execute("SELECT description FROM activities WHERE activity_id = 1").fetchone()[0] is None

    response = client.get("/api/activity-streams", params={"id": 1})
    assert response.status_code == 503
    assert "allowance" in response.json()["detail"]

    with pytest.raises(errors.StravaAllowanceLowError):
        streams.fetch_and_cache_streams(1)


@pytest.fixture
def no_strava_credentials(monkeypatch: pytest.MonkeyPatch):
    for key in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)


def test_missing_credentials_raise_a_message_naming_the_variables(no_strava_credentials):
    with pytest.raises(errors.StravaNotConfiguredError) as raised:
        strava_client.get_activity_streams(1)

    for name in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN", "MILES_ENV_FILE"):
        assert name in str(raised.value)


def test_a_partly_configured_environment_is_also_not_configured(monkeypatch: pytest.MonkeyPatch, no_strava_credentials):
    monkeypatch.setenv("STRAVA_CLIENT_ID", "1")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "s3cret-value")

    with pytest.raises(errors.StravaNotConfiguredError) as raised:
        strava_client.get_activity_streams(1)

    assert "s3cret-value" not in str(raised.value)


def test_the_streams_endpoint_answers_503_json_without_credentials(one_run, no_strava_credentials):
    response = TestClient(api.app).get("/api/activity-streams", params={"id": 1})

    assert response.status_code == 503
    assert "STRAVA_CLIENT_ID" in response.json()["detail"]


def test_the_sync_cli_exits_with_the_message_and_no_traceback(no_strava_credentials):
    from click.testing import CliRunner

    from miles import sync

    result = CliRunner().invoke(sync.main, [])

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert "STRAVA_CLIENT_ID" in str(result.exception)
