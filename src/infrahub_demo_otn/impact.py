"""Turn plant and service records into the answers the six reports print.

Same layering as the rest of the package. `budget` and `routing` decide,
`plant` adapts a GraphQL payload into plain mappings, and this module derives
the reporting answers on top of both. It imports `plant` and `units` and never
`infrahub_sdk`: a payload is a dict, and keeping it that way is what lets every
number below be asserted with no server running.

Five derivations, none of them arithmetic the engine already does:

- **Amplifier census.** A section's amplifiers split by the direction they
  serve. A single total reads the same for a healthy section and for one with a
  chain missing.
- **Reach.** A mode's catalog reach against a section's summed span length.
- **Latency.** The path's recorded delay against the service's budget, with
  propagation summed per span so the reader can see what fraction is fiber.
- **Exposure.** The conduits a circuit's spans occupy, unioned over its segments
  and grouped by conduit.
- **Diversity.** Pairs of services derived from those groups.

The transforms above this module unwrap, call in here, and format. The
arithmetic lives here once so six reports cannot disagree about it.

**Occupancy is not one of them, and used to be.** There was a `SectionOccupancy`
here that counted anchors, a `route_free_channels` that intersected sets of
channel numbers, and a `plant.occupied_channels` that projected the intervals
back down to feed them. All three are gone. A wavelength occupies a width and not
a number, so the free set on a section is `units.free_blocks` over the intervals
`plant.occupancy_from_graphql` returns, and the free set on a route is
`routing.route_free_blocks` over the same. Keeping a second, anchor-counting
answer here is what let the capacity report claim eight more 400G carriers fitted
on a section that had room for one.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from infrahub_demo_otn.plant import (
    nodes_of,
    peer,
    peers,
    unwrap,
)
from infrahub_demo_otn.units import (
    GBPS_PER_TBPS,
    GROUP_INDEX_G652_MILLI,
    gbps_to_tbps,
    m_to_km,
    mdb_to_db,
    mhz_to_thz,
    ns_to_us,
    propagation_delay_ns,
)

AI_PROFILES = frozenset({"ai-training-dci", "ai-inference", "hpc-research"})
"""The service profiles that carry a latency budget.

`hpc-research` is in here with the two AI profiles because what makes a profile
interesting to these reports is that it is latency-bound and loss-intolerant,
and research computing is both. `ip-transit` and `legacy-sdh` are neither.
"""

SPAN_KIND = "OtnFiberSpan"
"""The one hop kind that carries a length, a fiber type and a conduit."""

UNDUCTED = None
"""A span outside any conduit. Not a conduit named null.

