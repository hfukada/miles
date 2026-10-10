# Design: terrain model, area import, and route search

Status: implemented · 2026-09-28 (decisions in `adr/0003-terrain-model-and-route-search.md`)

## Goal

Answer three kinds of question that the route database (ADR 0002) can't:

- **"How runnable is this, for me?"** A route's grade profile read against the
  athlete's own history: where they actually walk, how fast they descend, and
  how both change over time.
- **"How did I do on that climb/descent?"** A segment of a finished run
  compared with what the athlete's recent history predicts for that grade and
  that point in the run.
- **"I'm staying at X — find me a highly runnable 13 miles with at least 6 on
  trail."** Routes generated from open map data for an area the athlete has
  never uploaded a GPX for, optionally starting from a trailhead within a
  drive-time budget.

The ranking of those answers against a free-text request is an agent's job,
done over MCP. miles supplies deterministic candidates with enough breakdown
for the agent to reason about and explain them.

## Non-goals

- A general-purpose map or navigation product. Turn-by-turn, offline maps on a
  phone, and live traffic are out.
- Seasonal or mud closures, fire closures, and permit rules. Open map data
  doesn't carry them reliably; the agent can mention them, miles won't model
  them.
- Measured traffic volume. Traffic is estimated from road attributes (see
  [Road comfort](#road-comfort)); per-state traffic-count datasets are an open
  question, not part of this design.

## Parts

1. **Terrain model** — the athlete's per-grade performance from their own
   history. Independent of map data; ships first.
2. **Area import** — open map data and terrain-model elevation for an area,
   fetched once and cached aggressively.
3. **Network and scoring** — a trail/road graph built from an area, with
   per-edge grade, surface, technicality, and road comfort.
4. **Route search** — candidate routes over that graph from a start point or a
   set of drive-to trailheads.
5. **MCP tools, skill, and UI.**

Each part is usable on its own: the terrain model scores the routes already in
the database before any area is imported, and `score_route` works on uploaded
GPX before route search exists.

---

## 1. Terrain model

### Data

- **Streams.** Every Strava stream type is requested in one call per
  activity, and the response is stored as returned (gzipped JSON, one file
  per activity, written atomically and replaced only by a refetch): ground
  truth, like an activity's `raw_json`. The DataFrame the model and the activity views use is derived
  from that file on read, so a parsing fix is a rebuild, not a refetch.
  Strava's default read limits (100 requests per 15 minutes, 1,000 per day)
  make a multi-year history a background backfill that runs over a few days;
  it is resumable, newest first, and leaves headroom for regular syncs. It
  also segments any run whose streams were stored by another path (opening an
  activity page stores them), and an activity that fails for its own reasons
  (a 404 on one deleted from Strava) is recorded and skipped, not retried
  forever. A run whose streams yield no segments (indoor, no altitude) is
  recorded as processed with zero segments, once per model version. Errors
  that are not about one activity (an expired token, repeated server errors)
  stop the run instead.
- **Elevation.** Recorded altitude varies by device: barometric altimeters
  are good on grade but drift, and GPS altitude is noisy. Where terrain-model
  tiles cover a track, grade is taken from the terrain model draped on the
  track's positions; recorded altitude is the fallback. The source used is
  stored with each segment so the two can be compared. Only the backfill
  fetches tiles. A rebuild after a model-version bump drapes from tiles
  already cached and flags every run that fell back to recorded altitude, and
  the backfill re-segments those with fetching on.
- **Cadence** is normalized to steps per minute for both feet; Strava's run
  cadence stream counts one foot.

### Segmentation

Each run is resampled to the same 20 m spacing routes use. Each segment
carries grade (over the same ±50 m window routes use), speed, heart rate,
cadence, elapsed time and distance into the run, and the activity's
temperature from the weather table, falling back to the device's own `temp`
stream when there's no weather record.

A segment is **walking** when cadence is below a walk threshold (default 130
spm, editable), falling back to speed when cadence is missing. Stops (the
`moving` stream false) are dropped.

### What the model holds

Per grade bin (2% bins from −30% to +30%, open-ended beyond), over a rolling
window (default 90 days), adjusted for fatigue (elapsed-time bands) and
temperature:

- median speed and grade-adjusted efficiency (speed per heartbeat after the
  existing Minetti cost adjustment)
- walk fraction
- sample count, so thin bins report low confidence rather than a number

Derived thresholds, each with a confidence:

- **walk-onset grade** — the uphill grade where walk fraction crosses 50%
- **comfortable descent grade** — the steepest downhill grade before speed
  stops rising with grade
- **expected speed** `f(grade, fatigue, temperature)` for scoring and
  segment comparison

Snapshots are stored at a fixed cadence (weekly) so the model's history is
itself queryable: "walk onset moved from 12% to 15% since spring".

### Uses

- **Runnability of any profile.** A route's 20 m grade profile read through
  the athlete's thresholds: percent runnable, percent grind (runnable but
  slow), percent hike, longest continuous hike, steep-descent distance. This
  replaces fixed grade bands with the athlete's own.
- **Segment comparison.** A finished run's climbs and descents (the existing
  climb segmentation) against expected speed for their grade, fatigue, and
  temperature, as a residual and a percentile of the athlete's own history
  at that grade.
