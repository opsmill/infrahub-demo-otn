"""Cover an ordered section route with carriers joined at an O-E-O device.

**This module is a search, and it exists because a measurement said the platform
cannot do this one.** The rest of the feature's route discovery is native and
stays native: `client.traverse_paths` finds the ordered section route and
`generators/optical_service.py` calls it with nothing but two relationship
identifiers and a depth cap. What the traversal cannot do is pick the carrier
chain out of the graph, and that half is here.

**The native-first record, because the constitution requires it to be findable
from the code.** Both entries live in `specs/017-oeo-cross-connect/research.md`.

R-008 measured `traverse_paths` against a live instance with a throwaway
`OtnOduSwitch` at Milan holding two carriers that terminate there. Filtered on
the device-to-carrier edge alone the call returns **zero** paths, because a
ROADM has no edge to a carrier. Widen the filter until a carrier is reachable
and the intended chain comes back once among 65 at depth 6, with **48** of the
rest joining two carriers at `oms-fra-mil`, the one section all 71 shipped
wavelengths cross, and no device between them. The control probe is what
settles it: drop the device edge from the filter and the same chain-shaped paths
still return. So the junction is not a fact about which edges exist, and no
`relationship_filter` separates a junction from a hand-off in the middle of a
section.

R-012 item 1 asked the same question of GraphQL filters and measured two answers.
A relationship filter reaches exactly one hop, so `carrier -> device -> site` is
not an argument the schema has; and the predicate is a **self-join**, asking
whether *two* carriers share *one* device, which is not a relation a filter can
express between two members of its own result set. Both were run, not reasoned.

**What keeps this a cover rather than a graph search.** It never looks at the
graph. Its input is the ordered route the traversal already returned, at most
`MAX_COVER_SECTIONS` sections long, and it enumerates the ways to cut that
ordered list into contiguous runs. Four sections have three interior boundaries,
so there are at most eight cuttings to consider, and a route longer than the cap
is refused rather than walked. Nothing here is iterative, nothing relaxes and
nothing is shortest anything.

**Pure.** No `infrahub_sdk`, no I/O, no fetching. Every input is already in the
payload `queries/optical_service.gql` returns, which is what lets the whole
decision layer be tested with no server running.

**Free of preference.** A direct wavelength beats a chain whenever both serve the
route, because a chain costs a regeneration and latency, and that is FR-009. It
is a ranking rule and it lives in the caller: `generators/optical_service.py`
plans the direct wavelength first and only asks for a chain when no candidate
route can carry the service on one. Nothing here knows that a chain is the
second choice, and nothing here evaluates a budget or a free slot either.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterator, Sequence

MAX_COVER_SECTIONS = 4
"""How many ordered sections a route handed to this module may hold.

The same four as `MAX_ROUTE_SECTIONS` in `generators/optical_service.py`, and it
has to be: the traversal never returns a longer route, so covering one would be
covering a route that cannot exist. `tests/unit/test_chains.py` asserts the two
numbers are equal by loading the generator, so the pair cannot drift.

Four is also the figure the topology gives. The widest shortest route on the
shipped plant is Brussels to Vienna at four sections, which the same test
recomputes from `objects/` by breadth-first search rather than trusting this
comment.
"""

MIN_CHAIN_SEGMENTS = 2
"""A chain has a junction, so it has at least two segments.

The one-segment cover is deliberately not returned. It is a direct wavelength,
which is what the generator's grooming and lighting paths already do, and
emitting it here would give two modules an opinion on the same candidate.
"""

MAX_CHAIN_SEGMENTS = MAX_COVER_SECTIONS
"""The derived ceiling on segments, not a chosen one.

