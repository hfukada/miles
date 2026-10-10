---
name: training-log
description: Keep the athlete's two standing documents — the runner profile (long-term traits) and the training log (the current block's state) — in the right shape, and decide which one an observation belongs in. Use after a notable session, at the end of a training week, after a plan revision or adjustment, at the end of a block, and whenever a session wants to record something about the athlete. Loaded by miles and miles-plan before they write either document.
---

# training-log — the athlete's standing documents

Two documents carry what a session can't get from the data alone. They
differ in how fast they change, and most mistakes come from putting
something in the wrong one.

| | Runner profile | Training log |
|---|---|---|
| Holds | Traits that hold across builds | The current block's state |
| Changes | Rarely, after a block review | Weekly or after a notable session |
| Size | About a page, and stays there | One block, older weeks folded down |
| Lifetime | Indefinite | Closed out at block end; a new one per block |

Until the miles server stores these, they are markdown files the deployment
keeps outside this plugin. The session's context names where they are. If it
doesn't, ask before creating them anywhere.

## Which document?

Ask: **would this still be true after three months off?**

- **Yes → profile.** Max and resting HR, HR ranges per effort, how the
  athlete responds to down weeks, tapers, and workouts, fueling tolerances,
  skills (descending technique), preferences and style.
- **No → log.** Anything that is conditioning: descent durability, long-run
  durability, heat acclimation, current fitness, how the legs feel this week,
  a fast recovery from one hard session.

A single block's observation never goes straight into the profile, however
striking. It goes in the log's watch list, dated. It moves to the profile at
block close-out if it repeated across at least two builds, or if the athlete
says it is long-standing. Ask before every profile edit; propose the exact
line.

Skill and conditioning can share a subject. "Descends hard well" is
technique (profile); "quads didn't notice last week's descent" is
conditioning (log).

## Runner profile

Fixed sections, in this order:

1. **Heart rate** — max, resting, and a table of effort → HR range →
   evidence. Note where HR misleads (it lags on short reps).
2. **Response to training** — down weeks, tapers, rest, workouts, travel.
3. **Fueling** — carbs per hour tolerated, fluid, how gels go down.
4. **Skills** — descending, climbing, technical terrain.
5. **Style and preferences** — what the athlete enjoys, how they run long
   days, what they trust.

Every entry carries a date and its evidence: the query that backs it, or
the athlete's own words, quoted. When a fact changes, edit the entry and
its date; don't append a second, conflicting one.

## Training log

One per block, with these sections:

- **Header** — goal race and date, what the race is for, block start, the
  plan version of record, last updated, and the date synced through.
- **Capacities** — the core of the log. A table:

  | Capacity | Status | Upkeep | Last stimulus | Evidence |
  |---|---|---|---|---|

  - *Status* is one of `building`, `maintaining`, `not a focus`, with the
    date each started, oldest first: `building since 03-02 → maintaining
    from 04-13`. The row then shows when the capacity was the block's focus,
    however sharply or loosely the block is periodized.
  - *Upkeep* is the stimulus that holds the capacity and how often: "a
    descent of −8% or steeper, 800+ ft, every ~2 weeks". Write it as
    something the record can confirm.
  - *Last stimulus* is the most recent session that met the upkeep rule,
    dated. Past the window, call the capacity fading in conversation until a
    session renews it.
- **Reference sessions** — pinned activities with their id and one line on
  why: the bar a later session will be judged against. Pin few; replace a
  reference when a better example of the same thing comes along.
- **Recent weeks** — the last ~4 weeks, one line each: miles against the
  plan band, the key sessions, how it felt in the athlete's words. Fold
  older weeks into one summary line.
- **Adjustments** — dated what-and-why for each plan change. The record of
  truth is the plan version note or the `log_plan_adjustment` entry; write
  there first, then summarize here. A decision that changed nothing in the
  plan (keeping a workout despite flat legs) still gets a line.
- **Watch list** — hypotheses the block is testing, each with the test that
  will settle it. These are the only candidates for the profile.
- **Block close-out** — written after the goal race: a short summary, a
  verdict on each watch-list item (confirmed, refuted, still open), and the
  profile edits proposed from them.

## When to write

- **After a notable session**: update *Last stimulus* for any capacity it
  met; pin it if it sets a bar; add anything surprising to the watch list.
- **End of the week**: add the week's line, fold the oldest, and check each
  capacity's upkeep window against the record.
- **After a plan revision or adjustment**: an Adjustments line, and a status
  change for any capacity whose focus moved.
- **At block end**: the close-out, then the promotion review with the
  athlete. Start the next block's log fresh; carry over only capacities that
  are still being maintained.

Pull every number from the miles tools before writing it. A capacity's
evidence and a week's mileage come from the record, not from memory or the
previous version of the log. State the sync date behind anything about the
in-progress week.

## What to avoid

- Writing a one-block observation into the profile.
- Restating plan targets or adjustment reasoning the plan already holds,
  rather than summarizing and pointing at the version.
- Upkeep rules too vague to check ("regular hills").
- A recent-weeks list that grows without folding.
- Coaching in the log. It records what is being built and why; prescribing
  what to run next is `miles-plan`'s job, in conversation.
