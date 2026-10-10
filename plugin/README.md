# miles plugin

Three coaching personas — `miles`, `miles-plan`, `marathon-analysis` — over a
Strava-backed training record, a `route-finder` skill that searches open map
data for runnable routes, and the `miles` MCP server they all read and write
through.

## Setup

`MILES_MCP_URL` must be set in the environment before Claude Code starts, and
must point at a running miles MCP endpoint (e.g. a `miles-api` container's
`/mcp` path). Without it, the `miles` server entry in `.mcp.json` won't
resolve and the skills will have no data to work from.
