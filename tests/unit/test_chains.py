"""The carrier cover, pinned against the shipped plant.

**This file is the only defence the feature has against a plausible wrong
answer.** R-008 measured that `client.traverse_paths` cannot pick a carrier chain
out of the graph: filtered on the device-to-carrier edge alone it returns zero
paths, and widened until a carrier is reachable it returns 65 at depth 6, of
which one is the intended chain and **48** join two carriers at `oms-fra-mil`
with no device between them. A wrong cover is not distinguishable from a right
one by shape, so the contract is not "the module runs", it is "these inputs
produce exactly this set".

Both halves are here and the negative is the one that matters. The positive
pins the exact chains for a named endpoint pair. The negative pins that a pair
of wavelengths meeting on a shared section with no device holding both is
**absent**, which is the case the traversal accepted 48 times out of 65.

**The route, the sections and the sites come from `objects/`.** The wavelengths
and the O-E-O devices are fixtures, because they are what Phase 6 of this
feature adds and they are not in the dataset yet. When T026 to T028 emit them,
the fixtures here are replaced by the shipped objects and the expected set is
retaken from a run rather than adjusted to fit.

Both sides of every comparison are sorted. `find_chains` returns a total order
and a test that leaned on it would still pass if that order silently became
partial, which is exactly the failure FR-011 asks to be guarded against.
"""

import importlib.util
from collections import defaultdict, deque
from functools import cache
from typing import Any

import pytest

from infrahub_demo_otn.chains import (
    MAX_CHAIN_SEGMENTS,
    MAX_COVER_SECTIONS,
    MIN_CHAIN_SEGMENTS,
    CarrierSpan,
    Chain,
    ChainSegment,
    JunctionDevice,
    RouteSection,
    find_chains,
    joins,
)
from tests.unit.conftest import REPO_ROOT, objects_of_kind

GENERATOR_PATH = REPO_ROOT / "generators" / "optical_service.py"

FRANKFURT_NODES = frozenset({"roadm-fra-01"})
"""The optical nodes at Frankfurt, which is one ROADM on the shipped plant.

Named rather than derived, because `OtnSite.devices` also holds the routers,
transponders and amplifiers there and the junction predicate only ever asks
whether the boundary ROADM is among them.
"""


@cache
def _sections() -> dict[str, tuple[str, str]]:
    """Every shipped section, as the pair of ROADMs it terminates on."""
    return {
        str(record["name"]): (str(record["roadm_a"]), str(record["roadm_b"]))
        for record in objects_of_kind("OtnOpticalMultiplexSection")
    }


@cache
def _carrier_families() -> dict[frozenset[str], tuple[str, ...]]:
    """The shipped wavelengths, grouped by the set of sections they cross.

    Five families over 71 wavelengths, and every one of them includes
    `oms-fra-mil`. That is not a coincidence in the dataset, it is the fact
    R-008's 48 phantom junctions came from: one section that every wavelength
    crosses is a shared low-cardinality object two carriers can appear to meet
    at.
    """
    families: dict[frozenset[str], list[str]] = defaultdict(list)
    for record in objects_of_kind("OtnOpticalCarrier"):
        families[frozenset(str(name) for name in record["sections"])].append(str(record["name"]))
    return {sections: tuple(sorted(names)) for sections, names in families.items()}


def _route(*nodes: str) -> tuple[RouteSection, ...]:
    """An ordered route through the named ROADMs, using the shipped sections.

    The section joining each consecutive pair is looked up rather than named, so
    a route in this file cannot claim a corridor the plant does not have.
    """
    steps: list[RouteSection] = []
    for before, after in zip(nodes, nodes[1:]):
        found = [name for name, ends in _sections().items() if set(ends) == {before, after}]
        assert len(found) == 1, f"{before} to {after} is not one section on the shipped plant: {found}"
        steps.append(RouteSection(name=found[0], node_a=before, node_z=after))
    return tuple(steps)


def _span(name: str, *sections: str) -> CarrierSpan:
    return CarrierSpan(name=name, section_names=frozenset(sections))


def _device(name: str, site: str, nodes: frozenset[str], *carriers: str) -> JunctionDevice:
    return JunctionDevice(name=name, site=site, site_nodes=nodes, carrier_names=frozenset(carriers))


