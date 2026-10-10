"""The gate every Strava request passes through.

Strava reports the remaining daily and 15-minute allowance on every response.
Anything beyond fetching new activities (detail, history backfills) stops at
a reserve of daily calls, so a later sync the same day can still fetch new
activities, and never takes the last calls of the 15-minute window.

The numbers come from the latest response (strava_client), aged by the clock:
an ended window reads as whole, and the daily figure is unknown once the UTC
date changes. An unknown figure never blocks a request.

A Gate belongs to one run or one page request. It raises
StravaAllowanceLowError for a request that would cross a reserve (a
background pass stops; an interactive caller reports "not fetched yet"); a
background gate waits out the 15-minute window instead when only that is low.
A 429 is waited out once; a second one with no success since, or one the
headers say is the daily limit, is final and recorded as the request's outcome.
"""

import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TypeVar

from stravalib.exc import Fault

from . import http_client, strava_client
from .errors import StravaAllowanceLowError

T = TypeVar("T")

RESERVE_ENV = "MILES_STRAVA_RESERVE"
DEFAULT_RESERVE = 12
# Calls left unused in the 15-minute window.
SHORT_WINDOW_RESERVE = 10
WINDOW_MINUTES = 15
_WINDOW_SLACK_S = 5
# The activity listing is the essential work of a sync: reserves never stop it.
LISTING_KIND = "activities"


def default_reserve() -> int:
    """MILES_STRAVA_RESERVE when it is a non-negative integer, else DEFAULT_RESERVE."""
    raw = os.environ.get(RESERVE_ENV, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RESERVE
    return value if value >= 0 else DEFAULT_RESERVE


def wait_for_next_window(on_wait: Callable[[str], None] | None = None) -> None:
    """Sleep until the next 15-minute rate-limit window opens. on_wait gets the
    local "HH:MM" it ends at, for progress reporting."""
    now = datetime.now()
    seconds_into_window = (now.minute % WINDOW_MINUTES) * 60 + now.second
    wait = WINDOW_MINUTES * 60 - seconds_into_window + _WINDOW_SLACK_S
    if on_wait is not None:
        on_wait((now + timedelta(seconds=wait)).strftime("%H:%M"))
    time.sleep(wait)


class Gate:
    def __init__(
        self,
        reserve: int | None = None,
        short_reserve: int = SHORT_WINDOW_RESERVE,
        on_wait: Callable[[str], None] | None = None,
    ) -> None:
        self.reserve = default_reserve() if reserve is None else reserve
        self.short_reserve = short_reserve
        self._on_wait = on_wait
        self._waited_for_429 = False

    def acquire(self, kind: str, interactive: bool = False) -> None:
        """Clear one request of `kind`. Raises StravaAllowanceLowError when the
        daily allowance is at its reserve, or the 15-minute window is nearly
        spent and the request is interactive; otherwise waits for the next window."""
        if kind == LISTING_KIND:
            return
        daily = strava_client.daily_calls_remaining()
        if daily is not None and daily <= self.reserve:
            raise StravaAllowanceLowError(
                f"Strava daily allowance is at its reserve ({daily} calls left, keeping {self.reserve})"
            )
        while self._short_window_low():
            if interactive:
                raise StravaAllowanceLowError("Strava 15-minute allowance is nearly spent")
            wait_for_next_window(self._on_wait)

    def on_429(self, kind: str, interactive: bool = False) -> None:
        """Handle a 429: wait and return so the request repeats, or raise
        StravaAllowanceLowError when waiting cannot help (daily limit spent, or
        a wait already bought nothing) or the caller is interactive. The
        activity listing keeps waiting unless the daily limit is spent, since
        it restarts from its first page. The final answer is recorded as an event."""
        daily = strava_client.daily_calls_remaining()
        if interactive:
            message = "Strava rate limit reached; try again in a few minutes"
        elif (self._waited_for_429 and kind != LISTING_KIND) or (daily is not None and daily <= 0):
            message = "Strava daily limit reached; resets 00:00 UTC"
        else:
            wait_for_next_window(self._on_wait)
            self._waited_for_429 = True
            return
        http_client.report_outcome("strava", 429)
        raise StravaAllowanceLowError(message)

    def call(self, kind: str, request: Callable[[], T], *, interactive: bool = False) -> T:
        """Acquire, run the request, and on a 429 let on_429 decide whether to repeat."""
        while True:
            self.acquire(kind, interactive)
            try:
                result = request()
            except Fault as e:
                if strava_client.fault_status(e) != 429:
                    raise
                self.on_429(kind, interactive)
            else:
                self._waited_for_429 = False
                return result

    def _short_window_low(self) -> bool:
        left = strava_client.short_window_calls_remaining()
        return left is not None and left <= self.short_reserve
