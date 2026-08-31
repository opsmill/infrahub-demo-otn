"""Render one service end to end: router, glass, and back.

Today this takes three systems and a phone call: the router inventory knows the
port, the transport system knows the wavelength, and a spreadsheet knows which
duct the fiber is in. This is one command.

**Python rather than Jinja2.** The body orders hops, resolves a generic
relationship into three different element shapes, and returns a dict so the
artifact and the demo guide consume the same structure.

**One segment per wavelength, in order, with the junction named between them.**
A circuit regenerated at an intermediate site rides more than one wavelength, so
`segments` is a list and each entry carries its own path figures, its own
carrier, its own hops and its own ducts. The device joining a segment to the next
is on the earlier one, which is the same place `chains.ChainSegment` and
`budget.SegmentInput` put it: N segments then hold exactly N-1 junctions and a
chain with a hole in it cannot be rendered as though it were joined.

**And there is no route margin, because there is no such figure.** `path` and
`carrier` stay at the top level for a circuit with one segment, so an
unregenerated trace renders exactly what it rendered before, and they are `None`
for a chain. That is deliberate rather than lazy: `budget.RouteBudget` exposes no
scalar margin for a multi-segment route and its `sole_segment` raises instead of
handing one back, for the reason `budget`'s docstring gives. A reader shown one
`osnr_margin_display` on a regenerated circuit would take it for the route's
headroom, and two segments closing is not that claim. `route` carries what
genuinely totals, length and latency, the per-segment margins each paired with
its segment number, and a conjunction rather than a single verdict.

**One circuit, not one wavelength.** Grooming puts several services on a
carrier, so the shared container walk returns all of their clients. This trace
keeps the ones whose `OtnContainer.service` names the service being traced. The
filter sits at this call site rather than inside `impact.client_containers`,
because the impact report reads the same walk and wants every client on the
wavelength.

**And it falls back, labelled, on data written before that relationship.** A
container saved before the 016 schema load answers `service = null`, so filtering
on it returns zero containers and the trace reads as a service with no client.
When no container on the wavelength names its service, every client is listed and
`containers_note` says why. See `_containers` for the choice.

**It says what it does not know.** The dataset wires line ports to ROADM add and
drop ports and nothing else, so there is no recorded adjacency between a router
port and a transponder client port. The trace names the endpoint routers and
their client-role ports and says the specific assignment is not modelled, rather
than picking one of two candidates and presenting the guess as a fact.
"""

from collections.abc import Iterable
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.impact import (
    CircuitSegment,
    ClientContainer,
    circuit_segments,
    client_containers,
    decibels,
    hop_element,
    is_ai_profile,
    kilometres,
    microseconds,
    path_hops,
    path_propagation_ns,
    signed_decibels,
    terahertz,
)
from infrahub_demo_otn.plant import nodes_of, peer, peers, unwrap

REGENERATED_ROUTE = (
    "This circuit is regenerated, so it has one optical path per segment and no single route margin. Each "
    "segment below carries its own budget against its own mode, because an O-E-O device terminates the "
    "light and re-originates it and the noise cascade restarts there. `route.segment_margins` quotes every "
    "margin with the segment number it belongs to; there is no route figure to quote instead."
)

JUNCTION_UNNAMED = (
    "No device on this branch records terminating both this wavelength and the next, so the segments either "
    "side of this boundary are joined by nothing the model can see. That is a data defect rather than a "
    "rendering gap: `OtnOduSwitch.carriers` is the whole of the junction predicate, and a chain provisioned "
    "without both edges written is a chain that cannot be verified."
)

CONTAINERS_UNFILTERED = (
    "No container on this wavelength records the service that owns it, so every client on the carrier "
    "is listed. These rows are pre-migration data, not a circuit that happens to hold one container: "
    "`OtnContainer.service` arrived with the 016 schema, and a container written before that load "
    "answers null. Re-run the optical service generator for this wavelength and the trace narrows to "
    "one circuit."
)

PORT_ASSIGNMENT_UNKNOWN = (
    "The dataset records no adjacency between a router port and a transponder client port, so the "
    "specific interface carrying this service is not modelled. Every client-role port on the endpoint "
    "router is listed instead."
)


