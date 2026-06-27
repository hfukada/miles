# Marathon Build Analysis

Call `get_marathon_comparison` (miles MCP, default 12-week build window).

## Table

Present results sorted fastest to slowest finish time. If any race name contains "Virtual", flag it separately rather than ranking it alongside real races — virtual efforts are not directly comparable.

Columns: Race | Date | Time | Build mpw | Workouts (n / pace) | Long runs (n / pace) | Easy HR

Format finish times as h:mm:ss. Workout and long run pace come from `by_type.workout.avg_pace_min_per_mile` and `by_type.long_run.avg_pace_min_per_mile`. If a run_type is absent from `by_type`, show `—`.

## Analysis lenses (apply all, be specific with numbers)

**Volume vs. quality.** Does avg_mpw predict the result? Call out where it does and where it breaks down — look for cases where the highest-mileage build didn't produce the best result, or where a moderate-mileage build outperformed expectations. High mileage is necessary but not sufficient.

**Workout sharpness.** `by_type.workout.avg_pace_min_per_mile` is the clearest proxy for race readiness. Look for the pace band that separates result tiers. Note outliers — builds with very few workouts that ran them unusually fast, or many workouts at soft paces.

**Long run count.** Count `by_type.long_run.runs` across builds. Note whether the best results cluster around higher long run counts, and what the typical range looks like across the history.

**Easy HR trend.** List easy HR values chronologically. Decreasing HR at similar effort is an aerobic fitness signal that accumulates over years, independent of any single build.

**Undercooked builds.** Flag any build with avg_mpw < 25 or zero workout sessions — these typically explain the slowest results and are worth separating from structurally normal builds when drawing conclusions.

**In-build marathons.** If `by_type.race` appears with distance ~26 miles, note it — it means a tune-up marathon was run during the build window, which affects how to read the training load.

## Closing take

Give a measured read of training evolution across the full history — not a formula. Each build inherits aerobic fitness from everything before it, so later results at lower mileage can reflect accumulated fitness, not just that cycle's work. Resist the urge to attribute outcomes to single variables.

Note genuine trends if they're visible (e.g. easy HR drifting down over years, long run structure becoming more consistent). If something stands out — a breakout result, a build that clearly underdelivered, a structural shift in how training is organized — name it. Otherwise sit with the complexity: race outcomes are shaped by fitness accumulated over years, conditions on race day, how the taper went, and things that don't show up in the data at all.