def _pairs(chains: tuple[Chain, ...]) -> list[tuple[tuple[str, str | None], ...]]:
    """Every chain as the ordered `(carrier, junction site)` pairs FR-008a names."""
    return sorted(
        tuple((segment.carrier_name, segment.junction_site) for segment in chain.segments) for chain in chains
    )


# ----------------------------------------------------------------------
# The positive: the exact chain set for a named endpoint pair
# ----------------------------------------------------------------------

PARIS_TO_BERLIN = _route("roadm-par-01", "roadm-fra-01", "roadm-ber-01")
"""Two sections, `oms-par-fra` then `oms-ber-fra`, meeting at Frankfurt.

The pair R-008 probed, minus the Milan detour. Frankfurt is where the two
sections abut, so it is the one place on this route a junction can be, and the
cover has exactly one cutting to consider.
"""

WEST = _span("oc-seg-par-fra", "oms-par-fra")
EAST = _span("oc-seg-fra-ber", "oms-ber-fra")
SPARE_WEST = _span("oc-spare-par-fra", "oms-par-fra")
FRANKFURT_SWITCH = _device("oeo-fra-01", "Frankfurt", FRANKFURT_NODES, WEST.name, EAST.name, SPARE_WEST.name)


def test_the_named_pair_covers_as_exactly_one_chain() -> None:
    """Paris to Berlin, two wavelengths, one device: one chain and no other.

    The exact set, not a membership test. A cover that also returned the
    reversed pair, or the same pair twice, or a one-segment cover would pass a
    membership assertion and be wrong.
    """
    chains = find_chains(PARIS_TO_BERLIN, [WEST, EAST], [FRANKFURT_SWITCH])
    assert _pairs(chains) == sorted([(("oc-seg-par-fra", "Frankfurt"), ("oc-seg-fra-ber", None))])
    assert chains[0].junctions == (("oeo-fra-01", "Frankfurt"),)
    assert chains[0].section_names == ("oms-par-fra", "oms-ber-fra")
    assert chains[0].segments[0].start_node == "roadm-par-01"
    assert chains[0].segments[1].start_node == "roadm-fra-01"


def test_every_qualifying_wavelength_produces_its_own_candidate() -> None:
    """Two wavelengths on the western half, so two chains and both are returned.

    Choosing between them is not this module's job: the free slots and the budget
    decide, and both live in the caller. What is asserted here is that neither
    candidate is dropped, because a cover that returned one of the two would
    refuse a service the plant can carry as soon as the other wavelength is full.
    """
    chains = find_chains(PARIS_TO_BERLIN, [WEST, SPARE_WEST, EAST], [FRANKFURT_SWITCH])
    assert _pairs(chains) == sorted(
        [
            (("oc-seg-par-fra", "Frankfurt"), ("oc-seg-fra-ber", None)),
            (("oc-spare-par-fra", "Frankfurt"), ("oc-seg-fra-ber", None)),
        ]
    )


def test_the_lowest_named_device_wins_when_two_could_join() -> None:
    """A redundant pair of cross-connects is not two candidates.

    Both terminate both wavelengths, so both make the same junction. Ranking them
    would need a budget and this module has none, so the order is by name and the
    result is one chain rather than two identical covers.
    """
    second = _device("oeo-fra-02", "Frankfurt", FRANKFURT_NODES, WEST.name, EAST.name)
    chains = find_chains(PARIS_TO_BERLIN, [WEST, EAST], [second, FRANKFURT_SWITCH])
    assert len(chains) == 1
    assert chains[0].junctions == (("oeo-fra-01", "Frankfurt"),)


# ----------------------------------------------------------------------
# The negative: the 48 the traversal accepted
# ----------------------------------------------------------------------

PARIS_TO_VIENNA = _route("roadm-par-01", "roadm-fra-01", "roadm-mil-01", "roadm-vie-01")
"""Three sections: `oms-par-fra`, `oms-fra-mil`, `oms-vie-mil`.

The route R-008's phantoms lived on. Two shipped families cover parts of it,
`{oms-par-fra, oms-fra-mil}` and `{oms-fra-mil, oms-vie-mil}`, and they overlap
on `oms-fra-mil`.
"""