def _router(service: dict[str, Any], endpoint: str) -> dict[str, Any]:
    device = peer(service, endpoint)
    site = (device.get("site") or {}).get("node")
    ports = [
        {
            "name": str(port["name"]),
            "role": str(port["role"]),
            "enabled": bool(port["enabled"]),
            "oper_state": str(port["oper_state"]),
        }
        for port in peers(device, "ports")
    ]
    return {
        "endpoint": endpoint,
        "router": str(device["name"]),
        "site": str(unwrap(site)["name"]) if isinstance(site, dict) else None,
        "client_ports": [port for port in ports if port["role"] == "client"],
        "port_assignment": PORT_ASSIGNMENT_UNKNOWN,
    }


def _hop_row(hop: dict[str, Any]) -> dict[str, Any]:
    element = hop_element(hop)
    kind = str(element.get("__typename"))
    row: dict[str, Any] = {
        "sequence": int(hop["sequence"]),
        "kind": kind,
        "element": str(element.get("name")),
        "cumulative_length_m": int(hop["cumulative_length_m"]),
        "cumulative_length_display": kilometres(int(hop["cumulative_length_m"])),
        "cumulative_loss_mdb": int(hop["cumulative_loss_mdb"]),
        "cumulative_loss_display": decibels(int(hop["cumulative_loss_mdb"])),
        "cumulative_osnr_mdb": hop.get("cumulative_osnr_mdb"),
        "cumulative_osnr_display": (
            None if hop.get("cumulative_osnr_mdb") is None else decibels(int(hop["cumulative_osnr_mdb"]))
        ),
        "cumulative_delay_ns": int(hop["cumulative_delay_ns"]),
        "cumulative_delay_display": microseconds(int(hop["cumulative_delay_ns"])),
    }
    if kind == "OtnFiberSpan":
        conduit = (element.get("conduit") or {}).get("node")
        fiber = (element.get("fiber_type") or {}).get("node")
        row["length_m"] = int(element["length_m"])
        row["length_display"] = kilometres(int(element["length_m"]))
        row["conduit"] = str(unwrap(conduit)["name"]) if isinstance(conduit, dict) else None
        row["fiber_type"] = str(unwrap(fiber)["name"]) if isinstance(fiber, dict) else None
    elif kind == "OtnRoadm":
        site = (element.get("site") or {}).get("node")
        row["site"] = str(unwrap(site)["name"]) if isinstance(site, dict) else None
        row["insertion_loss_display"] = decibels(int(element["insertion_loss_mdb"]))
    elif kind == "OtnAmplifier":
        row["gain_display"] = decibels(int(element["gain_mdb"]))
        row["noise_figure_display"] = decibels(int(element["noise_figure_mdb"]))
    return row


def _owned_by(container: dict[str, Any], service_name: str) -> bool:
    """Whether this client container belongs to the service being traced.

    **The filter lives here and not in `impact.client_containers`.** That walk is
    shared with `transforms/impact_report.py` on purpose, decided in commit
    `bebf2a0`, so a fix to how a client is found cannot land in one report and
    miss the other. The impact report wants every client on the wavelength: its
    question is who is affected by a cut, and a neighbour's circuit is affected.
    A trace wants one circuit. So the walk stays unfiltered and the call site
    narrows it.

    A container with no `service` is dropped rather than kept. Both shapes are in
    the dataset: `demo/90_fra_mil_saturated.yml` writes forty `odu-fill-*`
    clients that belong to no `OtnService` object at all. They occupy real slots,
    which is why the impact report and the capacity check still count them, and
    they are not part of the circuit being traced. Keeping them would be a guess
    dressed as a fact.

    Dropping them is safe only because `_containers` first checks that some
    container on the wavelength names a service at all. On a wavelength where
    none does, this predicate is false for every row and the answer would be an
    empty circuit.
    """
    node = (container.get("service") or {}).get("node")
    if not isinstance(node, dict):
        return False
    return str(unwrap(node)["name"]) == service_name


def _names_a_service(container: dict[str, Any]) -> bool:
    """Whether this container records an owning service, whichever one it is."""
    return isinstance((container.get("service") or {}).get("node"), dict)


