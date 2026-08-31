"""What a backhoe through one section costs.

The report leads with the transport capacity lost, because that is the number an
outage call opens with, then the services and the customers behind them, then
the wavelengths with nothing behind them at all.

**Unattached spectrum is counted, not dropped.** The forty carriers the dataset
ships have no service object: they are spectrum, loaded to make the corridor
realistically busy. A report that skipped a carrier for having a null optical
path would answer "no service affected" on a cut through `oms-fra-mil`, where
the true answer is forty wavelengths and 15.1 terabits.

**Each wavelength carries its interval as well as its anchor.** A cut is
described in channel numbers on the call and in megahertz in the plan, and the
two are not interchangeable: channel 40 is a frequency, and what the wavelength
actually holds is 79,600 MHz centred on it. The width comes from
`units.occupied_width_mhz` and the edges from `units.carrier_interval_mhz`, which
are the same two functions the collision check and the allocator call, so a
restoration plan drawn off this report cannot disagree with the check that gates
it.

**AI and HPC services are grouped separately.** A training
cluster split across two sites cannot tolerate a single cut, and the operator
triaging an outage needs those rows first, not sorted in alphabetically among
the transit services.

**A section that does not exist is an error.** Sixteen of the twenty-one sections
in the shipped dataset carry no wavelength, so "no impact" is a common and
correct answer, and a typo must not borrow it.

**One wavelength, several services.** Grooming puts more than one client on a
carrier, so the report reads a list of optical paths off each one and reports
`services` and `customers` as lists. The clients themselves are reached through
the parent hop rather than off the carrier, which is what
`impact.client_containers` is for: `carrier.containers` returns the line
container, and the clients are its children.

**A cut segment is not the loss of the circuit, and it is not resilience
either.** A chained circuit rides one wavelength per segment, so a cut takes one
of them and the others stay lit. This model has no protection switching and no
restoration, so the circuit is down all the same and the report says so first.
What the surviving segments change is the repair rather than the outage: the
light either side of the junction is intact and one segment has to be
re-provisioned, not the route. Flattening that away would lose the only fact an
operator can act on; inflating it into "the circuit may reroute" would promise a
capability nothing here implements. Both wordings are in `_segment_context`.
"""

from collections import defaultdict
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.impact import (
    amplifier_census,
    circuit_segments,
    client_containers,
    is_ai_profile,
    kilometres,
    microseconds,
    terabits,
    terahertz,
)
from infrahub_demo_otn.plant import nodes_of, peer, peers, unwrap
from infrahub_demo_otn.units import carrier_interval_mhz, occupied_width_mhz


def _endpoint(service: dict[str, Any], endpoint: str) -> dict[str, Any]:
    device = peer(service, endpoint)
    site = (device.get("site") or {}).get("node")
    return {
        "router": str(device["name"]),
        "site": str(unwrap(site)["name"]) if isinstance(site, dict) else None,
        "client_ports": sorted(str(port["name"]) for port in peers(device, "ports") if port.get("role") == "client"),
    }


SINGLE_SEGMENT = (
    "This circuit rides one wavelength, so the cut takes the whole of it. There is no other segment to be intact."
)

CIRCUIT_DOWN_ON_ONE_SEGMENT = (
    "The circuit is down. The cut takes segment {sequence} of {count}, on {carrier}, and {survivors} still "
    "lit. This model has no protection switching and no restoration, so a circuit that loses one segment "
    "loses the service: the surviving segment is not carrying it. What it changes is the repair, because "
    "only the cut segment has to be re-provisioned and the light either side of the junction is intact."
)

NO_SEGMENT_RECORDED = (
    "This path is attached to the cut carrier, and the service's own path list does not contain it. That is "
    "a payload the query cannot produce and a graph that should not hold: `OtnOpticalPath.service` is "
    "mandatory and both sides of the relationship are written together. Reported as the whole circuit, "
    "which is the safe reading of a circuit whose segments cannot be counted."
)


def _segments_of(service: dict[str, Any]) -> list[dict[str, Any]]:
    """The service's own paths, in segment order, each with its carrier's name.

    `impact.circuit_segments` is the walk, the same one the trace and the
    exposure report follow, even though this report arrives at the service from
    the other direction: from the cut carrier, down through the path. What the
    walk needs is a service with its `optical_path` edges, and
    `queries/span_impact.gql` selects them on the nested service for that reason.
    A second ordering rule here would be the drift that has a report disagreeing
    with a trace about which segment is segment 1.

    The junction comes back `None` from that walk and is not read. This query
    does not select `odu_switches`, because this report does not name the device:
    it names the segment that went and the segments that did not.
    `transforms/service_trace.py` is the one that names the junction.
    """
    return [{"sequence": segment.sequence, "carrier": segment.carrier_name} for segment in circuit_segments(service)]