- **Course gap analysis.** A course's grade histogram weighted by the
  athlete's per-bin deficit gives the grades worth practicing. This is input
  to the plan skill, which owns prescribing; this part only measures.

A model version constant (`TERRAIN_MODEL_VERSION`) triggers a rebuild of
segments and snapshots from cached streams, in the same way
`ROUTE_DERIVE_VERSION` does for routes.

---

## 2. Area import

### Sources

- **Map data:** OpenStreetMap. Ways with `highway` in the running-relevant
  set (paths, tracks, footways, cycleways, bridleways, steps, and every road
  class), plus points of interest: trailheads (`highway=trailhead`), parking,
  drinking water, toilets. Parking is mostly mapped as lot outlines, so an
  area-mapped amenity becomes a point at its center. The source is a
  **regional extract**: a Geofabrik `.osm.pbf` for a region, downloaded once,
  refreshed only on request with a conditional request, and cut with one filtered
  `osmium` pass per import. The regions are chosen automatically: the
  smallest one in Geofabrik's index whose boundary contains the area, or,
  when the area straddles a border and several smaller regions together cover
  it with less ground than that region, those (each is cut and the results
  combined, an OSM element once). The athlete can name them instead. An
  extract over 2 GB is refused, naming the smaller regions that would cover
  the area; the refusal starts no cooldown. A regional file is one download and a local
  pass, where public Overpass servers queue and throttle bulk queries, which
  is why miles does not use them. `miles-area-import --pbf` cuts an area on
  any machine and uploads the tiles, for anywhere without a usable extract or
  regions too large to process on the server.
- **Geocoding:** Nominatim, for "I'm staying at X". Results are cached
  forever, "no match" included, and re-fetched only on request (at most once a
  day per place); its usage policy (one request per second, identifying
  User-Agent, attribution) is followed, and the public instance is refused
  until `MILES_CONTACT` is set.
- **Elevation:** AWS terrain tiles (Terrarium-encoded PNG, global, zoom 13 ≈
  10–19 m); decoding needs Pillow. A tile is validated before it is cached. A
  404 means there is no data there; any other failed fetch raises, so a
  missing tile is never mistaken for an empty one.
- **Drive times:** an OSRM server's table service, for candidate trailheads
  only (section 4).

### Tiles and caching

Everything is fetched and cached per fixed tile, never per request, so
overlapping areas and repeated searches share downloads.

- **Map tiles:** a fixed lat/lng grid (0.05°, about 5.5 km). Each tile's
  payload is Overpass-JSON-shaped (a format miles keeps, not a service it
  calls), stored gzipped with its sha256, source (`extract:<region>`, or the
  regions comma-separated for a tile cut from several extracts; older rows
  may say `overpass`), query version, `fetched_at`, and the per-file
  `inputs` stamps. A way crossing tiles appears whole in each.
  Tiles are ground truth, as raw GPX is in ADR 0002; everything below is
  derived from them.