def _containers(found: Iterable[ClientContainer], service_name: str) -> tuple[list[ClientContainer], bool]:
    """The clients this trace prints, and whether the service filter was applied.

    **The filter has an unmigrated side, and it fails the same way.** `service`
    is an optional relationship added by 016, so every container written before
    that schema load answers null. Filtering on it then keeps nothing and the
    trace renders a provisioned service with an empty container list, which reads
    as a circuit with no client rather than as a report that could not tell. That
    was observed live: loading the schema onto the `demo` branch made both
    services trace nothing until the generator was re-run. The pre-filter bug
    over-reported a neighbour's container; this one under-reports to zero. Both
    are silent, so this returns the unfiltered list and the caller labels it.

    `impact.client_containers` already reads a pre-grooming shape rather than
    returning nothing, a client sitting directly on its carrier with no line
    container above it, so reading the old shape is the established answer here.

    **Scoped to this wavelength, not to the branch.** The test is whether any
    container on this carrier names a service. A branch can hold a mix, some
    services re-provisioned since the migration and some not, and a test that
    asked "does anything on this branch record a service" would let one
    re-provisioned service switch every report into filtered mode and hide the
    un-migrated ones behind an empty list. Per carrier is also the only test the
    payload can answer: `queries/service_trace.gql` fetches the traced service's
    own path and nothing else.
    """
    clients = list(found)
    if not clients:
        # Nothing groomed onto the wavelength, so both modes answer the same
        # empty list and nothing was suppressed. Reported as filtered, because
        # the fallback note would tell a reader that rows are pre-migration data
        # on a report that has no rows, and every unused carrier in the base
        # dataset arrives holding an empty line container.
        return clients, True
    if not any(_names_a_service(item.record) for item in clients):
        return clients, False
    return [item for item in clients if _owned_by(item.record, service_name)], True


def _carrier(path: dict[str, Any], service_name: str) -> dict[str, Any] | None:
    node = (path.get("carrier") or {}).get("node")
    if not isinstance(node, dict):
        return None
    carrier = unwrap(node)
    channel = (carrier.get("channel") or {}).get("node")
    mode = (carrier.get("optical_mode") or {}).get("node")
    grid = unwrap(channel) if isinstance(channel, dict) else {}
    optical_mode = unwrap(mode) if isinstance(mode, dict) else {}
    clients, filtered = _containers(client_containers(carrier), service_name)
    return {
        "name": str(carrier["name"]),
        "status": str(carrier.get("status") or ""),
        "channel": grid.get("channel_number"),
        "center_frequency_display": (
            terahertz(int(grid["center_frequency_mhz"])) if grid.get("center_frequency_mhz") else None
        ),
        "mode": optical_mode.get("name"),
        "modulation": optical_mode.get("modulation"),
        "mode_class": optical_mode.get("mode_class"),
        "fec_type": optical_mode.get("fec_type"),
        "fec_latency_display": (
            microseconds(int(optical_mode["fec_latency_ns"])) if optical_mode.get("fec_latency_ns") else None
        ),
        "sections": [
            {
                "name": str(section["name"]),
                "roadm_a": str(peer(section, "roadm_a")["name"]),
                "roadm_b": str(peer(section, "roadm_b")["name"]),
            }
            for section in peers(carrier, "sections")
        ],
        # Through the parent hop, not off the carrier. `client_containers` walks
        # a line container's children and yields the clients, whichever of the
        # two shapes the branch holds. Reading `peers(carrier, "containers")`
        # here would hand back the line container, which carries no signal, and
        # the trace would render a wavelength with no client on it and no error.
        # See `impact.client_containers` for how a line container is told from a
        # client one, and R-006 for what this cost before it was fixed.
        "containers": [
            {
                "name": str(item.record["name"]),
                "odu_type": str(item.record["odu_type"]),
                "mapping_mode": str(item.record["mapping_mode"]),
                # `None` for a client sitting directly on its carrier, which is
                # what container data written before grooming looks like. The key
                # stays in the row either way, so a reader can see that the trace
                # looked and found nothing above rather than that it did not look.
                "line_container": item.line_container,
                "client_signal": str(peer(item.record, "client_signal")["name"]),
                "client_layer": str(peer(item.record, "client_signal")["layer"]),
            }
            # Narrowed to this service by `_containers`. Grooming puts several
            # clients on one wavelength, so the unfiltered walk hands back the
            # neighbours' as well, and a trace that printed them would say a
            # stranger's circuit is part of this one. `impact.client_containers`
            # stays unfiltered because the impact report needs all of them.
            for item in clients
        ],
        # False when no container on this wavelength records its owning service,
        # and then the rows above are every client on the carrier. Both keys are
        # always present: a fallback a reader cannot see is the third way this
        # report goes quietly wrong.
        "containers_filtered": filtered,
        "containers_note": None if filtered else CONTAINERS_UNFILTERED,
    }


