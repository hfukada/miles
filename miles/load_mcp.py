"""MCP tools for intensity distribution and training load (intensity.py, load.py)."""

import json

from mcp.server.fastmcp import FastMCP

from . import db
from .derive import connect_derived
from .intensity import intensity_distribution
from .load import load_series


def register_load_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def get_intensity_distribution(weeks: int = 12) -> str:
        """
        Weekly easy/moderate/hard running time (seconds), two ways side by side:
        `hr` (by % of max HR: easy < 80%, moderate 80-88%, hard >= 88%; 20 m
        stream samples where synced, else the run's average HR) and `pace`
        (each lap's heat-adjusted pace vs the zones of the prior month's fitness
        checkpoint: slower than MP band = easy, MP band = moderate, threshold and
        faster = hard). `totals` sums the window. HR lags short reps and drifts
        up over long or hot runs; pace ignores hills — when the two disagree,
        say so rather than picking one. `unclassified` is time with no HR (hr)
        or no checkpoint (pace).
        """
        weeks = max(1, min(weeks, 156))
        with db.connection(connect_derived) as conn:
            return json.dumps(intensity_distribution(conn, weeks))

    @mcp.tool()
    def get_training_load(weeks: int = 8) -> str:
        """
        Daily HR load (Edwards TRIMP: minutes in each 10%-of-max-HR band
        weighted 1-5) from runs plus non-run activities (climbing, lifting,
        skiing...; scored from average HR, so intermittent efforts read low),
        split per day as `load_run` / `load_other`, with injury-risk signals per day: `acwr` (last 7 days of
        load / 28-day weekly average; bands low < 0.8, optimal 0.8-1.3, caution
        1.3-1.5, high > 1.5), `monotony` (7-day mean / sd of daily load; > 2 is
        flagged in the literature) and `strain` (7-day load x monotony).
        `current` is today. Ratios are null until 28 days of history exist.
        These are population heuristics with contested evidence, not a
        diagnosis — cite the number and band, never predict an injury.
        `estimated_runs` counts runs without HR charged the median rate;
        `unscored_other` counts non-run activities without HR (no load).
        """
        weeks = max(1, min(weeks, 156))
        with db.connection(connect_derived) as conn:
            return json.dumps(load_series(conn, weeks))