Most spans in the dataset have no conduit, and grouping them under one missing
key would invent the largest shared-risk group in the network out of the absence
of data.
"""


# ---------------------------------------------------------------------------
# The container tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientContainer:
    """One client container on a wavelength, and the line container above it.

    `line_container` is the name of the container this client is multiplexed
    into, or `None` when the client sits directly on the carrier with nothing
    between. Both shapes are real. The second one is the data written before
    grooming existed, and the spec is explicit that it is read as a container on
    its carrier rather than having a parent invented for it.
    """

    record: dict[str, Any]
    line_container: str | None


def holds_client(container: Mapping[str, Any]) -> bool:
    """Whether this container carries a client signal, which is what names its role.

    **There is no type field separating a line container from a client one.** Both
    are `OtnContainer`. The roles are told apart by what they hold: a line
    container holds a `carrier` and a `tributary_slot_capacity` and no
    `client_signal`, and a client container holds a `client_signal` and a
    `parent_container` and no carrier of its own.

    `client_signal` is the half tested here, and it is tested rather than
    `carrier` for a reason. A line container whose carrier was reclaimed still
    exists, inert, holding no wavelength, which
    `generators/optical_service.py._refuse` records as a real outcome of a
    refusal that reverses an earlier success. Keying on the absence of a carrier
    would read that inert row as a client container and then fail to find its
    signal. Keying on the presence of a signal reads it as what it is: a line
    container with nothing in it.

    Ordering is not used and must not be. The two roles arrive in one
    `containers` collection ordered by name, and `odu-line-oc-x` sorts before
    `odu-svc-x` today purely because of how the two writers happen to name
    things.
    """
    node = (container.get("client_signal") or {}).get("node")
    return isinstance(node, dict)


def client_containers(carrier: Mapping[str, Any]) -> list[ClientContainer]:
    """Every client on one wavelength, found through the parent hop.

    **This is the function FR-023 exists for.** A client container used to hang
    directly off its carrier, so `carrier.containers` returned it and reading
    `client_signal` off that worked. Grooming put a line container in between:
    `carrier.containers` now returns the line container, which has no
    `client_signal`, and a report that keeps reading the signal off it gets
    nothing rather than an error. The trace still renders. It renders a service
    with no client, silently, which is worse than a failure because nobody looks.
    One implementation, so a fix to the walk cannot land in one report and miss
    the other.

    Both shapes are walked. A container directly on the carrier that holds a
    signal is a client on its own wavelength, and it is yielded with no line
    container above it. A container that holds no signal is a line container, and
    its `child_containers` are the clients.

    **One hop, because one hop is what the queries select.** A client nested two
    levels down, if the model ever grew one, is not in the payload at all, so it
    is not silently dropped here: it never arrives. `queries/service_trace.gql`
    and `queries/span_impact.gql` are where that would be changed.

    Sorted by the line container and then by the client's own name, so two
    renders of one branch produce the same rows in the same order whatever order
    the server returned the edges in.
    """
    found: list[ClientContainer] = []
    for container in peers(carrier, "containers"):
        if holds_client(container):
            found.append(ClientContainer(record=container, line_container=None))
            continue
        line_name = str(container["name"])
        for child in peers(container, "child_containers"):
            if holds_client(child):
                found.append(ClientContainer(record=child, line_container=line_name))
    return sorted(found, key=lambda item: (item.line_container or "", str(item.record["name"])))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
#
# Every scaled integer is rendered next to its raw value, so no reader has to
# divide by a thousand in their head.
#
# The formatters live here rather than in each transform because six reports
# print the same quantities, and six copies is how two of them start disagreeing
# about how many decimal places a decibel has. They divide by nothing
# themselves: every conversion goes through `units.py`.


def decibels(value_mdb: int) -> str:
    return f"{mdb_to_db(value_mdb):.3f} dB"


def signed_decibels(value_mdb: int) -> str:
    return f"{mdb_to_db(value_mdb):+.3f} dB"


def kilometres(value_m: int) -> str:
    return f"{m_to_km(value_m):.3f} km"


def microseconds(value_ns: int) -> str:
    return f"{ns_to_us(value_ns):.3f} us"


def signed_microseconds(value_ns: int) -> str:
    return f"{ns_to_us(value_ns):+.3f} us"


def terahertz(value_mhz: int) -> str:
    return f"{mhz_to_thz(value_mhz):.5f} THz"


def terabits(rate_gbps: int) -> str:
    """Aggregate capacity, in the unit an outage call is conducted in."""
    return f"{gbps_to_tbps(rate_gbps):.1f} Tbps" if rate_gbps >= GBPS_PER_TBPS else f"{rate_gbps} Gbps"


@dataclass(frozen=True)
class AmplifierCensus:
    """How many amplifiers a section holds, split by the direction they serve.

    A single total stopped being an answer when the section grew two chains.
    "8 amplifiers affected" reads identically for a healthy four-span section
    and for one whose `b_to_a` chain is missing three members, and those are
    very different cuts to be told about.

    There is no third count. An amplifier is in one of the section's two lists,
    the other, or in no section at all, and a report scoped to one section
    cannot see the third case.
    """

    a_to_b: int
    b_to_a: int

    @property
    def total(self) -> int:
        return self.a_to_b + self.b_to_a

    @property
    def balanced(self) -> bool:
        """Both chains the same length."""
        return self.a_to_b == self.b_to_a


def amplifier_census(section: Mapping[str, Any]) -> AmplifierCensus:
    """Count one section's amplifiers per direction, from a raw section record.

    This takes the record rather than a `SectionInput` on purpose. The impact
    report counts amplifiers, it does not budget them, and `build_section` feeds
    a `validate()` that raises on a chain that does not hold N+1 members.
    Refusing to report the cut because the plant is malformed is exactly
    backwards: the malformed chain is the thing the operator needs to see.

    The direction is which list the record came out of, so nothing is read off
    the record itself and there is no token to be missing.
    """
    return AmplifierCensus(
        a_to_b=sum(1 for _ in peers(section, "amplifiers_a2b")),
        b_to_a=sum(1 for _ in peers(section, "amplifiers_b2a")),
    )


# ---------------------------------------------------------------------------
# Reach
# ---------------------------------------------------------------------------


def section_lengths_m(payload: Mapping[str, Any]) -> dict[str, int]:
    """Section name to the summed length of its spans, in metres."""
    return {
        str(record["name"]): sum(int(span["length_m"]) for span in peers(record, "spans"))
        for record in nodes_of(payload, "OtnOpticalMultiplexSection")
    }


@dataclass(frozen=True)
class ModeReach:
    """One catalog mode against every section in the plant."""

    name: str
    mode_class: str
    line_rate_gbps: int
    nominal_reach_m: int
    required_osnr_mdb: int
    in_reach: tuple[str, ...]
    out_of_reach: tuple[str, ...]
    shortest_section: str
    shortest_section_length_m: int

    @property
    def section_count(self) -> int:
        return len(self.in_reach) + len(self.out_of_reach)

    @property
    def reaches_nothing(self) -> bool:
        return not self.in_reach

    @property
    def reaches_everything(self) -> bool:
        return not self.out_of_reach

    @property
    def shortfall_to_shortest_m(self) -> int:
        """How far short of the shortest section this mode falls.

        Negative when the mode clears it. Positive is the number that matters:
        for a 120 km part against a 220 km shortest section it is 100 000, and
        that is the whole 400ZR finding in one integer.
        """
        return self.shortest_section_length_m - self.nominal_reach_m


def reach_table(payload: Mapping[str, Any]) -> list[ModeReach]:
    """Every mode against every section, sorted by reach.

    **Reach is not the budget.** A mode whose catalog reach covers a section can
    still fail the OSNR margin over that section's specific amplifier chain, and
    `budget.evaluate_path` is the thing that decides that. This answers the
    procurement question, is it worth ordering these parts, and the callers say
    so rather than implying the two tests are the same.
    """
    lengths = section_lengths_m(payload)
    if not lengths:
        raise ValueError("the payload contains no optical multiplex section, so no reach can be judged")
    shortest, shortest_length = min(lengths.items(), key=lambda item: (item[1], item[0]))

    modes = []
    for record in nodes_of(payload, "OtnOpticalMode"):
        reach = int(record["nominal_reach_m"])
        modes.append(
            ModeReach(
                name=str(record["name"]),
                mode_class=str(record["mode_class"]),
                line_rate_gbps=int(record["line_rate_gbps"]),
                nominal_reach_m=reach,
                required_osnr_mdb=int(record["required_osnr_mdb"]),
                in_reach=tuple(sorted(name for name, length in lengths.items() if length <= reach)),
                out_of_reach=tuple(sorted(name for name, length in lengths.items() if length > reach)),
                shortest_section=shortest,
                shortest_section_length_m=shortest_length,
            )
        )
    modes.sort(key=lambda mode: (mode.nominal_reach_m, mode.name))
    return modes


# ---------------------------------------------------------------------------
# Services, paths and hops
# ---------------------------------------------------------------------------


def is_ai_profile(profile: str | None) -> bool:
    """Whether a service profile carries a latency budget."""
    return profile in AI_PROFILES


def path_carrier(path: Mapping[str, Any]) -> dict[str, Any]:
    """The carrier a path rides, unwrapped, or `{}` when it names none.

    `{}` rather than a raise, because a path with no carrier is a shape the
    payload can hold: `queries/srlg_exposure.gql` selects the carrier only for
    its sections, and a sparse payload answers null. A caller that needs the
    carrier's name checks for it; a caller reading `odu_switches` off an empty
    mapping gets no devices, which is the same answer as a carrier nothing
    terminates.
    """
    node = (path.get("carrier") or {}).get("node")
    return unwrap(node) if isinstance(node, dict) else {}


@dataclass(frozen=True)
class CircuitSegment:
    """One segment of a circuit, with the device standing at its far end.

    `sequence` is `OtnOpticalPath.segment_sequence` as the graph holds it and not
    this segment's index in the list. That is what makes a margin quotable: a
    figure read off `path` is a figure about this segment, and a report printing
    it prints this number beside it. `budget.SegmentBudget` pairs the two for the
    same reason and `budget.RouteBudget` exposes no route margin at all.

    `junction` is the whole `OtnOduSwitch` record rather than its name, because
    the two fields a report wants beside the name, `switching_mode` and
    `framing_latency_ns`, are on it and re-fetching them from a name is not a
    thing a pure module can do.

    It is `None` on the last segment, because nothing regenerates light nobody
    carries on. It is also `None` when the payload records no device terminating
    both this carrier and the next, and a report says so rather than printing a
    chain with a silent hole in it: two segments joined by nothing is exactly the
    shape R-008 measured 48 phantom instances of.
    """

    sequence: int
    path: dict[str, Any]
    junction: dict[str, Any] | None = None

    @property
    def carrier(self) -> dict[str, Any]:
        return path_carrier(self.path)

    @property
    def carrier_name(self) -> str | None:
        name = self.carrier.get("name")
        return None if name is None else str(name)

    @property
    def junction_device(self) -> str | None:
        return None if self.junction is None else str(self.junction["name"])

    @property
    def junction_site(self) -> str | None:
        """The site the light is rebuilt at, or `None` when the payload omits it.

        A junction with no site is still a junction. The device is what joins the
        two wavelengths, and a query that did not select `site` has not stopped
        that being true.
        """
        if self.junction is None:
            return None
        node = (self.junction.get("site") or {}).get("node")
        return str(unwrap(node)["name"]) if isinstance(node, dict) else None


def _junction_between(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any] | None:
    """The `OtnOduSwitch` that terminates both carriers, or `None` for neither.

    The junction is derived and not stored. `OtnOpticalPath` has no relationship
    to the device, and it deliberately does not: the fact that makes a device a
    junction is that its `carriers` holds the wavelength arriving and the
    wavelength leaving, so a second edge saying the same thing could disagree
    with the first. `chains.joins` evaluates the same predicate when it picks a
    cover, and reads it off the same relationship.

    The lowest-named device when several qualify, matching `chains.junction_at`,
    so two cross-connects at one site produce the same answer on every run. An
    Infrahub relationship hands back a set and nothing in it is ordered.
    """
    later = {str(device["name"]) for device in peers(second, "odu_switches") if device.get("name")}
    qualifying = sorted(
        (device for device in peers(first, "odu_switches") if str(device.get("name")) in later),
        key=lambda device: str(device["name"]),
    )
    return qualifying[0] if qualifying else None


def circuit_segments(service: Mapping[str, Any]) -> list[CircuitSegment]:
    """A circuit's segments in order, with the junction device between each pair.

    **One walk, and every report follows it.** `OtnService.optical_path` is
    cardinality many because a circuit regenerated at an intermediate site is one
    path per wavelength, and an Infrahub relationship hands back a set, so
    nothing in the payload says which segment came first. `segment_sequence`
    does, and sorting on it here rather than in three transforms is the same
    decision commit `bebf2a0` made for `client_containers`: a report that stops
    following the segments then fails in one place instead of drifting from the
    other two.

    An unprovisioned service returns an empty list. A single-segment circuit
    returns one segment with no junction, which is the whole route, and that is
    every circuit written before this feature: `segment_sequence` defaults to 1
    and a path that was the route stays segment 1 of one.
    """
    paths = sorted(peers(service, "optical_path"), key=lambda path: int(path.get("segment_sequence") or 1))
    segments: list[CircuitSegment] = []
    for index, path in enumerate(paths):
        following = paths[index + 1] if index + 1 < len(paths) else None
        segments.append(
            CircuitSegment(
                sequence=int(path.get("segment_sequence") or 1),
                path=path,
                junction=(
                    None if following is None else _junction_between(path_carrier(path), path_carrier(following))
                ),
            )
        )
    return segments


def service_path(service: Mapping[str, Any]) -> dict[str, Any] | None:
    """The first segment of a service's route, or `None` when it is not provisioned.

    The lowest `segment_sequence`, through `circuit_segments` so there is one
    ordering rule and not two. For a circuit that spans a single wavelength that
    first segment is the whole route, which is the same path every caller read
    before the relationship widened.

    **On a chained circuit this is the first segment and not the route**, so a
    caller that totals anything off it under-reports. `transforms/ai_latency.py`
    is the one caller left, and its sum is wrong for a chain by the second
    segment's delay plus each device's `framing_latency_ns`; `budget.RouteBudget`
    already has that arithmetic and wiring the report to it is its own task. The
    three reports that follow the whole circuit call `circuit_segments`.
    """
    segments = circuit_segments(service)
    return segments[0].path if segments else None


def path_hops(path: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The hops of a path in sequence order.

    Infrahub relationships carry no order, so what arrives is a set in whatever
    order the query returned it. Sorting here rather than in the callers is the
    same decision `plant.build_section` made for spans and amplifiers, and for
    the same reason: only this layer has seen the raw records.
    """
    return sorted(peers(path, "hops"), key=lambda hop: int(hop.get("sequence") or 0))


