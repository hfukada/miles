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
  out-and-back along it, or a lollipop. Explicit
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
  in sections of different kinds.
- Sections are two-level. A **group** is everything that covers one stretch of
  ground, so a riverside path that is a trail system, a water path, and a named
  route at once is listed once; a **stretch** is one cut of a group, the unit a
  runner picks between and routes are built along. Both are rows of
  `net_sections` (`level`, and `group_id` on a stretch), and a survey ranks groups
  (`detail="overview"`) or stretches (`detail="detail"`, optionally `within` one
  group). Drafts of different kinds that share at least half of the shorter one's
  length join one group, transitively. A group stores the union of its drafts' edges,
  every contributing kind, and a `parallel_m` measure of paths running beside each
  other within 200 m, whatever they are named (both banks), which a runner covers
  only one of; its single-path length is the sum of its stretches'. A group's
  approach is its best stretch's, and its familiarity is matched over its whole
  extent. The pieces of one route relation that meet within 200 m along the network
  are joined before merging. Sections under 1.5 miles (or 15% of the run distance,
  if more) are stored but left out of a survey unless asked for.
- Trail is split into `singletrack` (footpath-width natural-surface ways, or
  anything with a hiking or mountain-bike difficulty grade) and `track`
  (`highway=track`, `cycleway`, wide ways, ways with a `tracktype` or a vehicle
  access tag, bicycle-designated graded paths), decided from the way's tags at
  network build and stored on each edge. A survey weighs a track at 0.8 of
  singletrack by default and takes a `surface_preference` to tilt either way.
- The cut keeps waterways, water bodies, parks and protected areas (outer rings,
  thinned), summits and viewpoints, and walking or cycling route relations.
  A tile cut at an older version is re-cut from the registered extract on the
  next rebuild, and never re-downloaded.
- The survey's score is `quality x adjustment - access cost`, in
  quality-miles; `survey.py` holds the formula and weights. Adjustments and
  exclusions are keyed by way ids and re-attach to the rebuilt section of the
  same level and kind that shares at least half of their length in both
  directions. A stretch's adjustment multiplies with its group's (bounded); a
  group's exclusion removes every stretch's ways.
- Familiarity is matched from the raw streams in the background and stored per
  section; a rebuild clears it and every activity is matched again.
- Each group is cut into **stretches** at bridges and tunnels, busy roads,
  trailheads and parking, other junctions, and name changes, in that priority.
  Intervals under 1.5 km merge into a neighbor and those over 6 km split nearest
  the middle. A cut is named for the road that meets it, else the bridge,
  trailhead or parking, park, or differently named way there, else by its
  distance along the path; a role never reads "between X and X". Edges off the
  spine fall in the stretch beside them; a branch of 3 km or more becomes a unit
  of its own, a shorter one is a spur, noted on its stretch ("includes spur to
  ...") and counted in its length. A stretch spans both banks between the same
  cut points and counts parallel paths once. A **mesh of trails** (a trail
  system of mostly natural-surface trail where the longest path is under half
  its length and no banks run beside it) is divided into **regions** instead:
  its edges are cut at access points, roads and hubs into connected parts, small
  parts joined to a neighbor and large ones bisected until each holds 3 to 10 km
  of trail, so a 20-mile system becomes a handful of regions named for their
  main trails. A region's chains are a few trails through it. Judgments,
  familiarity, and the survey's detail view work per stretch.
  `sections.building_blocks` returns stretches as the blocks route assembly runs
  along (ordered edge chains per side, end and entry nodes, the stretch's own
  edge ids; where consecutive stretches meet end to end their primary chains run
  on as one), and `net_edges.structure` keeps the bridge and tunnel marks.
- Route search is unchanged apart from dropping excluded sections' ways from
  its graph; a section steers it only through `include_way_ids`.

### Phase 2 as built

- `assemble.py` builds candidates around blocks. With `sections=[ids]` (a group
  or stretch id; a group stands for its stretches) it builds around exactly those
  and never falls back to the walk: an excluded section is refused, and a request
  none of whose sections can be reached returns no candidates and a warning.
  Without it the survey's best sections for each start (the first four, eight
  each) supply the blocks, and the walk runs only when no section-built candidate
  meets the request.
- One preference-weighted shortest-path tree per start, on the survey's access cost,
  reaches every block entry; an approach longer than 30% of the target one way, or
  with more continuous uncomfortable road than the preferences allow, drops that
  entry. Return trees run from the start with the block's chain at four times its
  cost, so a return avoids the way out where it can.
- Shapes. Out-and-back: approach, the chain from an entry in either direction, the
  last edge cut where the distance runs out, retrace; when the chain ends first, the
  route goes on over the next-best block within a short connector, up to six times.
  Loop: the chain to the node whose total with the return fits (of those that make
  the distance, the one whose way home retraces least), home by the penalized tree;
  a chain too short for the target goes out over further blocks, up to six chains
  in all with a block of the same group preferred, each reached by a connector as
  short as an out-and-back's, and returns by a tree that avoids all of them. A
  return that rejoins the approach for 200 m or more makes the route a lollipop. Lollipop: a stem and a loop inside the
  group's own edges, out to a far point and back by a path that avoids the way out.
  A loop-like candidate whose return retraces half or more of what it ran is
  reported as an out-and-back and earns no bonus.
- Every candidate goes through the same pipeline as a walk: `score_path`, the
  road-exposure limit, soft constraints, diversification (a section-built
  candidate's score is scaled to the best in its pool), persistence, and the search
  lock and time budget. The survey behind the plan runs before the lock is taken.
- Ranking, in quality-miles: `score = quality x loop x fit - access_cost`.
  `quality` sums, over the blocks run, the section's adjusted quality per mile
  (continuity x (1 + scenery) x runnability x surface x adjustment, survey.py's
  terms) times the miles run on it, counted each time it is run. `loop` is
  `1 + loop_bonus` (default 0.075, a request may set 0 to 0.5) for a loop or
  lollipop, else 1. `fit` is 1 inside the distance range and falls by twice the miss
  as a share of the target outside it. `access_cost` is the survey's per-mile charge
  on half the weighted cost of everything run off the blocks. A candidate reports
  `built_from`, `sections_used` (section, stretch, and meters on each) and the terms.
- The graph a search runs on keeps every node of the chosen sections' edges, whatever
  their distance from the start, so a long edge is not lost to the search radius.
- Drive time to a start does not enter the ranking (phase 3). Miles run on a
  stretch's edges outside its chain are credited to that stretch.

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