A segment covers at least one section, so a route of at most
`MAX_COVER_SECTIONS` sections cannot be cut into more than that many contiguous
runs, and it carries at most one fewer junction than it has segments. FR-011a
asks for a bound derived from the topology and asserted by a test rather than
assumed, and this is the derivation: the route cap bounds the segment count,
and nothing about the shipped scenarios needing two segments is allowed to
narrow it silently.
"""


@dataclass(frozen=True)
class RouteSection:
    """One section of the ordered route, with the optical node at each end.

    `node_a` is where light enters this section and `node_z` where it leaves, so
    a route is contiguous when each section's `node_z` is the next one's
    `node_a`. `find_chains` checks that rather than assuming it: an unordered
    section list would produce a cover that looks like a circuit and is not one,
    which is the whole class of failure R-008 found in the traversal.

    The nodes are ROADM names, because a section terminates on a ROADM. The site
    a junction is named with is reached through the **device** rather than
    through the section: a device already knows its site and the other optical
    nodes at it, and `queries/optical_service.gql` selects that in one hop.
    """

    name: str
    node_a: str
    node_z: str


@dataclass(frozen=True)
class CarrierSpan:
    """One wavelength that already exists, and the sections it crosses.

    A set rather than an ordered tuple, because `OtnOpticalCarrier.sections` is a
    `cardinality: many` relationship and an Infrahub relationship hands back a
    set. `budget.order_sections` is what puts one in traversal order, and this
    module never needs that: it compares a carrier's sections against a run of
    the route, and set equality is the comparison.

    **Set equality, not containment, and the reason is the plant rather than the
    code.** An ODU is added and dropped where its wavelength terminates and
    nowhere in between, and nothing in this schema is an ODU cross-connect at an
    intermediate ROADM. So a carrier crossing the run and one section more
    delivers the client one site past where the segment ends, and a carrier
    crossing less of it reaches nothing like the far end.
    `generators/optical_service.py::_line_options` applies the same test to the
    single-wavelength case for the same reason, and states the same honest limit:
    two different routes built from the same sections would compare equal, which
    on this topology cannot happen because a set of sections forming a path
    between two ROADMs fixes its endpoints.
    """

    name: str
    section_names: frozenset[str]


@dataclass(frozen=True)
class JunctionDevice:
    """One `OtnOduSwitch`, and everything the junction predicate reads.

    `site_nodes` is the optical elements at this device's site, which is how the
    module decides that two carriers meeting at a ROADM meet **where this device
    is**. Reached through `OtnOduSwitch.site` and that site's `devices`, one hop
    each, which is the shape R-012 item 1 records as staying native.

    `carrier_names` is `OtnOduSwitch.carriers`, the wavelengths this device
    terminates. A device with an empty set contributes no junction, which is the
    correct reading rather than an edge case: a device that terminates nothing
    joins nothing.
    """

    name: str
    site: str
    site_nodes: frozenset[str]
    carrier_names: frozenset[str]


@dataclass(frozen=True)
class ChainSegment:
    """One segment of a candidate chain: a carrier, and the junction it ends at.

    The `(carrier, junction site)` pair FR-008a asks for is `carrier_name` and
    `junction_site`. Three more fields travel with it because the caller cannot
    reconstruct them without redoing the cover: `section_names` is the run of the
    route this carrier covers, in route order, which is what the segment's own
    budget is computed over; `start_node` is the optical node the segment is
    walked from, which `budget.evaluate_route` needs to orient the sections; and
    `junction_device` names the `OtnOduSwitch` the light is rebuilt in, which is
    what charges the framing delay and what a report has to name.

    All three junction fields are `None` on the last segment, and only on the
    last segment. `Chain` refuses any other shape.
    """

    carrier_name: str
    section_names: tuple[str, ...]
    start_node: str
    junction_node: str | None
    junction_site: str | None
    junction_device: str | None

    @property
    def is_last(self) -> bool:
        return self.junction_device is None


@dataclass(frozen=True)
class Chain:
    """One candidate cover of the whole route, segment by segment.

    Every invariant a caller could otherwise get wrong in silence is checked
    here, in the manner of `budget.RouteBudget`, because a cover that is wrong in
    one of these ways still reads like a circuit:

    - At least `MIN_CHAIN_SEGMENTS` segments, since a chain with no junction is a
      direct wavelength and this module does not offer one.
    - Only the last segment lacks a junction, and every earlier one has all three
      of its junction fields set. A missing device in the middle is two segments
      joined by nothing, which is the shape R-008's traversal returned 48 times.
    - No section covered twice. Two carriers sharing a section is the mid-section
      hand-off, and it is not a junction because nothing there terminates the
      light and re-originates it.
    """

    segments: tuple[ChainSegment, ...]

    def __post_init__(self) -> None:
        if len(self.segments) < MIN_CHAIN_SEGMENTS:
            raise ValueError(f"a chain has at least {MIN_CHAIN_SEGMENTS} segments, not {len(self.segments)}")
        for segment in self.segments[:-1]:
            if segment.junction_device is None or segment.junction_site is None or segment.junction_node is None:
                raise ValueError(
                    f"segment on {segment.carrier_name} ends at no device, so the two segments either side of "
                    "it are joined by nothing"
                )
        if not self.segments[-1].is_last:
            raise ValueError(
                f"the last segment on {self.segments[-1].carrier_name} names junction "
                f"{self.segments[-1].junction_device}, which would regenerate light nobody carries on"
            )
        covered = [name for segment in self.segments for name in segment.section_names]
        if len(set(covered)) != len(covered):
            raise ValueError(f"the segments of this chain cover {sorted(covered)}, which repeats a section")

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def carrier_names(self) -> tuple[str, ...]:
        return tuple(segment.carrier_name for segment in self.segments)

    @property
    def junctions(self) -> tuple[tuple[str, str], ...]:
        """Every junction as `(device, site)`, in route order. One fewer than segments."""
        return tuple(
            (segment.junction_device, segment.junction_site)
            for segment in self.segments
            if segment.junction_device is not None and segment.junction_site is not None
        )

    @property
    def section_names(self) -> tuple[str, ...]:
        """The whole route this chain covers, in order."""
        return tuple(name for segment in self.segments for name in segment.section_names)

    @property
    def key(self) -> str:
        """A unique, stable name for this cover, and the last ranking term.

        Every carrier and every junction device, in route order, so two covers of
        one route can never compare equal unless they are the same cover. It is
        what makes `find_chains` total: an Infrahub relationship hands back a set
        and a payload can arrive in any order, so a caller reading the first
        chain has to be reading the same one on every run.
        """
        parts: list[str] = []
        for segment in self.segments:
            parts.append(segment.carrier_name)
            if segment.junction_device is not None:
                parts.append(segment.junction_device)
        return "|".join(parts)


def joins(device: JunctionDevice, node: str, first: CarrierSpan, second: CarrierSpan) -> bool:
    """Whether this device genuinely joins these two carriers at this node.

    **The whole junction predicate, written once, because the graph does not
    carry one.** Three conditions, all of them necessary:

    1. The carriers meet at `node`, which they do by construction: `node` is the
       boundary between two contiguous runs of one ordered route.
    2. That node is at this device's site. A device that terminates both
       wavelengths somewhere else in the network cannot hand one to the other
       here.
    3. `OtnOduSwitch.carriers` holds **both**. One is not a junction: the light
       arriving has to be terminated and the light leaving has to be originated,
       by the same device.

    **This function is why R-008's 48 phantom paths are not candidates.** Every
    one of them joined two carriers at `oms-fra-mil` with nothing between them,
    and the traversal returned them as structurally valid paths because the edge
    that reaches a carrier and the edge that joins two carriers are the same
    edge. There is no `relationship_filter` that admits one and refuses the
    other, so the predicate is evaluated here instead, over data the caller
    already fetched. Weaken any of the three conditions and a hand-off in the
    middle of a section becomes provisionable.
    """
    return node in device.site_nodes and first.name in device.carrier_names and second.name in device.carrier_names


def junction_at(
    node: str,
    first: CarrierSpan,
    second: CarrierSpan,
    devices: Sequence[JunctionDevice],
) -> JunctionDevice | None:
    """The device that joins two carriers at one node, or `None` for no junction.

    The lowest-named device when several qualify, so a payload that lists two
    cross-connects at one site produces the same chain on every run. Two devices
    both terminating both wavelengths is a redundant pair rather than a choice
    worth ranking, and this module has no budget with which to rank it.
    """
    qualifying = sorted(
        (device for device in devices if joins(device, node, first, second)),
        key=lambda device: device.name,
    )
    return qualifying[0] if qualifying else None


def _validate_route(route: Sequence[RouteSection]) -> None:
    """Refuse a route this module cannot honestly cover.

    The length cap is the bound that keeps the enumeration small, and exceeding
    it is a caller that widened the traversal without widening this. The
    contiguity check is the one that matters more: sections in the wrong order
    still cut into runs, still match carriers by set equality, and produce a
    cover for a circuit that does not exist.
    """
    if not route:
        raise ValueError("cannot cover an empty route")
    if len(route) > MAX_COVER_SECTIONS:
        raise ValueError(
            f"this route crosses {len(route)} sections and the cover is bounded at {MAX_COVER_SECTIONS}: "
            f"{', '.join(section.name for section in route)}"
        )
    names = [section.name for section in route]
    if len(set(names)) != len(names):
        raise ValueError(f"the route repeats a section: {sorted(names)}")
    for before, after in zip(route, route[1:]):
        if before.node_z != after.node_a:
            raise ValueError(
                f"{before.name} ends at {before.node_z} and {after.name} starts at {after.node_a}, "
                "so this section list is not one ordered route"
            )


def _cuttings(length: int, max_segments: int) -> Iterator[tuple[int, ...]]:
    """Every way to cut an ordered route into contiguous runs, as cut positions.

    A route of N sections has N-1 interior boundaries and a cutting is a subset
    of them, so this yields at most 2^(N-1) tuples: eight at the four-section
    cap, and the caller has already refused anything longer. That is the whole
    of the combinatorics, and it is why this module is an enumeration over a
    fixed handful rather than a search.

    Only cuttings producing between `MIN_CHAIN_SEGMENTS` and `max_segments` runs
    are yielded, and they come out fewest-runs-first so a two-segment cover is
    considered before a three-segment one.
    """
    boundaries = range(1, length)
    for count in range(MIN_CHAIN_SEGMENTS - 1, min(max_segments, length)):
        for cuts in combinations(boundaries, count):
            yield cuts


def _runs(route: Sequence[RouteSection], cuts: tuple[int, ...]) -> list[tuple[RouteSection, ...]]:
    """One cutting, as the contiguous runs of sections it produces."""
    edges = (0, *cuts, len(route))
    return [tuple(route[start:stop]) for start, stop in zip(edges, edges[1:])]


def _covering(carriers: Sequence[CarrierSpan], run: tuple[RouteSection, ...]) -> list[CarrierSpan]:
    """Every carrier whose sections are exactly this run's, by name.

    Sorted, because a payload's order is a server's answer and a candidate list
    that follows it makes the chosen chain depend on it.
    """
    wanted = frozenset(section.name for section in run)
    matching = (carrier for carrier in carriers if carrier.section_names == wanted)
    return sorted(matching, key=lambda carrier: carrier.name)


def find_chains(
    route: Sequence[RouteSection],
    carriers: Sequence[CarrierSpan],
    devices: Sequence[JunctionDevice],
    max_segments: int = MAX_CHAIN_SEGMENTS,
) -> tuple[Chain, ...]:
    """Every candidate chain covering this route, in a total order.

    The route is the ordered section list the traversal returned, `carriers` are
    the wavelengths that already exist with the sections each one crosses, and
    `devices` are the `OtnOduSwitch` records with the carriers each one
    terminates. Nothing is fetched here and nothing is written.

    What comes back is a cover per candidate, not a decision. Whether a segment's
    line container has room for the client, whether each segment closes
    optically, and whether a direct wavelength would have been better are three
    questions this module deliberately does not answer: the first two need the
    payload and the budget, and the third is FR-009's ranking rule. The caller
    owns all three.

    The result is sorted by segment count and then by `Chain.key`, which is
    unique, so no two candidates can tie and the same inputs produce the same
    first candidate on every run. `tests/unit/test_chains.py` sorts both sides of
    its comparison anyway, because a test that relies on this ordering would
    still pass if the ordering silently became partial.

    **One filter, and it changes no answer.** A carrier no device terminates
    cannot appear in any chain, because every segment of a chain of two or more
    abuts a junction and a junction needs the device to hold that carrier. So
    dropping those carriers before the enumeration removes candidates that would
    all have failed `joins`, and on the shipped dataset that is 71 wavelengths
    reduced to the handful a cross-connect actually terminates.
    """
    _validate_route(route)
    if max_segments < MIN_CHAIN_SEGMENTS:
        raise ValueError(f"a chain has at least {MIN_CHAIN_SEGMENTS} segments, so max_segments={max_segments} is empty")
    if max_segments > MAX_CHAIN_SEGMENTS:
        raise ValueError(f"max_segments={max_segments} exceeds the derived bound of {MAX_CHAIN_SEGMENTS}")

    terminated = {name for device in devices for name in device.carrier_names}
    usable = [carrier for carrier in carriers if carrier.name in terminated]
    if not usable:
        return ()

    chains: list[Chain] = []
    for cuts in _cuttings(len(route), max_segments):
        runs = _runs(route, cuts)
        candidates = [_covering(usable, run) for run in runs]
        if any(not options for options in candidates):
            # A run no wavelength covers end to end. Nothing about a different
            # carrier on another run can fix it, so the cutting is dropped whole.
            continue
        chains.extend(_extend(runs, candidates, chosen=[], junctions=[], devices=devices))
    return tuple(sorted(chains, key=lambda chain: (chain.segment_count, chain.key)))


def _extend(
    runs: list[tuple[RouteSection, ...]],
    candidates: list[list[CarrierSpan]],
    chosen: list[CarrierSpan],
    junctions: list[JunctionDevice],
    devices: Sequence[JunctionDevice],
) -> Iterator[Chain]:
    """Depth-first over one cutting's runs, refusing at the first bad junction.

    Recursive because the runs are, and safe because the recursion is as deep as
    the route is long, which `_validate_route` has capped at
    `MAX_COVER_SECTIONS`. The junction predicate is applied as the chain grows
    rather than at the end, so a pair no device joins prunes every extension of
    it instead of being enumerated and discarded.
    """
    position = len(chosen)
    if position == len(runs):
        yield _chain(runs, chosen, junctions)
        return
    for carrier in candidates[position]:
        if position == 0:
            yield from _extend(runs, candidates, [*chosen, carrier], junctions, devices)
            continue
        node = runs[position][0].node_a
        device = junction_at(node, chosen[-1], carrier, devices)
        if device is None:
            continue
        yield from _extend(runs, candidates, [*chosen, carrier], [*junctions, device], devices)


def _chain(
    runs: list[tuple[RouteSection, ...]],
    chosen: Sequence[CarrierSpan],
    junctions: Sequence[JunctionDevice],
) -> Chain:
    """Assemble one validated cover. `Chain.__post_init__` re-checks the shape."""
    segments: list[ChainSegment] = []
    for position, (run, carrier) in enumerate(zip(runs, chosen)):
        device = junctions[position] if position < len(junctions) else None
        segments.append(
            ChainSegment(
                carrier_name=carrier.name,
                section_names=tuple(section.name for section in run),
                start_node=run[0].node_a,
                junction_node=run[-1].node_z if device is not None else None,
                junction_site=device.site if device is not None else None,
                junction_device=device.name if device is not None else None,
            )
        )
    return Chain(segments=tuple(segments))