def test_a_pair_meeting_on_a_shared_section_is_absent() -> None:
    """**The assertion that matters most.** Two shipped wavelengths, a real device
    at Milan holding both, and no chain.

    `oc-ch081-par-mil` crosses `oms-par-fra` and `oms-fra-mil`;
    `oc-ch089-fra-vie` crosses `oms-fra-mil` and `oms-vie-mil`. They share
    `oms-fra-mil`, so the only place they meet is the middle of a section, where
    nothing terminates the light and re-originates it. R-008's traversal returned
    48 paths of exactly this shape out of 65 and could not be filtered to exclude
    them, because the edge that reaches a carrier is the edge that appears to join
    two.

    The device is real here and holds both wavelengths, which is what makes the
    test sharp: the junction predicate is not what rejects this pair, the cover is.
    A carrier covers a contiguous run of the route or it covers nothing, and two
    runs of one route share no section.
    """
    west = _carrier_families()[frozenset({"oms-par-fra", "oms-fra-mil"})][0]
    east = _carrier_families()[frozenset({"oms-fra-mil", "oms-vie-mil"})][0]
    switch = _device("oeo-mil-01", "Milan", frozenset({"roadm-mil-01"}), west, east)
    chains = find_chains(
        PARIS_TO_VIENNA,
        [_span(west, "oms-par-fra", "oms-fra-mil"), _span(east, "oms-fra-mil", "oms-vie-mil")],
        [switch],
    )
    assert chains == ()


def test_no_shipped_wavelength_pair_covers_any_route_end_to_end() -> None:
    """The same negative, generalised over the whole shipped dataset.

    Every one of the five families crosses `oms-fra-mil`, so no two of them are
    disjoint and no cover of two or more segments exists on the wavelengths that
    ship today. That is a fact about the dataset and it is why Phase 6 has to emit
    the wavelengths as well as the devices, and why the positive tests above use
    fixtures.
    """
    families = list(_carrier_families())
    assert all("oms-fra-mil" in sections for sections in families)
    assert not [
        (first, second) for first in families for second in families if first is not second and not first & second
    ]


def test_a_device_at_another_site_makes_no_junction() -> None:
    """A device terminating both wavelengths somewhere else joins nothing here.

    Condition two of the predicate. Without it a cross-connect at Milan would be
    read as joining two wavelengths that abut at Frankfurt, which is a hand-off
    at a site the device is not at.
    """
    elsewhere = _device("oeo-mil-01", "Milan", frozenset({"roadm-mil-01"}), WEST.name, EAST.name)
    assert find_chains(PARIS_TO_BERLIN, [WEST, EAST], [elsewhere]) == ()
    assert not joins(elsewhere, "roadm-fra-01", WEST, EAST)


def test_a_device_holding_one_of_the_two_makes_no_junction() -> None:
    """One wavelength is not a junction: the light has to be terminated and re-originated.

    Condition three, and the half of it a plausible implementation gets wrong.
    A device on the route that terminates the incoming wavelength says nothing
    about whether it can originate the outgoing one.
    """
    partial = _device("oeo-fra-01", "Frankfurt", FRANKFURT_NODES, WEST.name)
    assert find_chains(PARIS_TO_BERLIN, [WEST, EAST], [partial]) == ()
    assert not joins(partial, "roadm-fra-01", WEST, EAST)


def test_no_device_means_no_chain() -> None:
    """The state of the branch this feature has to refuse on.

    FR-025 asks for both halves of the regeneration story, and this is the first:
    with no cross-connect anywhere, a route that does not close on one wavelength
    is refused rather than covered.
    """
    assert find_chains(PARIS_TO_BERLIN, [WEST, EAST], []) == ()


def test_a_wavelength_covering_the_run_and_one_more_section_is_absent() -> None:
    """Set equality, not containment, and the reason is the plant.

    An ODU is added and dropped where its wavelength terminates. A wavelength
    running one section past the end of a segment delivers the client one site
    past where the segment ends, so covering the run and more is not covering it.
    """
    long_west = _span("oc-seg-par-mil", "oms-par-fra", "oms-fra-mil")
    switch = _device("oeo-fra-01", "Frankfurt", FRANKFURT_NODES, long_west.name, EAST.name)
    assert find_chains(PARIS_TO_BERLIN, [long_west, EAST], [switch]) == ()


