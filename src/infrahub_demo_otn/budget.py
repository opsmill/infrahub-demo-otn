"""Optical link budget: loss, OSNR, chromatic dispersion and latency.

The single source of truth for the arithmetic, called by the OSNR check, the
budget report transform, and the route generator.

Nothing here imports `infrahub_sdk`. The engine takes plain frozen dataclasses
and returns plain frozen dataclasses, so its tests need no server and no Docker.
`plant.py` is what turns a GraphQL payload into these inputs.

Scaled integers cross every boundary; floats live only inside a function body.
Rounding happens once, at the return. Floats never reach a stored attribute.

**Two span losses, and the difference matters.** `span_loss_mdb` includes the
ageing allowance and drives the power budget and the gain requirement.
`span_fiber_loss_mdb` excludes it and drives the OSNR stage input power. The
allowance is a reserve against loss that has not happened yet; charging it to
the amplifier input computes an end-of-life OSNR, and then charging the system
margin on top charges the same pessimism twice.

**Raman is credited as reduced effective span loss, and that is first order.**
A counter-propagating pump amplifies near the far end of the fibre, so its
on-off gain is equivalent to reducing the span's loss. `span_fiber_loss_mdb`
subtracts it, `span_loss_mdb` inherits the reduction, and the cascade needs no
new term. The combiner's insertion loss is charged in both directions and the
gain is credited in one, because light travelling either way passes through the
combiner and only one direction is pumped.

What that treatment does not do is charge the pump's own noise contribution, so
every Raman figure this module produces is slightly optimistic. The link budget
page says the same, for a reader who never opens this module.

**Ordering is the whole correctness risk.** A carrier's sections arrive as an
unordered set, because Infrahub relationships carry no order, and a section may
be traversed against the direction it is stored in. `order_sections` and
`flatten_path` are public and separately tested for exactly that reason: the
shipped dataset gives every span in a section the same length and every ROADM
the same insertion loss, so a reversal bug, an ordering bug and a
double-counted node all produce the right totals against it.

**A route with two segments has two margins and no route margin, and quoting
either one is the same mistake one level up.** `evaluate_path` already warns
that the cascade runs over a whole path and that summing per-section OSNRs
would be wrong and would look right. Regeneration makes that failure available
again at the level above: an O-E-O device terminates the light and re-originates
it, so each segment is a complete cascade over its own length, evaluated against
its own mode, and neither segment's margin describes the route. A reader shown
`+4.2 dB` for a regenerated Paris to Madrid would take it for the route's
headroom, and two segments closing is not that claim.

So `RouteBudget` carries no scalar margin at all. It reports margins with their
segment numbers attached, its verdict is a conjunction over the segments, and
`sole_segment` refuses rather than hand a caller one number for a route that has
two. Latency is the one figure that does honestly total across a regeneration,
because delay adds where noise restarts, and `RouteBudget.latency_ns` is
therefore a route figure while no margin is.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Sequence

from infrahub_demo_otn.units import (
    GROUP_INDEX_G652_MILLI,
    M_PER_KM,
    db_to_mdb,
    mdb_to_db,
    propagation_delay_ns,
)

OSNR_REFERENCE_DB = 58.0
"""`-10*log10(h * nu * delta_nu)` for a 12.5 GHz reference bandwidth, which is
0.1 nm at 1550 nm. A named constant rather than a literal in the formula."""

OSNR_REFERENCE_MDB = db_to_mdb(OSNR_REFERENCE_DB)
"""The same constant in the unit the engine works in."""

LAUNCH_POWER_PER_CHANNEL_DBM = 3.0
"""Per-channel power launched into each span by the amplifier ahead of it.

Not an attribute on any node. Three candidate sources exist in the loaded data
and none is this quantity: the line port's transmit power is measured before
7 dB of ROADM loss and before the booster, and the amplifier port's is a
composite figure across the whole band. It never varies, so it stays a constant
here rather than becoming an attribute.