def hop_element(hop: Mapping[str, Any]) -> dict[str, Any]:
    """The optical element a hop crosses, unwrapped, with its `__typename` kept."""
    return peer(hop, "element")


def path_propagation_ns(path: Mapping[str, Any]) -> int:
    """One-way fiber propagation over a path, summed per span.

    Summed per span at that span's own fiber type group index, not computed once
    over the total length at the G.652 default. Every span in the shipped dataset
    is G.652.D so the two agree exactly today; the catalog already carries
    G.654.E at 1467, which is the day they stop agreeing. A span whose fiber type
    the payload does not carry falls back to the published default rather than
    being dropped, because dropping it would understate the delay.
    """
    total = 0
    for hop in path_hops(path):
        element = hop_element(hop)
        if element.get("__typename") != SPAN_KIND:
            continue
        fiber = (element.get("fiber_type") or {}).get("node")
        index = int(unwrap(fiber)["group_index_milli"]) if isinstance(fiber, dict) else GROUP_INDEX_G652_MILLI
        total += propagation_delay_ns(int(element["length_m"]), index)
    return total


@dataclass(frozen=True)
class LatencyVerdict:
    """One service's accumulated delay against the budget it declared."""

    service: str
    customer: str
    profile: str
    sections: tuple[str, ...]
    total_length_m: int
    latency_ns: int
    propagation_ns: int
    budget_ns: int | None

    @property
    def overhead_ns(self) -> int:
        """Everything that is not fiber: ROADMs, amplifiers and FEC."""
        return self.latency_ns - self.propagation_ns

    @property
    def overhead_share_percent(self) -> float:
        return 100.0 * self.overhead_ns / self.latency_ns if self.latency_ns else 0.0

    @property
    def margin_ns(self) -> int | None:
        return None if self.budget_ns is None else self.budget_ns - self.latency_ns

    @property
    def ok(self) -> bool | None:
        """`None` means no budget was recorded, which is not the same as a pass."""
        margin = self.margin_ns
        return None if margin is None else margin >= 0


