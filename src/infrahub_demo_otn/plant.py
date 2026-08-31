"""Turn plant records into budget engine inputs.

`budget.py` is deliberately ignorant of where its numbers come from, so
something has to bridge the gap between a record and a `SpanInput`. The OSNR
check and the budget report transform both need that bridge. They read the
same plant and ask it the same questions, and two copies of a GraphQL
unwrapper is how one of them starts reading a different attribute.

Two layers, because two shapes exist:

- `build_*` take a flat mapping of attribute name to scalar. The unit tests feed
  them object YAML.
- `*_from_graphql` take an Infrahub GraphQL payload and unwrap it first. The
  check and the transform feed them a query result.

This module imports `budget`, `routing` and `units`. It does not import
`infrahub_sdk`: a payload is a dict, and keeping it that way is what lets the
adapter be tested without a server. A path traversal result is the third shape
handled under the same rule, and it arrives here as plain dicts for the same
reason.

The layering is: `budget` and `routing` decide, this module adapts, and the
check, the transform and the generator carry the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Iterator, Mapping, Sequence

from infrahub_demo_otn.budget import (
    AmplifierInput,
    ModeInput,
    NodeInput,
    SectionInput,
    SpanInput,
)
from infrahub_demo_otn.routing import ModeCandidate, RouteCandidate
from infrahub_demo_otn.units import (
    GROUP_INDEX_G652_MILLI,
    carrier_interval_mhz,
)
from infrahub_demo_otn.units import FreeBlock as FreeBlock
from infrahub_demo_otn.units import SpectralInterval as SpectralInterval
from infrahub_demo_otn.units import free_blocks as free_blocks

# Re-exported, not redefined. The spectral interval, the free block and the sweep
# that derives one from the other moved to `units.py` so `routing.py` could reach
# them: `plant.py` imports `routing.py`, so the arithmetic could not stay here
# without a cycle. Every consumer already reaches for `plant.free_blocks`, and
# the aliases keep that import path working against the one definition.

ROADM_KIND = "OtnRoadm"
"""The kind a traversal path must start and end on, and alternate through."""

SECTION_KIND = "OtnOpticalMultiplexSection"
"""The kind that must sit between every pair of ROADMs on a valid route."""

DIRECTION_A_TO_B = "a_to_b"
"""Towards the section's own `roadm_b`. Nothing stores this token any more. It
is what `_direction` returns and what the engine, the checks and the impact
report branch on."""

DIRECTION_B_TO_A = "b_to_a"
"""Towards the section's own `roadm_a`."""

INJECTION_ENDS = ("site_a", "site_b")
"""Which end of a span a Raman pump is spliced in at. The two tokens name the
span's own endpoint relationships."""

PROPAGATIONS = ("co", "counter")
"""Whether the pump fires with the signal or against it."""