+3 dBm per channel is at the top of the realistic range: across 96 channels it
is +22.8 dBm composite, a high-power C-band booster rather than a typical one.
`docs/docs/link-budget.mdx` says so where a reader will see it.
"""

LAUNCH_POWER_PER_CHANNEL_MDBM = db_to_mdb(LAUNCH_POWER_PER_CHANNEL_DBM)
"""Milli-dBm. The scale factor for dBm is the same as for dB, which is why the
repository's own divisor pin map sends both `_mdb` and `_mdbm` to
`MDB_PER_DB`."""

ROADM_FILTERING_PENALTY_MDB = db_to_mdb(0.5)
"""OSNR penalty per node traversed. A ROADM adds no ASE, so its cost is its
insertion loss, which reduces the next amplifier's input power, plus the
narrowing its filter shape imposes on the signal."""

SYSTEM_MARGIN_MDB = db_to_mdb(1.0)
"""Implementation penalty plus end-of-life degradation, on top of a
beginning-of-life OSNR."""

ROADM_LATENCY_NS = 150
"""WSS switching plus internal fiber at a node. Illustrative, in the same sense
the span lengths are: plausible, unsourced, and three orders of magnitude below
propagation, so it changes no verdict."""

AMPLIFIER_LATENCY_NS = 100
"""Gain fiber coil plus patching. Illustrative, as above."""

NODE = "node"
AMPLIFIER = "amplifier"
SPAN = "span"


def _round_div(numerator: int, denominator: int) -> int:
    """Integer division rounded half up. Never truncate a physical quantity.

    A 73,334 m span at 200 mdB/km is 14,666.8 mdB. Truncating loses 0.8 mdB per
    span, and a 38-amplifier route has 37 of them.
    """
    return (numerator + denominator // 2) // denominator


@dataclass(frozen=True)
class SpanInput:
    """One `OtnFiberSpan` plus the `OtnFiberType` behind it.

    **Raman needs three fields where one was asked for, and `flipped()` is the
    reason.** A span says nothing about which way light is travelling: the
    section decides that, and flipping a section reverses the span tuple. A lone
    `raman_gain_mdb` would follow the span into the unpumped walk and credit
    that walk with the pumped direction's gain.

    So the gain is paired. `raman_gain_mdb` belongs to the orientation being
    walked now, `raman_gain_reverse_mdb` to the other one, and `flipped()` swaps
    them. `pump_loss_mdb` needs no pair: the combiner sits in line on the fibre,
    so light travelling either way pays its insertion loss. Charged both ways,
    credited one way.

    All three are sums the marshaller produced, not per-pump figures. Two pumps
    on one span in one direction add up in `plant.py` and arrive here as one
    number, which is why the engine reads no pump object and needs no separate
    co-propagating case.
    """

    name: str
    length_m: int
    attenuation_mdb_per_km: int
    dispersion_fs_per_nm_km: int
    splice_count: int = 0
    splice_loss_mdb: int = 0
    connector_count: int = 0
    connector_loss_mdb: int = 0
    aging_margin_mdb: int = 0
    group_index_milli: int = GROUP_INDEX_G652_MILLI
    raman_gain_mdb: int = 0
    raman_gain_reverse_mdb: int = 0
    pump_loss_mdb: int = 0

    def flipped(self) -> SpanInput:
        """The same fibre walked the other way, so the two Raman gains swap."""
        return replace(
            self,
            raman_gain_mdb=self.raman_gain_reverse_mdb,
            raman_gain_reverse_mdb=self.raman_gain_mdb,
        )


@dataclass(frozen=True)
class AmplifierInput:
    """One `OtnAmplifier`. Position comes from where it sits in the tuple."""

    name: str
    noise_figure_mdb: int
    gain_mdb: int


@dataclass(frozen=True)
class NodeInput:
    """Any `OtnOpticalElement` light passes through without amplification."""

    name: str
    insertion_loss_mdb: int


@dataclass(frozen=True)
class ModeInput:
    """One `OtnOpticalMode`. What the receiver needs, and what it tolerates."""

    name: str
    required_osnr_mdb: int
    cd_tolerance_fs_per_nm: int
    fec_latency_ns: int = 0


@dataclass(frozen=True)
class SectionInput:
    """One `OtnOpticalMultiplexSection`, ROADM to ROADM.

    **Two amplifier chains, one per direction**, because an erbium amplifier
    amplifies one way only. `amplifiers_a2b` runs from `head_node` towards
    `tail_node` and `amplifiers_b2a` the other way. `spans` is shared, since one
    fibre pair carries both directions.

    Every tuple is already ordered by `oms_sequence`; sorting is the caller's
    job because only the caller has seen the raw records. N spans carry N+1
    amplifiers **per direction**: amplifier k feeds span k, and amplifier N+1 is
    the pre-amplifier after span N.

    That pairing only works under one numbering convention:
    `oms_sequence` counts along the direction the chain amplifies. Position 1 of
    `amplifiers_b2a` is therefore the booster at `roadm_b`, a sorted chain is
    already in traversal order, and neither chain is ever reversed.

    Every label here is relative to the section's *current* orientation.
    `flipped()` swaps the chains along with the two nodes, so `amplifiers_a2b`
    always means head to tail.
    """

    name: str
    head_node: NodeInput
    tail_node: NodeInput
    spans: tuple[SpanInput, ...]
    amplifiers_a2b: tuple[AmplifierInput, ...]
    amplifiers_b2a: tuple[AmplifierInput, ...]

    @property
    def endpoints(self) -> tuple[str, str]:
        return self.head_node.name, self.tail_node.name

    @property
    def active_amplifiers(self) -> tuple[AmplifierInput, ...]:
        """The chain that amplifies the orientation being walked.

        Always the head-to-tail chain, which is exactly the invariant `flipped()`
        maintains. A walker never has to ask which way round the section is.
        """
        return self.amplifiers_a2b

    def flipped(self) -> SectionInput:
        """The same section traversed the other way.

        Head and tail swap, the span tuple reverses because a walk from
        `roadm_b` meets the last span first, each span's two Raman gains swap,
        and the two amplifier chains **swap**. Neither chain is reversed.

        Reversing a single chain instead would model the same erbium amplifiers
        running backwards, and the shipped dataset would not catch it: all 306
        amplifiers carry the same noise figure and the same gain, so the wrong
        chain produces the right number.

        One carrier in the shipped dataset needs this: the five Frankfurt to
        Vienna wavelengths cross `oms-vie-mil` from Milan towards Vienna.
        """
        return replace(
            self,
            head_node=self.tail_node,
            tail_node=self.head_node,
            spans=tuple(span.flipped() for span in reversed(self.spans)),
            amplifiers_a2b=self.amplifiers_b2a,
            amplifiers_b2a=self.amplifiers_a2b,
        )

    def validate(self) -> None:
        """Every invariant the graph does not enforce.

        The server enforces no cardinality on either of the section's two
        amplifier relationships, `amplifiers_a2b` and `amplifiers_b2a`, so this
        is the only guard there is. Each direction is checked on its own, and
        the message names it: a chain short by one is a hole in that direction
        and says which.

        It is also what catches a query that selects one relationship and
        forgets the other. That arrives as an empty chain rather than an error,
        and without the N+1 rule the section would budget as healthy on half its
        amplifiers.

        The negative-loss check lives here rather than in `span_fiber_loss_mdb`
        because that function is reached once per hop while a report renders,
        and raising there turns a data error into a failed render. The check
        runs `validate()` inside a `ValueError` boundary, which is where a person
        will actually see the message.
        """
        if not self.spans:
            raise ValueError(f"{self.name} has no spans")
        expected = len(self.spans) + 1
        for direction, chain in (("a_to_b", self.amplifiers_a2b), ("b_to_a", self.amplifiers_b2a)):
            if len(chain) != expected:
                raise ValueError(
                    f"{self.name} {direction} chain has {len(chain)} amplifiers for {len(self.spans)} spans, "
                    f"expected {expected}"
                )
        for span in self.spans:
            for direction, gain in (("a_to_b", span.raman_gain_mdb), ("b_to_a", span.raman_gain_reverse_mdb)):
                effective = _effective_fiber_loss_mdb(span, gain)
                if effective < 0:
                    raise ValueError(
                        f"{self.name} span {span.name} is credited {-effective} mdB more {direction} Raman gain "
                        f"than the fibre has loss, which is a data error and not a negative loss"
                    )
        if self.head_node.name == self.tail_node.name:
            raise ValueError(f"{self.name} starts and ends at {self.head_node.name}")


@dataclass(frozen=True)
class PathElement:
    """One entry on the flattened chain. Exactly one payload field is set."""

    kind: str
    name: str
    node: NodeInput | None = None
    amplifier: AmplifierInput | None = None
    span: SpanInput | None = None

    @property
    def loss_mdb(self) -> int:
        """What this element costs the power budget, ageing included."""
        if self.node is not None:
            return self.node.insertion_loss_mdb
        if self.span is not None:
            return span_loss_mdb(self.span)
        return 0

    @property
    def fiber_loss_mdb(self) -> int:
        """What this element costs the OSNR path, ageing excluded."""
        if self.node is not None:
            return self.node.insertion_loss_mdb
        if self.span is not None:
            return span_fiber_loss_mdb(self.span)
        return 0


@dataclass(frozen=True)
class Hop:
    """One row of the budget table.

    The two Raman fields are zero on every hop that is not a pumped span, which
    is 123 of the 132 spans the dataset ships. They are carried here rather than
    recomputed by the report because the credit belongs to the orientation being
    walked, and by the time a report holds a `Hop` the orientation has already
    been settled. A report that went back to the section records to find the
    gain would have to redo that reasoning and could get it backwards.
    """

    index: int
    kind: str
    name: str
    length_m: int
    loss_mdb: int
    gain_mdb: int
    osnr_stage_mdb: int | None
    cumulative_length_m: int
    cumulative_loss_mdb: int
    cumulative_osnr_mdb: int | None
    cumulative_delay_ns: int
    raman_gain_mdb: int = 0
    pump_loss_mdb: int = 0

    @property
    def unpumped_loss_mdb(self) -> int:
        """What this span would cost with its pumps removed.

        The pump is credited its on-off gain and charged its combiner loss, so
        taking both back out is the figure to compare against. Zero Raman
        leaves this equal to `loss_mdb`.
        """
        return self.loss_mdb + self.raman_gain_mdb - self.pump_loss_mdb


@dataclass(frozen=True)
class PathBudget:
    """Everything the check gates on and the transform renders."""

    hops: tuple[Hop, ...]
    total_length_m: int
    total_loss_mdb: int
    osnr_total_mdb: int
    required_osnr_mdb: int
    system_margin_mdb: int
    osnr_margin_mdb: int
    cd_total_fs_per_nm: int
    cd_tolerance_fs_per_nm: int
    cd_margin_fs_per_nm: int
    latency_ns: int
    node_count: int
    amplifier_count: int
    span_count: int
    gain_shortfalls: tuple[str, ...]

    @property
    def osnr_ok(self) -> bool:
        return self.osnr_margin_mdb >= 0

    @property
    def cd_ok(self) -> bool:
        return self.cd_margin_fs_per_nm >= 0

    @property
    def gain_ok(self) -> bool:
        return not self.gain_shortfalls

    @property
    def ok(self) -> bool:
        return self.osnr_ok and self.cd_ok and self.gain_ok


def _effective_fiber_loss_mdb(span: SpanInput, raman_gain_mdb: int) -> int:
    """Effective fibre loss for one orientation, unclamped and signed.

    Separate from `span_fiber_loss_mdb` so `validate()` can see the negative
    value that the public function floors away.
    """
    attenuation = _round_div(span.length_m * span.attenuation_mdb_per_km, M_PER_KM)
    splices = span.splice_count * span.splice_loss_mdb
    connectors = span.connector_count * span.connector_loss_mdb
    return attenuation + splices + connectors + span.pump_loss_mdb - raman_gain_mdb


def span_fiber_loss_mdb(span: SpanInput) -> int:
    """Loss present in the fiber today: attenuation, splices and connectors.

    This is the OSNR path. The ageing allowance is deliberately absent; see the
    module docstring.

    A Raman pump on the span subtracts `raman_gain_mdb`, which is the gain for
    the orientation being walked, and adds `pump_loss_mdb` whichever way the
    walk goes. The result is floored at zero and this function never raises:
    `PathElement.fiber_loss_mdb` reaches it once per hop while a report renders,
    and raising there would turn a data error into a failed render.
    `SectionInput.validate()` is where the same data error is reported.
    """
    return max(_effective_fiber_loss_mdb(span, span.raman_gain_mdb), 0)


def span_loss_mdb(span: SpanInput) -> int:
    """Total span loss, ageing allowance included.

    This is the power budget: what the amplifier ahead has to recover, and what
    the loss column of the report shows.
    """
    return span_fiber_loss_mdb(span) + span.aging_margin_mdb


def span_dispersion_fs_per_nm(span: SpanInput) -> int:
    """Accumulated chromatic dispersion over one span."""
    return _round_div(span.length_m * span.dispersion_fs_per_nm_km, M_PER_KM)


def span_delay_ns(span: SpanInput) -> int:
    """One-way propagation delay. Delegates rather than reimplementing."""
    return propagation_delay_ns(span.length_m, span.group_index_milli)


def osnr_stage_mdb(input_power_mdbm: int, noise_figure_mdb: int) -> int:
    """`OSNR_stage = P_in_per_channel - NF + 58.0`."""
    return input_power_mdbm - noise_figure_mdb + OSNR_REFERENCE_MDB


def cascade_osnr_mdb(stages_mdb: Sequence[int]) -> int:
    """Cascade OSNR stages in the linear domain.

    `1 / OSNR_total = sum(1 / OSNR_stage)`. Two equal stages therefore land
    exactly 3.010 dB below either of them.

    Raises on an empty sequence rather than evaluating `log10(0)`. A section
    with no amplifiers is a data defect and the engine says so.
    """
    if not stages_mdb:
        raise ValueError("cannot cascade an empty stage sequence: the path has no amplifier")
    linear = sum(10 ** (-mdb_to_db(stage) / 10) for stage in stages_mdb)
    return db_to_mdb(-10 * math.log10(linear))


def osnr_margin_mdb(
    osnr_total_mdb: int,
    required_osnr_mdb: int,
    system_margin_mdb: int = SYSTEM_MARGIN_MDB,
) -> int:
    """Delivered OSNR less the mode requirement and the system margin.

    PASS when this is at or above zero.
    """
    return osnr_total_mdb - required_osnr_mdb - system_margin_mdb


def dispersion_margin_fs_per_nm(cd_total_fs_per_nm: int, cd_tolerance_fs_per_nm: int) -> int:
    """The second, independent gate. PASS when this is at or above zero."""
    return cd_tolerance_fs_per_nm - cd_total_fs_per_nm


def order_sections(
    sections: Sequence[SectionInput],
    start_node: str | None = None,
) -> tuple[tuple[SectionInput, ...], str]:
    """Turn an unordered set of sections into a traversal-ordered chain.

    `OtnOpticalCarrier.sections` is a `cardinality: many` relationship and
    Infrahub relationships carry no order, so what arrives is a set. A valid
    path is a simple chain: exactly two endpoints of degree one, every other
    node of degree two, one connected component, no repeated section.

    Anything else raises, naming the sections, because summing unrelated
    sections produces a plausible number for a path that does not exist.

    When `start_node` is not given the lexicographically smaller endpoint is
    chosen, so the result is deterministic. Direction is observable: with
    unequal node insertion losses the two directions give different OSNRs,
    because the first amplifier sees the head node's loss.
    """
    if not sections:
        raise ValueError("cannot order an empty section list")
    names = [section.name for section in sections]
    if len(set(names)) != len(names):
        raise ValueError(f"sections repeat: {sorted(names)}")
    for section in sections:
        section.validate()

    degree: dict[str, int] = defaultdict(int)
    neighbours: dict[str, list[SectionInput]] = defaultdict(list)
    for section in sections:
        head, tail = section.endpoints
        degree[head] += 1
        degree[tail] += 1
        neighbours[head].append(section)
        neighbours[tail].append(section)

    ends = sorted(name for name, count in degree.items() if count == 1)
    branches = sorted(name for name, count in degree.items() if count > 2)
    if branches:
        raise ValueError(f"{sorted(names)} branch at {branches}, which is not a path")
    if len(ends) != 2:
        raise ValueError(f"{sorted(names)} do not form a simple chain: endpoints are {ends}")

    if start_node is None:
        start_node = ends[0]
    elif start_node not in ends:
        raise ValueError(f"{start_node} is not an endpoint of {sorted(names)}; endpoints are {ends}")

    ordered: list[SectionInput] = []
    used: set[str] = set()
    current = start_node
    for _ in range(len(sections)):
        candidates = [section for section in neighbours[current] if section.name not in used]
        if not candidates:
            raise ValueError(f"{sorted(names)} are not connected: the walk stalled at {current}")
        section = candidates[0]
        used.add(section.name)
        ordered.append(section)
        head, tail = section.endpoints
        current = tail if head == current else head
    if len(used) != len(sections):
        raise ValueError(f"{sorted(names)} are not one connected chain")
    return tuple(ordered), start_node


def flatten_path(sections: Sequence[SectionInput], start_node: str) -> tuple[PathElement, ...]:
    """Flatten an ordered chain into one directed element list.

    ```text
    node, amp, span, amp, ..., amp, node, amp, span, ..., amp, node
    ```

    S sections give exactly S+1 nodes. A section stored the other way round is
    flipped first, which swaps its two amplifier chains, so the chain read here
    is always `active_amplifiers`, the one that amplifies the way the walk
    goes. Two rules fall out, and a per-section rule gets both wrong:

    - The loss ahead of an amplifier is the loss of the element immediately
      before it. No special case for the first amplifier of a section.
    - Every node appears once, so its insertion loss is charged once. A
      per-section sum charges the shared ROADM of a two-section path twice, and
      7 dB is larger than either margin the demo's story turns on.
    """
    if not sections:
        raise ValueError("cannot flatten an empty section list")
    elements: list[PathElement] = []
    current = start_node
    for index, section in enumerate(sections):
        if section.head_node.name != current:
            if section.tail_node.name != current:
                raise ValueError(f"{section.name} does not touch {current}; its endpoints are {section.endpoints}")
            section = section.flipped()
        section.validate()
        if index == 0:
            elements.append(PathElement(NODE, section.head_node.name, node=section.head_node))
        for position, amplifier in enumerate(section.active_amplifiers):
            elements.append(PathElement(AMPLIFIER, amplifier.name, amplifier=amplifier))
            if position < len(section.spans):
                span = section.spans[position]
                elements.append(PathElement(SPAN, span.name, span=span))
        elements.append(PathElement(NODE, section.tail_node.name, node=section.tail_node))
        current = section.tail_node.name
    return tuple(elements)


def path_latency_ns(elements: Sequence[PathElement], fec_latency_ns: int = 0) -> int:
    """Propagation, plus node and amplifier latency, plus FEC.

    Propagation dominates by three orders of magnitude at continental distances.
    The other terms are here because zero is a stronger claim than small.
    """
    total = fec_latency_ns
    for element in elements:
        if element.kind == SPAN and element.span is not None:
            total += span_delay_ns(element.span)
        elif element.kind == NODE:
            total += ROADM_LATENCY_NS
        else:
            total += AMPLIFIER_LATENCY_NS
    return total


def evaluate_path(
    sections: Sequence[SectionInput],
    mode: ModeInput,
    start_node: str | None = None,
    launch_power_mdbm: int = LAUNCH_POWER_PER_CHANNEL_MDBM,
    system_margin_mdb: int = SYSTEM_MARGIN_MDB,
    node_penalty_mdb: int = ROADM_FILTERING_PENALTY_MDB,
) -> PathBudget:
    """Budget one end-to-end path against one optical mode.

    The sections may arrive in any order and in either direction; ordering and
    orientation are settled first. The cascade runs over the whole path, never
    per section: summing per-section OSNRs would be wrong and would look right.
    """
    ordered, start = order_sections(sections, start_node)
    elements = flatten_path(ordered, start)

    hops: list[Hop] = []
    stages: list[int] = []
    shortfalls: list[str] = []
    cumulative_length = 0
    cumulative_loss = 0
    cumulative_delay = 0
    nodes_seen = 0
    amplifiers = 0
    spans = 0
    cd_total = 0

    for index, element in enumerate(elements, start=1):
        length = 0
        loss = 0
        gain = 0
        stage: int | None = None

        if element.kind == SPAN and element.span is not None:
            spans += 1
            length = element.span.length_m
            loss = span_loss_mdb(element.span)
            cd_total += span_dispersion_fs_per_nm(element.span)
            cumulative_delay += span_delay_ns(element.span)
        elif element.kind == NODE and element.node is not None:
            nodes_seen += 1
            loss = element.node.insertion_loss_mdb
            cumulative_delay += ROADM_LATENCY_NS
        elif element.amplifier is not None:
            amplifiers += 1
            gain = element.amplifier.gain_mdb
            cumulative_delay += AMPLIFIER_LATENCY_NS
            # index is 1-based, so index - 2 is the element immediately before.
            # An amplifier can never be first: flatten_path always opens with a
            # node, and that is what makes one uniform rule possible.
            if index < 2:
                raise ValueError(f"{element.name} is the first element on the path, so it has no input")
            preceding = elements[index - 2]
            stage = osnr_stage_mdb(
                launch_power_mdbm - preceding.fiber_loss_mdb,
                element.amplifier.noise_figure_mdb,
            )
            stages.append(stage)
            if element.amplifier.gain_mdb < preceding.loss_mdb:
                shortfalls.append(element.amplifier.name)

        cumulative_length += length
        cumulative_loss += loss
        running = cascade_osnr_mdb(stages) - nodes_seen * node_penalty_mdb if stages else None
        hops.append(
            Hop(
                index=index,
                kind=element.kind,
                name=element.name,
                length_m=length,
                loss_mdb=loss,
                gain_mdb=gain,
                osnr_stage_mdb=stage,
                cumulative_length_m=cumulative_length,
                cumulative_loss_mdb=cumulative_loss,
                cumulative_osnr_mdb=running,
                cumulative_delay_ns=cumulative_delay,
                raman_gain_mdb=element.span.raman_gain_mdb if element.kind == SPAN and element.span else 0,
                pump_loss_mdb=element.span.pump_loss_mdb if element.kind == SPAN and element.span else 0,
            )
        )

    # Both terms are already millidecibels, so this stays in integers. Rounding
    # happened once, inside the cascade.
    osnr_total = cascade_osnr_mdb(stages) - nodes_seen * node_penalty_mdb
    return PathBudget(
        hops=tuple(hops),
        total_length_m=cumulative_length,
        total_loss_mdb=cumulative_loss,
        osnr_total_mdb=osnr_total,
        required_osnr_mdb=mode.required_osnr_mdb,
        system_margin_mdb=system_margin_mdb,
        osnr_margin_mdb=osnr_margin_mdb(osnr_total, mode.required_osnr_mdb, system_margin_mdb),
        cd_total_fs_per_nm=cd_total,
        cd_tolerance_fs_per_nm=mode.cd_tolerance_fs_per_nm,
        cd_margin_fs_per_nm=dispersion_margin_fs_per_nm(cd_total, mode.cd_tolerance_fs_per_nm),
        latency_ns=cumulative_delay + mode.fec_latency_ns,
        node_count=nodes_seen,
        amplifier_count=amplifiers,
        span_count=spans,
        gain_shortfalls=tuple(shortfalls),
    )


def evaluate_both_directions(
    sections: Sequence[SectionInput],
    mode: ModeInput,
    start_node: str | None = None,
    launch_power_mdbm: int = LAUNCH_POWER_PER_CHANNEL_MDBM,
    system_margin_mdb: int = SYSTEM_MARGIN_MDB,
    node_penalty_mdb: int = ROADM_FILTERING_PENALTY_MDB,
) -> tuple[PathBudget, PathBudget]:
    """The same path budgeted each way round.

    A wavelength is a two-way service and the two walks are no longer the same
    arithmetic: each direction reads its own amplifier chain, meets the two
    ROADMs' unequal insertion losses in the opposite order, and is credited the
    Raman gain of only the direction its pumps serve. One number for a section
    is therefore a choice about which walk to show, and `worse_direction` makes
    that choice explicitly rather than by whichever endpoint sorted first.

    The return is `(forward, reverse)`. `forward` starts at `start_node`, or at
    the lexicographically smaller endpoint when none is given, so it is the
    budget `evaluate_path` alone would have produced. `reverse` starts at the
    other end, which is read off the forward walk's last hop rather than
    recomputed.
    """
    forward = evaluate_path(sections, mode, start_node, launch_power_mdbm, system_margin_mdb, node_penalty_mdb)
    reverse = evaluate_path(
        sections,
        mode,
        forward.hops[-1].name,
        launch_power_mdbm,
        system_margin_mdb,
        node_penalty_mdb,
    )
    return forward, reverse


def worse_direction(forward: PathBudget, reverse: PathBudget) -> PathBudget:
    """The direction that constrains the service.

    Ranked on OSNR margin, because that is the figure a report shows and the
    gate the demo's story turns on. Ties keep the forward walk, so a symmetric
    path reports the same numbers it always did.
    """
    return reverse if reverse.osnr_margin_mdb < forward.osnr_margin_mdb else forward


@dataclass(frozen=True)
class RegeneratorInput:
    """One `OtnOduSwitch` standing between two segments of one route.

    Only the two fields the route budget reads. `framing_latency_ns` is the
    delay the device adds, from framing and from the electrical crossing, and it
    is charged once per device.

    The device's inherited `insertion_loss_mdb` is deliberately absent. It
    applies to the incoming segment only, because the device terminates the
    light rather than passing it through, so it belongs to that segment's own
    section list and not to a term spanning both. The schema comment on
    `OtnOduSwitch` says the same thing from the other side.
    """

    name: str
    framing_latency_ns: int = 0


@dataclass(frozen=True)
class SegmentInput:
    """One segment of a route: its own sections, its own mode, its own end.

    A segment is what `OtnOpticalPath` already models, one wavelength's route
    with that wavelength's figures, so nothing here is a new concept. What is
    new is `mode`: a regenerated route may run a different mode either side of
    the device, because the two halves are independent optical paths and the
    shorter one can afford a denser constellation.

    `regenerator` is the device at this segment's **far end**, and it is `None`
    on the last segment. Attaching the device to the segment it terminates
    rather than listing devices separately is what makes a double-charged
    framing delay unrepresentable: N segments carry exactly N-1 devices, and
    `RouteBudget` refuses any other shape. It is the same trick
    `SectionInput.validate` uses for the N+1 amplifier rule.
    """

    sections: tuple[SectionInput, ...]
    mode: ModeInput
    start_node: str | None = None
    regenerator: RegeneratorInput | None = None


@dataclass(frozen=True)
class SegmentBudget:
    """One segment's budget, with the segment number it was quoted for.

    The number is not decoration. It is the only thing that makes a margin
    quotable at all on a multi-segment route, because a margin with no segment
    attached is a claim about the route that the model never computed. Every
    accessor on `RouteBudget` that returns a margin returns it paired with this
    number.

    `sequence` matches `OtnOpticalPath.segment_sequence`, which is 1-based and
    defaults to 1, so a path written before this feature reads as segment 1 of
    one.
    """

    sequence: int
    budget: PathBudget
    regenerator: RegeneratorInput | None = None


@dataclass(frozen=True)
class RouteBudget:
    """A whole route, segment by segment, with no route margin anywhere on it.

    **The absence is the design.** There is no `osnr_margin_mdb` here, no
    `osnr_total_mdb`, and no total loss, and adding one would be a defect rather
    than a convenience: see the module docstring for the failure that shape
    invites. A caller that wants a number gets `segment_margins_mdb`, which
    pairs each margin with its segment, or `sole_segment`, which returns the one
    `PathBudget` of a route that genuinely has one segment and raises on a route
    that does not.

    What is exposed as a route figure is what genuinely totals across a
    regeneration: `latency_ns`, because delay adds where noise restarts, and
    `total_length_m`, because geography does not care where the light was
    rebuilt.

    **Why the verdict is derived here rather than stored on the node.** R-012
    item 2 in `specs/017-oeo-cross-connect/research.md` is the native-first
    record, and it was measured rather than argued: a transform-backed computed
    attribute on `OtnService` was loaded against a live instance and the loader
    accepted it, so the capability is real here. It was declined for two reasons
    of its own. It relocates the Python instead of removing it, because a
    transform-backed computed attribute **is** a Python transform. And it would
    split the conjunction across this module's no-SDK boundary, since a
    transform has to read the service's paths through a query, so the one rule
    FR-014 exists to protect would live in two places and a check would then be
    testing a cache of the rule instead of the rule.
    """

    segments: tuple[SegmentBudget, ...]

    def __post_init__(self) -> None:
        """Every invariant a caller could otherwise get wrong in silence.

        Sequences must be exactly 1..n in order, because `segment_margins_mdb`
        reports them and a duplicated or missing number would make two margins
        indistinguishable. The schema takes the duplicate case at write time via
        `OtnOpticalPath`'s uniqueness constraint on `(service,
        segment_sequence)`; it cannot take the gap, so this is the only place
        the sequence 1, 2, 4 is refused.

        Only the last segment may lack a regenerator, and every earlier one must
        have one. A missing device in the middle would mean two segments joined
        by nothing, which is not a route, and a device on the last segment would
        charge a framing delay for a regeneration that never happens.
        """
        if not self.segments:
            raise ValueError("a route has at least one segment")
        expected = tuple(range(1, len(self.segments) + 1))
        actual = tuple(segment.sequence for segment in self.segments)
        if actual != expected:
            raise ValueError(f"segment sequences are {list(actual)}, expected {list(expected)} in order")
        for segment in self.segments[:-1]:
            if segment.regenerator is None:
                raise ValueError(
                    f"segment {segment.sequence} of {len(self.segments)} ends at no device, "
                    "so the two segments either side of it are joined by nothing"
                )
        if self.segments[-1].regenerator is not None:
            raise ValueError(
                f"the last segment carries regenerator {self.segments[-1].regenerator.name}, "
                "which would charge a framing delay for a regeneration that does not happen"
            )

    @property
    def segment_count(self) -> int:
        return len(self.segments)

    @property
    def regenerators(self) -> tuple[RegeneratorInput, ...]:
        """The devices the route is regenerated at, in order. Empty for one segment."""
        return tuple(segment.regenerator for segment in self.segments if segment.regenerator is not None)

    @property
    def is_regenerated(self) -> bool:
        """Whether any figure quoted from this route belongs to a regenerated route.

        FR-014 asks a report to say so, and this is the flag it says it with.
        """
        return len(self.segments) > 1

    @property
    def latency_ns(self) -> int:
        """Propagation and equipment delay across every segment, plus framing.

        The one honest route scalar, and the reason it is honest is that delay
        adds where noise restarts. Each segment's own `latency_ns` already
        carries that segment's mode FEC, which is charged per segment on purpose:
        a regenerator re-encodes, so the route pays FEC once per segment and not
        once end to end.

        Each device's `framing_latency_ns` is added exactly once, because a
        device is attached to the segment it terminates and the last segment
        carries none.
        """
        total = sum(segment.budget.latency_ns for segment in self.segments)
        return total + sum(device.framing_latency_ns for device in self.regenerators)

    @property
    def total_length_m(self) -> int:
        """Fibre crossed end to end. Geography does not restart at a device."""
        return sum(segment.budget.total_length_m for segment in self.segments)

    @property
    def segment_margins_mdb(self) -> tuple[tuple[int, int], ...]:
        """Every segment's OSNR margin, each paired with its segment number.

        The pairing is the point. A caller cannot read a margin out of here
        without also reading which segment it belongs to, so it cannot quote one
        as the route's.
        """
        return tuple((segment.sequence, segment.budget.osnr_margin_mdb) for segment in self.segments)

    @property
    def failing_segments(self) -> tuple[int, ...]:
        """Which segments do not close, by segment number. Empty when all do.

        A route that does not close is a first-class output, and naming the
        segment is what makes the refusal actionable: on a two-segment route the
        useful sentence is which half is short, not that the route failed.
        """
        return tuple(segment.sequence for segment in self.segments if not segment.budget.ok)

    @property
    def ok(self) -> bool:
        """The route's verdict, and a conjunction over the segments.

        Every segment closes or the route does not. There is no averaging here
        and no weakest-link margin, because a margin is not what the conjunction
        is over: a segment either delivers what its mode needs or it does not,
        and one that does not cannot be offset by another that does.
        """
        return all(segment.budget.ok for segment in self.segments)

    def sole_segment(self) -> PathBudget:
        """The one segment's budget, for a route that has exactly one.

        This is the whole of the compatibility surface for the 99 per cent of
        the dataset that is unregenerated, and it **raises** on a route with
        more than one segment. That refusal is the feature, not a limitation: a
        caller reaching for a single `PathBudget` is about to quote its margin,
        and on a regenerated route there is no such figure to quote. Failing
        here is how the wrong report becomes hard to write instead of merely
        documented as wrong.

        A method rather than a property, deliberately. A property that raises
        detonates inside an f-string, a `repr` and a debugger watch, which are
        three places a person is not asking a question and does not deserve an
        exception.
        """
        if len(self.segments) != 1:
            margins = ", ".join(
                f"segment {sequence} {mdb_to_db(margin):+.3f} dB" for sequence, margin in self.segment_margins_mdb
            )
            raise ValueError(
                f"this route has {len(self.segments)} segments and therefore no single margin: {margins}. "
                "Read segment_margins_mdb and say which segment the figure belongs to"
            )
        return self.segments[0].budget


def evaluate_route(
    segments: Sequence[SegmentInput],
    launch_power_mdbm: int = LAUNCH_POWER_PER_CHANNEL_MDBM,
    system_margin_mdb: int = SYSTEM_MARGIN_MDB,
    node_penalty_mdb: int = ROADM_FILTERING_PENALTY_MDB,
) -> RouteBudget:
    """Budget a route one segment at a time, restarting the cascade at each device.

    There is no arithmetic here beyond calling `evaluate_path` once per segment
    with that segment's own mode. That is what regeneration means: the device
    terminates the light and re-originates it, so the noise the first segment
    accumulated does not cross it and the second segment starts from a
    transmitter. A route budget that carried one cascade across the device would
    charge the second half for noise that was removed.

    A single-segment route is exactly `evaluate_path` wrapped, and returns the
    same `PathBudget` through `sole_segment`. That is deliberate: every existing
    caller and every existing figure is unchanged by this function existing.

    The verdict, the latency sum and the reason there is no route margin are all
    on `RouteBudget`.
    """
    if not segments:
        raise ValueError("cannot budget a route with no segments")
    return RouteBudget(
        segments=tuple(
            SegmentBudget(
                sequence=sequence,
                budget=evaluate_path(
                    segment.sections,
                    segment.mode,
                    segment.start_node,
                    launch_power_mdbm,
                    system_margin_mdb,
                    node_penalty_mdb,
                ),
                regenerator=segment.regenerator,
            )
            for sequence, segment in enumerate(segments, start=1)
        )
    )
