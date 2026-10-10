# miles

Local running training database built from Strava, queryable via Claude Code MCP.

## Quickstart

### 1. Get Strava API credentials

Go to [strava.com/settings/api](https://www.strava.com/settings/api) and create an app.
Set the callback domain to `localhost`.

Copy your Client ID and Client Secret into a `.env` file in this repo:

```
STRAVA_CLIENT_ID=123456
STRAVA_CLIENT_SECRET=abc...
```

### 2. Authorize

```bash
uv run miles-auth
```

Opens a browser, completes the OAuth flow, and writes `STRAVA_REFRESH_TOKEN` into `.env`.
If a key is missing or malformed, this prints exactly which one and points back here instead
of crashing.

Tokens stay out of the database, which is backed up and readable through queries. Access
tokens are short-lived and kept in memory, refreshed when under five minutes from expiry. When
Strava rotates the refresh token, miles rewrites that one line atomically (temp file, fsync,
rename) in the same file it loaded; set `MILES_ENV_FILE` to name that file explicitly,
otherwise the first `.env` found above the package is used. If the file cannot be replaced,
as with a single file bind-mounted into a container (EBUSY), miles rewrites it in place, fully
staged and fsynced first, and restores the original bytes if the write raises. No file lock is
involved: the process that makes outbound calls (see "Running it long-term") is the only writer.
A crash of the process or machine in the middle of an in-place rewrite can still leave the
file with mixed old and new bytes or truncated, so mounting the file's directory and pointing
`MILES_ENV_FILE` into it is preferred: only the atomic rename leaves either the old file or the
new one.
If neither works, miles logs a warning and keeps the new token in memory until restart.
The file is authoritative: its refresh token replaces the one in the process environment at
startup (a container started with `env_file:` keeps its creation-time value otherwise). A
refresh token Strava rejects is final: the failure is recorded as a 401 and the next sync fails
the same way until `miles-auth` is run again (stop `miles-api` first: `miles-auth` takes the same
outbound lock, see "Running it long-term"). The access token is sent in an `Authorization`
header, never in a URL.

### 3. Initial sync

```bash
uv run miles-sync
```

Downloads your full Strava history into `data/activities.db`. Two things to expect on a
first sync of a long history:

- Strava rate-limits at 15-minute windows. When hit, `miles-sync` prints
  `Rate limit hit — sleeping <n>s until next 15-min window...` and resumes automatically —
  this is normal, not a hang. A decade of daily running can take several windows back to back.
- Re-run the same command any time afterwards for an incremental sync (only new activities).
  Use `--full` to ignore the last sync date and re-fetch everything.
- Every sync runs in passes. Pass 1 lists new activities and always runs. Pass 2 fetches laps
  for new workouts and races and streams (for the terrain model) for new runs. Pass 3 fetches
  weather (Open-Meteo, not counted against Strava; paced under its published limits, and a failed request ends the pass until the next sync). Pass 4 trickles in history: laps for older
  runs, then streams and terrain segments, most valuable first, up to `--history-limit`
  (default 900) of each per sync. A year of history takes a few syncs. Passes 2 and 4 read
  Strava's rate-limit headers and stop while `--reserve` daily calls remain (default 12, or
  `MILES_STRAVA_RESERVE`), so a sync later the same day can still pull new activities; none
  takes the last calls of the 15-minute window, waiting for the next one instead. Turn the
  history pass off with `--no-history` or `MILES_SYNC_HISTORY=0`. `--extra`, `--extra-limit`,
  and `--extra-reserve` still work as older names. `miles-terrain-backfill` runs the stream
  part on its own with the same reserve; it and a sync's history pass never run at once (they
  share the backfill's status row), so they cannot spend the same allowance twice.
- The first interactive sync asks once for your max heart rate (Enter to skip). Set it later —
  or set a personal long-run distance floor — with `uv run miles-sync --max-hr 185` /
  `--long-run-floor 14` (updates the profile, rebuilds derived values, and exits; no Strava calls).

### 4. Hook the MCP server into Claude Code

```bash
claude mcp add miles -- uv run --directory /absolute/path/to/this/repo miles-mcp
```

`--directory` gives `uv` an absolute path so the server starts correctly regardless of
Claude Code's working directory. Reload your Claude Code session afterwards — the `miles`
MCP server is picked up automatically.

