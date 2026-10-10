import errno
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from stravalib.exc import Fault

from miles import envfile, strava_client


class _Response:
    ok = True
    status_code = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class _Session:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.posts: list[dict[str, str]] = []

    def post(self, url: str, **kwargs: object) -> _Response:
        assert "?" not in url
        self.posts.append(kwargs["data"])  # type: ignore[arg-type]
        return _Response(self.payload)


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


def _session(monkeypatch: pytest.MonkeyPatch, *, expires_in: float = 21600, refresh: str = "old-refresh") -> _Session:
    session = _Session({"access_token": "access-1", "refresh_token": refresh, "expires_at": time.time() + expires_in})
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


def test_a_fresh_token_is_reused(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    session = _session(monkeypatch, expires_in=21600)
    for _ in range(3):
        strava_client._get_client()
    assert len(session.posts) == 1


def test_a_token_near_expiry_is_refreshed_on_every_use(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    session = _session(monkeypatch, expires_in=strava_client.REFRESH_MARGIN_S - 30)
    for _ in range(2):
        strava_client._get_client()
    assert len(session.posts) == 2


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
def test_a_refused_replace_falls_back_to_an_in_place_rewrite(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, code: int
):
    env_file.chmod(0o640)
    inode = env_file.stat().st_ino
    _refuse_replace(monkeypatch, code)
    envfile.upsert_key(env_file, "STRAVA_REFRESH_TOKEN", "x")
    text = env_file.read_text()
    assert "STRAVA_REFRESH_TOKEN=x\n" in text and "old-refresh" not in text
    assert env_file.stat().st_ino == inode
    assert env_file.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in env_file.parent.iterdir()] == [".env"]


def test_the_in_place_rewrite_shrinks_a_longer_file(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    env_file.write_text("STRAVA_REFRESH_TOKEN=" + "a" * 500 + "\nOTHER=1\n")
    _refuse_replace(monkeypatch)
    envfile.upsert_key(env_file, "STRAVA_REFRESH_TOKEN", "b")
    assert env_file.read_text() == "STRAVA_REFRESH_TOKEN=b\nOTHER=1\n"


def test_a_failure_during_the_in_place_write_restores_the_original_bytes(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    original = env_file.read_bytes()
    _refuse_replace(monkeypatch)
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
        envfile.upsert_key(env_file, "STRAVA_REFRESH_TOKEN", "x" * 200)

    assert env_file.read_bytes() == original


def test_a_read_only_file_in_a_writable_directory_is_still_replaced(env_file: Path):
    env_file.chmod(0o400)
    envfile.upsert_key(env_file, "STRAVA_REFRESH_TOKEN", "new")
    assert "STRAVA_REFRESH_TOKEN=new" in env_file.read_text()
    assert env_file.stat().st_mode & 0o777 == 0o400


def test_a_failed_replace_keeps_the_old_file_and_warns_without_the_token(
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


def test_with_no_env_file_a_rotation_warns_without_the_token(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    monkeypatch.setattr(strava_client, "ENV_FILE", None)
    _session(monkeypatch, refresh="new-refresh-SECRET")
    with caplog.at_level(logging.WARNING):
        strava_client._get_client()
    assert "no dotenv file" in caplog.text
    assert "new-refresh-SECRET" not in caplog.text


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


def test_a_file_without_a_refresh_token_leaves_the_environment_alone(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    env_file.write_text("STRAVA_CLIENT_ID=1\n")
    strava_client._load_environment(env_file)
    assert os.environ["STRAVA_REFRESH_TOKEN"] == "old-refresh"


class _Rejected:
    ok = False

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_a_rejected_refresh_token_is_final_and_recorded_as_401(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[tuple[str, int]]
):
    posts: list[object] = []

    def post(self: object, url: str, **kwargs: object) -> _Rejected:
        posts.append(kwargs["data"])
        return _Rejected(400)

    monkeypatch.setattr(strava_client, "_http_session", lambda: type("S", (), {"post": post})())
    with pytest.raises(Fault):
        strava_client._get_client()
    assert len(posts) == 1
    assert outcomes == [("strava", 401)]


def test_a_token_endpoint_429_is_not_recorded_as_a_final_outcome(
    env_file: Path, monkeypatch: pytest.MonkeyPatch, outcomes: list[tuple[str, int]]
):
    monkeypatch.setattr(strava_client, "_http_session", lambda: type("S", (), {"post": lambda self, url, **kw: _Rejected(429)})())
    with pytest.raises(Fault):
        strava_client.oauth_token_request({"client_secret": "s"})
    assert outcomes == []


def test_a_rejected_token_request_carries_no_request_body_on_its_fault(env_file: Path, monkeypatch: pytest.MonkeyPatch):
    class _WithRequest(_Rejected):
        request = object()

    monkeypatch.setattr(strava_client, "_http_session", lambda: type("S", (), {"post": lambda self, url, **kw: _WithRequest(400)})())
    with pytest.raises(Fault) as raised:
        strava_client.oauth_token_request({"client_secret": "s"})
    response = raised.value.response
    assert response is not None and response.status_code == 400
    assert response.request is None


def test_an_unreadable_env_file_is_reported_by_error_type_and_does_not_crash(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING):
        strava_client._load_environment(tmp_path)  # a directory: reading it raises

    assert "could not read the Strava env file (IsADirectoryError)" in caplog.text
