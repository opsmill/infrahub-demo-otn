"""Turn one `OtnService` into a route, a wavelength and the objects behind them.

This is the thinnest layer in the feature, and deliberately so. It resolves two
endpoints to two ROADMs, asks the server for candidate routes, hands everything
to `routing.choose_route`, and writes down what came back. Every decision lives
in `routing.py`, every number lives in `budget.py`, and every unwrap lives in
`plant.py`, all three of which are tested with no server running.

**Discovery is native.** `client.traverse_paths()` finds the candidate routes
rather than a hand-written graph walker, because path discovery is what the
server is for. The `relationship_filter` names the two edges that mean "this
section terminates on that ROADM"; without it the traversal returns paths that
hop between sections through a shared fiber type.

**A run handles one service.** The query binds `$service` to one name, so
occupancy is read fresh for that service and two services cannot be allocated
against the same stale channel set. Concurrency between *branches* is the
collision check's job, and that is the right place for it.

**Idempotency is in the names.** Every object this writes is named from the
service name alone, never from the channel or the route. Determinism means a
re-run picks the same channel, so encoding it would work today and would turn
any later change of mind into an orphan plus a create rather than an update.

**Status is `active`, not `provisioning`.** The branch is the pending state: an
unmerged branch is not live, and Infrahub already shows that in the branch list,
the diff and the proposed change. A second attribute claiming to be the pending
state can only disagree with it.

**Groom first, light only when nothing on the route has room.** A provisioned
service always writes one client container. When a line container already on the
chosen route has room for it, that is the only container the run writes: the
client nests under the existing one and rides a wavelength somebody else lit.
That is the common path, and it is how two services come to share a wavelength.
When nothing on the route has room, the run lights a wavelength of its own, which
means the carrier and the line container that holds it, and nests the client under
that.

**A free channel is asked for on the lighting path only.** `choose_route` is
called with `require_free_channel=False`, so a route whose sections are full of
spectrum comes back as a candidate carrying `channel=None` rather than a
`capacity` refusal. That is the correct reading now that grooming exists:
nesting a client under a line container consumes no channel, so a section at 96
of 96 can still carry the service. `oms-fra-mil` under
`demo/90_fra_mil_saturated.yml` is exactly that section, and while the gate lived
in `routing.py` every service across it was refused for capacity before grooming
was tried.

**`channel=None` means two things and every message here says which.** A
wavelength occupies a width, so a route can hold free spectrum and still have no
contiguous block wide enough for the mode that closes on it. On a nearly full
corridor that is the common case, and it is a different answer from a corridor
with nothing free at all: one is somebody else's wavelength to turn down, the
other is a narrower transponder or another route. `no_anchor` is the one place
that sentence is written, and the candidate log line, the refusal and the raise
in `_carrier` all read it.

The cost of moving the requirement here is that this file now has to walk the
candidate list rather than take the winner. A one-section route with no spectrum
outranks a three-section detour with plenty, and if grooming fails on the short
route the service is still provisionable on the long one. `_plan` therefore takes
the first candidate in rank order that can either groom or light, and only the
candidates it had to pass over are lost. Reading `result.selection` alone would
turn the fix into a new refusal.

**A direct wavelength first, a chain only when no route carries one.** FR-009 is
a ranking rule and this is where it is applied: `_plan` walks the candidate
routes for a wavelength the service can groom into or light, and only when that
fails does `_chain` look for two wavelengths joined at an O-E-O device. A chain
costs a regeneration and latency, so it is the second answer and never the
first. `src/infrahub_demo_otn/chains.py` holds no preference at all, which is
deliberate: it returns covers and this file decides.

**The traversal is unchanged, and that is the point.** R-008 measured that no
`relationship_filter` picks a carrier chain out of the graph: filtered on the
device-to-carrier edge alone the call returns zero paths, and widened until a
carrier is reachable it returns 48 pairs joined in the middle of a shared
section for every one usable chain, with the control probe showing the device
edge changes nothing. So `_discover` still asks for exactly the two ROADM edges
and the same depth cap, the section route is what comes back, and covering it
with carriers is a separate step in a named module. Adding
`otn_carrier__sections` to that filter is what produced the 48, and it stays out.

**A chain grooms, it never lights.** Every segment of a chain rides a wavelength
that already exists, because `OtnOduSwitch.carriers` is what makes a junction
and a wavelength this run invented is terminated by no device. So the chain path
writes paths, hops and one client container per segment, and no carrier and no
line container at all. R-011 and R-012 are satisfied by construction rather than
by a flag: the run creates nothing shared, so there is nothing for a sibling
service's `delete_unused` to take away.

**A line container this run created is saved with `update_group_context=False`,
and a line container this run did not create is never saved at all.** Both halves
are load bearing and each one closes a different failure.

R-011 measured the first. `infrahub_sdk/node/node.py:1281-1300` adds a node to the
current run's tracking group on every `save()`, and an upsert of a node that
already existed counts the same as a create. Tracking is keyed on the generator
definition name plus a hash of the run parameters, `query_groups.py:61-70`, so it
is per service. A line container upserted by two services therefore joins both
their groups, and the next run of either one that stops writing it, which a
refusal is, deletes it while the other still holds a child underneath. That run
was executed on branch `spike-sp001` and the state afterwards was a deleted line
container and a client container left with an empty `parent_container`, still
reporting a carrier, with nothing in the surviving service's history saying so.
`OtnContainer` carries `on_delete: no-action` on every relationship, so nothing
cascaded and the sibling's container was orphaned rather than removed, which is
the quieter failure of the two.

R-012 found what makes the second half safe. `node.py:1288` reads
`if update_group_context is None and self._client.mode == TRACKING`, so the flag
only defaults to true when the caller leaves it unset, and
`query_groups.py:131` reads `if update_group_context is not False and (...)`, so
an explicit `False` skips `related_node_ids.extend(ids)` altogether. A node saved
that way never becomes a member of the run's group, and `delete_unused` computes
its victims as `existing_group.members.peer_ids - members`, so an untracked node
is in neither set and cannot be reclaimed. That is what lets a generator create a
durable line container without re-opening R-011.

The cost is stated rather than hidden: nothing reclaims a line container when its
last child goes away. A lit wavelength stays lit until somebody turns it down,
which is also what happens in the plant, so the model is not lying about the
network. It does mean untracked line containers accumulate on a branch that
provisions and then de-provisions.

**The same ownership rule reaches the carrier.** A run writes `oc-<service>`, the
carrier it lit for itself, and never the carrier a pre-provisioned wavelength or
another service's wavelength rides. An upsert of somebody else's carrier joins it
to this run's tracking group and R-011's arithmetic then applies to a wavelength
instead of to a container, which is the worse of the two because
`checks/channel_collision.py` reads every carrier as a channel claim. The carrier
is written on a grooming re-run as well, whenever the line container chosen turns
out to be one this service lit earlier: a run that reads its own carrier without
writing it drops the carrier out of the group, and `delete_unused` then takes the
wavelength out from under the container still riding it.

The parent edge is written from the child side only, which is what the schema was
shaped for. `OtnContainer.parent_container` is `outbound` on the
`otn_container__children` identifier and `child_containers` is `inbound` on the
same one, so one write on the client container produces the edge and the line
container reports the child with no second write. That is also what keeps the
grooming path down to a single write: nesting a client under somebody else's line
container touches only the client.
"""

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from infrahub_sdk.generator import InfrahubGenerator

from infrahub_demo_otn.budget import (
    Hop,
    RegeneratorInput,
    RouteBudget,
    SectionInput,
    SegmentInput,
    evaluate_route,
)
from infrahub_demo_otn.chains import (
    CarrierSpan,
    Chain,
    ChainSegment,
    JunctionDevice,
    RouteSection,
    find_chains,
)
from infrahub_demo_otn.containers import (
    LINE_CONTAINER_BY_LINE_RATE_GBPS,
    free_slots,
    slot_capacity,
    slots_for_client,
    slots_occupied,
)
from infrahub_demo_otn.plant import (
    carriers_from_graphql,
    modes_from_graphql,
    nodes_of,
    occupancy_from_graphql,
    peer,
    peers,
    routes_from_traversal,
    sections_from_graphql,
    unwrap,
)
from infrahub_demo_otn.routing import (
    REASON_NO_ROUTE,
    REASON_NO_SLOTS,
    RouteCandidate,
    Selection,
    choose_route,
    no_anchor_detail,
    route_sections,
)
from infrahub_demo_otn.units import KBPS_PER_GBPS, m_to_km, mdb_to_db, ns_to_us

MAX_ROUTE_SECTIONS = 4
"""How many ROADM-to-ROADM sections a candidate route may cross.

Traversal depth is twice this, because a section costs two edges. Four is the
cap rather than a number picked to produce a nice answer: on this fourteen-node
topology every route beyond four sections loses on the first ranking term before
any budget runs, and the cap keeps the search inside the server's own budget so
`truncated_at_depth` stays null. `test_routing_claims.py` asserts that widening
the cap from three to four does not change the winner, which is the check that
the cap is not making the decision.
"""