def latency_rows(payload: Mapping[str, Any]) -> list[LatencyVerdict]:
    """One row per provisioned service, unfiltered.

    Filtering to the AI and HPC profiles is the caller's job, because the caller
    is also the thing that has to say how many rows it dropped. A report that
    silently shows three of five services is a report the reader cannot check.
    """
    rows: list[LatencyVerdict] = []
    for service in nodes_of(payload, "OtnService"):
        path = service_path(service)
        if path is None:
            continue
        carrier = (path.get("carrier") or {}).get("node")
        sections = tuple(sorted(str(item["name"]) for item in peers(unwrap(carrier), "sections"))) if carrier else ()
        budget = service.get("max_latency_ns")
        rows.append(
            LatencyVerdict(
                service=str(service["name"]),
                customer=str(service.get("customer") or ""),
                profile=str(service.get("service_profile") or ""),
                sections=sections,
                total_length_m=int(path["total_length_m"]),
                latency_ns=int(path["latency_ns"]),
                propagation_ns=path_propagation_ns(path),
                budget_ns=None if budget is None else int(budget),
            )
        )
    rows.sort(key=lambda row: (row.margin_ns if row.margin_ns is not None else 1 << 62, row.service))
    return rows


# ---------------------------------------------------------------------------
# Shared risk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceExposure:
    """The ducts one service's light passes through, over its whole circuit.

    `conduits` is the **union** across the segments, which is FR-019. It is a set
    and not a per-segment list because the question the exposure answers is what
    one backhoe can take, and a duct under the second half of a regenerated
    circuit takes the circuit exactly as a duct under the first half does.

    `segment_count` travels with it so a report can say the set was collected
    over more than one wavelength. Without it a reader looking at a two-segment
    circuit's conduits has no way to tell them from one wavelength's.
    """

    service: str
    customer: str
    profile: str
    conduits: tuple[str, ...]
    span_count: int
    unducted_span_count: int
    segment_count: int = 1

    @property
    def is_ai(self) -> bool:
        return is_ai_profile(self.profile)

    @property
    def is_regenerated(self) -> bool:
        return self.segment_count > 1