def _conduits(hops: list[dict[str, Any]]) -> list[str]:
    """Every duct the spans of these hops are buried in, deduplicated and sorted."""
    return sorted(
        {
            str(unwrap((hop_element(hop).get("conduit") or {}).get("node") or {}).get("name"))
            for hop in hops
            if hop_element(hop).get("__typename") == "OtnFiberSpan"
            and isinstance((hop_element(hop).get("conduit") or {}).get("node"), dict)
        }
    )


def _path_row(path: dict[str, Any]) -> dict[str, Any]:
    """One optical path's own figures, which on a chain are one segment's.

    Every margin key here is a segment figure and the caller is what says which
    segment. This function is not given the sequence number on purpose: a row
    that carried both a margin and its own segment number would be quotable on
    its own, and the shape FR-014 asks for is a margin a reader cannot lift out
    of the list it came in.
    """
    propagation = path_propagation_ns(path)
    latency = int(path["latency_ns"])
    return {
        "name": str(path["name"]),
        "description": path.get("description"),
        "total_length_m": int(path["total_length_m"]),
        "total_length_display": kilometres(int(path["total_length_m"])),
        "total_loss_display": decibels(int(path["total_loss_mdb"])),
        "osnr_total_display": decibels(int(path["osnr_total_mdb"])),
        "osnr_margin_display": signed_decibels(int(path["osnr_margin_mdb"])),
        "latency_ns": latency,
        "latency_display": microseconds(latency),
        "propagation_display": microseconds(propagation),
        "electronics_display": microseconds(latency - propagation),
    }


def _junction(segment: CircuitSegment) -> dict[str, Any] | None:
    """The device the light is rebuilt in at this segment's far end.

    `None` on the last segment, which is not a gap: nothing regenerates light
    nobody carries on, and `impact.CircuitSegment` refuses to invent a device
    there. The middle-of-the-chain `None` is the one worth a note, and
    `_segment_row` attaches it.
    """
    if segment.junction is None:
        return None
    framing = segment.junction.get("framing_latency_ns")
    return {
        "device": segment.junction_device,
        "site": segment.junction_site,
        "switching_mode": segment.junction.get("switching_mode"),
        "framing_latency_ns": None if framing is None else int(framing),
        "framing_latency_display": None if framing is None else microseconds(int(framing)),
    }


def _segment_row(segment: CircuitSegment, service_name: str, *, last: bool) -> dict[str, Any]:
    """One segment: its own path, its own carrier, its own glass, its own duct.

    `sequence` is `OtnOpticalPath.segment_sequence` as the graph holds it, not the
    row's index, so the number beside a margin is the number a query would return
    for that path.
    """
    hops = path_hops(segment.path)
    junction = _junction(segment)
    return {
        "sequence": segment.sequence,
        "path": _path_row(segment.path),
        "carrier": _carrier(segment.path, service_name),
        "hop_count": len(hops),
        "hops": [_hop_row(hop) for hop in hops],
        "conduits": _conduits(hops),
        "junction": junction,
        "junction_note": None if last or junction is not None else JUNCTION_UNNAMED,
    }


