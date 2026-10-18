"""Section-anchored candidate assembly (adr/0004-section-first-route-search.md).

A candidate is an approach, one or more building blocks, and a return, where a
block is a stretch of a section (sections.py) with its edges ordered into one
or two side-by-side chains. Nothing here walks at random: each candidate is
built from shortest paths, and the distance is fitted by choosing how much of
a chain to run.

Shapes, for a block chain entered at one of its nodes J:

- out-and-back: approach to J, run the chain in one direction to the node
  that makes the target distance, retrace.
- loop: approach to J, run the chain to a node K, return from K to the start
  by the cheapest path that avoids the chain where it can. A return that
  rejoins the approach is a lollipop; one that retraces what was run is an
  out-and-back and earns no loop bonus.
- lollipop: a stem to J, a loop J -> far point -> J inside the block's own
  network (the cheapest path out, then the cheapest path back that avoids
  it), the stem again.
- chained loop: a chain too short to fill the distance, then further blocks
  (the group the route is in first) each within a short connector, up to
  MAX_LOOP_BLOCKS in all, then home by the cheapest path that avoids all of
  it. Of the nodes where the route would make the distance, the one whose way
  home retraces least is taken.

Approach and return paths are one shortest-path tree each, over the cost
survey.py uses for access: a comfortable edge costs its length, and
uncomfortable road, crossings and steep grades cost more.

Ranking, in quality-miles (survey.py's quality notion):

    score = quality x loop x fit - access_cost

    quality     = sum over blocks of density x miles run on that block, a
                  block's density being its stretch's adjusted quality per
                  mile (survey.density x survey.section_factor); a mile is
                  counted each time it is run, an out-and-back's twice
    loop        = 1 + loop_bonus for a loop or lollipop that returns by a
                  different path, else 1
    fit         = 1 inside the requested distance range; outside it, falling
                  by FIT_SLOPE of the miss as a share of the target, to 0
    access_cost = ACCESS_COST_PER_WEIGHTED_MI x half the weighted cost, in
                  miles, of everything run off the blocks (the approach and
                  the return together are about twice the one-way approach
                  the survey charges)
"""

import math
import random
import sqlite3
import time
from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field

from . import familiarity, network, route_search, sections, survey, terrain
from .route_search import GraphEdge, SearchGraph, Start, Traversal

MILE_M = survey.MILE_M

# Sections taken from the survey when a request names none, per start.
SURVEY_N = 8
MAX_SURVEY_STARTS = 4
JOINS_PER_CHAIN = 2

# The approach, one way, may be at most this share of the target distance.
APPROACH_SHARE_MAX = 0.3
# The approach tree is explored out to this share of the target in weighted cost.
APPROACH_TREE_SHARE = 0.6
# Return and loop-back trees are explored out to this share of the target in weighted cost.
RETURN_TREE_SHARE = 0.65
MIN_RUN_M = 300.0
# An edge is run whole, or its remainder dropped, when the distance still
# wanted is within this much of its length or under it.
CUT_SLACK_M = 50.0
# A stem shorter than this makes a lollipop indistinguishable from a loop.
MIN_STEM_M = 200.0
# A route that runs out of chain goes on to another block within this
# weighted cost, at most this many times; a loop chains at most MAX_LOOP_BLOCKS
# blocks. A block of the group the route is already in is worth
# SAME_GROUP_BONUS times its quality when choosing the next one.
EXTENSION_MAX_COST_M = 1500.0
MAX_EXTENSIONS = 6
MAX_LOOP_BLOCKS = 6
# Next blocks tried at each step of a loop.
LOOP_BRANCHING = 2
SAME_GROUP_BONUS = 1.25
# A way home after a chained loop is explored out to this many times the
# distance the route has left, in weighted cost.
HOME_REACH_SLACK = 1.5
# Turning points along a chain compared by how much of the way home they retrace,
# in steps of TURN_REUSE_STEP of it; the closest to the target distance wins a tie.
TURN_CANDIDATES = 12
TURN_REUSE_STEP = 0.05
# Far points tried per entry point when forming a loop inside a group's own
# network, and entry points.
INNER_FAR_POINTS = 8
INNER_JOINS = 2
# A far point's out leg is this share range of the loop wanted, and the loop
# built through it must land within this share of the wanted length.
INNER_FAR_SHARE = (0.25, 0.65)
INNER_LENGTH_SHARE = (0.75, 1.25)
OUT_AND_BACK_REUSE_SHARE = 0.5
# A walked inner loop that retraces more than this share of itself is not a loop.
INNER_RETRACE_MAX = 0.3
FIT_SLOPE = 2.0


