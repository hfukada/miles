"""Exception text that is safe to store, log, or return.

Some HTTP libraries put the full request URL, query string included, in their
exception messages, and the query can carry credentials. Every catch-all that
keeps or shows the text of an arbitrary exception (a stored job error, an MCP
tool's error reply) goes through describe_error(). Messages of exceptions
this package raises itself are written without credentials and shown as they
are.
"""

import functools
import json
import re
from collections.abc import Callable
from typing import ParamSpec

P = ParamSpec("P")

# Query-string and form-style `name=value` pairs that carry credentials.
_SECRET_PAIR = re.compile(r"\b(client_secret|refresh_token|access_token|code|key|token)=['\"]?[^&\s'\"]+", re.IGNORECASE)

MAX_LENGTH = 200


class StravaUnavailableError(RuntimeError):
    """A Strava request failed before any response arrived (connection, proxy,
    TLS, timeout). The message names the kind of request and the failure
    class, never the URL."""


class StravaNotConfiguredError(RuntimeError):
    """The Strava application credentials or refresh token are missing from
    the environment. The message names the variables, never their values."""

    def __init__(self) -> None:
        super().__init__(
            "Strava credentials are not configured: set STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, "
            "STRAVA_REFRESH_TOKEN (see MILES_ENV_FILE)"
        )


class StravaAllowanceLowError(RuntimeError):
    """A fetch made on a user's behalf was not attempted because Strava's
    allowance is at its reserve; the data is "not fetched yet", not missing."""


def redact(text: str) -> str:
    """`text` with the value of every credential-looking `name=value` pair
    replaced by `[redacted]`."""
    return _SECRET_PAIR.sub(lambda m: f"{m.group(1)}=[redacted]", text)


def describe_error(e: BaseException, limit: int | None = MAX_LENGTH) -> str:
    """`ExceptionName: message`, redacted and cut to `limit` characters (None
    for no cut)."""
    text = redact(f"{type(e).__name__}: {e}")
    return text if limit is None else text[:limit]


def reporting(*errors: type[Exception]) -> Callable[[Callable[P, str]], Callable[P, str]]:
    """Decorator for an MCP tool: answer `{"error": str(e)}` for any of `errors`
    raised in the tool body, rather than failing the call. The tool body should
    hold little besides its domain call, so a listed type cannot come from
    anywhere else, and the listed types' messages must be written by this
    package."""

    def decorate(tool: Callable[P, str]) -> Callable[P, str]:
        @functools.wraps(tool)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
            try:
                return tool(*args, **kwargs)
            except errors as e:
                return json.dumps({"error": str(e)})

        return wrapper

    return decorate
