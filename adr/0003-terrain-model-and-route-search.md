# ADR 0003 · Terrain model, area import, and route search

Status: accepted · 2026-09-27

Design: `docs/design-terrain-and-route-search.md`.

## Context

The route database (ADR 0002) answers questions about GPX the athlete already
has. Finding a run in an unfamiliar area, or judging how runnable a route is
for this particular athlete, needs two things it doesn't have: a model of the
athlete's own performance by grade, and a routable trail/road network built
from open map data.

## Decision

- **Raw downloads are ground truth, as raw GPX is in ADR 0002.** Map data is
  stored gzipped per fixed 0.05° tile with its sha256, query version, source,
  and cut time; elevation tiles are stored as fetched. The network
  graph is derived from them and rebuilt when `NETWORK_DERIVE_VERSION` bumps
  or a tile refreshes. Downloads are shared across areas by tile, so
  overlapping and repeated searches never refetch. Pinned areas are kept;
  unpinned areas expire. Nothing refreshes by age: an area is refreshed when
  asked, and reports the date its data is as of.
  **Amendment:** regional extracts are the only map source. Public Overpass
  servers queue and throttle bulk tile queries, so an import cuts its tiles
  from a Geofabrik `.osm.pbf` (one filtered `osmium` pass), downloaded once,
  verified against its published MD5 and registered with its bounds, then
  refreshed only on request. The regions are the cheapest cover of the area
  in Geofabrik's region index: the smallest region whose boundary contains it,
  or, at a border where a large parent would otherwise be chosen, the smaller
  regions that together cover it, each cut and the results merged with every
  OSM element kept once. The athlete never has to name a region; an explicit
  one is validated against the index. The tile payload keeps its
  Overpass-JSON shape and records its source (every extract it was cut
  from: all the registered ones touching the tile, except a region whose
  descendant contains the whole tile), so the tile stays the ground truth. An area
  where no usable extract exists, or one too large to cut on the server, is
  cut elsewhere with `miles-area-import --pbf` and uploaded as tiles.
- **The athlete's terrain model is derived from their own streams.** Runs are
  resampled to 20 m segments; per-grade-bin speed, efficiency, and walk
  fraction over a rolling window give a walk-onset grade, a comfortable
  descent grade, and an expected speed by grade, fatigue, and temperature.
  `TERRAIN_MODEL_VERSION` gates a rebuild from cached streams. Grade comes
  from terrain-model elevation draped on the track where tiles are cached,
  recorded altitude otherwise.
  **2026-09-28 amendment:** raw streams are ground truth. Every stream type
  is requested in the one call an activity costs, and the response is kept
  as returned (gzipped JSON per activity, replaced only by a refetch), like an
  activity's `raw_json`. The DataFrame is derived on read; the reshaped
  parquet cache is gone, since aligning streams of different lengths before
  storing them lost information a later fix couldn't recover.
- **Runnability and road comfort are scored at query time, not baked into the
  graph.** Edges store their raw inputs (grade profile, road class, speed,
  lanes, separation, surface, technicality tags); scoring reads them through
  the current terrain model and the athlete's preference profile. Fitness
  changing or a preference edit takes effect on the next search with no
  rebuild.
  **2026-09-28 amendment:** surface class is the exception that is decided
  at build time. `highway=footway` covers both sidewalks and trails, and
  telling them apart needs the surrounding roads, so the build classifies a
  footway as `sidewalk` from its tags or from running alongside a road.
  Sidewalks never count as trail.
- **Traffic is estimated, not measured.** OpenStreetMap carries no traffic
  volume; comfort tiers come from road class, speed limit (filled per class
  when missing), lanes, and separation, corrected by athlete overrides stored
  by OSM way id. Continuous exposure to uncomfortable road is a stateful
  constraint: a run of it longer than the athlete's limit rejects a route.
- **Search is heuristic, pure Python, and returns several diverse
  candidates.** Contracted graph, bounded around each start, randomized
  beam-limited generation per shape, full evaluation, a shared-edge penalty
  for diversity, a time budget, and near misses with the constraints they
  miss when nothing qualifies. Results are reproducible per request. Ranking
  a free-text request against the candidates is left to an agent over MCP.
- **Candidates are ephemeral until saved.** They live in a TTL'd table; saving
  one writes a GPX and a normal route, so everything in ADR 0002 applies.
- **Drive-to starts** are trailheads and trail-adjacent public parking, timed
  with one OSRM table call per search (only the best-ranked candidates, with
  flagged straight-line estimates if OSRM is down) and cached by origin
  cell. An
  amenity off the trail keeps its surveyed position and joins the network by
  a short synthetic access edge (negative way id).
- **Network access never blocks a request path.** Route and activity
  derivation read only cached elevation tiles; fetching happens in background
  imports, the stream backfill, or an explicit redrape.

## Consequences

- An area's first import is a background job (about a minute from an
  extract already on disk, minutes when its extract must be downloaded); later searches in it are local.
- Road comfort will be wrong where road class misleads (a busy unclassified
  road); overrides are the correction until a measured traffic source exists.
- Performance on small hardware is unmeasured for large areas; every search
  input is a portable artifact, so search can move to a faster machine
  without changing the data model.