- **Regional extracts:** registered with their bounds, sha256, fetch time,
  the server's ETag and Last-Modified, and the date their data is as of. An
  import uses, in order: the smallest extract registered from a local file
  whose header bounds contain the area; the regions the athlete names,
  validated against Geofabrik's index (unchecked when the index has never
  been fetched and cannot be); the cheapest set of regions in the index that
  covers the area by boundary, reusing any already registered. A header bbox
  is a rectangle that reaches across borders, so it decides coverage only
  when the index has never been stored or no region in it covers the area.
  **Tile rule:** a tile is always cut afresh from every registered extract
  whose region touches it (by the stored index's polygons, by header bbox for
  a region the index lacks or when none is stored), not only the extracts the
  importing area chose; stored elements are never merged back in, so a way
  deleted upstream leaves the tile when its extract is refreshed and the
  tile is cut again. The tile's source lists those regions, its `fetched_at`
  is the oldest `fetched_at` among them, and its `inputs` record each file's
  `fetched_at`: a tile is current only when its regions are exactly the ones
  touching it now and every file is the one it was cut from. A tile cut from a
  local file (`miles-area-import --pbf`, which has no boundary to consult) is
  built from that file alone and is replaced by the next cut from registered
  extracts, so `--pbf` imports are for places without Geofabrik coverage.
  Each tile is replaced in one `BEGIN IMMEDIATE` transaction. A downloaded
  file is registered only after its size and MD5 check out, the MD5 fetched
  from the URL the body was served from. An area's `data_as_of` is the oldest
  date among the extracts its tiles were cut from.
- **Elevation tiles:** stored as fetched, keyed by provider/zoom/x/y (the provider is always
  `terrarium`). They never expire.
- **Areas** are what the athlete names: a center, a radius, a label, a
  `pinned` flag, and `last_used_at`. An area references the tiles it covers.
  - A **pinned** area (home) is kept until unpinned.
  - An **unpinned** area (travel) is garbage-collected after 60 days unused,
    network included. Tiles are collected only when no remaining area
    references them.
  - Map data never refreshes by age. `import_area(..., refresh=true)` (or
    `miles-area-refresh LABEL`) re-fetches it; the area's status reports
    `data_as_of` and sets `refresh_suggested` once that is over 180 days old.
  - Importing an existing label again leaves its `pinned` flag and `region`
    alone unless the call sets them.
- **Geocodes** are cached by normalized query text forever. **Drive-time tables** are
  cached by input: 180 days for a measured drive time, 7 days for an
  unreachable answer. A drive time is cached only when OSRM answered (an
  unreachable destination is an answer; a failed request is not).

Imports run as a background job, one at a time per process: each is a burst
of requests to shared public services. `import_area` returns an estimate
(source, tile count, seconds once an extract is registered) and
`get_area_status` reports phase and progress. An import that
finds tiles missing from the cache, or can't fetch an elevation tile, ends in
`error` rather than `ready`; one interrupted by a restart is reset to `error`
at startup. From an extract, a city-sized area takes about a minute, most of
it elevation tiles and the network build; the first import in a region also
downloads its extract, which takes minutes. A refresh asks Geofabrik
whether the registered extract is out of date (at most once a day per region;
a 304 keeps the file), and an import whose tiles were already cut from the
extract's current file skips the cut.

Every remote fetch goes through one client (`http_client.py`) that sends an
identifying User-Agent, spaces requests per service, and retries 429/503/504
and connection errors with backoff, honoring `Retry-After`. The limits and the
services' published policies are in [external-services.md](external-services.md).

---

## 3. Network and scoring

### Graph

Built per area from its tiles' raw data, versioned by
`NETWORK_DERIVE_VERSION` and rebuilt when that bumps or a tile refreshes. An
area whose graph predates the current version is rebuilt from its cached tiles
(no downloads) the next time its status is read or a search targets it; the
search refuses with a "rebuilding" error until the new graph is in place.

- **Nodes:** OSM nodes shared by two or more ways, way endpoints, and
  trailheads/amenities spliced into the nearest way (within 300 m) so a route
  can start or pass through them. An amenity set back from the way keeps its
  own position and joins it by a short synthetic access edge (negative way
  id).
- **Edges:** way pieces between nodes, resampled to 20 m with draped
  elevation, stored with a per-direction grade histogram and climb list.
- **Excluded:** `access=private|no`, `foot=no`, motorways, trunk and primary
  roads and their links (kept only as geometry: the proximity measure for
  trails alongside a highway, and, for primary and trunk, where a footway
  counts as a sidewalk), ways under construction. `oneway` on paths is
  honored for direction.

Runnability is **not** baked into edges: an edge stores its grade histogram,
and scoring reads it through the current terrain model at query time. The
model changing never requires a network rebuild.

### Edge attributes