ROADM_A_EDGE = "otn_oms__roadm_a"
ROADM_B_EDGE = "otn_oms__roadm_b"
"""The two relationship identifiers a wavelength actually travels along."""

MAX_PATHS = 100
"""Requested path ceiling. Hitting it means the candidate set was cut, which is
the same failure as truncation and is treated the same way."""


def no_anchor(selection: Selection) -> str:
    """Why the route offers this selection's mode no anchor, in one sentence.

    A thin read of `routing.no_anchor_detail` off the fields the selector already
    carried back, so the generator states the two conditions FR-024a separates in
    the selector's own words. Every operator-facing message about a `None` channel
    goes through here: the candidate log line, the refusal, and the raise in
    `_carrier`. A second phrasing in any one of them is how an operator comes to
    be told a corridor is full when it has room for a narrower wavelength.

    `channel_reason` is never `None` when `channel` is, which `Selection` enforces
    on construction, so the cast below cannot hide a missing reason behind a
    plausible sentence.
    """
    return no_anchor_detail(selection.route, selection.mode, str(selection.channel_reason), selection.widest_free_mhz)


@dataclass(frozen=True)
class LineOption:
    """One line container the incoming client could be groomed into.

    `free` is `None` when the figure is not known, which happens when the line
    container's own type has no defined slot size or when one of its children has
    none. Unknown is never read as roomy: an option with no figure is excluded
    from the candidate set, because a container that might already be overfull
    cannot be shown to have room.

    `carrier_id` is what the service's optical path points at, which is the whole
    reason the carrier travels with the option rather than being looked up later.
    An earlier shape of this generator created `oc-<service>` and pointed the path
    at it while the client nested under a pre-provisioned carrier's line
    container, so the service claimed one wavelength and its ODU rode a second.
    Carrying the two together makes that shape unwritable.

    `own` marks the line container this service lit on an earlier run. It is a
    tie break rather than a filter: on equal free slots the service returns to its
    own wavelength, so a re-run is an upsert of the same tree instead of a move.
    """

    name: str
    odu_type: str
    capacity: int | None
    free: int | None
    carrier_name: str
    carrier_id: str
    own: bool


def packing_key(option: LineOption) -> tuple[int, bool, str]:
    """Sort key for a candidate whose free-slot figure is known.

    Fewest free slots first, then this service's own line container ahead of
    anybody else's, then name. `not option.own` is `False` for the service's own
    container and `False` sorts before `True`.

    The middle term is what makes a re-run stable in the one case where the first
    term cannot separate the candidates: a service that lit its own wavelength
    last run comes back to a route where its container and some other empty one
    both show the same free count, and without the tie break the name ordering
    could move the client to the other one. That move deletes the carrier this
    service owns, because a run that does not write its carrier drops it out of
    the tracking group, and leaves an untracked line container pointing at
    nothing.

    The raise is the invariant written down: an option with no figure is never a
    candidate, so reaching here with one is a caller that stopped filtering rather
    than a container to be packed into.
    """
    if option.free is None:
        raise ValueError(f"{option.name} has no free-slot figure, so it cannot be ranked as a packing candidate")
    return option.free, not option.own, option.name


@dataclass(frozen=True)
class WavelengthPlan:
    """One candidate route, and what could be done with it.

    Three things travel together because the decision is not separable. `line` is
    the line container the client would groom into, `None` when nothing on the
    route has room. `line_type` is the type a new line container would be, `None`
    when the mode's line rate has none. And `selection.channel` is `None` when the
    route has no spectrum left, which forbids lighting without forbidding grooming.

    `usable` is the whole test, written once. A plan is usable when grooming works,
    or when lighting works, and lighting needs both a container type and a channel.
    Splitting that across the call sites is how the channel requirement came to be
    applied to the grooming path in the first place.
    """

    selection: Selection
    line: LineOption | None
    line_type: str | None
    options: list[LineOption]

    @property
    def usable(self) -> bool:
        """Whether this route can carry the client, by grooming or by lighting."""
        if self.line is not None:
            return True
        return self.line_type is not None and self.selection.channel is not None


@dataclass(frozen=True)
class ChainSegmentPlan:
    """One segment of a chain, and the three objects it is written from.

    `segment` is what `chains.py` decided: the carrier, the run of the route it
    covers and the junction at its far end. `carrier_id` and `line` are what the
    payload adds, the wavelength's own id and the line container on it that has
    room for this client. They travel together for the reason `LineOption`
    already gives: an earlier shape of this file pointed a path at one wavelength
    while nesting the client under another's container, and carrying the pair
    makes that unwritable.
    """

    sequence: int
    """1-based, and the same number on the path and on the container.

    `OtnOpticalPath.segment_sequence` and `OtnContainer.segment_sequence` are two
    readings of one sequence from two places, which the schema comment on the
    container says a test asserts agree. One field here is what makes them agree
    at the point of writing rather than by coincidence.
    """

    segment: ChainSegment
    carrier_id: str
    line: LineOption
    path_name: str
    container_name: str


@dataclass(frozen=True)
class ChainPlan:
    """A whole chain, planned: the cover, the containers and the route budget.

    `budget` is a `RouteBudget` and deliberately not a `PathBudget`. It exposes
    no route margin, and this file must not derive one: FR-014 is that a figure
    quoted from a regenerated route says which segment it belongs to. Every
    margin logged or written here comes from one segment.
    """

    route: RouteCandidate
    chain: Chain
    segments: tuple[ChainSegmentPlan, ...]
    budget: RouteBudget


@dataclass(frozen=True)
class ChainAttempt:
    """Either a chain to provision, or the sentence saying why there is none.

    Both, in one object, because FR-010 needs the refusal to name which of the
    two answers was missing rather than implying the route is unreachable. A
    `None` plan with an empty detail would be a refusal that says nothing, so
    `detail` is always populated when `plan` is `None`.
    """

    plan: ChainPlan | None
    detail: str