@dataclass
class PlannedBlock:
    block: sections.Block
    # Adjusted quality per mile: survey.density x the section's factor.
    density: float


@dataclass
class BlockPlan:
    blocks: list[PlannedBlock] = field(default_factory=list)
    explicit: bool = False
    warnings: list[str] = field(default_factory=list)
    overrides: dict[int, str] = field(default_factory=dict)

    def keep_nodes(self, rows: Mapping[int, dict]) -> set[int]:
        """The end nodes of every edge of the blocks, which the search graph
        keeps even beyond its radius."""
        nodes: set[int] = set()
        for planned in self.blocks:
            for eid in planned.block["edge_ids"]:
                row = rows.get(eid)
                if row is not None:
                    nodes.update((row["from_node_id"], row["to_node_id"]))
        return nodes


@dataclass
class Built:
    """A candidate's path, before scoring."""
    shape: str
    start: Start
    traversal: Traversal
    loop_like: bool


# ---------------------------------------------------------------------------
# Choosing blocks
# ---------------------------------------------------------------------------


def plan_blocks(
    conn: sqlite3.Connection, request: route_search.RouteSearchRequest, model: terrain.TerrainModel
) -> BlockPlan:
    """The blocks to build around: those of the sections the request names, or
    of the survey's best sections for its starts. Takes the search slot (the
    survey does), so call it outside it."""
    area_id = request["area_id"]
    preference = request.get("surface_preference") or "any"
    if preference not in survey.SURFACE_WEIGHTS:
        raise ValueError(f"unknown surface_preference {preference!r}; choose from {', '.join(survey.SURFACE_PREFERENCES)}")
    target_m = (request["distance_m_min"] + request["distance_m_max"]) / 2.0
    unfamiliar_only = request.get("unfamiliar_only", False)
    named = list(dict.fromkeys(request.get("sections") or []))
    plan = BlockPlan(explicit=bool(named))
    plan.overrides = {
        r["way_id"]: r["verdict"] for r in conn.execute("SELECT way_id, verdict FROM net_way_overrides").fetchall()
    }

    by_id = {s.section_id: s for s in sections.load_sections(conn, area_id)}
    if not by_id and not named:
        return plan
    ids = named
    if named:
        for section_id in named:
            if section_id not in by_id:
                sections.get_section(conn, section_id)  # raises SectionNotFoundError for an unknown id
                raise ValueError(f"section {section_id} belongs to another area")
    else:
        ids = []
        for start in request["starts"][:MAX_SURVEY_STARTS]:
            result = survey.survey(
                conn, area_id, start["lat"], start["lng"], distance_m=target_m, n=SURVEY_N,
                surface_preference=preference, unfamiliar_only=unfamiliar_only,
            )
            ids.extend(r["section_id"] for r in result["sections"] if not r["excluded"] and r["section_id"] not in ids)
    judgments = sections.judgments_for(conn, list(by_id.values()))
    if named:
        for section_id in named:
            sec = by_id[section_id]
            parent = by_id.get(sec.group_id) if sec.group_id is not None else None
            if judgments[section_id]["exclusion"] or (parent and judgments[parent.section_id]["exclusion"]):
                raise ValueError(f"section {section_id} is excluded; clear its exclusion to route through it")
    visits = familiarity.visits(conn, area_id) if unfamiliar_only else {}
    for block in sections.building_blocks(conn, area_id, ids):
        stretch, group = by_id[block["block_id"]], by_id[block["section_id"]]
        if any(v in visits and visits[v].runs > 0 for v in (stretch.section_id, group.section_id)):
            continue
        factor, _adjustment = survey.section_factor(stretch, group, judgments)
        plan.blocks.append(PlannedBlock(block, survey.density(stretch, model, preference) * factor))
    return plan


# ---------------------------------------------------------------------------
# Search-graph geometry of a block
# ---------------------------------------------------------------------------


@dataclass
class _Chain:
    """A run of a block's edges in the search graph: edges[i] joins nodes[i]
    and nodes[i + 1], cum[i] meters from nodes[0]."""
    nodes: list[int]
    edges: list[GraphEdge]
    cum: list[float]
    # Nodes where the approach may join the chain.
    joins: set[int] = field(default_factory=set)
    keys: frozenset[int] = frozenset()
    # Index of the block the chain belongs to.
    block: int = -1

    def run(self, i: int, k: int) -> Traversal | None:
        """The way from nodes[i] to nodes[k], None when a one-way edge forbids it."""
        if k > i:
            out = [(self.edges[j], self.nodes[j]) for j in range(i, k)]
        else:
            out = [(self.edges[j], self.nodes[j + 1]) for j in reversed(range(k, i))]
        return out if all(e.traverse(frm) is not None for e, frm in out) else None