def service_exposure(service: Mapping[str, Any]) -> ServiceExposure | None:
    """One service's ducts, or `None` when it has no route to read them off.

    Read off the **spans**, through `OtnPathHop.element`, and not off the
    sections. A section can cross several conduits and a conduit can hold spans
    from several sections, so intersecting section lists gives the wrong answer
    in both directions.

    **Over every segment, not the first one.** A regenerated circuit is one path
    per wavelength, and reading only the lowest `segment_sequence` answered with
    the ducts of the first half and none of the ducts of the second. That answer
    was narrow rather than wrong, which is why it is the dangerous kind: a
    two-segment circuit came back looking more diverse than it is, and the pair
    that a duct under its second half exposes was simply absent from the report.
    A span crossed twice, which a chain cannot produce because a cover may not
    repeat a section, would still be one conduit here and two spans, since the
    set deduplicates and the count does not.

    **`None` is not an empty conduit set**, and the two callers need to tell them
    apart. A service nobody has provisioned has no route, so nothing can be said
    about which ducts it shares; a service whose every span is outside a recorded
    conduit has a route and shares no recorded duct with anybody. The report drops
    the first and lists the second, and `checks/diversity.py` reports the first as
    undetermined rather than as diverse.

    **This takes one service record and not a payload** because the check reads
    its members from `OtnDiversityGroup.services`, where the services arrive
    nested under the group and never as top-level nodes. Forking the walk to suit
    the second payload shape is what `contracts/diversity-check.md` forbids and
    what FR-021 exists to prevent: the drift would be a check that passes what
    the report flags.
    """
    segments = circuit_segments(service)
    if not segments:
        return None
    conduits: set[str] = set()
    spans = 0
    unducted = 0
    for segment in segments:
        for hop in path_hops(segment.path):
            element = hop_element(hop)
            if element.get("__typename") != SPAN_KIND:
                continue
            spans += 1
            conduit = (element.get("conduit") or {}).get("node")
            if isinstance(conduit, dict):
                conduits.add(str(unwrap(conduit)["name"]))
            else:
                unducted += 1
    return ServiceExposure(
        service=str(service["name"]),
        customer=str(service.get("customer") or ""),
        profile=str(service.get("service_profile") or ""),
        conduits=tuple(sorted(conduits)),
        span_count=spans,
        unducted_span_count=unducted,
        segment_count=len(segments),
    )


