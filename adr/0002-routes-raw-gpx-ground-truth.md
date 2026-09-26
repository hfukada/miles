# ADR 0002 · Routes: raw GPX as ground truth, no GIS dependency, connectors as a stopgap

Status: accepted · 2026-09-26

## Context

The route database stores uploaded GPX (AllTrails/Garmin/CalTopo exports,
race courses) so the athlete can compare a route to a race course, find
where two routes meet, and stitch pieces of routes into a new run. It's a
second kind of upload-driven data next to activities' Strava-sync-driven
data, and needed its own answers to the same three questions ADR 0001 asked
of plans: what's ground truth, what's derived, and what happens when the
derivation logic changes.

## Decision

- **Raw GPX bytes are ground truth, like a synced activity's `raw_json`.**
  `gpx_files` stores the exact uploaded blob plus its sha256 (so a re-upload
  dedupes instead of duplicating), original filename, the file's own
  `creator` attribute, and upload time. Every `routes` row derived from it —
  resampled points, distance/gain/loss, climbs, grade bands, snapped
  waypoints — is rebuildable from that blob alone and is rebuilt wholesale
  by `derive_all_routes` whenever `ROUTE_DERIVE_VERSION` bumps, the same
  self-healing contract `derive.py` uses for `DERIVE_VERSION`. It is a
  separate version stamp in a separate module (`routes.py`) rather than
  folded into `derive_all`, because nothing about it is Strava-sync-driven;
  running it as part of every `miles-sync` would rebuild routes that never
  changed.
- **name/source/notes/tags are athlete-authored metadata, not derived.**
  They're set once at upload (falling back to the GPX's own embedded track
  name when no name is given) and changed only through `update_route`.
  Re-deriving a route — including the parser-version rebuild above — never
  touches them, the same way re-deriving an activity never touches its
  Strava fields.
- **A file with no `<ele>` on any point gets `has_elevation = false` and
  every gain/grade stat stays NULL, never zero.** A GPX with *some* points
  missing elevation is treated identically to one with none — partial
  elevation can't support gain/grade math any more honestly than absent
  elevation, and averaging over a mix would silently bias the smoothing
  window with fabricated flat spots. DEM enrichment (filling elevation from
  a terrain model instead of the file) is future work and deliberately
  hasn't been started, but the schema already keeps elevation a
  separable layer (`route_points.ele_m`, `routes.has_elevation`) so adding
  it later is a derive-step change, not a schema change.
- **No GIS library.** Junction/overlap detection, shape classification, and
  proximity queries all run on an equirectangular flat-earth approximation
  (accurate to well under a meter at the point spacing and geographic extent
  a single hike or race course spans) plus a coarse lat/lng grid index,
  in pure Python. A real projection library (or a spatial SQLite extension)
  would buy correctness margin this problem doesn't need and would be a
  meaningfully heavier dependency than everything else in this project.
- **Elevation gain is smoothed, then thresholded.** A centered moving
  average removes point-to-point GPS/DEM jitter, then a hysteresis threshold
  counts a climb or descent only once elevation has moved past the noise
  floor. Summing raw deltas inflates gain badly on real exports, because the
  jitter is the same order of magnitude as a gentle grade.
- **A `connector` leg is a distance/gain estimate the athlete supplies, not
  a routed path.** `compose_route` accepts them precisely because most real
  stitched runs include a bit of road or unmapped trail between two GPX
  files, and there is no road-routing capability here — building one (a
  road network graph, a real router) is out of scope here. The limitation is real, not just undocumented: a connector
  contributes distance and (optionally) gain to the composed total, but
  contributes no points to the stitched profile, so it opens a gap in the
  climb/grade-band computation rather than fabricating a climb across a
  stretch with no data. Saving a composed route generates a real GPX from
  only the actual route-leg points (connectors leave no trace in it), so
  the saved route's own re-derived stats measure the straight-line jump
  across each connector gap, not the connector's authored distance/gain —
  the preview and the saved route can read slightly differently for a
  composition that uses connectors. Fixing that properly means routing, not
  a bigger workaround.

## Consequences

- Any parser bug fix or smoothing-constant change is a `ROUTE_DERIVE_VERSION`
  bump and a `derive_all_routes` rebuild; nothing needs re-uploading.
- Elevation stats are honest about their own absence (some mapping tools
  export GPX without elevation) instead of quietly reporting a wrong
  zero.
- Junction/overlap/near queries are approximate at the margins (a fixed
  tolerance in meters, not routing-aware), which is the right trade for
  matching resampled GPX points; this is not a mapping product.
- A composed route that leans on connectors is honestly partial — real
  distance, partial elevation, a visible gap in the saved GPX — rather than
  silently wrong.