- length, per-direction grade histogram, gain/loss
- **surface class:** paved, unpaved road, trail, sidewalk — from `highway` and
  `surface` (`tracktype` is not read: it grades a track's condition but no
  value implies a hard surface). `highway=footway` is both a sidewalk and a
  trail in OpenStreetMap, so a footway is a sidewalk when tagged as one
  (`footway=sidewalk|crossing|traffic_island`, `sidewalk=*`, `crossing=*`)
  or when nearly all of it runs within a few meters of a street. Tracks are
  not streets for this purpose, so a trail beside a fire road stays a trail.
  Sidewalks score like pavement and never count as trail.
- **technicality:** `sac_scale`, `mtb:scale`, `trail_visibility`, `smoothness`
  where tagged; unknown is reported as unknown, not as easy
- **road comfort** (below)
- **proximity:** distance to the nearest road of class `primary` or above,
  for trails that run alongside one
- nearby water and toilets

### Road comfort

OpenStreetMap has no traffic volume, so comfort is estimated from:

- road class (`primary` … `residential`, `service`, `track`), the strongest
  proxy for traffic
- `maxspeed`, filled with a per-class default when missing (and the fill
  reported)
- lane count, and whether there's a sidewalk or separated cycle path (a
  shoulder is paint beside moving traffic and does not count)

Each road edge gets a comfort tier at query time: **comfortable**,
**tolerable**, or **uncomfortable**. The tier boundaries come from the athlete's preference
profile; the defaults treat a paved road as comfortable at ≤ 35 mph with a
residential/unclassified/tertiary class, an unpaved road as comfortable
unless its class suggests through traffic, and anything ≥ 40 mph or
`secondary` and above as uncomfortable. A sidewalk or separated path lifts
the result one tier (uncomfortable to tolerable, tolerable to comfortable);
it never makes a fast or through road comfortable by itself. `maxspeed` is
read as its first alternative ("50;30" is 50), in mph, km/h (the default
unit), or knots.

**Overrides:** the athlete can mark a specific way comfortable or avoided.
Overrides are stored by OSM way id and survive tile refreshes; they are
athlete data, never derived.

### Preference profile

One stored, editable profile per athlete, read by scoring and search:

- comfort thresholds (speeds, classes)
- maximum continuous distance on uncomfortable road (default 0.5 mi)
- surface preferences (e.g. unpaved road weighted like trail)
- technicality tolerance
- trail-alongside-highway distance and penalty

Continuous exposure is a stateful cost: a quarter mile of uncomfortable road
is fine, a mile isn't, and two quarter miles separated by trail are fine.
Scoring tracks run lengths along the route rather than summing per-edge
costs, and search (section 4) prunes on it.

---

## 4. Route search

### Request

- **start:** a point, a saved place ("home"), or **drive ≤ N minutes** from a
  point
- distance range (or a time range, converted through the terrain model)
- shape: loop, out-and-back, lollipop, or any
- minimum trail distance, minimum runnable percent, gain range
- ways to include (a soft preference: a route that misses one is a near miss)
  and ways to avoid (a hard exclusion, by OSM way id; ways the athlete marked
  "avoid" are always excluded)
- number of candidates (default 5)

### Starts

- **Out the door:** the snapped start point.
- **Drive:** trailheads and trail-adjacent public parking in the area, cut
  first by straight-line distance (what a 120 km/h straight line could cover
  within the budget), ranked by the best score that time could give them, and
  the top few timed with one OSRM table call from the
  lodging (cached by origin cell; flagged straight-line estimates if OSRM is
  down). The drive budget is at most 90 minutes. Starts rank by the trail length
  actually within about 1 km (sidewalks excluded), with a bonus for a named
  trailhead, traded against drive time. The best K (default 8) within the
  budget become starts, after the origin itself; drive time is part of each
  candidate's breakdown.

### Algorithm

On a pure-Python graph, sized for a single-board computer:

1. **Contract** degree-2 chains into single edges, except at a start node;
   this shrinks trail networks by an order of magnitude.
2. **Bound** the graph around each start by half the maximum distance, after
   removing the avoided ways.
3. **Generate** candidates per shape: randomized, beam-limited walks outward
   that turn home when remaining distance approaches the shortest path back
   (loops, bounded by distance rather than step count, since a street grid
   needs many more steps than a trail network for the same distance);
   shortest-path trees to turnaround points (out-and-backs); and lollipops as
   a stem plus a loop. Shortest-path trees are computed once per start and
   cut off at the distance they're needed to.