def service_exposures(payload: Mapping[str, Any]) -> list[ServiceExposure]:
    """Every provisioned service in a payload and the conduits its spans occupy.

    The walk itself is `service_exposure`, once, and this is the loop over the
    top-level nodes of a service-rooted payload. Services with no route are
    dropped here rather than listed as sharing nothing, because a report row
    saying a circuit crosses no duct would read as a diversity finding about a
    circuit that does not exist yet.
    """
    exposures = [
        exposure for service in nodes_of(payload, "OtnService") if (exposure := service_exposure(service)) is not None
    ]
    exposures.sort(key=lambda exposure: exposure.service)
    return exposures


def conduit_groups(exposures: Sequence[ServiceExposure]) -> dict[str, tuple[str, ...]]:
    """Conduit name to the services crossing it, in one pass over the exposures.

    This is the shape an operator wants first, who else is in this duct, and it
    is also what the pairs are derived from, so the quadratic scan over every
    pair of services never happens.
    """
    groups: dict[str, set[str]] = defaultdict(set)
    for exposure in exposures:
        for conduit in exposure.conduits:
            groups[conduit].add(exposure.service)
    return {conduit: tuple(sorted(services)) for conduit, services in sorted(groups.items())}


@dataclass(frozen=True)
class DiversityFinding:
    """Two services that a route map calls diverse and a duct does not."""

    service_a: str
    service_b: str
    shared: tuple[str, ...]
    both_latency_sensitive: bool

    @property
    def severity(self) -> str:
        """`high` when both ends are AI or HPC.

        An AI training cluster split across two sites cannot tolerate a single
        fiber cut, so a pair of latency-sensitive services in one duct is a
        different finding from two best-effort services in one duct.
        """
        return "high" if self.both_latency_sensitive else "note"


def non_diverse_pairs(exposures: Sequence[ServiceExposure]) -> list[DiversityFinding]:
    """Every pair of services sharing at least one conduit.

    Derived from the conduit groups rather than from a scan over every pair, so
    the work is proportional to the services that actually share a duct.
    """
    by_name = {exposure.service: exposure for exposure in exposures}
    shared: dict[tuple[str, str], set[str]] = defaultdict(set)
    for conduit, services in conduit_groups(exposures).items():
        for index, first in enumerate(services):
            for second in services[index + 1 :]:
                shared[(first, second)].add(conduit)

    findings = [
        DiversityFinding(
            service_a=first,
            service_b=second,
            shared=tuple(sorted(conduits)),
            both_latency_sensitive=by_name[first].is_ai and by_name[second].is_ai,
        )
        for (first, second), conduits in shared.items()
    ]
    findings.sort(key=lambda finding: (-len(finding.shared), finding.service_a, finding.service_b))
    return findings