def _segment_context(
    service: dict[str, Any], path: dict[str, Any], carrier_name: str, cut_carriers: set[str]
) -> dict[str, Any]:
    """Which segment of this circuit the cut takes, and which segments survive.

    A segment is surviving when its wavelength is not one of the wavelengths this
    section carries, which is read off the carriers the query already returned
    rather than assumed from the sequence number. A chain may not repeat a
    section, so exactly one segment is cut whenever the graph is sound, and
    testing it means an unsound graph is reported rather than averaged over.

    The wording says the circuit is down before it says anything else. Naming a
    surviving segment without that sentence in front of it reads as protection
    that does not exist.
    """
    segments = _segments_of(service)
    sequence = int(path.get("segment_sequence") or 1)
    if not segments:
        return {
            "segment_sequence": sequence,
            "segment_count": 1,
            "regenerated": False,
            "segments_cut": [sequence],
            "segments_surviving": [],
            "segment_note": NO_SEGMENT_RECORDED,
        }
    cut = [row["sequence"] for row in segments if row["carrier"] in cut_carriers]
    surviving = [row for row in segments if row["carrier"] not in cut_carriers]
    if len(segments) == 1:
        note = SINGLE_SEGMENT
    else:
        listed = ", ".join(f"segment {row['sequence']} on {row['carrier']}" for row in surviving)
        note = CIRCUIT_DOWN_ON_ONE_SEGMENT.format(
            sequence=sequence,
            count=len(segments),
            carrier=carrier_name,
            survivors=f"{listed} {'is' if len(surviving) == 1 else 'are'}" if surviving else "no other segment is",
        )
    return {
        "segment_sequence": sequence,
        "segment_count": len(segments),
        "regenerated": len(segments) > 1,
        "segments_cut": cut or [sequence],
        "segments_surviving": surviving,
        "segment_note": note,
    }


def _service_row(
    service: dict[str, Any],
    path: dict[str, Any],
    carrier_name: str,
    channel: int,
    cut_carriers: set[str],
) -> dict[str, Any]:
    budget = service.get("max_latency_ns")
    return {
        **_segment_context(service, path, carrier_name, cut_carriers),
        "service": str(service["name"]),
        "customer": str(service["customer"]),
        "rate_gbps": int(service["rate_gbps"]),
        "sla": str(service["sla"]),
        "status": str(service["status"]),
        "service_profile": str(service["service_profile"]),
        "carrier": carrier_name,
        "channel": channel,
        "path_length_display": kilometres(int(path["total_length_m"])),
        "latency_display": microseconds(int(path["latency_ns"])),
        "max_latency_display": microseconds(int(budget)) if budget else None,
        "endpoint_a": _endpoint(service, "endpoint_a"),
        "endpoint_z": _endpoint(service, "endpoint_z"),
    }


def _interval(channel_peer: Any, mode: dict[str, Any]) -> dict[str, Any]:
    """The spectrum one carrier holds, beside the anchor it is named by.

    Every key is `None` when the anchor or the symbol rate is missing, and that
    is a report rather than a raise. `queries/span_impact.gql` selects both, so an
    absent one means the branch holds a carrier with no anchor or no mode, and an
    outage report is read while that is being fixed rather than after. The
    collision check is what refuses to let such a carrier through.

    The width comes from `units.occupied_width_mhz` and the edges from
    `units.carrier_interval_mhz`. Neither is recomputed here: a restoration plan
    that placed the edges a megahertz differently from the check that gates the
    replacement wavelength would pass one and fail the other.
    """
    absent = {"occupied_width_mhz": None, "lower_edge_mhz": None, "upper_edge_mhz": None, "interval_display": None}
    if not isinstance(channel_peer, dict):
        return absent
    center = unwrap(channel_peer).get("center_frequency_mhz")
    baud = mode.get("baud_mbaud")
    if center is None or baud is None:
        return absent
    lower, upper = carrier_interval_mhz(int(center), int(baud))
    return {
        "occupied_width_mhz": occupied_width_mhz(int(baud)),
        "lower_edge_mhz": lower,
        "upper_edge_mhz": upper,
        "interval_display": f"{terahertz(lower)} to {terahertz(upper)}",
    }