def unwrap(node: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten one GraphQL node to attribute name against scalar.

    Infrahub wraps every attribute as `{"value": ...}` and every relationship
    as `{"node": ...}` or `{"edges": [...]}`. Attributes are unwrapped here;
    relationships are handed back untouched, because their shape is what the
    caller is walking.
    """
    flat: dict[str, Any] = {}
    for key, item in node.items():
        if isinstance(item, dict) and set(item) == {"value"}:
            flat[key] = item["value"]
        else:
            flat[key] = item
    return flat


def peer(node: Mapping[str, Any], relationship: str) -> dict[str, Any]:
    """The single peer of a `cardinality: one` relationship, unwrapped."""
    related = node.get(relationship) or {}
    inner = related.get("node") if isinstance(related, dict) else None
    if not isinstance(inner, dict):
        raise ValueError(f"relationship {relationship} has no peer")
    return unwrap(inner)


def peers(node: Mapping[str, Any], relationship: str) -> Iterator[dict[str, Any]]:
    """Every peer of a `cardinality: many` relationship, unwrapped."""
    related = node.get(relationship) or {}
    edges = related.get("edges") if isinstance(related, dict) else None
    for edge in edges or []:
        inner = edge.get("node") if isinstance(edge, dict) else None
        if isinstance(inner, dict):
            yield unwrap(inner)


def nodes_of(payload: Mapping[str, Any], kind: str) -> Iterator[dict[str, Any]]:
    """Every top-level node of one kind in a GraphQL payload, unwrapped."""
    collection = payload.get(kind) or {}
    for edge in collection.get("edges") or []:
        inner = edge.get("node") if isinstance(edge, dict) else None
        if isinstance(inner, dict):
            yield unwrap(inner)


def _token(record: Mapping[str, Any], field: str, vocabulary: tuple[str, ...], name: str) -> str:
    """One dropdown value off a record, refusing to guess.

    A missing value or one the vocabulary does not contain is a data error.
    Defaulting it would put a pump on the wrong side of the fibre and report a
    plausible number for it.
    """
    value = record.get(field)
    if value is None:
        raise ValueError(f"{name} has no {field}, so the direction it amplifies cannot be worked out")
    token = str(value)
    if token not in vocabulary:
        allowed = " nor ".join(repr(choice) for choice in vocabulary)
        raise ValueError(f"{name} has {field} {token!r}, which is neither {allowed}")
    return token


def _direction(record: Mapping[str, Any], name: str) -> str:
    """Which direction of travel one Raman pump amplifies, derived from placement.

    Nothing stores the answer. Two physical facts fix it and this is the one
    place that reads them together, so the derived direction can never disagree
    with the facts beside it.

    A counter-propagating pump fires back up the fibre from the far end, so one
    injected at the B end amplifies the A to B signal. A co-propagating pump
    fires along with the signal from the near end, so one at the A end
    amplifies A to B as well. Both cases are
    `(injection_end == site_a) == (propagation == co)`.

    The function is no shorter than the one that read a stored label. What it
    gains is that its answer follows from the record rather than sitting beside
    it.
    """
    end = _token(record, "injection_end", INJECTION_ENDS, name)
    propagation = _token(record, "propagation", PROPAGATIONS, name)
    return DIRECTION_A_TO_B if (end == "site_a") == (propagation == "co") else DIRECTION_B_TO_A


def _raman_sums(span: Mapping[str, Any]) -> tuple[int, int, int]:
    """On-off gain each way, and the combiner loss, for one span.

    Two pumps in one direction sum here, which is what lets the engine hold one
    integer per direction and stay ignorant of pump objects. Insertion loss sums
    over every pump whichever way it points, because the combiner is in line on
    the fibre and both directions pass through it.

    A span record with no `raman_pumps` yields zeros. `peers` handles the
    missing key, so a query that does not select the relationship is not a
    crash. It is not a warning either, so a query that forgets it reports a
    pumped span as unpumped.

    A query that selects the relationship but forgets `injection_end` or
    `propagation` is different: `_direction` raises and names the pump. The
    derivation needs both fields, so this is a query contract of two rather
    than the one a stored direction needed.
    """
    forward = 0
    reverse = 0
    combiner = 0
    for pump in peers(span, "raman_pumps"):
        name = str(pump.get("name"))
        gain = int(pump.get("on_off_gain_mdb") or 0)
        if _direction(pump, name) == DIRECTION_B_TO_A:
            reverse += gain
        else:
            forward += gain
        combiner += int(pump.get("insertion_loss_mdb") or 0)
    return forward, reverse, combiner


def build_span(span: Mapping[str, Any], fiber: Mapping[str, Any]) -> SpanInput:
    """One `OtnFiberSpan` plus the `OtnFiberType` behind it, pumps included."""
    raman_gain, raman_gain_reverse, pump_loss = _raman_sums(span)
    return SpanInput(
        name=str(span["name"]),
        length_m=int(span["length_m"]),
        attenuation_mdb_per_km=int(fiber["attenuation_mdb_per_km"]),
        dispersion_fs_per_nm_km=int(fiber["dispersion_fs_per_nm_km"]),
        splice_count=int(span.get("splice_count") or 0),
        splice_loss_mdb=int(span.get("splice_loss_mdb") or 0),
        connector_count=int(span.get("connector_count") or 0),
        connector_loss_mdb=int(span.get("connector_loss_mdb") or 0),
        aging_margin_mdb=int(span.get("aging_margin_mdb") or 0),
        group_index_milli=int(fiber.get("group_index_milli") or GROUP_INDEX_G652_MILLI),
        raman_gain_mdb=raman_gain,
        raman_gain_reverse_mdb=raman_gain_reverse,
        pump_loss_mdb=pump_loss,
    )


def build_node(element: Mapping[str, Any]) -> NodeInput:
    """Any `OtnOpticalElement` that light passes through without gain."""
    return NodeInput(name=str(element["name"]), insertion_loss_mdb=int(element.get("insertion_loss_mdb") or 0))


def build_amplifier(amplifier: Mapping[str, Any]) -> AmplifierInput:
    return AmplifierInput(
        name=str(amplifier["name"]),
        noise_figure_mdb=int(amplifier["noise_figure_mdb"]),
        gain_mdb=int(amplifier["gain_mdb"]),
    )


def build_mode(mode: Mapping[str, Any]) -> ModeInput:
    return ModeInput(
        name=str(mode["name"]),
        required_osnr_mdb=int(mode["required_osnr_mdb"]),
        cd_tolerance_fs_per_nm=int(mode["cd_tolerance_fs_per_nm"]),
        fec_latency_ns=int(mode.get("fec_latency_ns") or 0),
    )


def _sequence(record: Mapping[str, Any], name: str) -> int:
    value = record.get("oms_sequence")
    if value is None:
        raise ValueError(f"{name} has no oms_sequence, so its section cannot be ordered")
    return int(value)


def build_section(
    name: str,
    head: Mapping[str, Any],
    tail: Mapping[str, Any],
    spans: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    amplifiers_a2b: Sequence[Mapping[str, Any]],
    amplifiers_b2a: Sequence[Mapping[str, Any]],
) -> SectionInput:
    """Assemble one section: one span list and one amplifier chain per direction.

    The two chains arrive already split, because the section holds one
    relationship per direction and which one an amplifier hangs off is which
    chain it is in. Nothing here groups, and nothing reads a stored direction.

    Each chain is sorted on `oms_sequence` independently. Sorting happens here
    rather than in the engine because only this layer has seen the raw records:
    Infrahub relationships carry no order, so what arrives is a set in whatever
    order the query returned it, and a missing sequence is an error rather than
    a position of zero. The sequence counts along the direction the chain
    amplifies, so a sorted chain is already in traversal order.

    A query that selects one relationship and forgets the other does not raise
    here. `peers` hands back an empty list for a key that is not there, so the
    chain arrives empty and looks like a section whose amplifiers are not
    loaded yet. `SectionInput.validate()` is what catches it: its N+1 rule fails
    for that direction and names the section. That guard is not optional, and it
    is the only place this feature makes a failure quieter before making it loud
    again.
    """
    ordered_spans = sorted(spans, key=lambda pair: _sequence(pair[0], str(pair[0].get("name"))))

    def chain(records: Sequence[Mapping[str, Any]]) -> tuple[AmplifierInput, ...]:
        ordered = sorted(records, key=lambda record: _sequence(record, str(record.get("name"))))
        return tuple(build_amplifier(record) for record in ordered)

    return SectionInput(
        name=name,
        head_node=build_node(head),
        tail_node=build_node(tail),
        spans=tuple(build_span(span, fiber) for span, fiber in ordered_spans),
        amplifiers_a2b=chain(amplifiers_a2b),
        amplifiers_b2a=chain(amplifiers_b2a),
    )


def sections_from_graphql(payload: Mapping[str, Any]) -> dict[str, SectionInput]:
    """Every `OtnOpticalMultiplexSection` in a payload, keyed by name."""
    sections: dict[str, SectionInput] = {}
    for record in nodes_of(payload, "OtnOpticalMultiplexSection"):
        name = str(record["name"])
        spans = [(span, peer(span, "fiber_type")) for span in peers(record, "spans")]
        sections[name] = build_section(
            name=name,
            head=peer(record, "roadm_a"),
            tail=peer(record, "roadm_b"),
            spans=spans,
            amplifiers_a2b=list(peers(record, "amplifiers_a2b")),
            amplifiers_b2a=list(peers(record, "amplifiers_b2a")),
        )
    return sections


def carriers_from_graphql(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every carrier as `{name, id, kind, mode, section_names}`.

    `mode` is `None` when the carrier has none. The caller decides whether that
    is a skip or an error; the check treats it as an error, because a carrier
    with no mode has no requirement to be measured against.

    `id` and `kind` are carried so a check can name the offending object on
    `log_error` and the proposed change links the failure to it. Every query
    that feeds this already selects both on its top-level nodes.
    """
    carriers: list[dict[str, Any]] = []
    for record in nodes_of(payload, "OtnOpticalCarrier"):
        mode_peer = (record.get("optical_mode") or {}).get("node")
        carriers.append(
            {
                "name": str(record["name"]),
                "id": str(record.get("id", "")),
                "kind": str(record.get("__typename", "OtnOpticalCarrier")),
                "mode": build_mode(unwrap(mode_peer)) if isinstance(mode_peer, dict) else None,
                "section_names": [str(section["name"]) for section in peers(record, "sections")],
            }
        )
    return carriers


def modes_from_graphql(payload: Mapping[str, Any]) -> list[ModeCandidate]:
    """Every `OtnOpticalMode` as a selector candidate.

    `build_mode` already produces the three numbers the engine needs. This adds
    the two the *selector* needs and the engine does not care about: the class,
    which decides whether the mode is provisionable at all, and the baud rate,
    which is the first ranking term.
    """
    return [
        ModeCandidate(
            name=str(record["name"]),
            mode_class=str(record["mode_class"]),
            line_rate_gbps=int(record["line_rate_gbps"]),
            baud_mbaud=int(record["baud_mbaud"]),
            budget_input=build_mode(record),
        )
        for record in nodes_of(payload, "OtnOpticalMode")
    ]


@dataclass(frozen=True)
class CarrierInterval:
    """The half-open slice of spectrum one carrier occupies on one section.

    Nothing stores this. It is the carrier's anchor centre frequency widened by
    its mode's symbol rate, and it exists only for as long as a check, a report
    or the allocator is holding it.

    `channel` and `carrier` ride along because every consumer needs them and
    re-deriving them costs a second traversal of the payload. The channel number
    stays the human-readable anchor, so a collision message says "channel 40 and
    channel 41" as well as naming the megahertz they fight over.
    """

    carrier: str
    channel: int
    center_mhz: int
    lower_mhz: int
    upper_mhz: int
    mode: str

    @property
    def width_mhz(self) -> int:
        return self.upper_mhz - self.lower_mhz


def occupancy_from_graphql(
    payload: Mapping[str, Any], exclude: Collection[str] = ()
) -> dict[str, tuple[CarrierInterval, ...]]:
    """Section name to the spectrum carriers already occupy on it, sorted by lower edge.

    Occupancy is not stored, it is this. A wavelength does not occupy a channel
    number, it occupies a width, so what a section holds is a list of intervals
    and not a set of integers. The check, the generator and the reports all read
    this one function: two derivations of "what is taken" is how an allocator and
    a report start disagreeing.

    **Sorted by lower edge on construction**, so every consumer downstream is a
    single pass. `free_blocks` relies on it and the overlap test can stop early.

    Four things are an error rather than a skip, and all four are the same
    argument: an allocator that silently ignores the carrier it could not read
    hands out spectrum that is already lit.

    - No channel: the interval has no centre.
    - No section: the interval is reserved nowhere, which is a claim on nothing.
    - No mode: the interval has no width.
    - A mode with no symbol rate: the same, one hop further out.

    Defaulting any of the four to one channel would report green for spectrum
    nobody measured.

    `exclude` names carriers that must not count against themselves. A generator
    re-run reads a branch that already contains the carrier its previous run
    wrote, and without this it finds its own spectrum occupied and moves to the
    next anchor: channel 1, then 2, then 3, a different answer every run.
    """
    skip = set(exclude)
    used: dict[str, list[CarrierInterval]] = {}
    for record in nodes_of(payload, "OtnOpticalCarrier"):
        name = str(record["name"])
        if name in skip:
            continue
        channel_peer = (record.get("channel") or {}).get("node")
        if not isinstance(channel_peer, dict):
            raise ValueError(f"carrier {name} has no channel, so its occupancy cannot be counted")
        anchor = unwrap(channel_peer)
        channel = int(anchor["channel_number"])
        center = anchor.get("center_frequency_mhz")
        if center is None:
            raise ValueError(
                f"carrier {name} anchors on channel {channel}, whose centre frequency the query did not select, "
                "so the spectrum it occupies cannot be placed"
            )
        mode_peer = (record.get("optical_mode") or {}).get("node")
        if not isinstance(mode_peer, dict):
            raise ValueError(f"carrier {name} has no optical mode, so the width it occupies cannot be derived")
        mode = unwrap(mode_peer)
        mode_name = str(mode.get("name", ""))
        baud = mode.get("baud_mbaud")
        if baud is None:
            raise ValueError(
                f"carrier {name} runs mode {mode_name} with no symbol rate, so the width it occupies cannot be derived"
            )
        sections = [str(section["name"]) for section in peers(record, "sections")]
        if not sections:
            raise ValueError(f"carrier {name} crosses no section, so the spectrum it occupies is reserved nowhere")
        lower, upper = carrier_interval_mhz(int(center), int(baud))
        interval = CarrierInterval(
            carrier=name,
            channel=channel,
            center_mhz=int(center),
            lower_mhz=lower,
            upper_mhz=upper,
            mode=mode_name,
        )
        for section in sections:
            used.setdefault(section, []).append(interval)
    return {
        section: tuple(sorted(intervals, key=lambda item: (item.lower_mhz, item.upper_mhz, item.carrier)))
        for section, intervals in used.items()
    }


def intervals_overlap(left: SpectralInterval, right: SpectralInterval) -> bool:
    """Whether two spectral intervals share any spectrum at all.

    **Both intervals are half-open, `[lower_mhz, upper_mhz)`.** The upper edge is
    the first frequency the interval does not hold, so two intervals that meet at
    exactly one frequency do not overlap. A 44,400 MHz carrier ending at
    191,500,000 MHz and one starting there are neighbours, not a collision. With
    closed intervals every densely packed plan would report false collisions on
    its own boundaries, which is the failure this convention exists to avoid.

    The convention is set in `units.carrier_interval_mhz`, which builds the edges,
    and it is stated again here because this is where a caller acts on it.

    `free_blocks` does not call this and is not a second copy of it. Its sweep
    asks two different questions of a running cursor: whether an interval is
    already covered, `upper <= cursor`, and whether a gap precedes it,
    `lower > cursor`. The second is deliberately strict, because two touching
    carriers leave no free block between them and emitting a zero-width one would
    be a report of spectrum that is not there. Both answers agree with this
    predicate on touching intervals and
    `test_the_gap_test_and_the_overlap_test_agree_about_touching_intervals` holds
    them to it.

    Symmetric in its arguments, and it makes no ordering assumption, so it is
    correct on a hand-built pair as well as on the sorted output of
    `occupancy_from_graphql`.
    """
    return left.lower_mhz < right.upper_mhz and right.lower_mhz < left.upper_mhz


def overlap_range(left: SpectralInterval, right: SpectralInterval) -> tuple[int, int] | None:
    """The spectrum two intervals share, `[lower, upper)`, or `None` if they share none.

    The higher of the two lower edges against the lower of the two uppers. That is
    all it is, and it lives here rather than in the caller because the collision
    check reports the range in its message and a capacity report will want the same
    number. Two copies of `max(lower)` and `min(upper)` is how a message and a
    report start disagreeing about how much spectrum a pair fights over.

    **The predicate above decides, and this function only measures.** It returns
    early on `intervals_overlap`, so a touching pair comes back as `None` rather
    than as a zero-width range, and there is one definition of overlap rather than
    two that agree by inspection.

    The result is half-open on the same terms as its inputs, so
    `upper - lower` is the shared width in MHz.
    """
    if not intervals_overlap(left, right):
        return None
    return max(left.lower_mhz, right.lower_mhz), min(left.upper_mhz, right.upper_mhz)


def routes_from_traversal(
    paths: Sequence[Sequence[Mapping[str, str]]],
    source: str,
    destination: str,
    truncated_at_depth: int | None = None,
) -> list[RouteCandidate]:
    """Traversal paths to route candidates, refusing anything that is not a route.

    A path arrives as an ordered list of `{"kind": ..., "name": ...}`. The SDK's
    own model is a pydantic object; the generator flattens it here so this stays
    testable without a server, and so the shape the tests exercise is the shape
    the generator passes.

    Three things are checked and all three are real. **Truncation** means the
    server ran out of budget and the candidate set is a prefix of the answer;
    ranking an incomplete set silently picks the wrong winner, so it raises.
    **Alternation** means every even position is a ROADM and every odd one a
    section; without it a path can wander through a shared fiber type and still
    budget cleanly, which is what filtering on `included_kinds` alone produces.
    **Revisiting** means a ROADM appears twice, and a
    wavelength does not pass through a node twice.
    """
    if truncated_at_depth is not None:
        raise ValueError(
            f"path traversal ran out of budget at depth {truncated_at_depth}, so the candidate routes are "
            "incomplete and ranking them would pick a winner from a partial set"
        )

    routes: list[RouteCandidate] = []
    for index, path in enumerate(paths):
        kinds = [str(hop.get("kind")) for hop in path]
        names = [str(hop.get("name")) for hop in path]
        if len(path) < 3 or len(path) % 2 == 0:
            raise ValueError(f"path {index} has {len(path)} hops, which cannot alternate ROADM and section")
        expected = [ROADM_KIND if position % 2 == 0 else SECTION_KIND for position in range(len(path))]
        if kinds != expected:
            raise ValueError(f"path {index} is not an alternating ROADM and section walk: {' -> '.join(kinds)}")
        if names[0] != source or names[-1] != destination:
            raise ValueError(f"path {index} runs {names[0]} to {names[-1]}, not {source} to {destination}")
        roadms = names[0::2]
        if len(set(roadms)) != len(roadms):
            raise ValueError(f"path {index} revisits a ROADM: {' -> '.join(roadms)}")
        sections = tuple(names[1::2])
        routes.append(RouteCandidate(key="|".join(sections), section_names=sections, start_node=source))
    return routes