class ServiceTraceTransform(InfrahubTransform):
    query = "service_trace"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        services = list(nodes_of(data, "OtnService"))
        if not services:
            raise ValueError(
                "no service matched the `service` variable on this branch. A trace of nothing is not an "
                "empty trace, it is a name that does not exist"
            )
        if len(services) > 1:
            raise ValueError(f"{len(services)} services matched; a trace renders exactly one")
        service = services[0]
        name = str(service["name"])

        header = {
            "branch": self.branch_name,
            "service": name,
            "description": service.get("description"),
            "customer": str(service["customer"]),
            "rate_gbps": int(service["rate_gbps"]),
            "sla": str(service["sla"]),
            "status": str(service["status"]),
            "service_profile": str(service["service_profile"]),
            "latency_sensitive": is_ai_profile(str(service["service_profile"])),
            "max_latency_ns": service.get("max_latency_ns"),
            "max_latency_display": (
                microseconds(int(service["max_latency_ns"])) if service.get("max_latency_ns") else None
            ),
            "endpoints": [_router(service, "endpoint_a"), _router(service, "endpoint_z")],
        }

        segments = circuit_segments(service)
        if not segments:
            return {
                **header,
                "provisioned": False,
                # Two keys, never one string split apart. The code is a Dropdown
                # of six values the server enforces, so a consumer can group or
                # colour by it without a substring test, and the detail is prose
                # that is only ever read. Before feature 022 both arrived as
                # `"{code}: {detail}"` in a single Text field and this transform
                # handed the concatenation on for somebody else to parse.
                "rejection_code": service.get("rejection_code"),
                "rejection_detail": service.get("rejection_detail"),
                # `refusal_accepted` is deliberately absent. `service_trace.gql`
                # does not select it, so reading it here would answer `None` on
                # every service and render every refusal as unsigned. Whether a
                # refusal was accepted is `checks/provisionable.py`'s question
                # and it has its own query.
                "note": (
                    "This service carries no optical path. A refusal is an answer, not a gap: the reason "
                    "the generator recorded is above."
                ),
            }

        rows = [_segment_row(segment, name, last=index == len(segments) - 1) for index, segment in enumerate(segments)]
        regenerated = len(rows) > 1
        latency = sum(row["path"]["latency_ns"] for row in rows)
        framing = sum(row["junction"]["framing_latency_ns"] or 0 for row in rows if row["junction"] is not None)
        return {
            **header,
            "provisioned": True,
            "segment_count": len(rows),
            "regenerated": regenerated,
            "segments": rows,
            # Every junction in route order, which is one fewer than the
            # segments. Lifted out of the rows because "where is this circuit
            # regenerated" is the question an operator asks before reading any
            # segment, and on a single-segment circuit the honest answer is an
            # empty list rather than a missing key.
            "junctions": [row["junction"] for row in rows if row["junction"] is not None],
            "route": {
                # The two figures that total across a regeneration, and no
                # third. Length totals because geography does not restart at a
                # device, and latency totals because delay adds where noise
                # restarts, which is the split `budget.RouteBudget` draws.
                "total_length_m": sum(row["path"]["total_length_m"] for row in rows),
                "total_length_display": kilometres(sum(row["path"]["total_length_m"] for row in rows)),
                "latency_ns": latency + framing,
                "latency_display": microseconds(latency + framing),
                "framing_latency_display": microseconds(framing),
                # Paired with their segment numbers, which is the whole of what
                # makes them quotable. `budget.RouteBudget.segment_margins_mdb`
                # returns the same shape and for the same reason: a caller cannot
                # read a margin out of here without reading which segment it
                # belongs to.
                "segment_margins": [
                    {"sequence": row["sequence"], "osnr_margin_display": row["path"]["osnr_margin_display"]}
                    for row in rows
                ],
                # A conjunction, not an average and not a weakest link. Named for
                # what it actually covers: the recorded OSNR margin on every
                # segment is positive. It is not the budget verdict, because
                # `budget.PathBudget.ok` also weighs chromatic dispersion and the
                # amplifier gains, and neither is stored on the path. Calling this
                # "closes" would claim two tests this payload cannot run.
                "osnr_positive_on_every_segment": all(
                    int(segment.path["osnr_margin_mdb"]) >= 0 for segment in segments
                ),
                "conduits": sorted({conduit for row in rows for conduit in row["conduits"]}),
            },
            "route_note": REGENERATED_ROUTE if regenerated else None,
            # The single-segment shape, unchanged, so an unregenerated trace and
            # every consumer of it render exactly what they did before this
            # feature. `None` on a chain rather than the first segment: a chain
            # has no one path and no one carrier, and answering with segment 1
            # would be the under-report `impact.service_path` warns about, one
            # level up in the output.
            "path": rows[0]["path"] if not regenerated else None,
            "carrier": rows[0]["carrier"] if not regenerated else None,
            "hops": rows[0]["hops"] if not regenerated else None,
            # These two are honest for a chain and stay populated. A hop count
            # totals, and the ducts union exactly as `impact.service_exposures`
            # unions them, so the trace and the exposure report cannot disagree
            # about which ducts a regenerated circuit is in.
            "hop_count": sum(row["hop_count"] for row in rows),
            "conduits": sorted({conduit for row in rows for conduit in row["conduits"]}),
        }
