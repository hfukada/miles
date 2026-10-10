"""Updating one `KEY=value` line of a dotenv file without losing the rest of it.

The process that makes outbound calls (see outbound.py) is the only writer, so
no locking is needed.
"""

import contextlib
import errno
import os
import stat
import tempfile
from pathlib import Path

# os.replace failures meaning the path cannot be swapped (single-file bind
# mount, cross-device temp file, no rename permission) rather than a failed write.
_REPLACE_REFUSED = {errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES}


def upsert_key(path: Path, key: str, value: str) -> None:
    """Set `key` to `value` in the dotenv file at `path`, creating it (mode
    0600) if missing. Content goes to a temporary file in the same directory,
    is flushed, and replaces the original, so a crash leaves the old file or
    the new one.

    When the path cannot be replaced (a single-file bind mount fails with
    EBUSY) the content is rewritten in place; an exception restores the
    original bytes, but a crash mid-write can leave a mixed or truncated file,
    so prefer mounting the file's directory and pointing MILES_ENV_FILE into
    it. Raises OSError when the file cannot be written."""
    try:
        original = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
        exists = True
    except FileNotFoundError:
        original, mode, exists = b"", 0o600, False

    lines = original.decode().splitlines()
    prefix = f"{key}="
    if any(line.startswith(prefix) for line in lines):
        lines = [f"{prefix}{value}" if line.startswith(prefix) else line for line in lines]
    else:
        lines.append(f"{prefix}{value}")
    content = ("\n".join(lines) + "\n").encode()

    try:
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    except OSError:
        # A read-only directory around a bind-mounted file: stage elsewhere
        # and rewrite in place.
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as staged:
            staged.write(content)
            staged.flush()
            os.fsync(staged.fileno())
        os.chmod(temp_name, mode)
        try:
            os.replace(temp_name, path)
        except OSError as exc:
            if exc.errno not in _REPLACE_REFUSED or not exists:
                raise
            try:
                _rewrite_in_place(path, content, original)
            except PermissionError:
                raise exc from None
        else:
            _sync_directory(path.parent)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)


def _sync_directory(directory: Path) -> None:
    """Flush the rename's directory entry so it survives power loss. A
    filesystem that refuses to fsync a directory is ignored."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _overwrite(path: Path, data: bytes) -> None:
    with open(path, "r+b") as handle:
        handle.write(data)
        handle.truncate(len(data))
        handle.flush()
        os.fsync(handle.fileno())


def _rewrite_in_place(path: Path, data: bytes, original: bytes) -> None:
    """Overwrite the file with `data`; if the write raises, restore `original`."""
    try:
        _overwrite(path, data)
    except BaseException:
        with contextlib.suppress(OSError):
            _overwrite(path, original)
        raise