4. **Evaluate** each candidate in full: runnability through the terrain
   model, surface mix, technicality, stateful road exposure, gain, drive
   time. Candidates violating hard constraints are dropped.
5. **Diversify:** generation continues past N (to a few times N, within the
   time budget) and drops repeats of the same set of edges, whichever way
   they're ridden; then the top N are chosen under a penalty on edges shared
   with already-chosen candidates, so five results aren't five variations of
   one loop.

An area's graph is loaded from SQLite once and kept in memory until its
network is rebuilt, so only the first search after a rebuild pays for it.
Search has a time budget (default 10 s) and returns what it has, with a flag
if the budget cut it short. When no candidate meets every constraint, the
closest few are returned with the constraints they miss, so the agent can
propose a relaxation instead of reporting nothing.

### Results

A candidate is ephemeral: stored in a search-results table with an id and a
TTL, carrying its geometry and full breakdown. Saving one generates a GPX and
a normal `routes` row (source `generated`), after which compare, junctions,
compose, and GPX export all apply. This also closes ADR 0002's connector gap:
a composition can route its connectors over the network instead of taking an
authored estimate.

---

## 5. MCP tools, skill, and UI

### Tools

Terrain model:

- `get_terrain_profile(window_days?)` — per-bin table and thresholds with
  confidence
- `get_terrain_trend(metric, grade_range, since?)` — snapshot series
- `analyze_activity_terrain(activity_id)` — each climb/descent against
  expectation, with percentiles
- `score_route(route_id)` — runnability breakdown of any stored route

Areas:

- `import_area(place | lat/lng, radius_km | drive_minutes, label, pinned?,
  region?)` — starts or reuses a background import, returns an estimate;
  `radius_km` is at most 60
- `list_areas()`, `get_area_status(area_id)`, `pin_area`, `unpin_area`

Search:

- `find_routes(request)` — candidates with breakdowns; `max_drive_minutes`
  switches to drive-to starts
- `find_trailheads(area_id, lat, lng, max_drive_minutes)` — the starts alone
- `get_route_candidate(id)`, `save_route_candidate(id, name)`

Routes:

- `update_route(..., elevation_source)`, `redrape_route(route_id)` — file or
  terrain-model elevation for a stored route

Preferences:

- `get_route_preferences()`, `set_route_preferences(patch)`,
  `set_way_override(way_id, verdict, note?)`

### Skill

A `route-finder` skill in the plugin turns a request into a `find_routes`
call:

- map the athlete's words onto constraints and weights ("highly runnable" →
  a minimum runnable percent; "some trail" → a minimum trail distance), and
  say which it chose
- import the area first if it isn't cached: out the door first, a drive-sized
  area behind it, telling the athlete the estimate and checking back rather
  than going silent
- present two or three candidates with their trade-offs, never one "best"
- on no results, propose the relaxation the misses point to
- link each candidate to the UI and offer GPX

The plan skill reads `get_terrain_profile` and a course's gap analysis when
discussing race-specific practice; the route-finder skill can then search
for terrain matching it.

### UI

- a route page with the route colored run / grind / hike by the athlete's
  thresholds, on the map and the elevation profile
- a Terrain page: per-bin pace and walk fraction, thresholds, and trends
- a climb/descent table on the activity page
- not yet built: road-comfort and technicality map layers, and a candidate
  comparison view for a search result set

## Open questions

- **Traffic data beyond OpenStreetMap.** State traffic-count datasets would
  sharpen road comfort but differ per state. Class-based estimates plus
  overrides come first; per-state counts are a later provider behind the
  same comfort tiers.
- **Walk threshold.** 130 spm is the default and is editable; learning it
  from the athlete's cadence distribution (the valley between walk and run
  peaks) can come later.
- **Directional trails.** `oneway` on paths is sparsely tagged; some
  directional rules exist only on land-manager signage.
- **Performance on small hardware.** On a desktop-class machine a city-sized
  area's search takes a second or two once its graph is loaded; loading a
  graph of a few hundred thousand edges takes a couple of seconds more.
  Single-board computers are unmeasured. Past the per-start bound and the
  graph cache, search can run on a faster machine against an exported area
  (the raw tiles plus the athlete's model and preferences); a compiled graph
  library is the other fallback.
- **OSRM.** The public demo server's usage policy tolerates occasional table
  calls; heavy use would mean self-hosting, which is out of scope.