(A `.mcp.json.example` is also included if you prefer project-scoped config checked in
alongside the repo — copy it to `.mcp.json` and set `cwd` to this repo's absolute path.)

#### Remote over HTTP

`miles-api` also serves the same MCP tools over streamable HTTP at `/mcp` on its port.
If the API runs on another machine (a home server, say), point Claude Code at it — no
local clone, Python, or uv needed on the client:

```bash
claude mcp add --transport http --scope user miles http://<host>:8000/mcp
```

The same command works in PowerShell on Windows. Note the endpoint has no
authentication — expose it only on a network you trust (LAN, Tailscale, etc.),
never the public internet.

The endpoint ships with DNS-rebinding protection that accepts only localhost
`Host` headers by default, so remote clients get `421 Invalid Host header`
until you set `MILES_MCP_ALLOWED_HOSTS` on the server (comma-separated, `:*`
matches any port) listing every name clients connect by:

```bash
MILES_MCP_ALLOWED_HOSTS='localhost:*,127.0.0.1:*,myhost.example.com:*'
```

### 5. Try it

Ask about your training — the `miles:miles` analyst persona picks it up:

- "What was my highest mileage week in 2023?"
- "Show me my workout pace trend over the last 6 months."
- "How did my HR compare across easy runs vs long runs in my last marathon build?"

### Running it long-term

Run **one long-running API process** (`miles-api`). It owns every outbound call: Strava and each
public service share one rate-limit state, one set of per-service throttles, and one Strava
allowance reading inside that process. Have a scheduler trigger the daily sync by calling it,
rather than starting a second `miles-sync` process:

```bash
curl -X POST 'http://miles-host:8000/api/sync?history=true'
```

Without `history=true` the sync skips the history trickle (the Sync button in the UI does this).
Poll `GET /api/sync/status` for progress; `GET /api/health` tells a monitor whether the last
sync worked.

The CLIs (`miles-auth`, `miles-sync`, `miles-terrain-backfill`,
`miles-area-refresh`) and the stdio server (`miles-mcp`) stay for development and self-hosting. Each is a
single-process tool with its own throttle state, so the process that makes outbound calls
takes an exclusive lock file beside the database (`activities.db.outbound.lock`) when it
starts, and any other one refuses to start with a message while it is held. The operating
system drops the lock when the process exits, however it exits. A restart marks any job the
previous process left running as interrupted. Docker Desktop (macOS, Windows) does not share
`flock` between the host and a container, so the lock only guards processes on the same side of
that boundary, and the process id in the message is the container's own.

The database runs in SQLite's WAL mode, so a query or a backup does not block the sync's
writes and the reverse. WAL keeps recent writes in a `-wal` file beside the database and needs
shared memory, so the data directory must be on a local filesystem (not NFS or SMB), and a
read-only mount of it will not open. Back up with `sqlite3 data/activities.db ".backup 'copy.db'"`,
which is safe while the API runs and writes a self-contained file; copying `activities.db` alone
with `cp` can miss writes that are still in the `-wal` file.

## Multiple athletes on one machine

Each athlete needs their own `.env` (tokens) and their own database. Point a second copy
at a different DB file with `MILES_DB`:

```bash
MILES_DB=/path/to/friend.db uv run miles-sync
MILES_DB=/path/to/friend.db uv run miles-mcp
```

Defaults to `data/activities.db` in the repo when unset. When registering a second MCP
server via `claude mcp add`, pass it through with `-e`:

```bash
claude mcp add miles-friend -e MILES_DB=/path/to/friend.db -- uv run --directory /absolute/path/to/this/repo miles-mcp
```

## Web UI

```bash
uv run miles-api
```

Opens a local web interface at `http://localhost:8000` with six pages:

- **Races** — race history and PRs, with a build-comparison chart and dense per-build pace tables broken out by distance (5K/10K/Half/Marathon/50K/Other)
- **Builds** — every detected training build, one row per race, with shape sparklines and volume/workout stats
- **Compare** — put up to four builds (same distance bucket) side by side, chart and stat table
- **Training** — training periods, weekly volume with race markers, and a fitness-trend chart with evidence drill-down
- **Years** — year-by-year volume comparison
- **Plan** — the active training plan vs reality. A **Today** view (today's session, the week at a glance, how the week is going, and a same-weekday comparison to last week) and an **Overall** view (planned-vs-actual weekly chart with adherence bands, color-coded weekly calendar with clickable day details, progression charts, version history); becomes a race retrospective once the goal race is run. Plans are drafted incrementally in conversation via the `miles:miles-plan` skill

## MCP tools

| Tool | Description |
|---|---|
| `get_weekly_mileage` | Miles per ISO week, optional date range |
| `get_activities` | List runs filtered by effective run type, date range |
| `get_training_block` | Aggregate stats for a date range, broken down by run type |
| `get_training_periods` | Detected stretches of consistent training, their gaps, and race-anchored builds |
| `get_consistency_report` | Streaks, gaps, rolling volume and ramp — the "how consistent am I" tool |
| `get_race_history` | Every race at every distance with PR flags, effort labels, pre-race context |
| `get_personal_bests` | Per-distance PRs with full progression |
| `get_race_equivalents` | Riegel cross-distance equivalents; casual-effort races excluded by default |
| `get_race_splits` | Post-race split analysis (negative/even/positive verdict) |
| `get_race_comparison` | Any race's result alongside its pre-race training window |
| `get_marathon_comparison` | All marathon results with 12-week build breakdowns and peak week stats |
| `get_build_snapshot` | Week-by-week view of the build before any race |
| `get_fitness_estimate` | Estimated race paces and training zones as of a date, with confidence |
| `get_fitness_trend` | Monthly estimated-fitness checkpoints over time |
| `get_workout_laps` | Workout sessions with per-lap breakdown, filterable by label |
| `get_workout_session` | Lap-by-lap detail for one workout: lap types and intensity zones |
| `compare_workouts_by_build` | Cross-build comparison of a workout label's rep pace/HR |
| `get_easy_hr_trend` | Monthly easy-run HR and pace — long-term aerobic fitness signal |
| `get_activity_weather` | Hourly weather breakdown for one activity |
| `get_training_plan` | The current training plan (or any version), version history, diffs, and this week's targets vs synced actuals |
| `get_plan_adherence` | Week-by-week plan adherence bands and pattern flags |
| `start_plan_draft` | Open a new mutable plan draft (used by the `miles:miles-plan` planner) |
| `start_revision_draft` | Copy the active plan's current version into a new draft for revision |
| `set_draft_weeks` / `set_draft_days` | Author or edit any subset of a draft incrementally |
| `delete_draft_weeks` / `delete_draft_days` | Retract part of a draft |
| `get_draft` | Draft state with a gap report and sanity warnings |
| `commit_plan` | Freeze and commit the draft after athlete approval; past weeks snapshotted, never rewritten |
| `discard_draft` | Throw a draft away |
| `log_plan_adjustment` | Record day-level reality ("skipped Tue") without a version bump |
| `abandon_plan` | Mark the active plan abandoned, with the reason logged |
| `run_sql` | Ad-hoc read-only SQL against all tables, including derived columns |
| `list_routes` | Stored routes, filterable by distance, ft/mi, a tag, or proximity to a lat/lng |
| `get_route` | Full detail for one route: stats, climbs, grade bands, waypoints, leg definition |
| `find_route_junctions` | Where two routes meet: contact points plus any shared/overlapping stretches |
| `find_routes_near` | Routes near a point or near another route |
| `compose_route` | Stitch route legs (partial spans, reversible) and connector legs into a combined run, optionally saving the result |
| `compare_route_to_course` | Side-by-side ft/mi, grade histograms, climb lists, and descent placement for two routes |
| `update_route` | Edit a route's name/notes/tags |
| `get_outbound_requests` | Requests miles made to each outside service per day, by host, endpoint kind, and status |

`run_type` reflects the label you set in Strava (`easy`, `workout`, `long_run`, `race`);
untagged activities get an inferred type from name, distance, and pace history — your
explicit tags always win.

## Route database

Upload GPX (AllTrails/Garmin/CalTopo exports, race courses) against a running
`miles-api`:

```bash
uv run miles-route-import ridge_loop.gpx --name "Ridge Loop" --source alltrails --tags trail,hilly
uv run miles-route-import course_50k.gpx --name "Spring 50K" --source race --url http://miles-host:8000
```

A multi-track file (a race course plus its aid-to-aid segments, say) becomes
one route per track, named `"<name> — <track name>"`. Raw bytes are ground
truth (`adr/0002-routes-raw-gpx-ground-truth.md`); distance, gain/loss,
climb segments, a grade-band histogram, and snapped waypoints are all
derived and rebuilt automatically if the parser changes. A file with no
`<ele>` anywhere gets `has_elevation = false` and null gain/grade stats,
never a fake zero.

`POST /api/routes/upload` (multipart) is the endpoint behind the CLI above;
`GET /api/routes`, `GET /api/routes/{id}`, `GET /api/routes/{id}/gpx` (raw
bytes), `GET /api/routes/{id}/profile` (resampled points), `GET
/api/routes/junctions`, `GET /api/routes/near` / `GET /api/routes/{id}/near`,
`POST /api/routes/compose`, `GET /api/routes/compare`, plus `PATCH`/`DELETE
/api/routes/{id}`, round out the HTTP surface; the MCP tools above cover the
same ground read-mostly for Claude Code.

## Public services

Area import, geocoding, drive times, elevation, and the map pages call public
services (OpenStreetMap's Nominatim, Geofabrik, an OSRM demo
server, AWS terrain tiles, OpenTopoMap and OpenStreetMap basemap tiles). `docs/external-services.md` lists each
one's usage policy, the limits miles enforces on itself, and the environment
variable that points it at your own instance. Two settings are worth making:

- `MILES_CONTACT`: an email address or URL, appended to the User-Agent so
  service operators can reach you. Geocoding place names against the public
  Nominatim instance is refused until it is set (or `MILES_NOMINATIM_URL` points at
  another instance); coordinates need no geocoding.
- `MILES_TILE_URL`, `MILES_TILE_ATTRIBUTION`, `MILES_TILE_MAX_ZOOM`: replace the
  default OpenTopoMap basemap on the map pages with another tile server.

## Operations

### Health check: `/api/health`

`GET /api/health` answers `200 {"status": "ok"}` when nothing needs attention and
`503 {"status": "attention", "reasons": [...]}` otherwise, so an uptime monitor's HTTP-status
or keyword check can watch it. `?verbose=1` adds the underlying values. It reads only
the database (a few indexed queries), so it gives the same answer whichever process did the work. It
reports:

- the last sync failed, or none has succeeded in `MILES_HEALTH_SYNC_MAX_HOURS` (default 36)
- any request that ended on a 429 from any service in the last 24 hours (a 429 that a retry got
  past does not count)
- the terrain backfill in error while history work remains, a terrain model rebuild in error, or an
  area import that failed in the last 7 days; a run cut off by a restart is not an alert
- Strava rejecting the credentials, until a later sync succeeds

### Metrics: `/metrics`

Off by default: set `MILES_METRICS=1` and `GET /metrics` serves the Prometheus text format
(it is a 404 otherwise). Series:

- `miles_outbound_requests_total{service,host,kind,status}`: every outbound request, from a durable
  per-day ledger (below), so it survives restarts and includes CLI runs. `status` is the HTTP
  code, or `error` for a connection failure; `kind` is the Strava endpoint (`activities`,
  `detail`, `laps`, `streams`, `token`, `other`) and empty for other services
- `miles_strava_daily_calls_remaining` and `miles_strava_short_window_calls_remaining`: the
  allowance in the latest Strava response any process saw
- `miles_sync_last_success_timestamp_seconds`, `miles_sync_last_failure_timestamp_seconds`,
  `miles_sync_last_duration_seconds`, `miles_sync_last_new_activities`, and
  `miles_backfill_remaining{kind="laps"|"streams"}`
- `miles_http_retries_total{service}` and `miles_http_wait_seconds_total{service}` (time spent
  in a throttle interval or backoff), and `miles_cache_requests_total{cache,result}` for
  `geocode`, `drive_time`, `map_tiles`, `elevation_tiles`, and `extract` (`hit`, `miss`,
  `not_modified`)

The retry, wait, and cache series are counters of the serving process only: the API server, the
stdio MCP server, and each CLI run count separately, and a counter restarts at zero when its
process does. Everything else is read from the database at scrape time, so it covers runs by any
process. The registry is a small module (`miles/metrics.py`) rather than `prometheus_client`, to
avoid a dependency for a few dozen series.

### Outbound request ledger

Each process that serves or runs miles (the API server, the MCP server, `miles-sync`, the
backfill and refresh CLIs) records every outbound request in the `outbound_requests` table: one
row per day, service, host, endpoint kind, and status, with a count. The hostname is the one
actually requested, so a self-hosted override appears as its own host. Rows older than 400 days
are dropped. A scheduled `miles-sync` that exits in a minute is counted the same as the server.

- `GET /api/outbound?days=30` returns the per-day rows for the last N days.
- The `get_outbound_requests` MCP tool returns the same, so "how many requests did we make to
  each service this month" is a question to ask directly.
- A request that ends on a 429 (after any retries) or a Strava credential rejection also leaves a
  timestamped row for a week, which is what the health check's 24-hour window reads. The
  per-day counts include every attempt, retried ones too.
