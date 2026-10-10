import errno
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import FakeStravaSession
from stravalib.exc import Fault

from miles import envfile, strava_client


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".env"
    path.write_text("STRAVA_CLIENT_ID=1\nSTRAVA_REFRESH_TOKEN=old-refresh\nOTHER=keep me\n")
    monkeypatch.setattr(strava_client, "ENV_FILE", path)
    monkeypatch.setenv("STRAVA_CLIENT_ID", "1")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", "old-refresh")
    return path


def _refuse_replace(monkeypatch: pytest.MonkeyPatch, code: int = errno.EBUSY) -> None:
    """os.replace in envfile fails with this errno, as a bind-mounted file does."""

    def refuse(src: str, dst: Path) -> None:
        raise OSError(code, "refused")

    monkeypatch.setattr(envfile.os, "replace", refuse)


def _session(monkeypatch: pytest.MonkeyPatch, *, expires_in: float = 21600, refresh: str = "old-refresh") -> FakeStravaSession:
    session = FakeStravaSession({"access_token": "access-1", "refresh_token": refresh, "expires_at": time.time() + expires_in})
    monkeypatch.setattr(strava_client, "_http_session", lambda: session)
    return session


def test_credentials_go_in_the_form_body_and_the_client_carries_only_a_bearer_header(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    session = _session(monkeypatch, expires_in=7000)

    client = strava_client._get_client()

    assert session.posts == [{
        "client_id": "1", "client_secret": "fake-client-secret",
        "refresh_token": "old-refresh", "grant_type": "refresh_token",
    }]
    assert client.protocol.rsession.headers["Authorization"] == "Bearer access-1"
    assert client.protocol.token_expires is not None
    assert abs(client.protocol.token_expires - (time.time() + 7000)) < 5  # the expiry comes from the response
    # stravalib must not be able to refresh on its own: it would send the secret in a query.
    protocol = client.protocol
    assert (protocol.refresh_token, protocol.client_id, protocol.client_secret, protocol.access_token) == (None, None, None, None)


@pytest.mark.parametrize(
    "expires_in, posts",
    [(21600, 1), (strava_client.REFRESH_MARGIN_S - 30, 3)],
    ids=["a fresh token is reused", "a token near expiry is refreshed on every use"],
)
def test_a_token_is_refreshed_only_when_near_expiry(env_file: Path, monkeypatch: pytest.MonkeyPatch, expires_in: float, posts: int):
    session = _session(monkeypatch, expires_in=expires_in)
    for _ in range(3):
        strava_client._get_client()
    assert len(session.posts) == posts


def test_a_rotated_refresh_token_is_written_to_the_loaded_file_and_an_unchanged_one_leaves_it_alone(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    before = env_file.stat().st_mtime_ns
    _session(monkeypatch, refresh="old-refresh")
    strava_client._get_client()
    assert env_file.stat().st_mtime_ns == before

    strava_client._forget_access_token()
    _session(monkeypatch, refresh="new-refresh")
    strava_client._get_client()
    assert env_file.read_text() == "STRAVA_CLIENT_ID=1\nSTRAVA_REFRESH_TOKEN=new-refresh\nOTHER=keep me\n"
    assert os.environ["STRAVA_REFRESH_TOKEN"] == "new-refresh"
    assert [p.name for p in env_file.parent.iterdir()] == [".env"]


def test_the_file_is_replaced_never_truncated(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    env_file.chmod(0o640)
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(envfile.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])

    def replace(src: str, dst: Path) -> None:
        calls.append("replace")
        assert Path(src).parent == dst.parent
        assert dst.read_text().count("old-refresh") == 1  # still the complete old file
        real_replace(src, dst)

    monkeypatch.setattr(envfile.os, "replace", replace)
    envfile.upsert_key(env_file, "STRAVA_REFRESH_TOKEN", "new-refresh")
    assert calls == ["fsync", "replace", "fsync"]  # the last one flushes the directory entry
    assert env_file.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("code", [errno.EBUSY, errno.EXDEV, errno.EPERM])
def test_a_refused_replace_falls_back_to_an_in_place_rewrite_that_shrinks_the_file_and_undoes_a_torn_write(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, code: int
):
    env_file.write_text("STRAVA_REFRESH_TOKEN=" + "a" * 500 + "\nOTHER=1\n")
    env_file.chmod(0o640)
    inode = env_file.stat().st_ino
    _refuse_replace(monkeypatch, code)

    envfile.upsert_key(env_file, "STRAVA_REFRESH_TOKEN", "x")

    assert env_file.read_text() == "STRAVA_REFRESH_TOKEN=x\nOTHER=1\n"
    assert env_file.stat().st_ino == inode
    assert env_file.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in env_file.parent.iterdir()] == [".env"]

    original = env_file.read_bytes()
    real_overwrite = envfile._overwrite
    attempts: list[int] = []

    def torn_write(path: Path, data: bytes) -> None:
        attempts.append(len(data))
        if len(attempts) == 1:
            with open(path, "r+b") as handle:
                handle.write(data[: len(data) // 2])
            raise OSError(errno.EIO, "disk error")
        real_overwrite(path, data)

    monkeypatch.setattr(envfile, "_overwrite", torn_write)
    with pytest.raises(OSError):
        envfile.upsert_key(env_file, "STRAVA_REFRESH_TOKEN", "y" * 200)

    assert env_file.read_bytes() == original  # the original bytes are restored


def test_a_read_only_file_in_a_writable_directory_is_still_replaced(env_file: Path):
    env_file.chmod(0o400)
    envfile.upsert_key(env_file, "STRAVA_REFRESH_TOKEN", "new")
    assert "STRAVA_REFRESH_TOKEN=new" in env_file.read_text()
    assert env_file.stat().st_mode & 0o777 == 0o400


def test_a_failed_replace_or_a_missing_file_keeps_the_old_file_and_warns_without_the_token(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    def refuse(src: str, dst: Path) -> None:
        raise OSError("Device or resource busy")

    monkeypatch.setattr(envfile.os, "replace", refuse)
    _session(monkeypatch, refresh="new-refresh-SECRET")
    with caplog.at_level(logging.WARNING):
        strava_client._get_client()
    assert "old-refresh" in env_file.read_text()
    assert [p.name for p in env_file.parent.iterdir()] == [".env"]
    assert "could not be updated" in caplog.text
    assert "new-refresh-SECRET" not in caplog.text
    assert os.environ["STRAVA_REFRESH_TOKEN"] == "new-refresh-SECRET"

    caplog.clear()  # with no dotenv file at all, a rotation warns the same way
    strava_client._forget_access_token()
    monkeypatch.setattr(strava_client, "ENV_FILE", None)
    _session(monkeypatch, refresh="newer-refresh-SECRET")
    with caplog.at_level(logging.WARNING):
        strava_client._get_client()
    assert "no dotenv file" in caplog.text
    assert "newer-refresh-SECRET" not in caplog.text


def test_a_rejected_access_token_is_dropped(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    unauthorized = SimpleNamespace(
        status_code=401, url="https://www.strava.com/api/v3/athlete", headers={}, request=SimpleNamespace(method="GET")
    )
    session = _session(monkeypatch)
    strava_client._get_client()
    strava_client._on_response(unauthorized)  # type: ignore[arg-type]
    strava_client._get_client()
    assert len(session.posts) == 2


def test_the_refresh_token_in_the_file_wins_over_a_stale_environment(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    env_file.write_text("STRAVA_CLIENT_ID=1\nSTRAVA_REFRESH_TOKEN=rotated\n")
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", "created-with-the-container")
    strava_client._load_environment(env_file)
    assert os.environ["STRAVA_REFRESH_TOKEN"] == "rotated"


def test_a_rejected_refresh_token_is_final_and_recorded_as_401(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[tuple[str, int]]
):
    session = FakeStravaSession(status=400)
    monkeypatch.setattr(strava_client, "_http_session", lambda: session)
    with pytest.raises(Fault) as raised:
        strava_client._get_client()
    assert len(session.posts) == 1
    assert outcomes == [("strava", 401)]
    response = raised.value.response
    assert response is not None and response.status_code == 400
    assert response.request is None  # the Fault carries no request body


def test_a_token_endpoint_429_is_not_recorded_as_a_final_outcome(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[tuple[str, int]]
):
    monkeypatch.setattr(strava_client, "_http_session", lambda: FakeStravaSession(status=429))
    with pytest.raises(Fault):
        strava_client.oauth_token_request({"client_secret": "s"})
    assert outcomes == []