@dataclass
class _Prepared:
    planned: PlannedBlock
    chains: list[_Chain]


def _expand(
    key: int, cur: int, graph: SearchGraph, rows: Mapping[int, dict], replaced: Mapping[int, tuple[int, int, int, int]]
) -> list[tuple[GraphEdge, int, int]] | None:
    """The graph edges standing for original edge `key`, walked from `cur`; an
    edge cut by a start point stands for its two pieces. None when the edge
    is gone (removed or outside the bound) or does not touch `cur`."""
    edge = graph.edges_by_key.get(key)
    if edge is not None:
        if cur == edge.a:
            return [(edge, cur, edge.b)]
        if cur == edge.b:
            return [(edge, cur, edge.a)]
        return None
    pieces = replaced.get(key)
    if pieces is None:
        return None
    first, second, a, _b = pieces
    ordered = (first, second) if cur == a else (second, first)
    out: list[tuple[GraphEdge, int, int]] = []
    for piece in ordered:
        part = _expand(piece, cur, graph, rows, replaced)
        if part is None:
            return None
        out.extend(part)
        cur = part[-1][2]
    return out


def _leaf_keys(key: int, graph: SearchGraph, replaced: Mapping[int, tuple[int, int, int, int]]) -> list[int]:
    if key in graph.edges_by_key:
        return [key]
    pieces = replaced.get(key)
    return [k for piece in pieces[:2] for k in _leaf_keys(piece, graph, replaced)] if pieces else []


def _chain_runs(
    edge_ids: Sequence[int], graph: SearchGraph, rows: Mapping[int, dict], replaced: Mapping[int, tuple[int, int, int, int]]
) -> list[_Chain]:
    """A block chain's edges as runs still present in the graph, each walked
    in the chain's own direction."""
    runs: list[_Chain] = []
    nodes: list[int] = []
    edges: list[GraphEdge] = []

    def flush() -> None:
        nonlocal nodes, edges
        if edges:
            cum = [0.0]
            for e in edges:
                cum.append(cum[-1] + e.length_m)
            runs.append(_Chain(nodes, edges, cum))
        nodes, edges = [], []

    ends = [(rows[e]["from_node_id"], rows[e]["to_node_id"]) for e in edge_ids]
    for idx, eid in enumerate(edge_ids):
        a, b = ends[idx]
        if not nodes or nodes[-1] not in (a, b):
            flush()
            follower = ends[idx + 1] if idx + 1 < len(ends) else None
            start = a if follower is None or a not in follower else b
            nodes = [start]
        steps = _expand(eid, nodes[-1], graph, rows, replaced)
        if steps is None:
            flush()
            continue
        for edge, _frm, to in steps:
            edges.append(edge)
            nodes.append(to)
    flush()
    return runs


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _reverse(traversal: Traversal) -> Traversal | None:
    back: Traversal = []
    for edge, frm in reversed(traversal):
        step = edge.traverse(frm)
        assert step is not None
        if edge.traverse(step[0]) is None:
            return None
        back.append((edge, step[0]))
    return back


def _keys(traversal: Traversal) -> set[int]:
    return {edge.edge_key for edge, _ in traversal}


def _length(traversal: Traversal) -> float:
    return sum(edge.length_m for edge, _ in traversal)


def _retraced_share(traversal: Traversal) -> float:
    """The share of the length that repeats an edge already run."""
    seen: set[int] = set()
    repeated = 0.0
    for edge, _ in traversal:
        if edge.edge_key in seen:
            repeated += edge.length_m
        seen.add(edge.edge_key)
    total = _length(traversal)
    return repeated / total if total > 0 else 0.0


def _shared_m(leg: Traversal, earlier: Traversal) -> float:
    """The length of `leg` on edges `earlier` already ran."""
    seen = _keys(earlier)
    return sum(e.length_m for e, _ in leg if e.edge_key in seen)