def _conduit_breakdown(section: dict[str, Any]) -> list[dict[str, Any]]:
    """Each duct the cut section's spans occupy, and what else is buried in it.

    A cut in a duct is a cut in every fiber in it. The section is what an
    operator names; the conduit is what the digger actually hits.
    """
    ducts: dict[str, dict[str, Any]] = {}
    unducted = 0
    for span in peers(section, "spans"):
        node = (span.get("conduit") or {}).get("node")
        if not isinstance(node, dict):
            unducted += 1
            continue
        conduit = unwrap(node)
        name = str(conduit["name"])
        entry = ducts.setdefault(
            name,
            {
                "conduit": name,
                "owner": conduit.get("owner"),
                "spans_of_this_section": [],
                "other_sections": set(),
            },
        )
        entry["spans_of_this_section"].append(str(span["name"]))
        for sibling in peers(conduit, "spans"):
            oms = (sibling.get("oms") or {}).get("node")
            if isinstance(oms, dict):
                entry["other_sections"].add(str(unwrap(oms)["name"]))

    rows = []
    for entry in ducts.values():
        entry["other_sections"].discard(str(section["name"]))
        rows.append(
            {
                "conduit": entry["conduit"],
                "owner": entry["owner"],
                "spans_of_this_section": sorted(entry["spans_of_this_section"]),
                "also_carries_sections": sorted(entry["other_sections"]),
            }
        )
    rows.sort(key=lambda row: row["conduit"])
    if unducted:
        rows.append(
            {
                "conduit": None,
                "owner": None,
                "spans_of_this_section": [],
                "also_carries_sections": [],
                "note": f"{unducted} span(s) of this section are outside any recorded conduit",
            }
        )
    return rows


