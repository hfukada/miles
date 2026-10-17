# ADR 0004 · Section-first route search

Status: accepted · 2026-10-17 · amends the search decision in ADR 0003

## Context

ADR 0003's search generates candidates by randomized walks and shortest-path
turnarounds, then filters them against the request. Constraints such as a
trail-mileage floor only reject candidates; they never steer generation, so
in a street grid with trail some distance away a walk rarely strings enough
trail together, and the search spends its budget on routes it then discards.
Ranking is distance fit plus runnable percentage, so nothing rewards
qualities a runner looks for: a long continuous path along a river,
a connected trail system, a stretch of quiet road without intersections, a
park, a viewpoint.

A runner planning by hand works the other way round. They survey an area for
stretches that look good, judge how hard each is to reach, rule out any that
need a long or unpleasant approach, and only then build a route around the
best of them, preferring a loop when one is as good, and accepting an
out-and-back when its stretch is better.

## Decision

- **Sections are the unit of search.** At network build, an area is
  segmented into sections: maximal stretches worth running as a unit. A
  connected cluster of trails is a trail system; a path following water, a
  park's paths, a named walking, hiking or cycling route, and a road run with
  few intersections and comfortable traffic are each a section. Sections are
  stored per area with a stable id and rebuilt with the network
  (`NETWORK_DERIVE_VERSION`), so the athlete and an agent can refer to one
  across searches.
- **A section stores only what depends on the map.** Length, surface mix,
  continuity (intersections and road crossings per mile), scenery tags, and a
  grade histogram. Scenery tags come from the area's own extract at build time
  and need no further downloads: adjacency to water (rivers, streams, lakes),
  lying within a park or protected area, membership in a named route
  relation, and viewpoints and summits on or near it.
- **Anything that depends on the athlete or the start is computed per query.**
  Runnability is the stored grade histogram read through the current terrain
  model, so it follows terrain-model updates without a rebuild. Familiarity is
  how often the athlete's own activity tracks cover a section, from the raw
  streams already stored. Access cost is a preference-weighted shortest path
  from the start to each section's nearest entry, where uncomfortable road,
  crossings, and steep grades cost more than their length; a section whose
  approach exceeds the athlete's limits is ruled out. In drive mode, access is
  the drive time to the trailheads and parking that reach the best sections,
  timed for those sections only, through the existing cached OSRM path.
- **A survey comes before a search.** A survey tool ranks the sections around
  a start by quality and access and returns them with their traits, so a
  conversation can open with what an area offers before committing to a
  route.
- **Knowledge the map lacks enters as a bounded adjustment.** Each section's
  measured quality is multiplied by an adjustment (bounded, about ×0.5 to
  ×2) that carries what OpenStreetMap can't: the athlete's own verdict, an
  agent's knowledge of a place, or what research found. Adjustments the
  athlete makes or confirms persist with their source (`athlete` or
  `research`) and a note (with a link, for research); they are keyed to the
  section's underlying OSM way ids, as way overrides are, so they survive a
  rebuild that reshapes sections. An agent can also apply an adjustment to a
  single survey or search without storing it. Surveys and results show every
  adjustment with its note, so a ranking can always be explained.
  The bounds keep research and an agent's guesses from burying a section;
  ruling one out is the athlete's call alone. An athlete can **exclude** a
  section: it is never used in a route (as avoided ways are removed from the
  graph), persists with its note keyed the same way, can be undone, and still
  appears in a survey marked excluded.
- **Routes are assembled around chosen sections.** A candidate is an approach,
  one or more sections, and a return: a loop through a section, an
  out-and-back along it, a lollipop, or two nearby sections linked. Explicit
  style requests (trail, flat, highly runnable) filter sections before
  assembly rather than candidates after it. The randomized walk remains as the
  fallback for requests with no section preference.
- **Ranking trades section quality against access cost.**
  A candidate's score is the adjusted quality of the distance spent on
  sections, less its access cost, with a loop bonus (default 7.5%, adjustable per request
  from 0 to about 50%) so a loop wins between otherwise equal routes and a
  better out-and-back still wins over a lesser loop. Scenery counts the same
  everywhere; unfamiliar sections are a filter the request can ask for, not a
  global weight.

## Phases

1. Sections and the survey: keep the scenery tags at build time, segment and
   store sections, add the survey tool with section adjustments (stored and
   per request), and open route conversations with it. An agent can already steer the existing search toward a section through
   `include_way_ids`.
2. Section-anchored candidate assembly and the new ranking.
3. Drive mode over sections and their trailheads.

### Phase 1 as built

- Sections are cut from the graph in the network build's transaction
  (`sections.py`; its docstring has each kind's rule and constant). A way may be
  in sections of different kinds, and a survey reports a twin of a higher-ranked
  section as `same_stretch_as`.
- The cut keeps waterways, water bodies, parks and protected areas (outer rings,
  thinned), summits and viewpoints, and walking or cycling route relations.
  A tile cut at an older version is re-cut from the registered extract on the
  next rebuild, and never re-downloaded.
- The survey's score is `quality x adjustment - access cost`, in
  quality-miles; `survey.py` holds the formula and weights. Adjustments and
  exclusions are keyed by way ids and re-attach to the rebuilt section of the
  same kind that shares at least half of their length in both directions.
- Familiarity is matched from the raw streams in the background and stored per
  section; a rebuild clears it and every activity is matched again.
- Route search is unchanged apart from dropping excluded sections' ways from
  its graph; a section steers it only through `include_way_ids`.

## Consequences

- Network build takes longer and stores more per area (sections and their
  histograms, plus scenery tags), and every area needs one rebuild when the
  version bumps.
- A survey and a search are cheap at query time: the per-query work is one
  weighted shortest-path pass from the start and a histogram product per
  section.
- Scenery is only as good as OpenStreetMap tagging; a beautiful stretch with
  no nearby water, park, route relation, or viewpoint scores on length,
  continuity, and runnability alone.
- Familiarity needs the athlete's streams; it fills in as they are fetched.

## Open questions

- Segmentation rules: where one section ends and the next begins on long
  multi-use paths, and how a trail system that is one huge connected cluster
  is split into units a person would name.
- How far from water or a viewpoint a section may lie and still carry the tag.
- Whether familiarity should also discount sections run very often, or only
  be offered as a filter.
