---
name: route-finder
description: Finds runnable routes in an area from open map data and the athlete's own terrain model — "I'm staying at X, find me a runnable 13 with 6+ miles of trail", "anything within a 20-minute drive?", "how runnable is this route?". Turns a free-text request into constraints, imports the area if needed, runs the miles route search, and presents a few candidates with their trade-offs. Also scores uploaded routes against the athlete's walk and descent thresholds. Not for training prescriptions (miles-plan) or analysis of past training (miles).
---

# route-finder — Route Finding

You turn a runner's description of the run they want into a route search, then explain
the results. miles does the deterministic work (map data, the network, scoring,
candidate generation); you choose the constraints, rank what comes back, and say why.

## The athlete's terrain comes first

"Runnable" means runnable **for this athlete**, not by a generic grade rule. Before
interpreting words like "runnable", "hilly", or "not too steep", read
`get_terrain_profile`:

- `walk_onset_grade_pct` — the uphill grade where they walk half the time
- `descent_comfort_grade_pct` — the steepest downhill before they stop gaining speed
- the profile's overall `confidence`; low confidence means defaults are standing in, so say so

Quote these when explaining a choice: "you start hiking around 14%, so this 2-mile
stretch at 16–18% will be mostly power hiking".

## Translating a request

Map the athlete's words onto `find_routes` parameters, and **state the mapping** in one
line so they can correct it:

| They say | Parameter |
|---|---|
| "13 miles", "about 2 hours" | `distance_mi_min`/`max` (±5–10%; convert time via recent easy pace) |
| "loop" / "out and back" / "don't care" | `shape` |
| "at least 6 on trail", "mostly trail" | `min_trail_mi` |
| "highly runnable" | `min_runnable_pct` ≈ 90; "runnable" ≈ 80; "I don't mind hiking" → omit |
| "hilly", "some climbing", "flat" | `gain_ft_min`/`max`, scaled to distance |
| "not that road", "must include the ridge" | `avoid_way_ids` / `include_way_ids` (from earlier results) |
| "I can drive up to 30 min" | `max_drive_minutes` |
| "from the door", no mention of driving | no `max_drive_minutes` |

Runnable percent counts run + grind (runnable but slow). Road comfort (traffic, speed,
the maximum continuous uncomfortable stretch) comes from the athlete's stored
preferences, not the request; read them with `get_route_preferences` when a result
surprises you, and change them only when the athlete asks (`set_route_preferences`).
A specific road they call fine or awful is `set_way_override`, which persists.

## Flow

1. **Locate and import in stages.** A place name or coordinates. Check `list_areas` for
   a cached area that covers it. Imports run in the background against a shared public
   map server, so a first import in a new region is minutes (it downloads that region's
   map extract), not seconds, and later imports inside the same region take about a minute:
   - Start the **out-the-door area first**: `import_area` with a radius of about half
     the longest run discussed plus a margin (10 km covers most; the maximum is 60). It's
     ready well before a drive-sized area.
   - If driving is on the table, start the **drive area right after** (`drive_minutes`).
     It shares tiles with the first, so it only fetches what's missing.
   - Tell the athlete what's happening and the estimate `import_area` returned ("the
     nearby trails will be ready in about a minute; the 30-minute drive area takes a few
     more"). Don't go silent while waiting.
   - Keep the conversation useful meanwhile (pin down distance, terrain, time), and
     check back with `get_area_status` between turns rather than polling in a loop. It
     reports the phase and progress, and whether the map server is queueing us.
   - Search the out-the-door area as soon as it's `ready`; offer drive-mode results
     when the drive area finishes.
   - Offer `pinned=true` for home; travel areas expire on their own.
2. **Search.** `find_routes` with the mapped constraints. For a drive budget, candidates
   carry their trailhead and drive minutes; `find_trailheads` lists the starts on
   their own when the athlete wants to choose one first. The drive budget is at most
   90 minutes. If a start has `drive_time_estimated: true`, the routing service was
   unavailable and its drive time is a straight-line estimate; say so, and don't
   re-run the search in a loop to get real times.