class OpticalServiceGenerator(InfrahubGenerator):
    """Provision one service, or refuse it and say why."""

    async def generate(self, data: dict[str, Any]) -> None:
        services = list(nodes_of(data, "OtnService"))
        if len(services) != 1:
            raise ValueError(f"the query bound {len(services)} services; a generator run provisions exactly one")
        service = services[0]
        name = str(service["name"])

        source = self._anchor(service, "endpoint_a", name)
        destination = self._anchor(service, "endpoint_z", name)

        if source["name"] == destination["name"]:
            await self._refuse(service, REASON_NO_ROUTE, f"both endpoints of {name} land on {source['name']}")
            return

        sections = sections_from_graphql(data)
        routes = await self._discover(source, destination)
        result = choose_route(
            routes=routes,
            sections=sections,
            modes=modes_from_graphql(data),
            # A re-run reads a branch that already holds the carrier the previous
            # run wrote. Counting it would make this service's own spectrum look
            # taken and move the allocation on by one every run.
            #
            # Passed as spectrum, not projected back to anchor numbers. The
            # projection is what let the allocator hand out an anchor a wide mode
            # cannot use, so the carrier this generator wrote was then refused by
            # `checks/channel_collision.py` on the branch that wrote it. The
            # selector now widens every candidate anchor by the mode it picked and
            # compares intervals, which is FR-025.
            occupancy=occupancy_from_graphql(data, exclude={self._carrier_name(name)}),
            rate_gbps=int(service["rate_gbps"]),
            max_latency_ns=service.get("max_latency_ns"),
            # A free channel is what lighting a wavelength needs, not what using
            # one needs. See the module docstring: a route with no spectrum comes
            # back as a candidate with `channel=None` and `_plan` decides whether
            # this service can live with that.
            require_free_channel=False,
        )

        # Both lists, because "rejected" and "not chosen" are different answers
        # and a reviewer wants to see the alternatives either way. On Berlin to
        # Amsterdam nothing is rejected at all: four routes are viable and three
        # lose the ranking.
        for rejection in result.rejections:
            self.logger.info(f"{name}: discarded {rejection.route_key} [{rejection.reason}] {rejection.detail}")
        for position, candidate in enumerate(result.candidates, start=1):
            self.logger.info(
                f"{name}: candidate {position} of {len(result.candidates)} is {candidate.route.key} on "
                f"{candidate.mode.name}, {candidate.route.hop_count} sections, "
                f"{m_to_km(candidate.budget.total_length_m):.0f} km, "
                f"margin {mdb_to_db(candidate.budget.osnr_margin_mdb):+.3f} dB, "
                f"{ns_to_us(candidate.budget.latency_ns):.3f} us, "
                f"{self._spectrum(candidate)}"
            )

        # Everything that can still refuse this service resolves before the first
        # write. The SDK skips the tracking-group update when `generate` raises,
        # so a carrier written ahead of a ValueError would never join the group
        # and no later `delete_unused_nodes` would reclaim it. On this model that
        # is not cosmetic: `checks/channel_collision.py` reads every carrier as a
        # channel claim, so an orphan would hold a channel on every section it
        # crosses with nothing owning it, and the next run's collision check
        # would fail against a carrier nobody can find.
        signal = self._client_signal(data, service, name)

        # `slots_for_client` rather than `slots_occupied`, so the four types
        # G.709 does not size are groomed and counted instead of refused. An E1
        # maps into a VC-12 and an InfiniBand HDR client into an ODUflex, and both
        # get a figure from the client's own bit rate. The table keeps holding no
        # number for the type itself, which is a different statement.
        odu_type = str(signal["default_container_type"])
        occupies = slots_for_client(odu_type, int(signal["bit_rate_kbps"]))

        # A direct wavelength first, and a chain only when no candidate route
        # carries one. FR-009: a chain costs a regeneration and latency, so it is
        # the second answer. `_plan` is a pure read over the payload, so asking
        # it first costs no round trip.
        plan = self._plan(data, result.candidates, occupies, name) if result.candidates else None
        if plan is not None and plan.usable:
            await self._provision(data, service, plan, signal, odu_type, occupies)
            return

        # The route may have failed end to end and still be coverable by two
        # wavelengths that each close on their own, which is the whole reason
        # this feature exists. So the chain is looked for over the discovered
        # routes rather than over the candidates `choose_route` accepted: a route
        # `choose_route` rejected for budget or latency is exactly the one a
        # regeneration rescues.
        attempt = self._chain(data, routes, sections, service, name, occupies, odu_type)
        if attempt.plan is not None:
            await self._provision_chain(data, service, attempt.plan, signal, odu_type, occupies)
            return

        if plan is None or result.selection is None:
            # No route was provisionable at all, so the route-level reason is the
            # one to report: `no-route`, `no-mode`, `budget` or `latency`.
            reason = str(result.reason)
            direct = str(result.detail)
        else:
            reason = REASON_NO_SLOTS
            direct = self._no_room(plan, odu_type, occupies)
        await self._refuse(service, reason, self._neither(direct, attempt.detail))

    # ------------------------------------------------------------------
    # Naming
    # ------------------------------------------------------------------

    @staticmethod
    def _spectrum(selection: Selection) -> str:
        """What the log line says about a candidate's channel.

        A route with no anchor is still a candidate now, so "channel None" is a
        reachable log line and a misleading one. It reads as a bug in the
        allocator when it is a fact about the plant.

        **It used to read "no channel free, groom only" and that sentence was
        true of only one of the two cases it covered.** A route can hold a
        quarter of the band free and take no 128 GBd carrier anywhere in it, and
        an operator told the corridor has no channel free would go looking for
        somebody's wavelength to turn down when the answer is a narrower
        transponder. So the line names which condition holds and, when a block is
        merely too narrow, how wide the widest one is.
        """
        if selection.channel is None:
            return f"{no_anchor(selection)}, groom only"
        return f"channel {selection.channel}"

    @staticmethod
    def _carrier_name(service_name: str) -> str:
        return f"oc-{service_name}"

    @staticmethod
    def _path_name(service_name: str, sequence: int = 1) -> str:
        """`path-<service>` for segment 1, `path-<service>-s<k>` after it.

        Segment 1 keeps the name every path written before this feature carries,
        so a circuit that spans one wavelength is byte-identical to what feature
        016 wrote and every re-run of one is an upsert rather than an orphan plus
        a create. A chain suffixes its later segments, which it must: `name` is
        unique on `OtnOpticalPath` and a circuit now holds one path per segment.

        The suffix is the sequence and nothing else. `segment_sequence` is the
        field that orders the segments, and encoding the junction site or the
        carrier here would put a second, competing answer in the name, which is
        the mistake feature 016 shipped with `odu-<service>`.
        """
        return f"path-{service_name}" if sequence == 1 else f"path-{service_name}-s{sequence}"

    @staticmethod
    def _container_name(service_name: str, sequence: int = 1) -> str:
        """`odu-<service>` for segment 1, `odu-<service>-s<k>` after it.

        The same rule as `_path_name` and for the same reasons. A client container
        per segment is what FR-005 asks for, and each one has to be nameable
        without colliding with its siblings.
        """
        return f"odu-{service_name}" if sequence == 1 else f"odu-{service_name}-s{sequence}"

    @staticmethod
    def _line_container_name(carrier_name: str) -> str:
        """`odu-line-<carrier>`, the same rule `scripts/generate_geant_dataset.py` uses.

        One naming rule across the two writers, so nothing reading the model can
        tell a wavelength this generator lit from a pre-provisioned one by the
        name of its line container. That is the intent: the ODU map, the capacity
        check and the grooming decision treat the two identically, and a second
        naming convention would invite one of them to start special casing.

        Derived from the carrier rather than from the service, because the
        container belongs to the wavelength. A service that later re-routes onto a
        different wavelength leaves this one behind under its own carrier's name,
        which is what a lit wavelength nobody turned down should look like.
        """
        return f"odu-line-{carrier_name}"

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _anchor(service: dict[str, Any], endpoint: str, service_name: str) -> dict[str, Any]:
        """The ROADM a service endpoint hands its light to.

        A service names a device; a wavelength starts at a ROADM. The bridge is
        the site, and an endpoint at a site with no ROADM has no optical layer to
        be provisioned onto, which is a refusal rather than a crash.

        Reached through the endpoint's own site rather than through a
        network-wide ROADM fetch. `OtnSite.devices` peers the device generic, so
        the site hands back its routers and transponders too and `__typename`
        is what separates them. Two sites are read per dispatch instead of every
        ROADM in the network, and the two the run needs are the two it reads.
        """
        device = peer(service, endpoint)
        site_peer = (device.get("site") or {}).get("node")
        if not isinstance(site_peer, dict):
            raise ValueError(f"{service_name}: {endpoint} {device['name']} has no site, so it cannot be anchored")
        site = unwrap(site_peer)
        roadms = [record for record in peers(site, "devices") if record.get("__typename") == "OtnRoadm"]
        if not roadms:
            raise ValueError(f"{service_name}: {endpoint} {device['name']} is at {site['name']}, which has no ROADM")
        chosen = min(roadms, key=lambda record: str(record["name"]))
        return {"id": str(chosen["id"]), "name": str(chosen["name"])}

    async def _discover(self, source: dict[str, Any], destination: dict[str, Any]) -> list[Any]:
        """Candidate routes, from the server's own path traversal."""
        result = await self.client.traverse_paths(
            source["id"],
            destination["id"],
            max_depth=2 * MAX_ROUTE_SECTIONS,
            max_paths=MAX_PATHS,
            relationship_filter=[ROADM_A_EDGE, ROADM_B_EDGE],
            branch=self.branch_name,
        )
        if result.count >= MAX_PATHS:
            raise ValueError(
                f"path traversal returned {result.count} routes, which is the requested ceiling, so the "
                "candidate set was cut and ranking it would pick a winner from a partial set"
            )
        paths = [
            [
                {"kind": hop.node.kind, "name": hop.node.hfid[0] if hop.node.hfid else hop.node.display_label}
                for hop in path.hops
            ]
            for path in result.paths
        ]
        return routes_from_traversal(
            paths, source["name"], destination["name"], truncated_at_depth=result.truncated_at_depth
        )

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def _refuse(self, service: dict[str, Any], reason: str, detail: str) -> None:
        """Record the refusal on the service, and write nothing else.

        The generator's tracking group removes whatever a previous successful run
        created and this run did not, so a refusal that reverses an earlier
        success leaves no carrier holding a channel it no longer has a claim to.

        A line container this service lit earlier is the one thing that survives,
        and that is the intended behaviour rather than a leak. It is in no
        tracking group, so `delete_unused` cannot see it, which is exactly what
        stops this refusal from deleting a wavelength a sibling service is
        grooming into. R-011's measured failure was this refusal path taking a
        shared line container down with it. A lit wavelength stays lit until
        somebody turns it down, and nothing in this generator turns one down.

        The negative result that comes with it, stated rather than left to be
        discovered: the carrier under that line container *is* tracked, so a
        refusal that reverses this service's own earlier success leaves the line
        container holding no carrier at all. It is inert rather than misleading.
        Every reader of a line container reaches it through a carrier, so the ODU
        map does not draw it, the capacity check sums nothing into it and
        `_line_options` never offers it as a candidate. What it costs is a row in
        the container list that no longer means anything, on a branch that
        provisioned and then refused.

        Nothing is written to `OtnService.optical_path` here either. See
        `_activate` for why: the relationship is `cardinality: many` since feature
        017, an assignment to it never reached `save()`, and a path this run stops
        writing is reclaimed by the tracking group along with its edge.

        **The code and the detail are two fields and are written together.** Until
        feature 022 they were one 512-character Text holding `"{code}: {detail}"`,
        and the pair was truncated as a pair, which cut the prose of two of the
        three longest refusals mid-word. The code now lands in a `Dropdown` the
        server validates and the detail gets the 512 characters to itself. This
        generator is the only writer of both, so FR-006's invariant holds here or
        nowhere: never a code without a detail, and never a detail without a code.

        **`refusal_accepted` is deliberately not touched.** It is the one field on
        the service a generator never sets, and clearing it here is the mistake
        FR-007 exists to forbid: a rerun that re-refuses the same service would
        silently un-accept a decision a person signed for, and the proposed change
        would start failing again with no record of why. `_activate` clears it,
        and only there, because provisioning is the one moment the accepted
        refusal has ceased to exist.
        """
        self.logger.info(f"{service['name']}: refused, {reason}: {detail}")
        node = await self.client.get(kind="OtnService", id=str(service["id"]), branch=self.branch_name)
        node.status.value = "rejected"
        node.rejection_code.value = reason
        node.rejection_detail.value = detail[:512]
        await node.save(allow_upsert=True)

    async def _provision(
        self,
        data: dict[str, Any],
        service: dict[str, Any],
        plan: WavelengthPlan,
        signal: dict[str, Any],
        odu_type: str,
        occupies: int,
    ) -> None:
        """Write the one-wavelength circuit: a carrier or a groom, one path, one container.

        The plan arrives already made, because `generate` has to know whether a
        direct wavelength works before it decides whether to look for a chain.
        Groom first, light second, which is the behaviour rather than an
        optimisation: preferring an existing wavelength is what puts ten 10G
        services into one ODU4 instead of lighting ten of them.
        """
        name = str(service["name"])
        selection = plan.selection
        line = plan.line
        options = plan.options
        budget = selection.budget
        self.logger.info(
            f"{name}: chose {selection.route.key} on {selection.mode.name}, {self._spectrum(selection)}, "
            f"{m_to_km(budget.total_length_m):.0f} km, margin {mdb_to_db(budget.osnr_margin_mdb):+.3f} dB, "
            f"{ns_to_us(budget.latency_ns):.3f} us"
        )

        elements = self._element_ids(data)
        absent = [hop.name for hop in budget.hops if hop.name not in elements]
        if absent:
            raise ValueError(f"{name}: the path crosses {', '.join(absent)}, which the payload does not contain")

        if line is not None:
            self.logger.info(
                f"{name}: grooms into {line.name} on {line.carrier_name}, the tightest of {len(options)} line "
                f"containers on {selection.route.key}, offering {line.capacity} slots with {line.free} free, and "
                f"the {odu_type} takes {occupies}"
            )

        carrier_id, line_name = await self._wavelength(name, selection, line, plan.line_type)

        path = await self.client.create(
            kind="OtnOpticalPath",
            data={
                "name": self._path_name(name),
                "description": (
                    f"{selection.route.hop_count} sections, chosen over "
                    f"{len(selection.route.section_names)} candidate sections on lowest hop count then margin"
                ),
                "total_length_m": budget.total_length_m,
                "total_loss_mdb": budget.total_loss_mdb,
                "osnr_total_mdb": budget.osnr_total_mdb,
                "osnr_margin_mdb": budget.osnr_margin_mdb,
                "latency_ns": budget.latency_ns,
                "service": str(service["id"]),
                # The same carrier the client container's parent holds, always.
                # `_wavelength` returns the pair so the two cannot be sourced
                # separately and drift.
                "carrier": carrier_id,
            },
        )
        await path.save(allow_upsert=True)

        for hop in budget.hops:
            await self._write_hop(self._path_name(name), path.id, hop, elements)

        # The client container, and on the grooming path the only container this
        # run writes. `parent_container` carries the edge, so the line container
        # gains a child without being saved: see the module docstring and R-011
        # for what a `save()` on somebody else's line container would cost. No
        # `carrier` either, because the line container above holds the wavelength
        # and a client container claiming one as well would report a second,
        # competing answer to what this client rides.
        container = await self.client.create(
            kind="OtnContainer",
            data={
                "name": self._container_name(name),
                "odu_type": odu_type,
                "mapping_mode": signal["default_mapping"],
                "description": f"{signal['name']} for {name}, groomed into {line_name}",
                "client_signal": {"hfid": [signal["name"]]},
                "parent_container": {"hfid": [line_name]},
                # Set here and nowhere else. It is the client container that
                # belongs to a circuit; the line container above belongs to a
                # wavelength, and `_light` deliberately leaves its `service`
                # empty. Setting it there too would say one of the several
                # services groomed into that wavelength
                # owns the whole of it, which is the ownership confusion R-011 measured.
                # Without this field the only link was the `odu-<service>` name
                # and nothing read it, so `transforms/service_trace.py` listed
                # every client on a shared wavelength as part of this circuit.
                "service": str(service["id"]),
                "tributary_slots": occupies,
            },
        )
        await container.save(allow_upsert=True)

        await self._activate(service)

    async def _activate(self, service: dict[str, Any]) -> None:
        """Mark the service provisioned. The paths carry the edge themselves.

        **Nothing is written to `OtnService.optical_path` here, and the omission
        is the fix rather than a gap.** `OtnOpticalPath.service` is mandatory and
        every path above sets it, so the edge exists from the child side, which is
        the side the schema shaped it for.

        Feature 017 widened this relationship to `cardinality: many`, and the
        assignment that used to stand here, `node.optical_path = path.id`, stopped
        doing anything at that moment. `InfrahubNode.__setattr__` intercepts
        cardinality-**one** relationships only, `node.py:962-984`, so a value
        assigned to a many relationship lands in the instance dictionary and
        `save()` never sees it. It failed silently and it would have gone on
        failing silently, because the path's own mandatory `service` was
        producing the edge the assignment appeared to produce.

        The refusal path drops the same assignment for the same reason, and loses
        nothing: a path this generator wrote is in the run's tracking group, so a
        run that stops writing it has `delete_unused` remove the path, and the
        edge goes with the object rather than being cleared from this side.

        **Three fields are cleared here and only two of them are this generator's
        own.** The code and the detail are cleared because the refusal they
        describe is gone (FR-006). `refusal_accepted` is cleared because the thing
        a person accepted has ceased to exist (FR-007a), and this is the one
        boundary at which a generator may touch it at all.

        That third clear is not tidiness, and leaving it out is the bug the first
        draft of feature 022 shipped. An operator accepts a `capacity` refusal so
        the record can merge; spectrum frees up; this rerun provisions the service.
        With the flag still set, the service is provisioned, unrefused and
        accepted, which the provisionable check reports as an error because an
        acceptance with no refusal under it means the flag was set on the wrong
        node. The merge would then be blocked because the network got better.
        Critique finding P1.

        The two rules read as opposites and are not: the generator must never
        clear acceptance while the refusal is still there, and must always clear it
        the moment the refusal is not. `_refuse` holds the other half.
        """
        node = await self.client.get(kind="OtnService", id=str(service["id"]), branch=self.branch_name)
        node.status.value = "active"
        node.rejection_code.value = None
        node.rejection_detail.value = None
        node.refusal_accepted.value = False
        await node.save(allow_upsert=True)

    async def _write_hop(self, path_name: str, path_id: str, hop: Hop, elements: dict[str, str]) -> None:
        """Write one hop. The caller has already proved every hop is covered.

        Named from the path rather than from the service, because a chain has one
        path per segment and `OtnPathHop.name` is unique. `hop.index` restarts at
        one per segment, which is correct: a hop's sequence is its position on its
        own path.
        """
        node = await self.client.create(
            kind="OtnPathHop",
            data={
                "name": f"{path_name}-hop-{hop.index:03d}",
                "sequence": hop.index,
                "cumulative_length_m": hop.cumulative_length_m,
                "cumulative_loss_mdb": hop.cumulative_loss_mdb,
                "cumulative_osnr_mdb": hop.cumulative_osnr_mdb,
                "cumulative_delay_ns": hop.cumulative_delay_ns,
                "path": path_id,
                "element": elements[hop.name],
            },
        )
        await node.save(allow_upsert=True)

    # ------------------------------------------------------------------
    # The wavelength: groomed into, or lit
    # ------------------------------------------------------------------

    async def _wavelength(
        self,
        service_name: str,
        selection: Selection,
        line: LineOption | None,
        line_type: str | None,
    ) -> tuple[str, str]:
        """The carrier this service rides and the line container it grooms into.

        One return for both paths, and the pair travels together. The optical path
        points at the carrier and the client container nests under the line
        container, and if those two came from separate lookups a service could end
        up claiming one wavelength while its ODU rode another. That is not
        hypothetical: it is what the previous pass at this file shipped, and it is
        the coherence defect FR-008a exists to close.

        `line is None` means nothing on the route had room, which is the only case
        that lights a wavelength. Lighting needs two things the caller has already
        checked through `WavelengthPlan.usable`: a line container type for the
        mode's line rate, and a free channel. The raise below is therefore a
        statement about the caller rather than a case to handle, and it names both
        halves because either one being absent is a caller that stopped checking.
        """
        if line is None:
            if line_type is None or selection.channel is None:
                blocked = "no line container type is defined for" if line_type is None else "no channel is free for"
                raise ValueError(
                    f"{service_name}: no line container has room and {blocked} "
                    f"{selection.mode.line_rate_gbps} Gbit/s on {selection.route.key}, which the caller should "
                    "have refused before writing"
                )
            return await self._light(service_name, selection, line_type)

        if line.own:
            # This service lit the wavelength on an earlier run. The carrier is
            # written again, not because anything about it changed, but because a
            # run that reads its own carrier without saving it leaves the carrier
            # out of the run's tracking group, and `delete_unused` then deletes
            # the wavelength out from under the line container still riding it.
            await self._carrier(service_name, selection)
        return line.carrier_id, line.name

    async def _carrier(self, service_name: str, selection: Selection) -> Any:
        """Write `oc-<service>`, the one carrier this run is allowed to write.

        Never called for a wavelength this service does not own. An upsert of a
        pre-provisioned carrier or of another service's carrier joins it to this
        run's tracking group, and the next run of this service that stops writing
        it, which a refusal is, deletes a wavelength somebody else is riding.
        R-011 measured that arithmetic on a container; on a carrier it is worse,
        because `checks/channel_collision.py` reads every carrier as a channel
        claim and the claim would vanish with it.

        **A channel is required here and the requirement cannot be met by chance.**
        On the lighting path `WavelengthPlan.usable` has already established that
        one is free. On the re-run path, where this service comes back to a
        wavelength it lit earlier, the channel is free by construction: `generate`
        reads occupancy with `exclude={oc-<service>}`, so this service's own claim
        is not counted against it, and the route the line container sits on is the
        route its carrier crosses. The raise is what stops that reasoning being
        load bearing without being checked, because a channel of `None` here would
        fetch the whole `OtnFrequencyGrid` collection and write whichever row came
        back first.
        """
        if selection.channel is None:
            raise ValueError(
                f"{service_name}: on {selection.route.key} {no_anchor(selection)}, so "
                f"{self._carrier_name(service_name)} cannot be written. A wavelength this service owns always "
                "leaves its own spectrum free, because occupancy excludes this service's carrier, so reaching here "
                "means the route and the carrier disagree about which sections the wavelength crosses"
            )
        channel = await self.client.get(
            kind="OtnFrequencyGrid", channel_number__value=selection.channel, branch=self.branch_name
        )
        carrier = await self.client.create(
            kind="OtnOpticalCarrier",
            data={
                "name": self._carrier_name(service_name),
                "description": f"{service_name} on channel {selection.channel} via {selection.route.key}",
                # Planned, not active: this allocates spectrum and places no
                # hardware. `carrier_termination` judges active carriers, so
                # `active` here would fail every provisioning branch.
                "status": "planned",
                "channel": channel.id,
                "optical_mode": {"hfid": [selection.mode.name]},
                "sections": [{"hfid": [section]} for section in selection.route.section_names],
            },
        )
        await carrier.save(allow_upsert=True)
        return carrier

    async def _light(self, service_name: str, selection: Selection, line_type: str) -> tuple[str, str]:
        """Light a wavelength: the carrier, and the line container that holds it.

        Reached only when no line container on the route has room, which is what
        makes grooming the preferred outcome and lighting the fallback rather than
        the other way round.

        **The line container is saved with `update_group_context=False`.** That
        flag is the entire reason a generator is allowed to create a durable line
        container. `node.py:1288` only defaults it to true when it is `None`, and
        `query_groups.py:131` skips the group membership when it is explicitly
        false, so the container joins no run's group and `delete_unused` cannot
        reach it. Drop the flag and R-011's measured failure returns: a sibling
        service that grooms into this wavelength later has its container orphaned
        the first time this service refuses.

        `tributary_slots` is zero because the line container has no parent to
        occupy slots in, which is also what `scripts/generate_geant_dataset.py`
        writes for a pre-provisioned one. `tributary_slot_capacity` is the table's
        figure for the type, so the two writers put the same number on the same
        kind of object.

        **No `service`.** A line container belongs to the wavelength, not to the
        service that happened to light it, and the next service to groom in has
        as much claim on it as this one. `_provision` sets `service` on the client
        container instead.
        """
        carrier = await self._carrier(service_name, selection)
        carrier_name = self._carrier_name(service_name)
        line_name = self._line_container_name(carrier_name)
        capacity = slot_capacity(line_type)
        self.logger.info(
            f"{service_name}: no line container on {selection.route.key} has room, so it lights {carrier_name} on "
            f"channel {selection.channel} and creates {line_name}, a {line_type} offering {capacity} slots"
        )
        line = await self.client.create(
            kind="OtnContainer",
            data={
                "name": line_name,
                "description": f"{line_type} on {carrier_name}. Lit for {service_name}.",
                "odu_type": line_type,
                "tributary_slots": 0,
                "tributary_slot_capacity": capacity,
                "carrier": carrier.id,
            },
        )
        await line.save(allow_upsert=True, update_group_context=False)
        return str(carrier.id), line_name

    # ------------------------------------------------------------------
    # The chain: two wavelengths joined at an O-E-O device
    # ------------------------------------------------------------------

    @staticmethod
    def _junctions(data: dict[str, Any]) -> list[JunctionDevice]:
        """Every O-E-O device in the payload, as a junction candidate.

        `site_nodes` is the optical elements at the device's site, and it is what
        lets `chains.joins` decide that two carriers meeting at a ROADM meet
        where this device is. A section names its ROADMs and a device names its
        site, so the two are joined through the site's own device list, which is
        one hop and the same shape `_anchor` already uses on the endpoints.

        A device with no site raises rather than being skipped. Nothing places its
        junction, so every chain through it would claim a hand-off at a site
        nobody can name, and a quiet skip would report that as "no chain covers
        this route".
        """
        devices: list[JunctionDevice] = []
        for record in nodes_of(data, "OtnOduSwitch"):
            site_peer = (record.get("site") or {}).get("node")
            if not isinstance(site_peer, dict):
                raise ValueError(
                    f"O-E-O device {record['name']} has no site, so nothing places the junction it would make"
                )
            site = unwrap(site_peer)
            devices.append(
                JunctionDevice(
                    name=str(record["name"]),
                    site=str(site["name"]),
                    site_nodes=frozenset(str(peer_record["name"]) for peer_record in peers(site, "devices")),
                    carrier_names=frozenset(str(peer_record["name"]) for peer_record in peers(record, "carriers")),
                )
            )
        return sorted(devices, key=lambda device: device.name)

    @staticmethod
    def _framing_latencies(data: dict[str, Any]) -> dict[str, int]:
        """Each O-E-O device's framing delay, by name.

        Separate from `_junctions` because it is a budget input and not a cover
        input. `chains.py` decides where the light is rebuilt and knows nothing
        about what that costs; `budget.RegeneratorInput` carries the cost. Keeping
        the two apart is what stops the cover growing an opinion on the budget.
        """
        return {
            str(record["name"]): int(record.get("framing_latency_ns") or 0) for record in nodes_of(data, "OtnOduSwitch")
        }

    @staticmethod
    def _route_steps(route: RouteCandidate, sections: Mapping[str, SectionInput]) -> tuple[RouteSection, ...]:
        """The route as ordered sections, each with the node at either end.

        `chains.py` needs the boundary between two consecutive sections, because
        that is the only place a junction can be. The traversal already returned
        the sections in order and `RouteCandidate.start_node` is where the walk
        began, so the boundaries are a walk over the section endpoints and no
        second query.

        The raise is not decoration. A section that touches neither end of the
        walk means the ordered list is not one route, and covering it would cut a
        circuit that does not exist into segments that all look plausible.
        """
        steps: list[RouteSection] = []
        current = route.start_node
        for section in route_sections(route, sections):
            head, tail = section.endpoints
            if current == head:
                following = tail
            elif current == tail:
                following = head
            else:
                raise ValueError(
                    f"route {route.key} reaches {current} and {section.name} runs {head} to {tail}, "
                    "so the traversal order and the plant payload disagree"
                )
            steps.append(RouteSection(name=section.name, node_a=current, node_z=following))
            current = following
        return tuple(steps)

    def _chain(
        self,
        data: dict[str, Any],
        routes: Sequence[RouteCandidate],
        sections: Mapping[str, SectionInput],
        service: dict[str, Any],
        service_name: str,
        occupies: int,
        odu_type: str,
    ) -> ChainAttempt:
        """The first chain, over the first route, that can carry this client.

        Routes are walked fewest sections first and `chains.find_chains` returns
        its covers in a total order, so the chain chosen is reproducible. Nothing
        here is a search over the graph: the cover is an enumeration over at most
        four ordered sections and every input is already in the payload.

        **Routes, not candidates.** `choose_route` may have rejected every one of
        them, for budget or for latency, and that is the case a regeneration
        exists to rescue: Paris to Madrid does not close on one wavelength and
        does close as two. So the cover runs over what the traversal returned.

        The returned `detail` is the sentence a refusal quotes. It names the
        best-ranked chain that failed, or the absence of any cover, or the absence
        of any device, because those send a reader to three different places.
        """
        devices = self._junctions(data)
        if not devices:
            return ChainAttempt(
                plan=None,
                detail=(
                    "no O-E-O device exists on this branch, so no wavelength can be handed to another and every "
                    "circuit is one wavelength end to end"
                ),
            )

        described = carriers_from_graphql(data)
        spans = [
            CarrierSpan(name=str(entry["name"]), section_names=frozenset(entry["section_names"])) for entry in described
        ]
        by_name = {str(entry["name"]): entry for entry in described}
        records = {str(record["name"]): record for record in nodes_of(data, "OtnOpticalCarrier")}
        latencies = self._framing_latencies(data)
        max_latency_ns = service.get("max_latency_ns")

        refused: list[str] = []
        for route in sorted(routes, key=lambda candidate: (candidate.hop_count, candidate.key)):
            steps = self._route_steps(route, sections)
            for chain in find_chains(steps, spans, devices):
                outcome = self._chain_plan(
                    route=route,
                    chain=chain,
                    described=by_name,
                    records=records,
                    latencies=latencies,
                    sections=sections,
                    service_name=service_name,
                    occupies=occupies,
                    odu_type=odu_type,
                    max_latency_ns=max_latency_ns,
                )
                if isinstance(outcome, ChainPlan):
                    return ChainAttempt(plan=outcome, detail="")
                self.logger.info(f"{service_name}: discarded chain {chain.key} [chain] {outcome}")
                refused.append(outcome)
        if not refused:
            return ChainAttempt(
                plan=None,
                detail=(
                    f"no pair of wavelengths covers a candidate route end to end and meets at one of the "
                    f"{len(devices)} O-E-O devices holding both"
                ),
            )
        return ChainAttempt(plan=None, detail=refused[0])

    def _chain_plan(
        self,
        route: RouteCandidate,
        chain: Chain,
        described: dict[str, dict[str, Any]],
        records: dict[str, dict[str, Any]],
        latencies: dict[str, int],
        sections: Mapping[str, SectionInput],
        service_name: str,
        occupies: int,
        odu_type: str,
        max_latency_ns: int | None,
    ) -> ChainPlan | str:
        """One cover, either planned or refused with a sentence saying why.

        Three things are checked here and none of them belongs in `chains.py`,
        which holds the cover and nothing else. Each segment's wavelength needs a
        **mode**, or there is no requirement to budget it against. Each segment
        needs a line container with **room** for the client, because a chain
        grooms and never lights. And the route needs to **close**, segment by
        segment, with the cascade restarting at every device.

        The budget comes from `budget.evaluate_route`, which is the FR-012
        implementation: one `evaluate_path` per segment on that segment's own
        mode, so noise the first segment accumulated does not cross the device.
        `RouteBudget` exposes no route margin and this method quotes none.
        """
        planned: list[ChainSegmentPlan] = []
        inputs: list[SegmentInput] = []
        for sequence, segment in enumerate(chain.segments, start=1):
            entry = described.get(segment.carrier_name)
            record = records.get(segment.carrier_name)
            if entry is None or record is None:
                raise ValueError(
                    f"{service_name}: chain {chain.key} names carrier {segment.carrier_name}, which the payload "
                    "does not contain, so the cover and the payload were read from different states"
                )
            mode = entry["mode"]
            if mode is None:
                return (
                    f"{segment.carrier_name} carries no optical mode, so segment {sequence} of "
                    f"{chain.segment_count} has no requirement to be budgeted against"
                )

            container_name = self._container_name(service_name, sequence)
            options = self._carrier_options(record, own_child=container_name)
            line = self._best_fit(options, occupies)
            if line is None:
                return self._no_segment_room(chain, sequence, segment, options, odu_type, occupies)

            planned.append(
                ChainSegmentPlan(
                    sequence=sequence,
                    segment=segment,
                    carrier_id=str(entry["id"]),
                    line=line,
                    path_name=self._path_name(service_name, sequence),
                    container_name=container_name,
                )
            )
            inputs.append(
                SegmentInput(
                    sections=tuple(sections[name] for name in segment.section_names),
                    mode=mode,
                    start_node=segment.start_node,
                    regenerator=(
                        None
                        if segment.junction_device is None
                        else RegeneratorInput(
                            name=segment.junction_device,
                            framing_latency_ns=latencies.get(segment.junction_device, 0),
                        )
                    ),
                )
            )

        budget = evaluate_route(inputs)
        if not budget.ok:
            failing = ", ".join(
                f"segment {sequence} {mdb_to_db(margin):+.3f} dB"
                for sequence, margin in budget.segment_margins_mdb
                if sequence in budget.failing_segments
            )
            return f"{chain.key} does not close: {failing}"
        if max_latency_ns is not None and budget.latency_ns > max_latency_ns:
            over = budget.latency_ns - int(max_latency_ns)
            return (
                f"{chain.key} closes and takes {ns_to_us(budget.latency_ns):.3f} us across "
                f"{budget.segment_count} segments against a budget of {ns_to_us(int(max_latency_ns)):.3f} us, "
                f"which it misses by {ns_to_us(over):.3f} us"
            )
        return ChainPlan(route=route, chain=chain, segments=tuple(planned), budget=budget)

    @staticmethod
    def _no_segment_room(
        chain: Chain,
        sequence: int,
        segment: ChainSegment,
        options: list[LineOption],
        odu_type: str,
        occupies: int,
    ) -> str:
        """Why one segment of a chain had nowhere to put the client.

        The same three cases `_no_room` separates, at the segment level, and named
        rather than collapsed for the same reason: no line container at all on the
        wavelength is a dataset question, an unknown free count is a container
        holding a child nobody sized, and a full one is a wavelength that is
        genuinely full. There is no lighting half to report here, because a chain
        grooms into wavelengths that already exist.
        """
        where = f"segment {sequence} of {chain.segment_count} on {segment.carrier_name}"
        if not options:
            return f"{where} carries no line container, so there is nothing for the {odu_type} to be groomed into"
        known = [option for option in options if option.free is not None]
        if not known:
            return (
                f"{where} has {len(options)} line containers and an unknown free-slot count on every one, so none "
                f"can be shown to have room for the {occupies} slots {odu_type} takes"
            )
        tightest = min(known, key=packing_key)
        return (
            f"{where} offers {tightest.capacity} slots on {tightest.name} with {tightest.free} free, and none of "
            f"its {len(options)} line containers has room for the {occupies} slots {odu_type} takes"
        )

    @staticmethod
    def _neither(direct: str, chain: str) -> str:
        """The refusal FR-010 asks for: which of the two was missing, and why each.

        The verdict comes first and the two explanations after it, because
        `OtnService.rejection_detail` is truncated at 512 characters and the
        sentence that must survive is the one saying a chain was looked for and
        not found. A refusal reading only "no route closes" implies the route is
        unreachable, which is a different and more discouraging claim than "no
        wavelength closes and no pair of wavelengths can be joined either".
        """
        return f"neither a direct wavelength nor a chain serves this route. Direct: {direct}. Chain: {chain}"

    async def _provision_chain(
        self,
        data: dict[str, Any],
        service: dict[str, Any],
        plan: ChainPlan,
        signal: dict[str, Any],
        odu_type: str,
        occupies: int,
    ) -> None:
        """Write the regenerated circuit: one path and one client container per segment.

        **Nothing shared is written.** Every segment rides a wavelength that
        already exists and a line container somebody else lit, so this writes no
        carrier and no line container at all. That is what makes feature 016's
        R-011 and R-012 satisfied by construction here: the run creates nothing a
        sibling service also holds, so no `delete_unused` can take a wavelength
        out from under a neighbour's container.

        `segment_sequence` is written on both the path and the container from the
        one number in `ChainSegmentPlan`, so the two readings of the sequence
        agree by construction. The uniqueness constraint on
        `(service, segment_sequence)` refuses a duplicate at write time; what it
        cannot see is a gap, which is why the sequence is generated by
        enumeration here rather than derived from anything that could skip.

        No route margin is logged. `RouteBudget.segment_margins_mdb` pairs every
        margin with its segment, and FR-014 is that a figure quoted from a
        regenerated route says which segment it belongs to.
        """
        name = str(service["name"])
        budget = plan.budget
        margins = ", ".join(
            f"segment {sequence} {mdb_to_db(margin):+.3f} dB" for sequence, margin in budget.segment_margins_mdb
        )
        junctions = ", ".join(f"{device} at {site}" for device, site in plan.chain.junctions)
        self.logger.info(
            f"{name}: no direct wavelength serves {plan.route.key}, so it takes the chain {plan.chain.key} in "
            f"{budget.segment_count} segments regenerated at {junctions}, "
            f"{m_to_km(budget.total_length_m):.0f} km, {margins}, {ns_to_us(budget.latency_ns):.3f} us including "
            f"the framing delay"
        )

        elements = self._element_ids(data)
        absent = [hop.name for segment in budget.segments for hop in segment.budget.hops if hop.name not in elements]
        if absent:
            raise ValueError(f"{name}: the chain crosses {', '.join(absent)}, which the payload does not contain")

        for step, measured in zip(plan.segments, budget.segments):
            segment = step.segment
            figures = measured.budget
            ends = (
                f"regenerated at {segment.junction_device}, {segment.junction_site}"
                if segment.junction_device is not None
                else "the last segment, terminated at the destination"
            )
            path = await self.client.create(
                kind="OtnOpticalPath",
                data={
                    "name": step.path_name,
                    "description": (
                        f"segment {step.sequence} of {plan.chain.segment_count} on {segment.carrier_name}, "
                        f"{len(segment.section_names)} sections, {ends}"
                    ),
                    "segment_sequence": step.sequence,
                    "total_length_m": figures.total_length_m,
                    "total_loss_mdb": figures.total_loss_mdb,
                    "osnr_total_mdb": figures.osnr_total_mdb,
                    "osnr_margin_mdb": figures.osnr_margin_mdb,
                    "latency_ns": figures.latency_ns,
                    "service": str(service["id"]),
                    "carrier": step.carrier_id,
                },
            )
            await path.save(allow_upsert=True)

            for hop in figures.hops:
                await self._write_hop(step.path_name, path.id, hop, elements)

            self.logger.info(
                f"{name}: segment {step.sequence} grooms into {step.line.name} on {step.line.carrier_name}, "
                f"offering {step.line.capacity} slots with {step.line.free} free, and the {odu_type} takes "
                f"{occupies}"
            )
            container = await self.client.create(
                kind="OtnContainer",
                data={
                    "name": step.container_name,
                    "odu_type": odu_type,
                    "mapping_mode": signal["default_mapping"],
                    "description": (
                        f"{signal['name']} for {name}, segment {step.sequence} of {plan.chain.segment_count}, "
                        f"groomed into {step.line.name}"
                    ),
                    "client_signal": {"hfid": [signal["name"]]},
                    "parent_container": {"hfid": [step.line.name]},
                    "segment_sequence": step.sequence,
                    "service": str(service["id"]),
                    "tributary_slots": occupies,
                },
            )
            await container.save(allow_upsert=True)

        await self._activate(service)

    # ------------------------------------------------------------------
    # Grooming
    # ------------------------------------------------------------------

    def _plan(
        self,
        data: dict[str, Any],
        candidates: tuple[Selection, ...],
        occupies: int,
        service_name: str,
    ) -> WavelengthPlan:
        """The first candidate route, in rank order, that can carry this client.

        **Why this walks the list instead of taking the winner.** `choose_route` is
        called with `require_free_channel=False`, so a route whose sections are out
        of spectrum is now a candidate rather than a `capacity` refusal, and it can
        outrank a longer route that has spectrum to spare: hop count comes first
        and a full one-section corridor beats a three-section detour. That is the
        right ranking, because grooming into a wavelength already lit on the short
        route needs no channel. It does mean the top-ranked route can be one where
        grooming fails and lighting is impossible, while a lower-ranked route would
        have provisioned the service outright. Reading `result.selection` alone
        would trade the old wrong refusal for a new one.

        `_line_options` and `_best_fit` are pure reads over the payload, so the
        walk costs no round trip and writes nothing. On this dataset it usually
        stops on the first candidate.

        When no candidate works the top-ranked plan comes back anyway, `usable`
        false, so the refusal message names the route the planner would have
        preferred rather than whichever one happened to be examined last. Rank
        order is total, so that choice is reproducible.
        """
        preferred: WavelengthPlan | None = None
        for selection in candidates:
            options = self._line_options(data, selection, service_name)
            plan = WavelengthPlan(
                selection=selection,
                line=self._best_fit(options, occupies),
                line_type=LINE_CONTAINER_BY_LINE_RATE_GBPS.get(selection.mode.line_rate_gbps),
                options=options,
            )
            if plan.usable:
                if preferred is not None:
                    self.logger.info(
                        f"{service_name}: {preferred.selection.route.key} outranks {selection.route.key} and has "
                        f"neither room to groom into nor spectrum to light, so the service takes "
                        f"{selection.route.key} instead"
                    )
                return plan
            if preferred is None:
                preferred = plan
        if preferred is None:
            raise ValueError(
                f"{service_name}: no candidate route reached the planner. `generate` tests `result.selection` "
                "before provisioning, so an empty candidate list here is a caller that stopped testing it"
            )
        return preferred

    def _line_options(self, data: dict[str, Any], selection: Selection, service_name: str) -> list[LineOption]:
        """Every line container on the chosen route, with its free-slot figure.

        **The candidate rule is set equality between the carrier's sections and
        the route's, and the choice is deliberate rather than inherited.** The
        previous pass at this file used the same test for a weaker reason and it
        had a bad consequence: with no lighting path, a route matching no
        pre-provisioned carrier was refused, and since all 71 pre-provisioned
        wavelengths sit on `oms-fra-mil` across five distinct section sets, that
        refused most of a fifteen-site network. The lighting path is back, so a
        route with no matching carrier now lights one and the test is free to be
        as strict as the physics.

        And the physics says strict. A superset was the alternative worth weighing:
        a carrier crossing the route's sections and one more still covers every
        section the service needs. It does not work, because an ODU is added and
        dropped where its wavelength terminates and nowhere in between. Nothing in
        this schema is an ODU cross-connect, so a client groomed into a carrier
        that runs one section past the destination is delivered one site past the
        destination. A subset fails the other way and reaches nothing like the far
        end. Set equality is what is left, and it stands for "the same two
        endpoints and the same sections between them".

        The honest limit of the test: it compares sets, so two different routes
        built from the same sections would compare equal. Both sets here are
        derived from ROADM-to-ROADM path traversals, and a set of sections that
        forms a path between two ROADMs on this topology fixes the endpoints, so
        the case does not arise. If a section is ever added that closes a ring
        tightly enough to make it arise, this is the line to change and comparing
        ordered endpoints is the fix.

        This service's own client container is left out of every child sum. A
        re-run reads a branch that already holds the container the previous run
        wrote, and counting it would make the wavelength look eight slots fuller
        than it is, move the best fit on by one every run, and eventually refuse a
        service that is already provisioned. The same reasoning as the
        `exclude=` on channel occupancy in `generate`.

        Nothing here is written. The carriers and their container trees arrive in
        the payload and this walks them, which is what keeps the read of somebody
        else's wavelength free of any save at all.
        """
        wanted = set(selection.route.section_names)
        own_child = self._container_name(service_name)
        own_carrier = self._carrier_name(service_name)
        options: list[LineOption] = []
        for carrier in nodes_of(data, "OtnOpticalCarrier"):
            if {str(record["name"]) for record in peers(carrier, "sections")} != wanted:
                continue
            options.extend(self._carrier_options(carrier, own_child=own_child, own_carrier=own_carrier))
        return sorted(options, key=lambda option: option.name)

    def _carrier_options(
        self,
        carrier: dict[str, Any],
        own_child: str,
        own_carrier: str = "",
    ) -> list[LineOption]:
        """One wavelength's line containers, with their free-slot figures.

        Split out of `_line_options` because a chain packs into a **named**
        wavelength, the one its cover chose, rather than into whichever ones cross
        the route. Both callers get the same arithmetic, which is the point: two
        readings of a free-slot figure is how one of them starts refusing a
        service the other calls fine.

        `own_child` is this service's own client container **on this segment**, and
        it is excluded from the sum for the reason `_line_options` documents: a
        re-run reads a branch that already holds the container the previous run
        wrote, and counting it would move the best fit on by one every run. A
        chain has one such container per segment, so the name is a parameter here
        rather than derived from the service.

        `own_carrier` defaults to empty, which no carrier is named, so the `own`
        tie-break is simply false for a chain. That is correct: a chain grooms into
        wavelengths it did not light, and it never lights one to come back to.
        """
        carrier_name = str(carrier["name"])
        options: list[LineOption] = []
        for line in peers(carrier, "containers"):
            occupancies = [
                self._child_occupancy(child)
                for child in peers(line, "child_containers")
                if str(child["name"]) != own_child
            ]
            capacity = self._offered(line)
            options.append(
                LineOption(
                    name=str(line["name"]),
                    odu_type=str(line["odu_type"]),
                    capacity=capacity,
                    free=free_slots(capacity, occupancies),
                    carrier_name=carrier_name,
                    carrier_id=str(carrier["id"]),
                    own=carrier_name == own_carrier,
                )
            )
        return sorted(options, key=lambda option: option.name)

    @staticmethod
    def _offered(line: dict[str, Any]) -> int | None:
        """What one line container offers its children, or `None` when unknown.

        Two sources, and each answers a different question. The table in
        `containers.py` says whether the figure exists at all: four of the
        sixteen types have no defined tributary slot size and a container of one
        of those is excluded rather than read as roomy. The stored
        `tributary_slot_capacity` supplies the value, because that is the figure
        the ODU map draws and the capacity check compares against, and a
        generator that packed against a different number would refuse a service
        the check calls fine or fill a wavelength the check calls full.
        """
        if slot_capacity(str(line["odu_type"])) is None:
            return None
        return int(line["tributary_slot_capacity"])

    @staticmethod
    def _child_occupancy(child: dict[str, Any]) -> int | None:
        """What one existing child takes in its parent, read from the child.

        The stored `tributary_slots` is the figure, for a sized type and an
        unsized one alike, because that is the number the ODU map draws and the
        capacity check sums. A generator packing against a different number would
        refuse a service the check calls fine, or fill a wavelength the check calls
        full.

        One guard, and it is where the unknown still lives. A container of one of
        the four types G.709 does not size never legitimately occupies zero slots:
        `containers.slots_for_client` floors the figure at one, so a stored zero on
        such a container means nobody wrote a real count. Reading it as zero would
        report a wavelength that might be overfull as having room, which is the
        failure `containers.py` exists to make impossible, so it is reported as
        unknown and `free_slots` propagates that to the whole parent.

        A sized type with a stored zero is a different case and is left alone. An
        ODU0 occupies one, so zero there is a data error rather than an unknown,
        and it belongs to the capacity check rather than to a provisioning
        decision that would silently refuse the whole wavelength.
        """
        stored = int(child["tributary_slots"])
        if slots_occupied(str(child["odu_type"])) is None and stored == 0:
            return None
        return stored

    @staticmethod
    def _best_fit(options: list[LineOption], occupies: int) -> LineOption | None:
        """The tightest line container that still takes the incoming client.

        Best fit, not first fit and not emptiest. A partly filled wavelength wins
        over an empty one, which is the whole of the grooming behaviour: ten
        STM-64 services land in one ODU4 instead of lighting ten of them, and the
        eleventh has a full container to be refused against. Emptiest fit would
        spread them and leave the map with nothing to show.

        `None` here is not a refusal. It says nothing on the route has room, and
        the caller answers that by lighting a wavelength. The refusal is one step
        further on, when lighting is impossible too.

        An option with an unknown free figure is not a candidate. `None` on the
        figure means nobody knows how full that wavelength is, and packing into it
        would be a guess reported as a fact.

        Free slots come from `containers.free_slots` by way of `_line_options`.
        Nothing here subtracts anything, which is what keeps the generator and the
        capacity check on one arithmetic.

        Ties break on the service's own container first and then on name, so two
        runs of the same service pick the same container. See `packing_key`:
        determinism is what makes the write an upsert rather than a delete and a
        create.
        """
        fitting = [option for option in options if option.free is not None and option.free >= occupies]
        if not fitting:
            return None
        return min(fitting, key=packing_key)

    @staticmethod
    def _no_room(plan: WavelengthPlan, odu_type: str, occupies: int) -> str:
        """Why the client did not fit and why no wavelength could be lit for it.

        Both halves in one sentence, because the refusal is only reached when both
        are true and a message naming one of them sends the reader to fix the wrong
        thing. A reader told only that the wavelengths are full will go looking for
        a way to light another, which is the part that was already impossible.

        Three ways to have no room, and they send the reader to three different
        places, so they are not collapsed. No line container at all on the route
        means the wavelengths there are dark or their containers have been removed,
        which is a dataset question. Every figure unknown means the route's line
        containers hold a child whose slot count nobody wrote. A tightest container
        with real figures means the wavelengths are genuinely full, and it is named
        with what it offers and what is left so the reader can see how far short
        the client was.

        **Two ways lighting can be impossible, and the message says which.** A mode
        whose line rate has no line container type has nothing to put on a new
        carrier, and emitting the carrier without one would leave a wavelength the
        ODU map draws as unlit forever. A route with no anchor free wide enough for
        the mode has nowhere to put the carrier at all, which is what
        `demo/90_fra_mil_saturated.yml` engineers on `oms-fra-mil`. Both can hold
        at once, so both are reported when they do. Naming only the container type
        would send a reader to `containers.py` over a full section, and naming only
        the spectrum would send them looking for a channel that would still be
        unusable.

        **The spectrum half is itself two answers, and `no_anchor` separates
        them.** No spectrum free at all is somebody else's wavelength to turn
        down. No free block wide enough is a narrower transponder or another
        route, and on a nearly full corridor it is the common case. This message
        used to state the first and mean either.
        """
        selection = plan.selection
        options = plan.options
        reasons = []
        if plan.line_type is None:
            rates = ", ".join(str(gbps) for gbps in sorted(LINE_CONTAINER_BY_LINE_RATE_GBPS))
            reasons.append(
                f"{selection.mode.name} runs at {selection.mode.line_rate_gbps} Gbit/s and a line container type "
                f"is defined only for {rates} Gbit/s"
            )
        if selection.channel is None:
            reasons.append(f"on {selection.route.key} {no_anchor(selection)}")
        unlit = f"and no wavelength can be lit either, because {' and '.join(reasons)}"
        if not options:
            return (
                f"no carrier on {selection.route.key} carries a line container, so there is nothing for the "
                f"{odu_type} to be groomed into, {unlit}"
            )
        known = [option for option in options if option.free is not None]
        if not known:
            return (
                f"all {len(options)} line containers on {selection.route.key} have an unknown free-slot count, "
                f"so none of them can be shown to have room for the {occupies} slots {odu_type} takes, {unlit}"
            )
        tightest = min(known, key=packing_key)
        return (
            f"{tightest.name} is the tightest of {len(options)} line containers on {selection.route.key} and "
            f"offers {tightest.capacity} slots with {tightest.free} free, and none of the {len(options)} has "
            f"room for the {occupies} slots {odu_type} takes, {unlit}"
        )

    @staticmethod
    def _element_ids(data: dict[str, Any]) -> dict[str, str]:  # noqa: D401
        """Every optical element the payload mentions, keyed by name.

        `budget.Hop` names an element; `OtnPathHop.element` needs its id. The
        three kinds a hop can be (ROADM, amplifier, span) all arrive nested
        under the sections, so this walks them once rather than re-querying.

        Both amplifier relationships are walked. A section holds one per
        direction of travel, and a hop can name an amplifier from either, so
        listing one would leave every hop of the other direction with no id.
        """
        found: dict[str, str] = {}
        for record in nodes_of(data, "OtnOpticalMultiplexSection"):
            for side in ("roadm_a", "roadm_b"):
                roadm = (record.get(side) or {}).get("node")
                if isinstance(roadm, dict):
                    flat = unwrap(roadm)
                    found[str(flat["name"])] = str(flat["id"])
            for relationship in ("spans", "amplifiers_a2b", "amplifiers_b2a"):
                related = record.get(relationship) or {}
                for edge in related.get("edges") or []:
                    flat = unwrap(edge["node"])
                    found[str(flat["name"])] = str(flat["id"])
        return found

    @staticmethod
    def _client_signal(data: dict[str, Any], service: dict[str, Any], service_name: str) -> dict[str, Any]:
        """What the service hands over: the one it states, or the one the rate picks.

        A stated signal wins outright. That is the point of the relationship: a
        service asking for InfiniBand cannot reach it any other way, because the
        automatic path never selects a specialised signal.

        The automatic path is the smallest catalog signal carrying
        `auto_selectable` that can carry the requested rate. Not "nearest at or
        below": 400GBASE-FR4 runs at 412.5 Gbps, so nearest at or below would
        map a 400G service to 100GBASE-LR4.

        The rule is the schema's, not this file's. `OtnClientSignal.auto_selectable`
        defaults to false, so a signal added without a decision is unreachable
        here until somebody writes `true` in a diff. The polarity is the point,
        and the reasoning is in `schemas/otn_logical.yml`.
        """
        rate_gbps = int(service["rate_gbps"])
        wanted_kbps = rate_gbps * KBPS_PER_GBPS

        stated = (service.get("client_signal") or {}).get("node")
        if isinstance(stated, dict):
            record = unwrap(stated)
            if int(record["bit_rate_kbps"]) < wanted_kbps:
                # Refused, not substituted. Substituting a faster row would make
                # the relationship advisory, which is the failure mode it exists
                # to close: the service would provision, name a signal it did not
                # ask for, and report success.
                raise ValueError(
                    f"{service_name}: states client signal {record['name']} at "
                    f"{int(record['bit_rate_kbps'])} kbps, which cannot carry {rate_gbps} Gbps"
                )
            return record

        candidates = []
        for record in nodes_of(data, "OtnClientSignal"):
            if "auto_selectable" not in record:
                # Loud, and named. Quietly dropping the record leaves an empty
                # candidate set, and the refusal that follows reads "no client
                # signal in the catalog carries 400 Gbps", which blames the
                # catalog for a query defect and sends the next reader to
                # objects/04_client_signals.yml, where nothing is wrong.
                raise ValueError(
                    f"{service_name}: client signal {record.get('name')} carries no auto_selectable flag, so it "
                    "cannot be matched. queries/optical_service.gql has to select "
                    "`auto_selectable` on the OtnClientSignal block"
                )
            if record["auto_selectable"] and int(record["bit_rate_kbps"]) >= wanted_kbps:
                candidates.append(record)
        if not candidates:
            raise ValueError(f"{service_name}: no client signal in the catalog carries {rate_gbps} Gbps")
        return min(candidates, key=lambda record: (int(record["bit_rate_kbps"]), str(record["name"])))
