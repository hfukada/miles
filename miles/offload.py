"""Keeping blocking work off the event loop.

FastMCP calls a synchronous tool function directly on the event loop, and the
API serves MCP and HTTP from one loop, so a tool that waits on a public
service (geocoding, OSRM, an elevation fetch) would stall every other
request for as long as that takes. `threaded` turns such a tool into a
coroutine that runs it on a worker thread. A tool built this way opens its
own database connection inside the call and closes it before returning, so
the connection lives and dies on one thread.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import anyio.to_thread

P = ParamSpec("P")
R = TypeVar("R")


def threaded(fn: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    """`fn` as a coroutine function running it in a worker thread, with its
    name, docstring, and signature intact so FastMCP builds the same schema."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper  # type: ignore[return-value]