# ----------------------------------------------------------------------
# The bound, derived from the topology
# ----------------------------------------------------------------------


def _widest_shortest_route() -> tuple[int, tuple[str, str]]:
    """The longest shortest route between any two ROADMs, in sections.

    Breadth-first over the shipped section graph. This is the figure the cover's
    bound has to admit: a pair whose shortest route is longer than the cap has no
    provisionable route at all, so the cap would be deciding rather than bounding.
    """
    adjacency: dict[str, list[str]] = defaultdict(list)
    for head, tail in _sections().values():
        adjacency[head].append(tail)
        adjacency[tail].append(head)
    worst = 0
    pair = ("", "")
    for source in sorted(adjacency):
        distance = {source: 0}
        queue = deque([source])
        while queue:
            current = queue.popleft()
            for following in adjacency[current]:
                if following not in distance:
                    distance[following] = distance[current] + 1
                    queue.append(following)
        for node, hops in sorted(distance.items()):
            if hops > worst:
                worst, pair = hops, (source, node)
    return worst, pair


def test_the_cover_bound_is_the_topology_s_own_figure() -> None:
    """FR-011a: derived from the plant, not assumed.

    Brussels to Vienna is four sections and it is the widest shortest route on
    the fifteen-site plant, so the cover has to admit four. A segment covers at
    least one section, so four sections cannot be cut into more than four runs,
    and `MAX_CHAIN_SEGMENTS` is that figure rather than the two the shipped
    scenarios happen to need.
    """
    worst, pair = _widest_shortest_route()
    assert worst == MAX_COVER_SECTIONS, (
        f"{pair} needs {worst} sections and the cover is bounded at {MAX_COVER_SECTIONS}"
    )
    assert MAX_CHAIN_SEGMENTS == MAX_COVER_SECTIONS
    assert MIN_CHAIN_SEGMENTS == 2


def test_the_cover_bound_matches_the_generator_s_route_cap() -> None:
    """One number, two homes, and a test so they cannot drift.

    The traversal never returns a route longer than `MAX_ROUTE_SECTIONS`, so a
    cover bounded above it would be covering routes that cannot arrive and one
    bounded below it would refuse routes that do.
    """
    spec = importlib.util.spec_from_file_location(GENERATOR_PATH.stem, GENERATOR_PATH)
    assert spec and spec.loader, f"{GENERATOR_PATH} could not be loaded"
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.MAX_ROUTE_SECTIONS == MAX_COVER_SECTIONS


def test_the_derived_ceiling_is_reachable_and_not_exceeded() -> None:
    """Brussels to Vienna, one wavelength per section: four segments, three junctions.

    The widest cover the bound allows, asserted to exist rather than assumed, and
    asserted not to be exceeded. Nothing enumerated here can produce a fifth
    segment, because a fifth run would need a fifth section.
    """
    route = _route("roadm-bru-01", "roadm-par-01", "roadm-fra-01", "roadm-mil-01", "roadm-vie-01")
    assert len(route) == MAX_COVER_SECTIONS
    spans = [_span(f"oc-seg-{step.name}", step.name) for step in route]
    devices = [
        _device(
            f"oeo-{step.node_z}",
            step.node_z,
            frozenset({step.node_z}),
            *[span.name for span in spans],
        )
        for step in route[:-1]
    ]
    chains = find_chains(route, spans, devices)
    assert [chain.segment_count for chain in chains] == [MAX_CHAIN_SEGMENTS]
    assert len(chains[0].junctions) == MAX_CHAIN_SEGMENTS - 1
    assert chains[0].section_names == tuple(step.name for step in route)


def test_a_route_longer_than_the_bound_is_refused() -> None:
    """A widened traversal must not silently get a partial cover.

    Five sections is not a route this feature's traversal returns. If it ever
    becomes one, the cap has to be raised deliberately rather than the cover
    quietly covering the first four sections of it.
    """
    route = (
        *_route("roadm-bru-01", "roadm-par-01", "roadm-fra-01", "roadm-mil-01", "roadm-vie-01"),
        RouteSection(name="oms-invented", node_a="roadm-vie-01", node_z="roadm-prg-01"),
    )
    with pytest.raises(ValueError, match=r"bounded at 4"):
        find_chains(route, [], [])


