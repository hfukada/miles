# ADR 0003 · Terrain model, area import, and route search

Status: accepted · 2026-09-27

## Context

The route database (ADR 0002) answers questions about GPX the athlete already
has. Finding a run in an unfamiliar area, or judging how runnable a route is
for this particular athlete, needs two things it doesn't have: a model of the
athlete's own performance by grade, and a routable trail/road network built
from open map data. The ranking of candidates against a free-text request is
an agent's job over MCP; miles supplies deterministic candidates with enough
breakdown to reason about and explain them.

Out of scope: turn-by-turn navigation, seasonal or permit closures (open map
data doesn't carry them reliably), and measured traffic volume.

## Decision

- **Raw downloads are ground truth, as raw GPX is in ADR 0002.** Map data is
  stored gzipped per fixed 0.05° tile with its sha256, query version, source,
  and cut time; elevation tiles are stored as fetched. The network
  graph is derived from them and rebuilt when `NETWORK_DERIVE_VERSION` bumps
  or a tile refreshes (from the cached tiles, no downloads; a search of an
  area being rebuilt is refused until the new graph is in place). Downloads are shared across areas by tile, so
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
  descendant contains the whole tile), so the tile stays the ground truth.
  A tile is always cut afresh from those extracts, never merged with its
  stored elements, so a way deleted upstream leaves the tile when its extract
  is refreshed; it is current only when its regions and each file's
  `fetched_at` are the ones it was cut from. An area
  where no usable extract exists, or one too large to cut on the server, is
  cut elsewhere with `miles-area-import --pbf` and uploaded as tiles.
- **The athlete's terrain model is derived from their own streams.** Runs are
  resampled to 20 m segments; per-grade-bin speed, efficiency, and walk
  fraction over a rolling window give a walk-onset grade, a comfortable
  descent grade, and an expected speed by grade, fatigue, and temperature.
  Weekly snapshots keep the model's own history queryable.
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
  lanes, separation, surface); scoring reads them through
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
- **Path elevation is smoothed over the whole path, as a stored route's is.**
  Edges keep the terrain model's elevation unsmoothed, because the smoothing
  window is chosen from a profile's roughness (`gpx_parse.roughness_smoothed`)
  and most edges are too short to measure one. `score_path` joins consecutive
  edges into one profile and smooths that, so a candidate's gain, loss, and
  grades equal those of the route saved from it.
- **Search is heuristic, pure Python, and returns several diverse
  candidates.** Degree-2 chains are contracted (except at a start), the graph
  is bounded around each start by half the maximum distance after removing
  avoided ways, and candidates are generated per shape: randomized,
  beam-limited walks that turn home as the remaining distance nears the
  shortest way back (bounded by distance, since a street grid needs many more
  steps than a trail network), shortest-path trees to turnaround points, and
  lollipops as a stem plus a loop. Each is evaluated in full and dropped on a
  hard-constraint violation; generation continues past N, repeats of the same
  edge set are dropped, and the top N are chosen under a penalty on edges
  shared with those already chosen. A time budget bounds the search, and when
  nothing qualifies the closest few come back with the constraints they miss.
  Results are reproducible per request.
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

## Open questions

- Traffic data beyond OpenStreetMap: per-state traffic counts would sharpen
  road comfort but differ per state; class-based estimates plus overrides come
  first, with counts a later provider behind the same tiers.
- The walk threshold (130 steps per minute) is a constant; it could be learned
  from the valley in the athlete's cadence distribution.
- `oneway` on paths is sparsely tagged; some directional rules exist only on
  land-manager signage.
- Edge technicality (`sac_scale`, `smoothness`), distance to a major road for
  trails alongside one, and nearby water are not stored, since nothing reads
  them.
