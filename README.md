# miles

Local running training database built from Strava, queryable via Claude Code MCP.

## Setup

### 1. Get Strava API credentials

Go to [strava.com/settings/api](https://www.strava.com/settings/api) and create an app. Set the callback domain to `localhost`.

Copy your credentials into a `.env` file:

```
STRAVA_CLIENT_ID=123456
STRAVA_CLIENT_SECRET=abc...
```

### 2. Authorize

```bash
uv run miles-auth
```

Opens a browser, completes the OAuth flow, and writes tokens to `.env`.

### 3. Initial sync

```bash
uv run miles-sync
```

Downloads your full Strava history into `data/activities.db`. For a decade of daily running this takes a few minutes.

### 4. Incremental sync

Run the same command any time to pull only new activities:

```bash
uv run miles-sync
```

Force a full re-sync if needed:

```bash
uv run miles-sync --full
```

### 5. Configure the MCP server

Copy the example config and update the path:

```bash
cp .mcp.json.example .mcp.json
# edit .mcp.json and set "cwd" to the absolute path of this repo
```

## Querying with Claude Code

Reload your Claude Code session after the initial sync — the `miles` MCP server will be picked up from `.mcp.json` automatically.

You can then ask things like:

- "What was my highest mileage week in 2023?"
- "Show me my workout pace trend over the last 6 months."
- "How did my HR compare across easy runs vs long runs in my last marathon build?"

## Web UI

```bash
uv run miles-api
```

Opens a local web interface at `http://localhost:8000` with two tables:

- **Build Comparison** — each marathon with its 12-week build stats (volume, workout quality, long run count, easy HR)
- **Peak Weeks** — peak single week and rolling 3-week average for each build

## MCP tools

| Tool | Description |
|---|---|
| `get_weekly_mileage` | Miles per ISO week, optional date range |
| `get_activities` | List runs filtered by `run_type`, date range |
| `get_training_block` | Aggregate stats for a date range, broken down by run type |
| `get_marathon_comparison` | All marathon results with 12-week build breakdowns and peak week stats |
| `run_sql` | Ad-hoc read-only SQL against the activities table |

`run_type` values reflect the label you set in Strava: `easy`, `workout`, `long_run`, `race`.