def test_max_segments_outside_the_bound_is_refused() -> None:
    """Both ends of the parameter, because both are a caller error rather than a case."""
    with pytest.raises(ValueError, match="at least 2 segments"):
        find_chains(PARIS_TO_BERLIN, [WEST, EAST], [FRANKFURT_SWITCH], max_segments=1)
    with pytest.raises(ValueError, match="exceeds the derived bound"):
        find_chains(PARIS_TO_BERLIN, [WEST, EAST], [FRANKFURT_SWITCH], max_segments=MAX_CHAIN_SEGMENTS + 1)


def test_max_segments_narrows_the_cover_without_changing_it() -> None:
    """Asking for two segments on a three-section route drops the three-segment covers.

    The parameter is a bound and not a preference: what it returns is a subset of
    what the default returns, in the same order.
    """
    route = _route("roadm-par-01", "roadm-fra-01", "roadm-mil-01")
    spans = [_span(f"oc-seg-{step.name}", step.name) for step in route]
    pair = _span("oc-seg-par-mil", "oms-par-fra", "oms-fra-mil")
    devices = [_device("oeo-fra-01", "Frankfurt", FRANKFURT_NODES, *[span.name for span in [*spans, pair]])]
    everything = find_chains(route, [*spans, pair], devices)
    narrowed = find_chains(route, [*spans, pair], devices, max_segments=2)
    assert [chain.key for chain in narrowed] == [chain.key for chain in everything if chain.segment_count == 2]
    assert narrowed


# ----------------------------------------------------------------------
# The shapes a cover must refuse
# ----------------------------------------------------------------------


def test_an_unordered_route_is_refused() -> None:
    """Sections in the wrong order still cut into runs and still match carriers.

    That is the whole danger: the cover would be a plausible circuit that does
    not exist. The contiguity check is what refuses it, and it names both ends.
    """
    steps = _route("roadm-par-01", "roadm-fra-01", "roadm-ber-01")
    with pytest.raises(ValueError, match="not one ordered route"):
        find_chains((steps[1], steps[0]), [WEST, EAST], [FRANKFURT_SWITCH])


def test_a_repeated_section_is_refused() -> None:
    """A wavelength does not cross a section twice."""
    steps = _route("roadm-par-01", "roadm-fra-01")
    with pytest.raises(ValueError, match="repeats a section"):
        find_chains((steps[0], steps[0]), [], [])


def test_an_empty_route_is_refused() -> None:
    with pytest.raises(ValueError, match="empty route"):
        find_chains((), [WEST, EAST], [FRANKFURT_SWITCH])


def _segment(carrier: str, section: str, device: str | None) -> ChainSegment:
    return ChainSegment(
        carrier_name=carrier,
        section_names=(section,),
        start_node="roadm-par-01",
        junction_node=None if device is None else "roadm-fra-01",
        junction_site=None if device is None else "Frankfurt",
        junction_device=device,
    )


def test_a_chain_refuses_every_shape_it_is_not() -> None:
    """`Chain`'s invariants, each one a cover that reads like a circuit and is not.

    The middle case is R-008's: two segments joined by nothing. The last case is
    the repeated section, which is the same phantom seen from the cover's side.
    """
    with pytest.raises(ValueError, match="at least 2 segments"):
        Chain(segments=(_segment("oc-one", "oms-par-fra", None),))
    with pytest.raises(ValueError, match="joined by nothing"):
        Chain(segments=(_segment("oc-one", "oms-par-fra", None), _segment("oc-two", "oms-ber-fra", None)))
    with pytest.raises(ValueError, match="regenerate light nobody carries on"):
        Chain(
            segments=(
                _segment("oc-one", "oms-par-fra", "oeo-fra-01"),
                _segment("oc-two", "oms-ber-fra", "oeo-fra-01"),
            )
        )
    with pytest.raises(ValueError, match="repeats a section"):
        Chain(
            segments=(
                _segment("oc-one", "oms-par-fra", "oeo-fra-01"),
                _segment("oc-two", "oms-par-fra", None),
            )
        )