def _return_shape(back: Traversal, ran: Traversal, stem: Traversal) -> str:
    """What a return leg makes of the route: an out-and-back when it retraces
    OUT_AND_BACK_REUSE_SHARE of what was run, a lollipop when it rejoins the
    stem for at least MIN_STEM_M, otherwise a loop."""
    total = _length(back)
    if total > 0 and _shared_m(back, ran) / total >= OUT_AND_BACK_REUSE_SHARE:
        return "out_and_back"
    return "lollipop" if _shared_m(back, stem) >= MIN_STEM_M else "loop"


class Assembly:
    """Candidate paths around a BlockPlan's blocks, within one search's graph.
    The graph, rows and start nodes are the search's own (start points
    spliced in, `replaced` recording the cuts); nothing is contracted."""

    def __init__(
        self,
        plan: BlockPlan,
        graph: SearchGraph,
        rows: MutableMapping[int, dict],
        replaced: Mapping[int, tuple[int, int, int, int]],
        start_nodes: Sequence[tuple[int, Start]],
        prefs: dict,
        model: terrain.TerrainModel,
        *,
        distance_m_min: float,
        distance_m_max: float,
        shapes: Sequence[str],
        loop_bonus: float,
        ids: Iterable[int],
    ) -> None:
        self.graph, self.rows, self.start_nodes = graph, rows, list(start_nodes)
        self.ids = iter(ids)
        self.prefs = prefs
        self.target_m = (distance_m_min + distance_m_max) / 2.0
        self.dmin, self.dmax = distance_m_min, distance_m_max
        self.shapes = set(shapes)
        self.loop_bonus = loop_bonus
        self.costs = survey.EdgeCosts(prefs, model, plan.overrides, rows)
        self.replaced = replaced
        self.blocks: list[_Prepared] = []
        # The block each edge of the search graph is credited to: the first
        # whose stretch holds it.
        self.owner: dict[int, int] = {}
        # Block indices by group, whose own edges an inner loop may use.
        self.groups: dict[int, list[int]] = {}
        self._group_graphs: dict[int, SearchGraph] = {}
        start_ids = {n for n, _ in start_nodes}
        for index, planned in enumerate(plan.blocks):
            block = planned.block
            joins = {*block["entry_nodes"], *block["end_nodes"], *start_ids}
            prep = _Prepared(planned, [])
            self.blocks.append(prep)
            self.groups.setdefault(block["section_id"], []).append(index)
            for eid in block["edge_ids"]:
                for leaf in _leaf_keys(eid, graph, replaced):
                    self.owner.setdefault(leaf, index)
            for chain in block["chains"]:
                for run in _chain_runs(chain, graph, rows, replaced):
                    if run.cum[-1] >= MIN_RUN_M:
                        run.joins, run.keys, run.block = joins, frozenset(e.edge_key for e in run.edges), index
                        prep.chains.append(run)
            # A chain can also be joined where it ends and where another chain
            # of the block crosses it.
            at_node = Counter(n for chain in prep.chains for n in set(chain.nodes))
            for chain in prep.chains:
                chain.joins = joins | {chain.nodes[0], chain.nodes[-1]} | {n for n in chain.nodes if at_node[n] > 1}
            prep.chains.sort(key=lambda c: -c.cum[-1])
        self._trees: dict[tuple, survey.Tree] = {}
        self.deadline = math.inf

    def _tree(
        self, key: tuple, source: int, max_cost: float, penalized: frozenset[int] = frozenset(),
        targets: Collection[int] = (),
    ) -> survey.Tree:
        if key not in self._trees:
            self._trees[key] = survey.weighted_tree(self.graph, source, self.costs, max_cost, penalized, targets)
        return self._trees[key]

    def _totals(self, chain: _Chain, ji: int, direction: int, base_m: float, tree: survey.Tree) -> dict[int, float]:
        """For each node of `chain` at least MIN_RUN_M on from ji in
        `direction` that the tree reaches, the length of a route of base_m that
        runs the chain to it and goes home by the tree."""
        return {
            k: base_m + abs(chain.cum[k] - chain.cum[ji]) + tree.length[chain.nodes[k]]
            for k in range(ji + direction, len(chain.nodes) if direction > 0 else -1, direction)
            if chain.nodes[k] in tree.cost and abs(chain.cum[k] - chain.cum[ji]) >= MIN_RUN_M
        }

    def candidates(self, deadline: float) -> Iterator[Built]:
        """Built candidates, best blocks first, until the deadline."""
        self.deadline = deadline
        trees: dict[int, survey.Tree] = {}
        for s_node, _start in self.start_nodes:
            trees[s_node] = self._tree(("approach", s_node), s_node, self.target_m * APPROACH_TREE_SHARE)
        inner_done: set[tuple[int, int]] = set()
        for index, prep in enumerate(self.blocks):
            group_id = prep.planned.block["section_id"]
            for s_node, start in self.start_nodes:
                t1 = trees[s_node]
                for chain in prep.chains:
                    for ji in self._joins(chain, t1):
                        if time.monotonic() > deadline:
                            return
                        yield from self._single(s_node, start, t1, chain, ji)
                if {"loop", "lollipop"} & self.shapes and (group_id, s_node) not in inner_done:
                    inner_done.add((group_id, s_node))
                    if time.monotonic() > deadline:
                        return
                    yield from self._inner_loops(group_id, s_node, start, t1)
        if {"loop", "lollipop"} & self.shapes:
            # Chaining blocks is the costliest shape, so it gets what is left of the budget.
            for prep in self.blocks:
                for s_node, start in self.start_nodes:
                    for chain in prep.chains:
                        joins = self._joins(chain, trees[s_node])
                        for built in self._chained_from(s_node, start, trees[s_node], chain, joins[:1]):
                            if time.monotonic() > deadline:
                                return
                            yield built

    def _joins(self, chain: _Chain, t1: survey.Tree) -> list[int]:
        """Indices of the chain's nodes worth entering at, cheapest approach first."""
        reachable = [
            (t1.cost[n], i) for i, n in enumerate(chain.nodes)
            if n in chain.joins and n in t1.cost and t1.length[n] <= APPROACH_SHARE_MAX * self.target_m
        ]
        return [i for _cost, i in sorted(reachable)[:JOINS_PER_CHAIN]]

    def _approach(self, t1: survey.Tree, node: int) -> tuple[route_search.Steps, float] | None:
        """The path to `node` and its length, None when it has more continuous
        uncomfortable road than the preferences allow."""
        approach = survey.approach_to(t1, node, self.costs, self.rows)
        if approach.max_uncomfortable_run_m > self.prefs["max_continuous_uncomfortable_m"]:
            return None
        return t1.steps(node), approach.approach_m

    def _single(
        self, s_node: int, start: Start, t1: survey.Tree, chain: _Chain, ji: int
    ) -> Iterator[Built]:
        found = self._approach(t1, chain.nodes[ji])
        if found is None:
            return
        steps, approach_m = found
        stem = route_search._forward(steps)
        looping = "loop" in self.shapes or "lollipop" in self.shapes
        for direction in (1, -1):
            if "out_and_back" in self.shapes:
                built = self._out_and_back(start, chain, ji, direction, stem, approach_m)
                if built:
                    yield built
            if looping:
                built = self._turn_home(start, chain, ji, direction, stem, approach_m, stem, self._home_tree(s_node, chain, ji, direction, set(), approach_m))
                if built and built.shape in self.shapes:
                    yield built

    def _group_graph(self, group_id: int) -> SearchGraph:
        """The search graph restricted to the edges of a group's blocks."""
        if group_id not in self._group_graphs:
            sub = SearchGraph()
            for index in self.groups[group_id]:
                for eid in self.blocks[index].planned.block["edge_ids"]:
                    for key in _leaf_keys(eid, self.graph, self.replaced):
                        edge = self.graph.edges_by_key[key]
                        sub.add_edge(edge)
                        sub.nodes[edge.a] = self.graph.nodes[edge.a]
                        sub.nodes[edge.b] = self.graph.nodes[edge.b]
            self._group_graphs[group_id] = sub
        return self._group_graphs[group_id]

    def _far_loops(self, sub: SearchGraph, join: int, loop_m: float, rng: random.Random) -> Iterator[Traversal]:
        """Loops from `join` through a group's own edges: out by the cheapest
        path to a far point, back by the cheapest path that avoids the way
        out. Each is at least INNER_LENGTH_SHARE[0] of loop_m, never over
        INNER_LENGTH_SHARE[1]."""
        out_tree = survey.weighted_tree(sub, join, self.costs, loop_m)
        lo, hi = INNER_FAR_SHARE
        far = [n for n, d in out_tree.length.items() if lo * loop_m <= d <= hi * loop_m]
        rng.shuffle(far)
        for node in far[:INNER_FAR_POINTS]:
            out = route_search._forward(out_tree.steps(node))
            back_tree = survey.weighted_tree(sub, join, self.costs, loop_m, _keys(out), [node])
            if node not in back_tree.cost:
                continue
            back = _reverse(route_search._forward(back_tree.steps(node)))
            if back is None:
                continue
            loop = out + back
            if (
                INNER_LENGTH_SHARE[0] * loop_m <= _length(loop) <= INNER_LENGTH_SHARE[1] * loop_m
                and _retraced_share(loop) < INNER_RETRACE_MAX
            ):
                yield loop

    def _inner_loops(self, group_id: int, s_node: int, start: Start, t1: survey.Tree) -> Iterator[Built]:
        """Loops formed inside a group's own network, entered by the cheapest
        approach: a lollipop when the approach is a real stem, a loop when the
        start is at the group."""
        sub = self._group_graph(group_id)
        reachable = sorted(
            (t1.cost[n], n) for n in sub.nodes if n in t1.cost and t1.length[n] <= APPROACH_SHARE_MAX * self.target_m
        )
        for _cost, join in reachable[:INNER_JOINS]:
            found = self._approach(t1, join)
            if found is None:
                continue
            steps, approach_m = found
            stem = route_search._forward(steps)
            stem_back = _reverse(stem)
            loop_m = self.target_m - 2.0 * approach_m
            shape = "lollipop" if approach_m >= MIN_STEM_M else "loop"
            if stem_back is None or loop_m < 2.0 * MIN_RUN_M or shape not in self.shapes:
                continue
            rng = random.Random(group_id * 7919 + join)
            for loop in self._far_loops(sub, join, loop_m, rng):
                yield Built(shape, start, stem + loop + stem_back, True)

    def _cut(self, edge: GraphEdge, frm: int, travel_m: float) -> tuple[GraphEdge, int] | None:
        """The first travel_m of `edge` walked from `frm`, as an edge of its
        own in this search's rows."""
        row = self.rows[edge.edge_key]
        forward = frm == edge.a
        split = network.split_edge_row(row, travel_m if forward else edge.length_m - travel_m)
        if split is None:
            return None
        mid = next(self.ids)
        before_key, after_key = next(self.ids), next(self.ids)
        self.rows[before_key], self.rows[after_key] = split["before"], split["after"]
        split["before"]["origin"] = split["after"]["origin"] = row.get("origin", edge.edge_key)
        if forward:
            key, piece = before_key, GraphEdge(before_key, edge.a, mid, split["before"]["length_m"], edge.allowed, [(before_key, True)])
        else:
            key, piece = after_key, GraphEdge(after_key, mid, edge.b, split["after"]["length_m"], edge.allowed, [(after_key, True)])
        if edge.edge_key in self.owner:
            self.owner[key] = self.owner[edge.edge_key]
        return piece, frm

    def _partial_run(self, chain: _Chain, ji: int, direction: int, want_m: float) -> Traversal | None:
        """The way along `chain` from nodes[ji] for about want_m, the last
        edge cut where the distance runs out; the whole remainder when the
        chain is shorter."""
        out: Traversal = []
        run_m = 0.0
        i = ji
        while run_m < want_m - CUT_SLACK_M:
            e = i if direction > 0 else i - 1
            if not 0 <= e < len(chain.edges):
                break
            edge, frm = chain.edges[e], chain.nodes[i]
            if edge.traverse(frm) is None:
                return None
            left = want_m - run_m
            if edge.length_m <= left + CUT_SLACK_M:
                out.append((edge, frm))
                run_m += edge.length_m
                i += direction
                continue
            cut = self._cut(edge, frm, left)
            if cut is not None:
                out.append(cut)
                run_m += left
            break
        return out if run_m >= MIN_RUN_M else None

    @staticmethod
    def _end_node(traversal: Traversal) -> int:
        edge, frm = traversal[-1]
        step = edge.traverse(frm)
        assert step is not None
        return step[0]

    def _next_chains(
        self, node: int, left_m: float, used: set[int], group_id: int, limit: int
    ) -> list[tuple[Traversal, _Chain, int, int]]:
        """From `node`, the connectors to the `limit` block chains worth most
        for the distance still wanted, best first (a group the route is
        already in counting SAME_GROUP_BONUS times over), each with where on
        its chain to join it and in which direction."""
        tree = self._tree(("extension", node), node, EXTENSION_MAX_COST_M)
        options: dict[int, tuple[float, _Chain, int, int]] = {}
        for prep in self.blocks:
            bonus = SAME_GROUP_BONUS if prep.planned.block["section_id"] == group_id else 1.0
            for chain in prep.chains:
                if chain.keys & used:
                    continue
                for i, n in enumerate(chain.nodes):
                    if n not in chain.joins or n not in tree.cost:
                        continue
                    for direction in (1, -1):
                        avail = chain.cum[-1] - chain.cum[i] if direction > 0 else chain.cum[i]
                        if avail < MIN_RUN_M:
                            continue
                        value = bonus * prep.planned.density * min(avail, left_m) / MILE_M
                        value -= survey.ACCESS_COST_PER_WEIGHTED_MI * tree.cost[n] / MILE_M
                        if id(chain) not in options or value > options[id(chain)][0]:
                            options[id(chain)] = (value, chain, i, direction)
        best = sorted(options.values(), key=lambda o: (-o[0], o[2], o[3]))[:limit]
        return [(route_search._forward(tree.steps(chain.nodes[i])), chain, i, direction) for _v, chain, i, direction in best]

    def _group_of(self, chain: _Chain) -> int:
        return self.blocks[chain.block].planned.block["section_id"]

    def _extension(
        self, node: int, left_m: float, used: set[int], group_id: int
    ) -> tuple[Traversal, float, _Chain] | None:
        """From `node`, a connector to the next block chain and the run along
        it (the last edge cut where the distance runs out), with the distance
        both cover."""
        found = self._next_chains(node, left_m, used, group_id, 1)
        if not found:
            return None
        connector, chain, i, direction = found[0]
        run = self._partial_run(chain, i, direction, left_m)
        if run is None:
            return None
        return connector + run, _length(connector) + _length(run), chain

    def _out_leg(
        self, chain: _Chain, ji: int, direction: int, stem: Traversal, approach_m: float, want_m: float
    ) -> Traversal | None:
        """The way out: the stem, the run along `chain`, and, when the chain
        ends before want_m, further blocks reached by short connectors."""
        run = self._partial_run(chain, ji, direction, want_m - approach_m)
        if run is None:
            return None
        out = stem + run
        have = approach_m + _length(run)
        used = _keys(out)
        group_id = self._group_of(chain)
        for _ in range(MAX_EXTENSIONS):
            if want_m - have < MIN_RUN_M:
                break
            extension = self._extension(self._end_node(out), want_m - have, used, group_id)
            if extension is None:
                break
            out = out + extension[0]
            have += extension[1]
            used |= _keys(extension[0])
            group_id = self._group_of(extension[2])
        return out

    def _out_and_back(
        self, start: Start, chain: _Chain, ji: int, direction: int, stem: Traversal, approach_m: float
    ) -> Built | None:
        out = self._out_leg(chain, ji, direction, stem, approach_m, self.target_m / 2.0)
        if out is None:
            return None
        back = _reverse(out)
        return None if back is None else Built("out_and_back", start, out + back, False)

    def _home_tree(self, s_node: int, chain: _Chain, ji: int, direction: int, used: set[int], out_m: float) -> survey.Tree:
        """The cheapest ways home from the start to the nodes of `chain` on
        from ji, where the chain and what has been run (`used`) cost more to
        reuse, explored as far as the distance left after `out_m` allows. Only
        the tree for a chain alone is kept."""
        penalized = frozenset(used | chain.keys)
        if used:
            reach = max(self.target_m - out_m, 0.0) * HOME_REACH_SLACK
            ahead = chain.nodes[ji + 1:] if direction > 0 else chain.nodes[:ji]
            return survey.weighted_tree(self.graph, s_node, self.costs, reach, penalized, ahead)
        return self._tree(("home", s_node, id(chain)), s_node, self.target_m * RETURN_TREE_SHARE, penalized, chain.nodes)

    def _chained_from(
        self, s_node: int, start: Start, t1: survey.Tree, chain: _Chain, joins: Sequence[int]
    ) -> Iterator[Built]:
        """Loops that chain further blocks on from a chain, entered at its
        cheapest join: out over this chain and on to further blocks
        (MAX_LOOP_BLOCKS in all, LOOP_BRANCHING choices for each next one, the
        group the route is in first), each reached by a connector within
        EXTENSION_MAX_COST_M, turning home at the node of a later chain where
        the cheapest way back that avoids what was run makes the target
        distance. A chain that can fill the distance alone is left to the
        single-block loop."""
        for ji in joins:
            found = self._approach(t1, chain.nodes[ji])
            if found is None:
                continue
            steps, approach_m = found
            stem = route_search._forward(steps)
            for direction in (1, -1):
                for built in self._chain_on(s_node, start, t1, chain, ji, direction, stem, stem, approach_m, set(), 0):
                    if built.shape in self.shapes:
                        yield built

    def _chain_on(
        self, s_node: int, start: Start, t1: survey.Tree, chain: _Chain, ji: int, direction: int, stem: Traversal,
        out: Traversal, out_m: float, used: set[int], stage: int,
    ) -> Iterator[Built]:
        # The approach tree stands in for the way home while the route is still
        # too short to turn; the penalized tree is worked out only once it can.
        reachable = self._totals(chain, ji, direction, out_m, t1)
        best_m = max(reachable.values(), default=0.0)
        if stage and best_m >= self.dmin:
            built = self._turn_home(
                start, chain, ji, direction, out, out_m, stem, self._home_tree(s_node, chain, ji, direction, used, out_m)
            )
            if built is not None:
                yield built
        last = len(chain.nodes) - 1 if direction > 0 else 0
        run = chain.run(ji, last)
        if best_m >= self.target_m or run is None or last == ji or stage + 1 >= MAX_LOOP_BLOCKS:
            return
        out, out_m, used = out + run, out_m + _length(run), used | _keys(run)
        for connector, nxt, i, d in self._next_chains(
            self._end_node(out), max(self.target_m - best_m, MIN_RUN_M), used | _keys(stem), self._group_of(chain),
            LOOP_BRANCHING,
        ):
            if time.monotonic() > self.deadline:
                return
            yield from self._chain_on(
                s_node, start, t1, nxt, i, d, stem, out + connector, out_m + _length(connector),
                used | _keys(connector), stage + 1,
            )

    def _turn_home(
        self, start: Start, chain: _Chain, ji: int, direction: int, out: Traversal, out_m: float, stem: Traversal,
        tree: survey.Tree,
    ) -> Built | None:
        """`out`, then the chain run to a node, and home by the tree's path:
        of the nodes that make the requested distance (the closest few to the
        target if none does), the one whose way home retraces the least."""
        totals = self._totals(chain, ji, direction, out_m, tree)
        if not totals:
            return None
        by_miss = sorted(totals, key=lambda k: (abs(totals[k] - self.target_m), k))
        fitting = [k for k in by_miss if self.dmin <= totals[k] <= self.dmax]
        best: tuple[float, float, Built] | None = None
        for k in (fitting or by_miss[:1])[:TURN_CANDIDATES]:
            run = chain.run(ji, k)
            back = _reverse(route_search._forward(tree.steps(chain.nodes[k])))
            if run is None or back is None:
                continue
            ran = out + run
            total = _length(back)
            reuse = _shared_m(back, ran) / total if total > 0 else 0.0
            shape = _return_shape(back, ran, stem)
            key = (round(reuse / TURN_REUSE_STEP), abs(totals[k] - self.target_m))
            if best is None or key < best[:2]:
                best = (*key, Built(shape, start, ran + back, shape != "out_and_back"))
        return None if best is None else best[2]

    # -----------------------------------------------------------------------
    # Ranking
    # -----------------------------------------------------------------------

    def rank(self, built: Built, segments: Sequence[tuple[int, bool]]) -> dict:
        """The terms of a built candidate's score and the sections it runs."""
        miles: Counter[int] = Counter()
        off_block_cost_m = 0.0
        distance_m = 0.0
        for edge_id, _forward in segments:
            length_m = self.rows[edge_id]["length_m"]
            distance_m += length_m
            owner = self.owner.get(edge_id)
            if owner is None:
                off_block_cost_m += self.costs[edge_id][0]
            else:
                miles[owner] += length_m
        quality = sum(self.blocks[i].planned.density * m / MILE_M for i, m in miles.items())
        miss = max(self.dmin - distance_m, distance_m - self.dmax, 0.0)
        fit = max(0.0, 1.0 - FIT_SLOPE * miss / max(self.target_m, 1.0))
        loop = 1.0 + self.loop_bonus if built.loop_like else 1.0
        access_cost = survey.ACCESS_COST_PER_WEIGHTED_MI * off_block_cost_m / 2.0 / MILE_M
        used = [
            {
                "section_id": self.blocks[i].planned.block["section_id"],
                "block_id": self.blocks[i].planned.block["block_id"],
                "name": self.blocks[i].planned.block["name"],
                "length_m": m,
            }
            for i, m in sorted(miles.items(), key=lambda kv: -kv[1])
        ]
        return {
            "score": quality * loop * fit - access_cost,
            "quality_mi": quality,
            "loop": loop,
            "fit": fit,
            "access_cost": access_cost,
            "sections_used": used,
        }
