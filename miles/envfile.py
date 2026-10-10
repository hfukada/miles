"""Updating one `KEY=value` line of a dotenv file without losing the rest of it.

The process that makes outbound calls (see outbound.py) is the only writer, so
no locking is involved: the new content is staged in a temporary file and
moved over the original.
"""

import contextlib
import errno
import os
import stat
import tempfile
from pathlib import Path

# os.replace failures that mean "this path cannot be swapped out from under its
# mount" rather than "the write failed": a single-file bind mount, a
# cross-device temp file, or a directory the process may not rename in.
_REPLACE_REFUSED = {errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES}


def upsert_key(path: Path, key: str, value: str) -> None:
    """Set `key` to `value` in the dotenv file at `path`, creating the file
    (mode 0600) if it is missing. The new content is written to a temporary
    file in the same directory, flushed to disk, and moved over the original,
    so a crash leaves either the old file or the new one.

    When the path cannot be replaced (a single-file Docker bind mount fails
    with EBUSY), the content is rewritten in place instead. An exception during
    that write restores the original bytes. A crash of the process or machine
    in the middle of it can still leave a file that mixes old and new bytes, or
    is truncated; mount the file's directory and point MILES_ENV_FILE into it
    where possible. Raises OSError when the file cannot be written."""
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
    """Flush the directory entry the rename created, so the new file survives
    a power loss and not just a process crash. Some filesystems refuse to
    fsync a directory; the rename has already happened by then, so that is
    ignored."""
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
    """Overwrite the file with `data`. If the write raises, put `original`
    back before re-raising, so the caller never sees a half-copied file."""
    try:
        _overwrite(path, data)
    except BaseException:
        with contextlib.suppress(OSError):
            _overwrite(path, original)
        raise
