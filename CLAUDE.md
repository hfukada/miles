# miles

Strava activity sync and MCP server for querying running data.

## Commands

```bash
uv run miles-auth   # one-time OAuth setup
uv run miles-sync   # sync activities from Strava (--full to ignore last sync date)
uv run miles-mcp    # start MCP server (stdio)
uv run miles-api    # start web UI on http://localhost:8000
uv run pyright      # type check
```

## Type checking

All code must pass `uv run pyright` with zero errors. Follow these practices:

- Annotate all function parameters and return types.
- Use `X | None` instead of `Optional[X]`.
- Use `TypedDict` for structured dicts that cross function boundaries (see `ActivityRow` in `db.py`).
- Use `isinstance` for type narrowing, not `hasattr`.
- Avoid `Any`; if a library type is imprecise, use `cast` or a narrow `assert` at the boundary.