class ImpactReportTransform(InfrahubTransform):
    query = "span_impact"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        sections = list(nodes_of(data, "OtnOpticalMultiplexSection"))
        if not sections:
            raise ValueError(
                "no optical multiplex section matched the `section` variable on this branch. An empty "
                "result and a name that does not exist are different answers, and this is the second one"
            )
        section = sections[0]
        name = str(section["name"])

        carriers = list(nodes_of(data, "OtnOpticalCarrier"))
        # Every wavelength this section carries, which is the set a segment is
        # tested against to decide whether the cut takes it. The query filtered
        # these on the section, so membership here is the cut.
        cut_carriers = {str(record["name"]) for record in carriers}
        lost_gbps = 0
        wavelengths: list[dict[str, Any]] = []
        ai_services: list[dict[str, Any]] = []
        other_services: list[dict[str, Any]] = []
        unattached: list[dict[str, Any]] = []
        customers: set[str] = set()
        routers: set[str] = set()
        signals: dict[str, int] = defaultdict(int)

        for record in carriers:
            channel_peer = (record.get("channel") or {}).get("node")
            channel = int(unwrap(channel_peer)["channel_number"]) if isinstance(channel_peer, dict) else None
            mode_peer = (record.get("optical_mode") or {}).get("node")
            mode = unwrap(mode_peer) if isinstance(mode_peer, dict) else {}
            rate = int(mode.get("line_rate_gbps") or 0)
            lost_gbps += rate
            interval = _interval(channel_peer, mode)

            # Through the parent hop, not off the carrier. `client_containers`
            # walks a line container's children and yields the clients, whichever
            # of the two shapes the branch holds. Reading
            # `peers(record, "containers")` here would hand back the line
            # container, which carries no signal, and `client_signals` would
            # report an empty census over a busy corridor with nothing raised.
            # See `impact.client_containers` for how a line container is told
            # from a client one, and R-006 for what this cost before it was
            # fixed.
            containers = [
                {
                    "name": str(item.record["name"]),
                    "odu_type": str(item.record["odu_type"]),
                    "mapping_mode": str(item.record["mapping_mode"]),
                    "line_container": item.line_container,
                    "client_signal": str(peer(item.record, "client_signal")["name"]),
                }
                for item in client_containers(record)
            ]
            for container in containers:
                signals[container["client_signal"]] += 1

            # Several paths, not one. `OtnOpticalCarrier.optical_path` is
            # cardinality many since grooming let two services share a
            # wavelength, and both point their path at the same carrier. A bare
            # `node` on a many relationship is not a partial read either: the
            # server answers it with a 500 and the whole query errors, which is
            # what `queries/span_impact.gql` records above the selection.
            #
            # `services` and `customers` are plural for the same reason the
            # relationship is. A singular key would have to hold the first of
            # several, and the first of several read as "the" service is how an
            # outage call ends up notifying one customer of three.
            attached: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for path in peers(record, "optical_path"):
                service_peer = (path.get("service") or {}).get("node")
                if isinstance(service_peer, dict):
                    attached.append((path, unwrap(service_peer)))
            attached.sort(key=lambda pair: str(pair[1]["name"]))

            wavelengths.append(
                {
                    "carrier": str(record["name"]),
                    "channel": channel,
                    "center_frequency_display": (
                        terahertz(int(unwrap(channel_peer)["center_frequency_mhz"]))
                        if isinstance(channel_peer, dict) and unwrap(channel_peer).get("center_frequency_mhz")
                        else None
                    ),
                    **interval,
                    "mode": mode.get("name"),
                    "rate_gbps": rate,
                    "status": str(record.get("status") or ""),
                    "sections": sorted(str(item["name"]) for item in peers(record, "sections")),
                    "containers": containers,
                    "services": [str(service["name"]) for _, service in attached],
                    "customers": sorted({str(service["customer"]) for _, service in attached}),
                }
            )

            if not attached:
                unattached.append(
                    {
                        "carrier": str(record["name"]),
                        "channel": channel,
                        "occupied_width_mhz": interval["occupied_width_mhz"],
                        "rate_gbps": rate,
                    }
                )
                continue

            for path, service in attached:
                row = _service_row(service, path, str(record["name"]), channel or 0, cut_carriers)
                customers.add(row["customer"])
                routers.add(row["endpoint_a"]["router"])
                routers.add(row["endpoint_z"]["router"])
                if is_ai_profile(row["service_profile"]):
                    ai_services.append(row)
                else:
                    other_services.append(row)

        # One row per service, and a chained circuit is one row. A cover may not
        # repeat a section, so no circuit has two segments on the section being
        # cut, and `segments_cut` holding one sequence is that invariant showing
        # through rather than a simplification.
        chained = sorted(
            (
                {
                    "service": row["service"],
                    "segment_cut": row["segment_sequence"],
                    "segment_count": row["segment_count"],
                    "segments_surviving": row["segments_surviving"],
                }
                for row in ai_services + other_services
                if row["regenerated"]
            ),
            key=lambda row: str(row["service"]),
        )

        census = amplifier_census(section)
        wavelengths.sort(key=lambda item: (item["channel"] is None, item["channel"]))
        ai_services.sort(key=lambda row: row["service"])
        other_services.sort(key=lambda row: row["service"])
        unattached.sort(key=lambda item: (item["channel"] is None, item["channel"]))
        span_length = sum(int(span["length_m"]) for span in peers(section, "spans"))

        return {
            "branch": self.branch_name,
            "section": name,
            "description": section.get("description"),
            "headline": (
                f"Cutting {name} drops {len(wavelengths)} wavelength(s), {terabits(lost_gbps)} of transport, "
                f"{len(ai_services) + len(other_services)} recorded service(s) and {len(customers)} customer(s)."
            ),
            "chained_service_count": len(chained),
            "chained_services": chained,
            "chained_note": (
                f"{len(chained)} of the affected circuit(s) are regenerated and ride more than one "
                "wavelength. Each is down: the cut takes one segment and this model has no protection "
                "switching and no restoration, so the segments that stay lit are not carrying the service. "
                "They narrow the repair to one segment, which is what the per-service note says."
                if chained
                else None
            ),
            "capacity_lost_gbps": lost_gbps,
            "capacity_lost_display": terabits(lost_gbps),
            "wavelength_count": len(wavelengths),
            "service_count": len(ai_services) + len(other_services),
            "customer_count": len(customers),
            "customers": sorted(customers),
            "router_endpoints": sorted(routers),
            "client_signals": dict(sorted(signals.items())),
            "plant": {
                "roadm_a": str(peer(section, "roadm_a")["name"]),
                "roadm_b": str(peer(section, "roadm_b")["name"]),
                "span_count": len(list(peers(section, "spans"))),
                "amplifier_count": census.total,
                # Per direction, because the total alone cannot tell a healthy
                # section from one with a chain missing. `chains_balanced` is
                # false when the two counts differ, and that is worth saying in
                # a report about a cut: an unbalanced section is already
                # degraded before the backhoe.
                #
                # There is no third count. An amplifier is in one of the
                # section's two lists, the other, or in no section at all, and a
                # report scoped to one section cannot see the third case.
                "amplifiers_a_to_b": census.a_to_b,
                "amplifiers_b_to_a": census.b_to_a,
                "chains_balanced": census.balanced,
                "length_m": span_length,
                "length_display": kilometres(span_length),
            },
            "latency_sensitive_services": ai_services,
            "other_services": other_services,
            "wavelengths": wavelengths,
            "unattached_wavelengths": unattached,
            "unattached_note": (
                "These wavelengths carry no service object. They are real spectrum and a real loss; the "
                "model has no customer recorded behind them."
                if unattached
                else None
            ),
            "conduits": _conduit_breakdown(section),
        }