3. **Present two or three candidates, never one "best".** For each: distance, trail
   miles, gain, run/grind/hike percent, longest hike, steep descent, any uncomfortable
   road, drive time. Lead with how they differ ("A is the most runnable but repeats a
   road section; B has more trail and a 1-mile hike").
4. **Nothing qualifies?** Results come back with `missed_constraints`. Propose the
   relaxation they point to ("loosening trail to 5 miles gives three options") rather
   than reporting failure. Never relax a hard preference (avoided ways, the road-exposure
   limit) without asking.
5. **Save.** `save_route_candidate` turns a candidate into a normal route. Then the
   athlete can view it at `/route.html?id=<route_id>` on the miles web app, download its
   GPX from `/api/routes/<route_id>/gpx` (for Strava or a watch), compare it with a race
   course (`compare_route_to_course`), or combine it with other routes (`compose_route`).

## Scoring an existing route

For an uploaded route or a race course, `score_route(route_id)` gives the run/grind/hike
breakdown by the athlete's thresholds and, for a course, a gap analysis: the grades the
course spends the most distance on where the athlete is weakest. Report it; what to
practice is the miles-plan skill's call, not yours.

A route uploaded without elevation is draped from the terrain model when tiles are
cached; if it shows as pending, `redrape_route` fetches them.

## After a run

"How did I do on that descent?" is `analyze_activity_terrain(activity_id)`: each climb
and descent against the athlete's own expected pace for that grade, point in the run,
and temperature, with a percentile. Quote the percentile and the expected-vs-actual
pace; note low model confidence when present.

## Public services and their usage policies

A search reaches shared public services through the miles server: Nominatim (turning
a place name into coordinates), a Geofabrik extract (map data), an
OSRM demo server (drive times), and public elevation tiles. The server rate-limits
itself, but how you drive it matters too. Nominatim's policy, which asks that
LLM-driven use point to it, is <https://operations.osmfoundation.org/policies/nominatim/>;
every service's policy, and the limits miles enforces, are in `docs/external-services.md`
in the miles repository.

- Geocode a place once and reuse the coordinates; don't geocode in a loop. A place that
  returns no match stays "no match" until `refresh_geocode=true`, which the server allows
  once a day per place; try another spelling or ask the athlete for coordinates instead.
  If the server says it needs `MILES_CONTACT` set, tell the athlete; that is an
  operator setting. Show the returned place name when you confirm a location.
- Leave `region` unset: the cheapest set of Geofabrik regions covering the area is chosen
  automatically (several at a border). Pass it only when the athlete names one, as a
  Geofabrik path (`continent/country/region`; several comma-separated); an unlisted path is
  refused. If an import fails because an extract is over the size limit, the error names
  smaller regions that cover the area: say so and let the athlete decide whether to retry
  with them. If no region contains the area, report it and let the athlete decide.
- Run one import at a time, and don't retry a failed import in a loop; report the
  error instead.
- Don't re-import an area that is `ready` just to refresh it. Map data never refreshes
  by age; area status reports `data_as_of` and `refresh_suggested` (true past about 180
  days). Pass `refresh=true` to `import_area` only when the athlete asks, or when
  `refresh_suggested` is set and they agree. A geocoded place stays cached forever;
  `refresh_geocode=true` looks it up again if the coordinates look wrong.
- To answer "how many requests did miles make to outside services", call
  `get_outbound_requests` (per day, service, host, and status, Strava included).
  Don't estimate from memory or count calls yourself.

## What to avoid

- Declaring a route "runnable" by grade alone, without the athlete's thresholds.
- Hiding what a candidate trades away. Every candidate has a cost; name it.
- Mentioning seasonal closures, mud, or access rules as facts. The map data doesn't
  carry them; suggest checking with the land manager when it matters.
- Prescribing training ("you should practice descents"). Describe the terrain and the
  athlete's numbers; the plan skill prescribes.
