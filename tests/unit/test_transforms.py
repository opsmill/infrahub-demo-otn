"""Every registered transform, executed. Offline, against hand-built payloads.

`test_repository_config.py` proves the class exists and binds a registered
query. This is what proves `transform()` returns rather than raising.

The failure it catches: six of the seven transforms unwrap nested optional
relationships, and the difference between `(x or {}).get("node")` and
`(x or {}).get("node") or {}` is an `AttributeError` on the first null conduit.

Two payloads per transform therefore: one where every relationship is populated,
and one **sparse** payload where every optional peer is null and every list is
empty. The sparse one is the test.

The parametrisation is driven by `.infrahub.yml` rather than by a hand-written
list, so a transform registered without a payload here fails the suite instead of
quietly going untested.

**Eight return a dict and two return a string.** `network_map` and `odu_map`
render SVG documents, and both artifacts declare `image/svg+xml`; a dict against
a non-JSON content type writes a stringified Python dict into the artifact body.
So the shape assertions below take either, and the two tests that read a key off
the result say why they skip the ones that have no keys.
"""

import asyncio
import importlib.util
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest
from infrahub_sdk.ctl.repository import get_repository_config

from infrahub_demo_otn.containers import slot_capacity, slots_occupied
from infrahub_demo_otn.mapchrome import ROUTE_FOCUS_WIDTH, ROUTE_WIDTH
from infrahub_demo_otn.mapdraw import UNKNOWN_BAND
from infrahub_demo_otn.odudraw import FIT_UNKNOWN, HEADROOM_BANDS, NO_ODU_BAND
from infrahub_demo_otn.units import (
    CBAND_EXTENT_MHZ,
    CBAND_LOWER_EDGE_MHZ,
    CBAND_UPPER_EDGE_MHZ,
    channel_to_frequency_mhz,
)
from tests.unit.conftest import REPO_ROOT
from tests.unit.test_impact import attribute, edges, named, one

CONFIG = get_repository_config(REPO_ROOT / ".infrahub.yml")
BRANCH = "probe-under-test"


def _load(file_path: str, class_name: str) -> Any:
    """Import a transform by path.

    `transforms/` is not a package and deliberately is not: Infrahub loads these
    files by path too. Importing them the same way the server does is closer to
    the thing being tested than adding an `__init__.py` for the tests' benefit.
    """
    path = REPO_ROOT / file_path
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


def _run(file_path: str, class_name: str, payload: dict[str, Any]) -> Any:
    """Call `transform()` without constructing a client.

    `InfrahubOperation.__init__` clones a client, which needs a server. The
    transforms under test read `self.branch_name` and nothing else off the
    instance, and `branch_name` returns `self.branch` when it is set. Building
    the instance this way keeps the test offline without stubbing the SDK.
    """
    cls = _load(file_path, class_name)
    instance = cls.__new__(cls)
    instance.branch = BRANCH
    return asyncio.run(instance.transform(data=payload))


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _fiber(name: str = "G.652.D") -> dict[str, Any]:
    return {
        "name": attribute(name),
        "attenuation_mdb_per_km": attribute(200),
        "dispersion_fs_per_nm_km": attribute(17_000),
        "group_index_milli": attribute(1468),
    }


def _pump(name: str, injection_end: str = "site_b", gain_mdb: int = 8000) -> dict[str, Any]:
    """One Raman pump as `raman_pumps` returns it.

    The pump stores where it sits and which way it fires; the direction it
    credits follows from those two. Counter-propagating at the B end is A to B.
    The insertion loss is charged to both directions, so a pump makes the two
    walks over its section disagree. That disagreement is what the
    two-direction reporting exists for, and it is why the pumped payload below
    is the one that tests it.
    """
    return {
        "__typename": "OtnRamanPump",
        "name": attribute(name),
        "injection_end": attribute(injection_end),
        "propagation": attribute("counter"),
        "on_off_gain_mdb": attribute(gain_mdb),
        "insertion_loss_mdb": attribute(800),
    }


def _span(
    name: str,
    sequence: int,
    length_m: int,
    conduit: str | None,
    pumps: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    return {
        "__typename": "OtnFiberSpan",
        "name": attribute(name),
        "raman_pumps": edges(*pumps),
        "oms_sequence": attribute(sequence),
        "length_m": attribute(length_m),
        "splice_count": attribute(10),
        "splice_loss_mdb": attribute(50),
        "connector_count": attribute(2),
        "connector_loss_mdb": attribute(300),
        "aging_margin_mdb": attribute(1500),
        "fiber_type": one(_fiber()),
        "conduit": one(
            {
                "name": attribute(conduit),
                "owner": attribute("GEANT"),
                "description": attribute("A duct."),
                "spans": edges(named(name)),
            }
            if conduit
            else None
        ),
        "oms": one(named("oms-a")),
    }


def _amplifier(name: str, sequence: int) -> dict[str, Any]:
    """One amplifier as a query returns it.

    No direction on the record. Which chain it is in is which of the section's
    two relationships holds it, so a payload that puts it in the wrong list is
    wrong in a way the graph itself would refuse.
    """
    return {
        "__typename": "OtnAmplifier",
        "name": attribute(name),
        "oms_sequence": attribute(sequence),
        "noise_figure_mdb": attribute(4000),
        "gain_mdb": attribute(22_000),
    }


def _roadm(name: str) -> dict[str, Any]:
    return {
        "__typename": "OtnRoadm",
        "name": attribute(name),
        "insertion_loss_mdb": attribute(7000),
        "site": one(named("ams")),
    }


def _section(name: str = "oms-a", conduit: str | None = "cd-north") -> dict[str, Any]:
    return {
        "id": name,
        "__typename": "OtnOpticalMultiplexSection",
        "name": attribute(name),
        "description": attribute("A section."),
        "roadm_a": one(_roadm("roadm-a")),
        "roadm_b": one(_roadm("roadm-b")),
        "spans": edges(_span("span-1", 1, 80_000, conduit), _span("span-2", 2, 80_000, None)),
        # Two spans, so three amplifiers per direction, in one relationship
        # each. Names are position-based and carry no direction: the hut at
        # index k holds ordinals 2k+1 and 2k+2, and which chain each is in is
        # the list it appears in here.
        "amplifiers_a2b": edges(
            _amplifier("amp-a-01", 1),
            _amplifier("amp-a-03", 2),
            _amplifier("amp-a-05", 3),
        ),
        "amplifiers_b2a": edges(
            _amplifier("amp-a-06", 1),
            _amplifier("amp-a-04", 2),
            _amplifier("amp-a-02", 3),
        ),
    }


def _mode(name: str = "DP-16QAM 64GBd 400G", reach_m: int = 1_000_000, baud: int = 64_000) -> dict[str, Any]:
    """One catalog mode. `baud` is what the occupied width is derived from.

    Parameterised because the width is what the capacity report now compares free
    spectrum against, and two modes at the same rate occupy 79,600 and 150,000
    MHz. A fixture with one symbol rate cannot show a pair of carriers on
    different anchors overlapping, which is what "contested" means now.
    """
    return {
        "id": name,
        "__typename": "OtnOpticalMode",
        "name": attribute(name),
        "mode_class": attribute("transponder"),
        "modulation": attribute("DP-16QAM"),
        "description": attribute("A mode."),
        "line_rate_gbps": attribute(400),
        "baud_mbaud": attribute(baud),
        "nominal_reach_m": attribute(reach_m),
        "required_osnr_mdb": attribute(24_500),
        "cd_tolerance_fs_per_nm": attribute(50_000_000),
        "fec_type": attribute("SD-FEC"),
        "fec_latency_ns": attribute(4000),
    }


def _container(name: str = "odu-svc-a", odu_type: str = "ODUC4", *, service: str | None = "svc-a") -> dict[str, Any]:
    """One client container: it holds a client signal and no children.

    Placed under a line container's `child_containers` this is the shape
    grooming writes. Placed directly under a carrier's `containers` it is the
    shape written before grooming existed, which the spec requires be read as a
    container on its own carrier rather than having a parent invented for it.
    Both are exercised below.

    The two slot figures come off `containers.py` rather than being typed here,
    which is what the generator does when it writes one. A fixture carrying a
    hand-typed occupancy would drift from the table on the first row that
    changes, and the ODU map reads both figures.

    `service` names the circuit that owns this client, which is what 016 D-003
    added and what lets a trace tell its own container from a neighbour's.
    `service=None` gives the shape the dataset still writes for a client that
    belongs to no `OtnService` object: `demo/90_fra_mil_saturated.yml` has forty
    of them, they occupy real slots, and no trace can claim them.
    """
    return {
        "name": attribute(name),
        "odu_type": attribute(odu_type),
        "mapping_mode": attribute("GMP"),
        "tributary_slots": attribute(slots_occupied(odu_type) or 0),
        "tributary_slot_capacity": attribute(slot_capacity(odu_type) or 0),
        "client_signal": one(
            {"name": attribute("400GBASE-FR4"), "layer": attribute("ethernet"), "bit_rate_kbps": attribute(412_500_000)}
        ),
        "service": one(None if service is None else {"name": attribute(service)}),
        "child_containers": edges(),
    }


def _line_container(
    *clients: dict[str, Any],
    name: str = "odu-line-oc-svc-a",
    odu_type: str = "ODUC4",
) -> dict[str, Any]:
    """One line container: it holds the clients and no client signal of its own.

    This is the hop FR-023 is about. `carrier.containers` returns this node, not
    a client, so a transform reading `client_signal` off it gets nothing and
    raises nothing. The `client_signal: one(None)` is not padding: it is what the
    server returns for an unset optional relationship, and it is what
    `impact.holds_client` tests.
    """
    # `list[dict[str, object]]` because `dict` is invariant in its value type and
    # an inferred `dict[str, dict[str, object]]` does not satisfy `edges`.
    children: list[dict[str, object]] = [dict(client) for client in clients]
    return {
        "name": attribute(name),
        "odu_type": attribute(odu_type),
        "mapping_mode": attribute("GMP"),
        "tributary_slots": attribute(slots_occupied(odu_type) or 0),
        "tributary_slot_capacity": attribute(slot_capacity(odu_type) or 0),
        "client_signal": one(None),
        "child_containers": edges(*children),
    }


def _carrier(
    name: str = "oc-svc-a",
    channel: int = 7,
    *,
    with_path: bool = True,
    mode: dict[str, Any] | None = None,
    section: str = "oms-a",
) -> dict[str, Any]:
    """One carrier. `optical_path` is a collection, not a single peer.

    `OtnOpticalCarrier.optical_path` is cardinality many since grooming let two
    services share a wavelength. A payload shaped with `one(...)` here would pass
    every test and disagree with what the server returns, and the server's own
    answer to a bare `node` on a many relationship is a 500 rather than a partial
    read, so the shape has to match.
    """
    return {
        "id": name,
        "__typename": "OtnOpticalCarrier",
        "name": attribute(name),
        "description": attribute("A carrier."),
        "status": attribute("active"),
        "channel": one(
            {
                "channel_number": attribute(channel),
                # The real grid, not a fixed frequency. The width a carrier
                # occupies is centred on this, so two carriers that differed only
                # in channel number used to be indistinguishable in spectrum.
                "center_frequency_mhz": attribute(channel_to_frequency_mhz(channel)),
            }
        ),
        "optical_mode": one(mode or _mode()),
        "sections": edges(named(section)),
        "containers": edges(_line_container(_container())),
        "optical_path": edges(*((_path(),) if with_path else ())),
    }


def _router(name: str = "rtr-a") -> dict[str, Any]:
    return {
        "id": name,
        "name": attribute(name),
        "site": one(named("ams")),
        "ports": edges(
            {
                "name": attribute("1/1/1"),
                "role": attribute("client"),
                "enabled": attribute(True),
                "oper_state": attribute("up"),
            }
        ),
    }


def _hops(
    span: str = "span-1", conduit: str | None = "cd-north", *, ends: tuple[str, str] = ("roadm-a", "roadm-b")
) -> dict[str, Any]:
    # Annotated because `dict` is invariant in its value type: an inferred
    # list[dict[str, dict[str, object]]] does not satisfy edges(*nodes:
    # dict[str, object]).
    rows: list[dict[str, object]] = []
    for index, element in enumerate(
        (_roadm(ends[0]), _span(span, 1, 80_000, conduit), _amplifier("amp-bst", 1), _roadm(ends[1]))
    ):
        rows.append(
            {
                "name": attribute(f"path-hop-{index:03d}"),
                "sequence": attribute(index),
                "cumulative_length_m": attribute(80_000 * index),
                "cumulative_loss_mdb": attribute(7000 * (index + 1)),
                "cumulative_osnr_mdb": attribute(30_000 - index),
                "cumulative_delay_ns": attribute(100_000 * index),
                "element": one(element),
            }
        )
    return edges(*rows)


def _switch(name: str = "oeo-fra", site: str = "fra", framing_ns: int = 1200) -> dict[str, Any]:
    """One `OtnOduSwitch`, as `service_trace.gql` selects it off a carrier.

    Only the four fields the trace reads. It is reached through
    `OtnOpticalCarrier.odu_switches`, and the junction between two segments is
    the device that appears on both carriers, which is why these fixtures share
    one instance between the two paths of a chain rather than writing two
    identically named ones.
    """
    return {
        "name": attribute(name),
        "switching_mode": attribute("regenerator"),
        "framing_latency_ns": attribute(framing_ns),
        "site": one(named(site)),
    }


def _path(
    *,
    service: dict[str, Any] | None = None,
    clients: tuple[dict[str, Any], ...] = (),
    name: str = "path-svc-a",
    sequence: int = 1,
    carrier: str = "oc-svc-a",
    section: str = "oms-a",
    span: str = "span-1",
    conduit: str | None = "cd-north",
    switches: tuple[dict[str, Any], ...] = (),
    margin_mdb: int = 2284,
) -> dict[str, Any]:
    """The path a trace walks, with the carrier it rides inline.

    `clients` are the client containers groomed into the one line container on
    that carrier, so a shared wavelength is written as the shape it is. Empty
    means the single default client, which is every case that predates grooming
    into somebody else's wavelength.

    `sequence`, `carrier`, `span`, `conduit` and `switches` are what make a
    second one of these a second **segment** rather than a duplicate. A chain is
    two of these with different wavelengths, different ducts and one device named
    on both, which is the shape `impact.circuit_segments` derives the junction
    from. Every default is the single-segment circuit the repository had before
    this feature, so a payload that does not ask for a chain is byte-for-byte
    what it was.
    """
    groomed: tuple[dict[str, Any], ...] = clients or (_container(),)
    return {
        "name": attribute(name),
        "segment_sequence": attribute(sequence),
        "description": attribute("A path."),
        "total_length_m": attribute(160_000),
        "total_loss_mdb": attribute(45_000),
        "osnr_total_mdb": attribute(27_784),
        "osnr_margin_mdb": attribute(margin_mdb),
        "latency_ns": attribute(783_000),
        "hops": _hops(span, conduit),
        "carrier": one(
            {
                "name": attribute(carrier),
                "status": attribute("active"),
                "channel": one({"channel_number": attribute(7), "center_frequency_mhz": attribute(191_650_000)}),
                "optical_mode": one(_mode()),
                "sections": edges(
                    {"name": attribute(section), "roadm_a": one(named("roadm-a")), "roadm_b": one(named("roadm-b"))}
                ),
                "containers": edges(_line_container(*groomed, name=f"odu-line-{carrier}")),
                "odu_switches": edges(*switches),
            }
        ),
        "service": one(service),
    }


def _service(
    name: str = "svc-a",
    profile: str = "ai-training-dci",
    *,
    provisioned: bool = True,
    budget: int | None = 4_000_000,
    clients: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    return {
        "id": name,
        "__typename": "OtnService",
        "name": attribute(name),
        "description": attribute("A service."),
        "customer": attribute("EuroHPC-Test"),
        "rate_gbps": attribute(400),
        "sla": attribute("gold"),
        "status": attribute("active" if provisioned else "rejected"),
        "service_profile": attribute(profile),
        "max_latency_ns": attribute(budget),
        # Two fields, matching what `service_trace.gql` and
        # `service_latency.gql` select. A fixture that still carried the old
        # single `rejection_reason` string would pass while the transform read
        # a field the query no longer returns, which is how this went unnoticed
        # once already.
        "rejection_code": attribute(None if provisioned else "latency"),
        "rejection_detail": attribute(None if provisioned else "too slow by 853.605 us"),
        "endpoint_a": one(_router("rtr-a")),
        "endpoint_z": one(_router("rtr-z")),
        # A collection, because `OtnService.optical_path` is cardinality many:
        # a circuit regenerated at an intermediate site is one path per
        # wavelength. One segment here, which is the whole route of a circuit
        # that spans one wavelength, and an unprovisioned service is an empty
        # collection rather than a null peer.
        "optical_path": edges(*((_path(clients=clients),) if provisioned else ())),
    }


def _conduit(name: str = "cd-north") -> dict[str, Any]:
    return {
        "id": name,
        "__typename": "OtnConduit",
        "name": attribute(name),
        "owner": attribute("GEANT"),
        "description": attribute("A duct."),
        "spans": edges({"name": attribute("span-1"), "length_m": attribute(80_000), "oms": one(named("oms-a"))}),
    }


def _impact_segment(sequence: int, carrier: str) -> dict[str, Any]:
    """One of a circuit's own paths, as the impact query reaches it back down.

    A leaf: its sequence and its wavelength and nothing else. The wavelength is
    what tells a cut segment from a surviving one, by testing it against the
    carriers the section returned, so a fixture that omitted it would let the
    report guess from the sequence number.
    """
    return {
        "name": attribute(f"path-segment-{sequence}"),
        "segment_sequence": attribute(sequence),
        "carrier": one(named(carrier)),
    }


def _service_for_impact(
    name: str = "svc-a",
    customer: str = "EuroHPC-Test",
    profile: str = "ai-training-dci",
    segments: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """A service reached from a carrier, which is the direction impact reads."""
    return {
        "name": attribute(name),
        "customer": attribute(customer),
        "rate_gbps": attribute(400),
        "sla": attribute("gold"),
        "status": attribute("active"),
        "service_profile": attribute(profile),
        "max_latency_ns": attribute(4_000_000),
        "endpoint_a": one(_router("rtr-a")),
        "endpoint_z": one(_router("rtr-z")),
        "optical_path": edges(*(segments or (_impact_segment(1, "oc-svc-a"),))),
    }


def _impact_path(
    service: str,
    customer: str = "EuroHPC-Test",
    profile: str = "ai-training-dci",
    *,
    sequence: int = 1,
    segments: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """One optical path off a carrier, as `span_impact.gql` returns it.

    Two of these hang off one carrier in the populated payload, because that is
    what grooming produces: `OtnOpticalCarrier.optical_path` is cardinality many
    and two services sharing a wavelength both point at it.

    `sequence` is which segment of its circuit this path is, and `segments` is
    the circuit's whole path list reached back down through the service. The
    default is one segment on the default carrier, which is every circuit that
    rides one wavelength.
    """
    return {
        "name": attribute(f"path-{service}"),
        "segment_sequence": attribute(sequence),
        "total_length_m": attribute(160_000),
        "latency_ns": attribute(783_000),
        "service": one(_service_for_impact(service, customer, profile, segments)),
    }


def _chained_service(name: str = "svc-chain", *, junction: bool = True) -> dict[str, Any]:
    """A two-segment circuit, with segment 2 written first.

    Out of order deliberately. An Infrahub relationship hands back a set, so a
    report that renders the payload order instead of sorting on
    `segment_sequence` renders the chain backwards and raises nothing.

    The two segments carry different wavelengths and different ducts, and one
    `OtnOduSwitch` instance appears on both carriers, which is the whole of the
    junction predicate: `impact.circuit_segments` intersects the two
    `odu_switches` lists.

    `junction=False` removes the shared device and leaves the two carriers
    terminated by different ones. That is the chain R-008 measured 48 phantom
    instances of, two segments joined by nothing, and the trace has to say so
    rather than render a boundary with a blank where the device goes.
    """
    shared = _switch()
    second = _path(
        name="path-chain-2",
        sequence=2,
        carrier="oc-fra-waw",
        section="oms-fra-waw",
        span="span-poland",
        conduit="cd-north",
        switches=(shared,) if junction else (_switch("oeo-waw", "waw"),),
        clients=(_container("odu-chain-2", service=name),),
        margin_mdb=5740,
    )
    first = _path(
        name="path-chain-1",
        sequence=1,
        carrier="oc-mad-fra",
        section="oms-mad-fra",
        span="span-iberia",
        conduit="cd-iberia",
        switches=(shared,) if junction else (),
        clients=(_container("odu-chain-1", service=name),),
        margin_mdb=2745,
    )
    return {**_service(name), "optical_path": edges(second, first)}


def _monitored(
    name: str, kind: str, monitor_kind: str, configured_field: str, configured: int, measured: int
) -> dict[str, Any]:
    """A device with a gain and the monitor that reports what it is delivering.

    The monitor kind is named because the query names it. Each fragment in
    `monitor_drift.gql` matches one kind, so a monitor of the wrong family
    cannot match at all and a match always carries a reading.
    """
    return {
        "__typename": kind,
        "name": attribute(name),
        configured_field: attribute(configured),
        "ports": edges(
            {"__typename": monitor_kind, "measured_gain_mdb": attribute(measured)},
        ),
    }


def _map_roadm(name: str, site: str) -> dict[str, Any]:
    """A ROADM as the map query returns it, with the site it stands in.

    The site arrives as a shortname on the `site` relationship, which is what
    the map keys its nodes on. The ROADM's own name says nothing about where it
    is and nothing reads it for that.
    """
    return {
        "__typename": "OtnRoadm",
        "name": attribute(name),
        "insertion_loss_mdb": attribute(7000),
        "site": one({"shortname": attribute(site)}),
    }


def _map_section(name: str = "oms-a", site_a: str = "ams", site_b: str = "bru") -> dict[str, Any]:
    """The standard section, re-ended on two sites the map can draw."""
    return {
        **_section(name),
        "roadm_a": one(_map_roadm("roadm-a", site_a)),
        "roadm_b": one(_map_roadm("roadm-b", site_b)),
    }


def _map_site(name: str, shortname: str, longitude: int, latitude: int, facility: str | None = None) -> dict[str, Any]:
    """One PoP with its coordinates, and its facility when it hosts one.

    The facility arrives on the `facility` edge and the matching `eurohpc-` tag
    arrives beside it, because the shipped dataset still carries both and a
    payload that dropped the tag would not agree with the server. Only the edge
    is read; `schemas/location.yml` records why the tag stopped being.
    """
    # Annotated because `dict` is invariant in its value type: an inferred
    # list[dict[str, dict[str, object]]] does not satisfy edges(*nodes:
    # dict[str, object]).
    tags: list[dict[str, object]] = (
        [{"name": attribute(f"eurohpc-{facility}"), "description": attribute("A facility.")}] if facility else []
    )
    return {
        "id": shortname,
        "__typename": "OtnSite",
        "name": attribute(name),
        "shortname": attribute(shortname),
        "site_type": attribute("pop"),
        "latitude_microdeg": attribute(latitude),
        "longitude_microdeg": attribute(longitude),
        "tags": edges(*tags),
        "facility": one({"name": attribute(facility)}) if facility else None,
    }


def _odu_section(name: str, site_a: str = "ams", site_b: str = "bru") -> dict[str, Any]:
    """A section as `odu_map.gql` returns it: a name and the two sites, nothing else.

    Leaner than `_map_section` on purpose. That query pulls spans, fibre types
    and both amplifier chains for a margin the ODU map states nothing about, and
    a payload carrying fields the query does not select is a payload that agrees
    with no server.
    """
    return {
        "id": name,
        "__typename": "OtnOpticalMultiplexSection",
        "name": attribute(name),
        "roadm_a": one(_map_roadm(f"{name}-roadm-a", site_a)),
        "roadm_b": one(_map_roadm(f"{name}-roadm-b", site_b)),
    }


def _odu_carrier(
    name: str,
    section: str,
    line_type: str = "ODUC4",
    clients: tuple[str, ...] = (),
    *,
    lit: bool = True,
) -> dict[str, Any]:
    """One carrier as `odu_map.gql` returns it, holding one line container.

    `clients` are the `odu_type` values of the children groomed into that line
    container, so a case is written as the shape it is testing: an ODU4 line with
    an ODU2 in it has 72 slots free and nothing else has to be worked out by
    hand.

    `lit=False` gives a carrier with no container at all. It is the dark
    wavelength, and it must contribute to no numerator and no denominator: read
    as zero free it makes an unprovisioned section look full, and as fully free it
    makes one look available.
    """
    children = [_container(f"odu-{name}-{index}", odu) for index, odu in enumerate(clients)]
    line: list[dict[str, object]] = (
        [_line_container(*children, name=f"odu-line-{name}", odu_type=line_type)] if lit else []
    )
    return {
        "id": name,
        "__typename": "OtnOpticalCarrier",
        "name": attribute(name),
        "status": attribute("active"),
        "sections": edges(named(section)),
        "containers": edges(*line),
    }


def _amsterdam() -> dict[str, Any]:
    return _map_site("Amsterdam", "ams", 4_895_168, 52_370_216)


def _brussels() -> dict[str, Any]:
    return _map_site("Brussels", "bru", 4_351_721, 50_850_346, "meluxina")


RICH: dict[str, dict[str, Any]] = {
    "service_trace": {"OtnService": edges(_service())},
    "impact_report": {
        "OtnOpticalMultiplexSection": edges(_section()),
        "OtnOpticalCarrier": edges(
            {
                **_carrier(),
                # Two services on one wavelength, which is what grooming
                # produces and what `optical_path` widened to cardinality many
                # for. Different profiles and different customers, so the report
                # has to split the two service lists and count two customers
                # rather than reading the first path as "the" service.
                "optical_path": edges(
                    _impact_path("svc-a"),
                    _impact_path("svc-b", customer="NREN-DE", profile="ip-transit"),
                ),
            },
            _carrier("oc-orphan", 9, with_path=False),
        ),
    },
    # The mode catalog is in here because the report's fit verdict is a
    # comparison of two widths. Without a mode it has nothing to compare a free
    # block against, and `SPARSE` below leaves it out on purpose to reach the
    # branch that says so rather than guessing.
    "capacity_view": {
        "OtnOpticalMode": edges(_mode()),
        "OtnOpticalMultiplexSection": edges(_section(), _section("oms-b", None)),
        "OtnOpticalCarrier": edges(_carrier(with_path=False)),
    },
    "reach_report": {
        "OtnOpticalMode": edges(_mode(), _mode("400ZR", 120_000)),
        "OtnOpticalMultiplexSection": edges(_section()),
    },
    "ai_latency": {"OtnService": edges(_service(), _service("svc-transit", "ip-transit", budget=None))},
    "monitor_drift": {
        "OtnAmplifier": edges(
            _monitored("amp-healthy", "OtnAmplifier", "OtnAmplifierMonitor", "gain_mdb", 22_000, 21_700),
            _monitored("amp-drooping", "OtnAmplifier", "OtnAmplifierMonitor", "gain_mdb", 22_000, 20_600),
        ),
        "OtnRamanPump": edges(
            _monitored("pump-a", "OtnRamanPump", "OtnRamanMonitor", "on_off_gain_mdb", 10_000, 9_700)
        ),
    },
    "srlg_exposure": {
        "OtnService": edges(_service(), _service("svc-b", "hpc-research")),
        "OtnConduit": edges(_conduit(), _conduit("cd-empty")),
    },
    "budget_report": {
        "OtnOpticalMultiplexSection": edges(_section()),
        "OtnOpticalCarrier": edges(_carrier(with_path=False)),
    },
    "network_map": {
        "focus_site": edges(_amsterdam()),
        "OtnSite": edges(_amsterdam(), _brussels()),
        "OtnOpticalMultiplexSection": edges(_map_section()),
        "OtnOpticalCarrier": edges(_carrier(with_path=False)),
        "OtnOpticalMode": edges(_mode()),
    },
    # Six sections, one per band plus both ways of having no figure. The bands
    # are earned by the slot arithmetic rather than asserted: an empty ODUC4 has
    # 320 free, an ODU4 holding an ODU2 has 72, one holding 32+32+8+2 has 6, and
    # one holding 32+32+8+8 has none. The last two are the pair that must not
    # collapse into each other: a carrier holding an unsized VC-4 is lit and its
    # figure is unknown, and a section with no carrier at all has no figure to
    # be unknown about.
    "odu_map": {
        "focus_site": edges(_amsterdam()),
        "OtnSite": edges(_amsterdam(), _brussels()),
        "OtnOpticalMultiplexSection": edges(
            _odu_section("oms-roomy"),
            _odu_section("oms-groomed"),
            _odu_section("oms-nearly"),
            _odu_section("oms-full"),
            _odu_section("oms-unsized"),
            _odu_section("oms-dark"),
        ),
        "OtnOpticalCarrier": edges(
            _odu_carrier("oc-roomy", "oms-roomy"),
            _odu_carrier("oc-groomed", "oms-groomed", "ODU4", ("ODU2",)),
            _odu_carrier("oc-nearly", "oms-nearly", "ODU4", ("ODU3", "ODU3", "ODU2", "ODU1")),
            _odu_carrier("oc-full", "oms-full", "ODU4", ("ODU3", "ODU3", "ODU2", "ODU2")),
            _odu_carrier("oc-unsized", "oms-unsized", "ODU4", ("VC-4",)),
            _odu_carrier("oc-dark", "oms-dark", lit=False),
        ),
    },
}
"""One populated payload per registered transform, keyed by **transform** name.

Not by query name. Four of the six differ (`impact_report` runs `span_impact`,
`capacity_view` runs `channel_occupancy`) and keying by the query is a mistake
that reads correctly and fails on the first lookup.
"""


def _sparse_section() -> dict[str, Any]:
    return {
        "id": "oms-bare",
        "__typename": "OtnOpticalMultiplexSection",
        "name": attribute("oms-bare"),
        "description": attribute(None),
        "roadm_a": one(_roadm("roadm-a")),
        "roadm_b": one(_roadm("roadm-b")),
        "spans": edges(_span("span-bare", 1, 80_000, None)),
        "amplifiers": edges(),
    }


def _sparse_service() -> dict[str, Any]:
    return {
        "id": "svc-bare",
        "__typename": "OtnService",
        "name": attribute("svc-bare"),
        "description": attribute(None),
        "customer": attribute("NREN-XX"),
        "rate_gbps": attribute(100),
        "sla": attribute("best_effort"),
        "status": attribute("rejected"),
        "service_profile": attribute("ai-inference"),
        "max_latency_ns": attribute(None),
        "rejection_code": attribute("no-route"),
        "rejection_detail": attribute("nothing connects those two"),
        "endpoint_a": one({"id": "rtr-a", "name": attribute("rtr-a"), "site": one(None), "ports": edges()}),
        "endpoint_z": one({"id": "rtr-z", "name": attribute("rtr-z"), "site": one(None), "ports": edges()}),
        # Empty, not a null peer. A refused service carries no path at all, and
        # on a cardinality-many relationship that is a collection of nothing.
        "optical_path": edges(),
    }


SPARSE: dict[str, dict[str, Any]] = {
    "service_trace": {"OtnService": edges(_sparse_service())},
    "impact_report": {"OtnOpticalMultiplexSection": edges(_sparse_section()), "OtnOpticalCarrier": edges()},
    "capacity_view": {"OtnOpticalMultiplexSection": edges(_sparse_section()), "OtnOpticalCarrier": edges()},
    "reach_report": {
        "OtnOpticalMode": edges(_mode("400ZR", 120_000)),
        "OtnOpticalMultiplexSection": edges(_sparse_section()),
    },
    "ai_latency": {"OtnService": edges(_sparse_service())},
    "monitor_drift": {"OtnAmplifier": edges(), "OtnRamanPump": edges()},
    "srlg_exposure": {"OtnService": edges(_sparse_service()), "OtnConduit": edges()},
    "budget_report": {"OtnOpticalMultiplexSection": edges(_sparse_section()), "OtnOpticalCarrier": edges()},
    # One site, no plant and no wavelengths. The reference mode stays, because
    # a route colour is a margin against a named mode and a catalog without it
    # is an error rather than a grey map; `reach_report` keeps a mode for the
    # same reason. What this exercises is the isolated node: a PoP no section
    # touches still gets drawn, at degree zero.
    "network_map": {
        "focus_site": edges(_amsterdam()),
        "OtnSite": edges(_amsterdam()),
        "OtnOpticalMultiplexSection": edges(),
        "OtnOpticalCarrier": edges(),
        "OtnOpticalMode": edges(_mode()),
    },
    # One route and one dark wavelength on it, which is the branch FR-019 is
    # about: every route unknown, under a caption naming the branch. It is a
    # successful render and not an error, so the sparse payload keeps the section
    # rather than emptying the list as `network_map` does.
    "odu_map": {
        "focus_site": edges(_amsterdam()),
        "OtnSite": edges(_amsterdam(), _brussels()),
        "OtnOpticalMultiplexSection": edges(_odu_section("oms-bare")),
        "OtnOpticalCarrier": edges(_odu_carrier("oc-bare", "oms-bare", lit=False)),
    },
}
"""The same transforms, with every optional peer null and every list empty."""


CHAINED: dict[str, dict[str, Any]] = {
    "service_trace": {"OtnService": edges(_chained_service())},
    # `svc-a` is in `cd-north` and so is the chain's **second** segment, while
    # its first is alone in `cd-iberia`. That asymmetry is the test: a report
    # reading only the lowest segment finds no shared duct at all and answers
    # that the two services are diverse.
    "srlg_exposure": {
        "OtnService": edges(_service(), _chained_service()),
        "OtnConduit": edges(_conduit(), _conduit("cd-iberia"), _conduit("cd-empty")),
    },
    # The cut section carries `oc-svc-a`, which is segment 1 of the chain.
    # `oc-fra-waw` is segment 2 and is deliberately not in this payload's carrier
    # list, because it is on another section: that is what makes it a surviving
    # segment rather than a second casualty.
    "impact_report": {
        "OtnOpticalMultiplexSection": edges(_section()),
        "OtnOpticalCarrier": edges(
            {
                **_carrier(),
                "optical_path": edges(
                    _impact_path(
                        "svc-chain",
                        segments=(_impact_segment(1, "oc-svc-a"), _impact_segment(2, "oc-fra-waw")),
                    ),
                    _impact_path("svc-plain", customer="NREN-DE", profile="ip-transit"),
                ),
            }
        ),
    },
}
"""The three reports of US2, each given a circuit that rides two wavelengths.

**These are the assertions that cover the multi-segment path, and only these.**
No chained circuit exists on any branch: every one of the 71 shipped wavelengths
crosses `oms-fra-mil` and a cover may not repeat a section, so no pair of them
can form a chain, and the Madrid-to-Warsaw scenario provisions its two carriers
later in this feature. A live dry-run therefore proves the single-segment path is
unbroken and cannot reach the chain at all. Fixtures are what reach it.
"""


def _registered() -> list[tuple[str, str, str]]:
    return [(entry.name, str(entry.file_path), entry.class_name) for entry in CONFIG.python_transforms]


# ---------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------


def test_every_registered_transform_has_a_payload_here() -> None:
    """The reason the parametrisation reads `.infrahub.yml`.

    A transform registered without a payload in this module would otherwise be
    silently untested.
    """
    registered = {name for name, _, _ in _registered()}
    assert registered <= set(RICH), f"no populated payload for {sorted(registered - set(RICH))}"
    assert registered <= set(SPARSE), f"no sparse payload for {sorted(registered - set(SPARSE))}"


@pytest.mark.parametrize(("name", "file_path", "class_name"), _registered())
def test_a_populated_payload_transforms(name: str, file_path: str, class_name: str) -> None:
    result = _run(file_path, class_name, RICH[name])
    assert isinstance(result, (dict, str))
    assert result


@pytest.mark.parametrize(("name", "file_path", "class_name"), _registered())
def test_a_sparse_payload_transforms(name: str, file_path: str, class_name: str) -> None:
    """Nulls everywhere. This is the one that catches the unwrapping bug."""
    result = _run(file_path, class_name, SPARSE[name])
    assert isinstance(result, (dict, str))


@pytest.mark.parametrize(("name", "file_path", "class_name"), _registered())
def test_every_report_names_the_branch_it_read(name: str, file_path: str, class_name: str) -> None:
    """A capacity or impact claim is a property of a branch, not of the model,
    so every report has to say which branch it read."""
    if name == "budget_report":
        pytest.skip("the budget report's output shape is pinned by the demo guide")
    if name in {"network_map", "odu_map"}:
        # An SVG has no keys, so both maps name their branch in the footer text
        # instead. The claim the test makes is the same one: a reader can tell
        # which branch the figures came from without being told. It matters more
        # on the ODU map, where a free-slot count is a capacity claim and one
        # with no branch on it is a capacity claim with no date on it.
        assert BRANCH in _run(file_path, class_name, RICH[name])
        return
    assert _run(file_path, class_name, RICH[name])["branch"] == BRANCH


# ---------------------------------------------------------------------------
# The failures that must stay failures
# ---------------------------------------------------------------------------


def _entry(name: str) -> tuple[str, str]:
    entry = next(item for item in CONFIG.python_transforms if item.name == name)
    return str(entry.file_path), entry.class_name


def test_a_section_that_does_not_exist_is_an_error_not_an_empty_report() -> None:
    """The single most important assertion in this file.

    Sixteen of the twenty-one sections in the shipped dataset carry no
    wavelength, so an empty impact report is a common and correct answer. An
    operator who mistypes a section name and is told "no impact" stops looking.
    """
    file_path, class_name = _entry("impact_report")
    with pytest.raises(ValueError, match="section"):
        _run(file_path, class_name, {"OtnOpticalMultiplexSection": edges(), "OtnOpticalCarrier": edges()})


def test_a_service_that_does_not_exist_is_an_error_not_an_empty_trace() -> None:
    file_path, class_name = _entry("service_trace")
    with pytest.raises(ValueError, match="service"):
        _run(file_path, class_name, {"OtnService": edges()})


def test_two_services_matching_one_trace_is_an_error() -> None:
    file_path, class_name = _entry("service_trace")
    with pytest.raises(ValueError, match="exactly one"):
        _run(file_path, class_name, {"OtnService": edges(_service(), _service("svc-b"))})


def test_a_branch_with_no_plant_cannot_be_asked_about_capacity() -> None:
    file_path, class_name = _entry("capacity_view")
    with pytest.raises(ValueError, match="no optical multiplex section"):
        _run(file_path, class_name, {"OtnOpticalMultiplexSection": edges(), "OtnOpticalCarrier": edges()})


def test_a_branch_with_no_plant_cannot_be_asked_about_reach() -> None:
    file_path, class_name = _entry("reach_report")
    with pytest.raises(ValueError, match="no optical multiplex section"):
        _run(file_path, class_name, {"OtnOpticalMode": edges(_mode()), "OtnOpticalMultiplexSection": edges()})


# ---------------------------------------------------------------------------
# The answers, not only the absence of an exception
# ---------------------------------------------------------------------------


def test_the_reach_report_leads_with_the_mode_that_reaches_nothing() -> None:
    file_path, class_name = _entry("reach_report")
    result = _run(file_path, class_name, RICH["reach_report"])
    assert result["unusable_modes"] == ["400ZR"]
    assert "400ZR" in result["headline"]
    assert "reach nothing" in result["headline"]


def test_the_capacity_view_answers_the_route_question_and_the_section_question() -> None:
    """Spectrum, not anchors. One 64 GBd carrier holds 79,600 MHz of 4,800,000.

    The figures are derived rather than typed: the carrier anchors on channel 7,
    `units.occupied_width_mhz` puts a 64 GBd mode at 79,600 MHz, and the free
    spectrum is the band either side of it, so the section reports two free
    blocks rather than a count of ninety-five channels.
    """
    file_path, class_name = _entry("capacity_view")
    result = _run(file_path, class_name, RICH["capacity_view"])
    busiest = result["sections"][0]
    assert busiest["section"] == "oms-a"
    assert busiest["occupied_mhz"] == 79_600
    assert busiest["free_mhz"] == CBAND_EXTENT_MHZ - 79_600
    assert busiest["free_block_count"] == 2
    assert sum(block["width_mhz"] for block in busiest["free_blocks"]) == busiest["free_mhz"]
    assert busiest["carriers"][0]["width_mhz"] == 79_600
    assert busiest["another_400g_fits"]
    assert result["empty_sections"] == ["oms-b"]
    assert result["mode_asked_about"]["occupied_width_mhz"] == 79_600
    assert "upper bound" in result["upper_bound_note"]
    assert "quantised" in result["quantisation_note"]
    assert any(not route["resolvable"] for route in result["routes"])


def test_the_capacity_view_calls_two_carriers_contested_when_their_spectrum_overlaps() -> None:
    """Different anchors, one collision, which the old report could not see.

    Channel 7 at 128 GBd occupies 150,000 MHz and reaches from 191,575,000 to
    191,725,000. Channel 8 at 64 GBd occupies 79,600 and starts at 191,660,200.
    They share 64,800 MHz. Neither claims the other's channel number, so the
    report that counted anchors called this section clean.
    """
    file_path, class_name = _entry("capacity_view")
    payload = {
        "OtnOpticalMode": edges(_mode()),
        "OtnOpticalMultiplexSection": edges(_section()),
        "OtnOpticalCarrier": edges(
            _carrier("oc-wide", 7, with_path=False, mode=_mode("DP-QPSK 128GBd 400G", baud=128_000)),
            _carrier("oc-narrow", 8, with_path=False),
        ),
    }
    result = _run(file_path, class_name, payload)

    contested = result["contested"]
    assert [row["section"] for row in contested] == ["oms-a"]
    overlap = contested[0]["overlaps"][0]
    assert sorted(overlap["carriers"]) == ["oc-narrow", "oc-wide"]
    assert overlap["channels"] != [overlap["channels"][0]] * 2, "the two carriers must be on different anchors"
    assert overlap["width_mhz"] == 64_800
    # The note still names the check that blocks it, which stayed true.
    assert "channel_collision" in result["contested_note"]


def test_the_capacity_view_reports_free_spectrum_as_blocks_with_edges_and_widths() -> None:
    """Blocks with edges, not a count of free channels.

    One 64 GBd carrier on channel 7 occupies 79,600 MHz centred on
    191,650,000, so the free spectrum is the two runs either side of it and the
    report has to name where each one starts and stops. A count cannot: 95
    unclaimed anchors and a 4,435,200 MHz run at the top of the band are
    different facts, and only the second one tells a planner where to put a
    wavelength.
    """
    file_path, class_name = _entry("capacity_view")
    result = _run(file_path, class_name, RICH["capacity_view"])
    busiest = result["sections"][0]
    centre = channel_to_frequency_mhz(7)

    assert busiest["free_blocks"] == [
        {
            "lower_mhz": CBAND_LOWER_EDGE_MHZ,
            "upper_mhz": centre - 39_800,
            "width_mhz": centre - 39_800 - CBAND_LOWER_EDGE_MHZ,
        },
        {
            "lower_mhz": centre + 39_800,
            "upper_mhz": CBAND_UPPER_EDGE_MHZ,
            "width_mhz": CBAND_UPPER_EDGE_MHZ - centre - 39_800,
        },
    ]
    assert busiest["widest_free_block_mhz"] == max(block["width_mhz"] for block in busiest["free_blocks"])
    assert sum(block["width_mhz"] for block in busiest["free_blocks"]) == CBAND_EXTENT_MHZ - 79_600
    # Ascending by lower edge, which is the order a spectrum plan is read in.
    assert busiest["free_blocks"][0]["upper_mhz"] < busiest["free_blocks"][1]["lower_mhz"]
    # And no count of free channels survives anywhere in the row.
    assert "free_channels" not in busiest
    assert "lowest_free_channel" not in busiest


def _packed_band_payload() -> dict[str, Any]:
    """A section packed with 128 GBd carriers, holding one 52,800 MHz hole either side of a narrow one.

    Channels 2 to 95 in steps of three, which is exactly 150,000 MHz apart, so
    thirty-one 128 GBd carriers tile the band edge to edge with channel 47 left
    out. A 32 GBd carrier then takes the middle 44,400 MHz of that 150,000 MHz
    hole, leaving 52,800 MHz on each side.

    That figure is the whole point of the fixture: 52,800 is wider than the
    44,400 MHz a 32 GBd carrier occupies and narrower than the 79,600 a 64 GBd
    one does, so one mode in the catalog fits and two do not, on one section, in
    one run.
    """
    narrow = _mode("DP-QPSK 32GBd 100G", baud=32_000)
    wide = _mode("DP-QPSK 128GBd 400G", baud=128_000)
    carriers = [
        _carrier(f"oc-ch{channel:03d}", channel, with_path=False, mode=wide)
        for channel in range(2, 96, 3)
        if channel != 47
    ]
    carriers.append(_carrier("oc-ch047", 47, with_path=False, mode=narrow))
    return {
        "OtnOpticalMode": edges(narrow, _mode(), wide),
        "OtnOpticalMultiplexSection": edges(_section()),
        "OtnOpticalCarrier": edges(*carriers),
    }


def test_the_capacity_view_says_which_modes_do_not_fit_rather_than_omitting_them() -> None:
    """Three modes, one fits and two do not, and all three get a row.

    This is the answer the channel-counting report could never give. 105,600 MHz
    is free on this section, which is more than the 79,600 a 64 GBd carrier
    occupies and two thirds of what a 128 GBd one does, and neither can be
    provisioned: the free spectrum is two 52,800 MHz slivers and a carrier has to
    fit inside one block, not across two.

    The assertion that matters is the last pair. A report that dropped the modes
    that do not fit would still pass every other line here, and would tell an
    operator that a 64 GBd carrier had never been considered rather than that it
    was considered and refused.
    """
    file_path, class_name = _entry("capacity_view")
    result = _run(file_path, class_name, _packed_band_payload())
    section = result["sections"][0]

    assert section["section"] == "oms-a"
    assert section["free_mhz"] == 105_600
    assert section["free_block_count"] == 2
    assert section["widest_free_block_mhz"] == 52_800

    # Every catalog mode, narrowest occupied width first, and none dropped.
    fits = section["mode_fit"]
    assert [row["mode"] for row in fits] == [
        "DP-QPSK 32GBd 100G",
        "DP-16QAM 64GBd 400G",
        "DP-QPSK 128GBd 400G",
    ]
    assert [row["occupied_width_mhz"] for row in fits] == [44_400, 79_600, 150_000]
    assert [row["fits"] for row in fits] == [True, False, False]

    # The one that fits says where, on both sides of the narrow carrier.
    assert fits[0]["anchor_count"] == 2
    assert fits[0]["lowest_anchor"] == 46
    assert "Fits on this section" in fits[0]["verdict"]

    # The two that do not say so, name their own width against the widest block,
    # and are reported as a spectrum problem rather than a fragmentation one.
    for row in fits[1:]:
        assert row["reason"] == "too-narrow"
        assert row["anchor_count"] == 0
        assert row["lowest_anchor"] is None
        assert "Does not fit on this section" in row["verdict"]
        assert f"{row['occupied_width_mhz']:,} MHz" in row["verdict"]
        assert "52,800 MHz" in row["verdict"]

    assert result["mode_catalog_size"] == 3
    assert result["modes_that_fit_nowhere"] == ["DP-16QAM 64GBd 400G", "DP-QPSK 128GBd 400G"]
    assert result["modes_blocked_on_busiest_section"] == result["modes_that_fit_nowhere"]
    assert "2 of the 3 catalog modes fit nowhere on it" in result["headline"]
    assert "fits nowhere" in result["mode_fit_note"]


def test_the_impact_report_counts_a_wavelength_with_no_service_behind_it() -> None:
    """Two carriers, two services on one of them and nothing on the other.

    `service_count` is two rather than one now, and the reason is the point of
    the test rather than a renumbering: one wavelength carries both services
    because grooming packed them into it, so counting one service per carrier
    would under-report an outage by however many clients shared the wavelength.
    """
    file_path, class_name = _entry("impact_report")
    result = _run(file_path, class_name, RICH["impact_report"])
    assert result["wavelength_count"] == 2
    assert result["service_count"] == 2
    assert result["customer_count"] == 2
    assert [item["carrier"] for item in result["unattached_wavelengths"]] == ["oc-orphan"]
    assert result["capacity_lost_display"] == "800 Gbps"
    assert [row["service"] for row in result["latency_sensitive_services"]] == ["svc-a"]
    assert [row["service"] for row in result["other_services"]] == ["svc-b"]
    shared = next(item for item in result["wavelengths"] if item["carrier"] == "oc-svc-a")
    assert shared["services"] == ["svc-a", "svc-b"]
    assert shared["customers"] == ["EuroHPC-Test", "NREN-DE"]


def test_the_impact_report_carries_the_interval_beside_the_anchor() -> None:
    """A cut is described in channels on the call and in megahertz in the plan.

    Channel 7 is 191,650,000 MHz and a 64 GBd carrier occupies 79,600 MHz around
    it, so the row has to say both. The width and the edges come from `units.py`,
    which is what the collision check reads, so the spectrum this report says is
    freed is the spectrum the check will let a replacement wavelength into.
    """
    file_path, class_name = _entry("impact_report")
    result = _run(file_path, class_name, RICH["impact_report"])
    row = next(item for item in result["wavelengths"] if item["carrier"] == "oc-svc-a")

    assert row["channel"] == 7
    assert row["occupied_width_mhz"] == 79_600
    assert row["lower_edge_mhz"] == channel_to_frequency_mhz(7) - 39_800
    assert row["upper_edge_mhz"] == row["lower_edge_mhz"] + 79_600
    assert row["upper_edge_mhz"] - row["lower_edge_mhz"] == row["occupied_width_mhz"]
    assert row["interval_display"] == "191.61020 THz to 191.68980 THz"
    assert result["unattached_wavelengths"][0]["occupied_width_mhz"] == 79_600


def test_the_latency_report_excludes_the_profiles_with_no_budget_and_says_how_many() -> None:
    file_path, class_name = _entry("ai_latency")
    result = _run(file_path, class_name, RICH["ai_latency"])
    assert result["reported_count"] == 1
    assert result["excluded_service_count"] == 1
    assert result["services"][0]["service"] == "svc-a"
    assert result["services"][0]["electronics_share_percent"] < 100.0


def test_the_latency_report_names_a_refused_service_by_code_and_detail_apart() -> None:
    """FR-017 on the second of the two transforms that read the verdict.

    The `unprovisioned` list is the only place `ai_latency` touches the refusal,
    and nothing asserted on it until now, which is why this transform could
    read a deleted attribute and still pass its suite. The assertion is on both
    keys and on the old one being absent.
    """
    file_path, class_name = _entry("ai_latency")
    result = _run(file_path, class_name, SPARSE["ai_latency"])
    row = next(item for item in result["unprovisioned"] if item["service"] == "svc-bare")
    assert row["status"] == "rejected"
    assert row["rejection_code"] == "no-route"
    assert row["rejection_detail"] == "nothing connects those two"
    assert "rejection_reason" not in row


def test_the_exposure_report_pairs_two_services_in_one_duct_at_high_severity() -> None:
    file_path, class_name = _entry("srlg_exposure")
    result = _run(file_path, class_name, RICH["srlg_exposure"])
    assert result["pair_count"] == 1
    assert result["pairs"][0]["shared_conduits"] == ["cd-north"]
    assert result["pairs"][0]["severity"] == "high"
    assert result["empty_conduits"] == ["cd-empty"]


def test_the_trace_of_a_refused_service_reports_the_refusal_as_two_fields() -> None:
    """FR-017. The code and the detail arrive apart and neither is a parse.

    `rejection_reason` is gone from the schema, so a transform still reading it
    emits `None` and the trace shows a refused service with no reason on it.
    The assertion is on both keys and on the absence of the old one, because
    the failure this guards is silent: the payload still renders.
    """
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, SPARSE["service_trace"])
    assert result["provisioned"] is False
    assert result["rejection_code"] == "no-route"
    assert result["rejection_detail"] == "nothing connects those two"
    assert "rejection_reason" not in result


def test_the_two_queries_that_read_the_verdict_still_select_both_of_its_fields() -> None:
    """The shape assertion the two tests above cannot make, and the one that was missing.

    `_service` and `_sparse_service` are built by hand, so they carry
    `rejection_code` and `rejection_detail` whatever the `.gql` files select.
    Delete either field from either query and both refusal tests stay green while
    the live transform emits `None` on it. That is not hypothetical: it is exactly
    how this feature shipped a transform reading a deleted attribute once already.

    `tests/unit/test_checks.py` guards `provisionable.gql` the same way. The
    absence of `rejection_reason` is asserted too, because a query that grew the
    free-text field back would be reading something the schema no longer has.
    """
    for query in ("service_trace", "service_latency"):
        document = (REPO_ROOT / "queries" / f"{query}.gql").read_text()
        for field in ("rejection_code", "rejection_detail"):
            assert f"{field} {{" in document, f"{query}.gql stopped selecting {field}"
        assert "rejection_reason" not in document, f"{query}.gql went back to the free-text field"


def test_the_trace_says_the_router_port_assignment_is_not_modelled() -> None:
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, RICH["service_trace"])
    assert result["provisioned"] is True
    assert result["hop_count"] == 4
    assert result["conduits"] == ["cd-north"]
    assert all("not modelled" in endpoint["port_assignment"] for endpoint in result["endpoints"])


# ---------------------------------------------------------------------------
# The parent hop
# ---------------------------------------------------------------------------
#
# FR-023, and the one failure in this feature that raises nothing. Before
# grooming, a client container hung directly off its carrier and carried the
# client signal, so `carrier.containers` returned it and reading `client_signal`
# off that worked. Grooming nests the client under a line container, and
# `carrier.containers` now returns the line container, which has no
# `client_signal`. A transform that keeps reading the signal off it gets nothing
# rather than an error: the trace still renders, it renders a service with no
# client, and it looks fine.
#
# Every payload above is already shaped that way, so the smoke tests exercise the
# hop. What these add is the assertion that the value arrives, which is what
# fails when a transform stops following the hop.


def _flat_carrier() -> dict[str, Any]:
    """A carrier holding a client container with no line container above it.

    Container data written before this feature, which the spec requires be read
    as a container sitting on its own carrier with nothing invented above it.
    """
    return {**_carrier("oc-legacy", 11, with_path=False), "containers": edges(_container("odu-legacy"))}


def test_the_trace_follows_the_parent_hop_to_the_client_signal() -> None:
    """T019 for `service_trace`. The client is a grandchild of the carrier now."""
    file_path, class_name = _entry("service_trace")
    rows = _run(file_path, class_name, RICH["service_trace"])["carrier"]["containers"]
    assert len(rows) == 1, "one client is groomed into the wavelength, so one row"
    assert rows[0]["name"] == "odu-svc-a"
    assert rows[0]["line_container"] == "odu-line-oc-svc-a"
    assert rows[0]["client_signal"] == "400GBASE-FR4"
    assert rows[0]["client_layer"] == "ethernet"


def test_the_impact_report_follows_the_parent_hop_to_the_client_signal() -> None:
    """T019 for `impact_report`. The signal census is what goes empty otherwise."""
    file_path, class_name = _entry("impact_report")
    result = _run(file_path, class_name, RICH["impact_report"])
    assert result["client_signals"] == {"400GBASE-FR4": 2}, "one client on each of the two carriers"
    rows = result["wavelengths"][0]["containers"]
    assert rows[0]["line_container"] == "odu-line-oc-svc-a"
    assert rows[0]["client_signal"] == "400GBASE-FR4"


def test_a_container_written_before_grooming_is_read_on_its_own_carrier() -> None:
    """The other half of the walk, and the reason it is not a blind child lookup.

    A client container that predates grooming sits directly under the carrier and
    carries its own signal. It is reported with no line container above it rather
    than dropped, and rather than having a parent invented for it.
    """
    payload = {
        "OtnOpticalMultiplexSection": edges(_section()),
        "OtnOpticalCarrier": edges(_flat_carrier()),
    }
    file_path, class_name = _entry("impact_report")
    result = _run(file_path, class_name, payload)
    assert result["client_signals"] == {"400GBASE-FR4": 1}
    rows = result["wavelengths"][0]["containers"]
    assert rows[0]["name"] == "odu-legacy"
    assert rows[0]["line_container"] is None
    assert rows[0]["client_signal"] == "400GBASE-FR4"


def test_an_empty_line_container_reports_no_client_and_raises_nothing() -> None:
    """A lit wavelength with nothing groomed into it, which is the base dataset.

    Every pre-provisioned carrier arrives carrying an empty line container, so
    this is the common case rather than an edge one. The right answer is no
    client rows, not a raise and not a row naming the line container as if it
    were a client.
    """
    payload = {
        "OtnOpticalMultiplexSection": edges(_section()),
        "OtnOpticalCarrier": edges(
            {**_carrier("oc-empty", 13, with_path=False), "containers": edges(_line_container(name="odu-line-empty"))}
        ),
    }
    file_path, class_name = _entry("impact_report")
    result = _run(file_path, class_name, payload)
    assert result["client_signals"] == {}
    assert result["wavelengths"][0]["containers"] == []


# ---------------------------------------------------------------------------
# One circuit, not one wavelength
# ---------------------------------------------------------------------------
#
# 016 D-003, and the assertion the naming convention could never support.
# `OtnContainer` had no relationship to a service, so the only link was the name
# `odu-<service>` and nothing read it. `impact.client_containers` therefore
# returns every client on a wavelength: exactly right for an impact report, whose
# question is who is affected by a cut, and wrong for a trace. Once two services
# groom into one wavelength, tracing one listed the other's container as part of
# its circuit, silently, and the better grooming worked the worse it read.
#
# The walk stays shared, decided in commit `bebf2a0`, and the filter sits at the
# `service_trace` call site. The three tests below are the pair of answers that
# says so: one payload, one row for the trace and two for the impact report.

GROOMED_PAIR = (_container("odu-svc-a", service="svc-a"), _container("odu-svc-b", service="svc-b"))
"""Two client containers on one line container, owned by two different services."""


def test_a_shared_wavelength_traces_one_container_for_the_traced_service() -> None:
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, {"OtnService": edges(_service("svc-a", clients=GROOMED_PAIR))})
    rows = result["carrier"]["containers"]
    assert [row["name"] for row in rows] == ["odu-svc-a"], "two clients on the wavelength, one of them is svc-a's"
    assert rows[0]["line_container"] == "odu-line-oc-svc-a"


def test_the_other_service_on_that_wavelength_traces_its_own_container() -> None:
    """The half that a name-prefix filter would also have passed, on its own.

    Run without the test above it, an assertion that svc-b sees one row says
    nothing: a trace that dropped every container would pass it too. The pair is
    the evidence, and each row has to be the right one.
    """
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, {"OtnService": edges(_service("svc-b", clients=GROOMED_PAIR))})
    assert [row["name"] for row in result["carrier"]["containers"]] == ["odu-svc-b"]


def test_the_impact_report_still_counts_every_client_on_that_wavelength() -> None:
    """The other side of the same walk, and the reason the filter is not in it.

    A cut takes both circuits down. An impact report narrowed the way the trace
    is narrowed would name one customer of two, which is how an outage call goes
    wrong. `demo/90_fra_mil_saturated.yml`'s unowned `odu-fill-*` clients are the
    third row here for the same reason: they occupy real slots, no service claims
    them, and the report still counts them.
    """
    carrier = {
        **_carrier(),
        "containers": edges(_line_container(*GROOMED_PAIR, _container("odu-fill-a", service=None))),
        "optical_path": edges(_impact_path("svc-a"), _impact_path("svc-b", customer="NREN-DE")),
    }
    payload = {
        "OtnOpticalMultiplexSection": edges(_section()),
        "OtnOpticalCarrier": edges(carrier),
    }
    file_path, class_name = _entry("impact_report")
    result = _run(file_path, class_name, payload)
    rows = result["wavelengths"][0]["containers"]
    assert [row["name"] for row in rows] == ["odu-fill-a", "odu-svc-a", "odu-svc-b"]
    assert result["client_signals"] == {"400GBASE-FR4": 3}


# ---------------------------------------------------------------------------
# The unmigrated side of the same filter
# ---------------------------------------------------------------------------
#
# T063, and the failure the filter above introduced pointed the other way.
# `OtnContainer.service` is optional and arrived with the 016 schema, so every
# container written before that load answers null and a filter on it keeps
# nothing. The trace then renders a provisioned service with an empty container
# list, which reads as a circuit with no client rather than as a report that
# could not tell. Observed live: loading the schema onto the `demo` branch made
# both services trace nothing until the generator was re-run.
#
# So the trace falls back to the unfiltered list and labels it. The pair below is
# the evidence that both halves are live: one wavelength whose containers all
# name a service filters and says so, one whose containers name none falls back
# and says so. Neither passes on its own.


def test_a_wavelength_whose_containers_name_their_service_is_filtered_and_says_so() -> None:
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, {"OtnService": edges(_service("svc-a", clients=GROOMED_PAIR))})
    carrier = result["carrier"]
    assert [row["name"] for row in carrier["containers"]] == ["odu-svc-a"]
    assert carrier["containers_filtered"] is True
    assert carrier["containers_note"] is None


def test_a_wavelength_whose_containers_predate_the_relationship_falls_back_labelled() -> None:
    """Pre-migration data reads as pre-migration data, not as a circuit of one.

    Both clients answer `service = null`, which is every container on any branch
    written before the 016 schema load. Filtering keeps neither, and an empty
    container list on a provisioned service is the silent wrong answer this test
    exists to keep out. The rows come back unfiltered, and `containers_note`
    tells the reader that is what they are looking at.
    """
    unmigrated = (_container("odu-svc-a", service=None), _container("odu-svc-b", service=None))
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, {"OtnService": edges(_service("svc-a", clients=unmigrated))})
    carrier = result["carrier"]
    assert [row["name"] for row in carrier["containers"]] == ["odu-svc-a", "odu-svc-b"]
    assert carrier["containers_filtered"] is False
    assert "records the service that owns it" in carrier["containers_note"]


# ---------------------------------------------------------------------------
# Two directions, and the Raman credit
# ---------------------------------------------------------------------------


def _pumped_payload() -> dict[str, Any]:
    """One section whose first span is pumped `a_to_b` and nothing else.

    Asymmetry has to be built in on purpose. Every ROADM in these payloads
    carries the same insertion loss and every amplifier the same noise figure
    and gain, so an unpumped section budgets identically both ways and a test
    written against it would pass whichever direction the report happened to
    pick.
    """
    section = {
        **_section(),
        "spans": edges(
            _span("span-1", 1, 80_000, "cd-north", (_pump("pump-span-1"),)),
            _span("span-2", 2, 80_000, None),
        ),
    }
    return {
        "OtnOpticalMultiplexSection": edges(section),
        "OtnOpticalCarrier": edges(_carrier(with_path=False)),
    }


def test_the_budget_report_headlines_the_worse_of_the_two_directions() -> None:
    """A wavelength is a two-way service and it is only as good as its weaker
    direction. Reporting the walk that happens to start at the lexicographically
    smaller ROADM would publish the pumped direction's margin for a service the
    unpumped direction limits."""
    file_path, class_name = _entry("budget_report")
    report = _run(file_path, class_name, _pumped_payload())["carriers"][0]

    both = report["directions"]
    assert len(both) == 2
    assert {row["from"] for row in both} == {"roadm-a", "roadm-b"}
    assert both[0]["osnr_margin_mdb"] != both[1]["osnr_margin_mdb"], both

    worse = min(row["osnr_margin_mdb"] for row in both)
    assert report["direction"]["osnr_margin_mdb"] == worse
    assert report["verdict"]["osnr_margin_mdb"] == worse


def test_the_pumped_direction_is_the_better_one_and_it_is_not_the_one_reported() -> None:
    """The pump points `a_to_b`, so the walk from `roadm-a` is credited its gain
    and the walk back is charged the combiner loss with nothing to show for it.
    The report has to name the second one."""
    file_path, class_name = _entry("budget_report")
    report = _run(file_path, class_name, _pumped_payload())["carriers"][0]
    forward, reverse = report["directions"]
    assert forward["from"] == "roadm-a"
    assert forward["osnr_margin_mdb"] > reverse["osnr_margin_mdb"]
    assert report["direction"]["from"] == "roadm-b"


def test_only_the_pumped_span_is_listed_in_the_raman_block() -> None:
    """Raman ships on nine spans of a hundred and thirty-two. A row of zeros
    over the other hundred and twenty-three reads as a broken column, so an
    unpumped span is absent rather than listed empty."""
    file_path, class_name = _entry("budget_report")
    report = _run(file_path, class_name, _pumped_payload())["carriers"][0]
    assert [row["span"] for row in report["raman_spans"]] == ["span-1"]
    assert all("raman_gain_mdb" not in hop for hop in report["hops"] if hop["kind"] != "span")
    assert "raman_gain_mdb" not in next(hop for hop in report["hops"] if hop["name"] == "span-2")


def test_the_hop_row_explains_the_combiner_loss_on_the_direction_it_credits_nothing() -> None:
    """The reported walk is the unpumped one, and it still pays the combiner.

    Without this the span would show half a decibel more loss than its
    neighbours and carry nothing to say why, which reads as a modelling error
    on the one row an operator is most likely to query.
    """
    file_path, class_name = _entry("budget_report")
    report = _run(file_path, class_name, _pumped_payload())["carriers"][0]
    row = next(hop for hop in report["hops"] if hop["name"] == "span-1")
    assert row["raman_gain_mdb"] == 0
    assert row["pump_loss_mdb"] == 800
    assert row["loss_mdb"] - row["unpumped_loss_mdb"] == 800
    assert "serves the other direction" in row["raman_note"]


def test_the_raman_block_carries_both_directions_because_the_headline_cannot() -> None:
    """The reported direction is the worse one, and for a span pumped one way
    only that is the direction with no Raman on it. A report that showed only
    the reported walk would show a Raman span with no Raman on it."""
    file_path, class_name = _entry("budget_report")
    report = _run(file_path, class_name, _pumped_payload())["carriers"][0]
    ways = {row["towards"]: row for row in report["raman_spans"][0]["directions"]}
    assert set(ways) == {"roadm-a", "roadm-b"}
    assert ways["roadm-b"]["raman_gain_mdb"] == 8_000
    assert ways["roadm-a"]["raman_gain_mdb"] == 0
    assert report["direction"]["osnr_margin_mdb"] < max(row["osnr_margin_mdb"] for row in report["directions"])


def test_the_raman_block_shows_what_the_span_would_have_cost_unpumped() -> None:
    """The credit only means something next to the figure it improved on.

    Pumped, the span is better off by the on-off gain less the combiner loss.
    The opposite direction pays the combiner and is credited nothing, so it is
    worse by the combiner loss alone. That is the identity the live read-back
    checks against the server.
    """
    file_path, class_name = _entry("budget_report")
    report = _run(file_path, class_name, _pumped_payload())["carriers"][0]
    ways = {row["towards"]: row for row in report["raman_spans"][0]["directions"]}

    pumped, unpumped = ways["roadm-b"], ways["roadm-a"]
    assert pumped["unpumped_loss_mdb"] - pumped["effective_loss_mdb"] == 8_000 - 800
    assert unpumped["effective_loss_mdb"] - unpumped["unpumped_loss_mdb"] == 800
    assert report["raman_spans"][0]["span"] == "span-1"


def test_an_unpumped_report_carries_no_raman_block_anywhere() -> None:
    """The shipped payload has no pump, so nothing in it should mention one."""
    file_path, class_name = _entry("budget_report")
    result = _run(file_path, class_name, RICH["budget_report"])
    assert result["raman_span_count"] == 0
    assert result["raman_note"] is None
    assert result["carriers"][0]["raman_spans"] == []
    assert all("raman_gain_mdb" not in hop for hop in result["carriers"][0]["hops"])


def test_the_impact_report_counts_amplifiers_per_direction() -> None:
    """Two chains make a bare total unreadable: eight amplifiers is a healthy
    three-span section in both directions and it is also a section missing half
    a chain."""
    file_path, class_name = _entry("impact_report")
    plant = _run(file_path, class_name, RICH["impact_report"])["plant"]
    assert plant["amplifier_count"] == 6
    assert plant["amplifiers_a_to_b"] == 3
    assert plant["amplifiers_b_to_a"] == 3
    assert plant["chains_balanced"] is True
    # No third count. An amplifier is in one list, the other, or in no section
    # at all, and a section-scoped report cannot see the third case.
    assert "amplifiers_without_direction" not in plant


def test_the_impact_report_says_when_a_section_has_no_amplifiers_at_all() -> None:
    """The sparse section holds none. An unbalanced or empty plant is reported
    rather than raised: the malformed chain is the thing the operator reading an
    impact report needs to see."""
    file_path, class_name = _entry("impact_report")
    plant = _run(file_path, class_name, SPARSE["impact_report"])["plant"]
    assert plant["amplifier_count"] == 0
    assert plant["amplifiers_a_to_b"] == 0
    assert plant["amplifiers_b_to_a"] == 0
    assert plant["chains_balanced"] is True


# ---------------------------------------------------------------------------
# The map: an SVG document, and the route that could not be budgeted
# ---------------------------------------------------------------------------


def test_the_map_is_an_svg_document_and_not_a_dict() -> None:
    """The artifact declares `image/svg+xml`.

    A dict paired with that content type is written to the artifact body as a
    stringified Python dict, which parses as nothing and renders as nothing.
    """
    file_path, class_name = _entry("network_map")
    rendered = _run(file_path, class_name, RICH["network_map"])
    assert isinstance(rendered, str)
    root = ElementTree.fromstring(rendered)
    assert root.tag.endswith("svg")


def test_the_map_draws_both_sites_and_the_facility_it_hosts() -> None:
    """The two node captions and the facility name, read off the edge.

    The sparse payload beside this one has the same sites with no facility, so
    a read that returned a caption for every site would fail there instead.
    """
    file_path, class_name = _entry("network_map")
    rendered = _run(file_path, class_name, RICH["network_map"])
    assert "Amsterdam" in rendered
    assert "Brussels" in rendered
    assert "MELUXINA" in rendered


def test_a_site_that_does_not_exist_is_an_error_not_a_map_with_nobody_on_it() -> None:
    """A map is drawn for one site. Rendering it with no focus is a different
    picture from the one the artifact asked for, and it would look correct."""
    file_path, class_name = _entry("network_map")
    payload = {**RICH["network_map"], "focus_site": edges()}
    with pytest.raises(ValueError, match="exactly one"):
        _run(file_path, class_name, payload)


def test_a_catalog_without_the_reference_mode_cannot_colour_a_route() -> None:
    """An OSNR margin means nothing without the mode it is measured against."""
    file_path, class_name = _entry("network_map")
    payload = {**RICH["network_map"], "OtnOpticalMode": edges(_mode("400ZR", 120_000))}
    with pytest.raises(ValueError, match="margin against"):
        _run(file_path, class_name, payload)


def test_a_section_that_will_not_budget_keeps_its_route_in_the_unknown_colour() -> None:
    """D-010, from the transform side.

    The reverse chain is short an amplifier, so `SectionInput.validate` rejects
    that direction. The route stays on the map and loses its colour. Dropping it
    would remove a real fiber route from a picture an operator reads as the
    network, and defaulting it into a passing band would be worse still.
    """
    file_path, class_name = _entry("network_map")
    section = {**_map_section(), "amplifiers_b2a": edges(_amplifier("amp-a-06", 1))}
    payload = {**RICH["network_map"], "OtnOpticalMultiplexSection": edges(section)}
    rendered = _run(file_path, class_name, payload)
    assert UNKNOWN_BAND.colour in rendered
    assert "n/a" in rendered
    # 160 km of fiber, still drawn and still labelled with its distance.
    assert "160 km" in rendered


# ---------------------------------------------------------------------------
# The ODU map: the response read as slot figures
# ---------------------------------------------------------------------------


def _texts(svg: str) -> list[str]:
    """Every text node in the document, stripped. The panel's figures are here."""
    return [(element.text or "").strip() for element in ElementTree.fromstring(svg).iter()]


def _route_bands(svg: str) -> dict[str, int]:
    """Count the routes on a rendered ODU map by the band they were painted in.

    Read off the document rather than off the records, because the transform
    hands back an SVG and there is nothing else to read. Routes are told from
    legend swatches by their stroke weight: a route is drawn at route weight and
    a swatch is not, deliberately, because the legend is a key to a colour and
    not a scale drawing. The casing under each route is in a chrome colour and
    never in a band colour, so it does not match either.
    """
    keys = {band.colour: band.key for band in [*HEADROOM_BANDS, NO_ODU_BAND]}
    counted = {key: 0 for key in keys.values()}
    widths = {f"{ROUTE_WIDTH:.1f}", f"{ROUTE_FOCUS_WIDTH:.1f}"}
    for element in ElementTree.fromstring(svg).iter():
        if not element.tag.endswith("line") or element.get("stroke-width") not in widths:
            continue
        key = keys.get(element.get("stroke", ""))
        if key is not None:
            counted[key] += 1
    return counted


def test_the_odu_map_is_an_svg_document_and_not_a_dict() -> None:
    """The artifact declares `image/svg+xml`, same as the other map."""
    file_path, class_name = _entry("odu_map")
    rendered = _run(file_path, class_name, RICH["odu_map"])
    assert isinstance(rendered, str)
    assert ElementTree.fromstring(rendered).tag.endswith("svg")


def test_the_odu_map_paints_one_route_per_band_from_the_response() -> None:
    """The whole of what this transform does, in one assertion.

    Six sections, and the six verdicts are computed from the container tree in
    the payload rather than declared anywhere: 320 free on an empty ODUC4, 72 on
    an ODU4 holding an ODU2, 6 on one holding 32+32+8+2, none on one holding
    32+32+8+8. A transform that read the wrong field, or summed the children
    instead of subtracting them, lands sections in the wrong band here rather
    than on a live map nobody has recounted by hand.
    """
    file_path, class_name = _entry("odu_map")
    rendered = _run(file_path, class_name, RICH["odu_map"])
    assert _route_bands(rendered) == {"odu4": 1, "odu2": 1, "odu0": 1, "full": 1, "no-odu": 2}


def test_an_unsized_container_makes_its_section_unknown_and_not_roomy() -> None:
    """The failure this whole layer is written against.

    A VC-4 is not an OTN construct and the slot table gives it no size, so the
    line container holding one has no free-slot figure. The section is lit, its
    row says so, and its colour is the explicit unknown rather than the 76 slots
    its sized siblings would have left. Reading the unsized child as zero would
    report a wavelength that might be overfull as one with room on it.
    """
    file_path, class_name = _entry("odu_map")
    payload = {
        **RICH["odu_map"],
        "OtnOpticalMultiplexSection": edges(_odu_section("oms-unsized")),
        "OtnOpticalCarrier": edges(_odu_carrier("oc-unsized", "oms-unsized", "ODU4", ("VC-4",))),
    }
    rendered = _run(file_path, class_name, payload)
    assert _route_bands(rendered) == {"odu4": 0, "odu2": 0, "odu0": 0, "full": 0, "no-odu": 1}
    # Lit, and still without a figure. "1 of 1 unknown" is the finding; a dark
    # section would report no lit carrier at all.
    assert "Sections with a headroom figure" in rendered
    assert "0 of 1" in _texts(rendered)
    assert FIT_UNKNOWN in _texts(rendered)


def test_a_dark_wavelength_counts_towards_neither_figure() -> None:
    """A carrier holding no container is skipped, not read as zero free.

    The sparse payload is one route with one dark wavelength on it, which is the
    branch someone stripped the ODU layer from. It renders, every route is
    unknown, and the caption names the branch so nobody reads the picture as the
    model being empty.
    """
    file_path, class_name = _entry("odu_map")
    rendered = _run(file_path, class_name, SPARSE["odu_map"])
    assert _route_bands(rendered) == {"odu4": 0, "odu2": 0, "odu0": 0, "full": 0, "no-odu": 1}
    assert f"No ODU layer is provisioned on branch {BRANCH}" in rendered
    assert "n/a" in _texts(rendered)


def test_a_site_that_does_not_exist_is_an_error_on_the_odu_map_too() -> None:
    """One map per site, and a map with nobody highlighted would look correct."""
    file_path, class_name = _entry("odu_map")
    with pytest.raises(ValueError, match="exactly one"):
        _run(file_path, class_name, {**RICH["odu_map"], "focus_site": edges()})


# ---------------------------------------------------------------------------
# The chain: three reports following the segments
# ---------------------------------------------------------------------------
#
# T033, and what fails when a report stops following the segments. Every
# assertion below is fixture-only and has to be: no chained circuit exists on any
# branch yet, because all 71 shipped wavelengths cross `oms-fra-mil` and a cover
# may not repeat a section, so no pair of them can chain. A live dry-run proves
# the single-segment path is unbroken and cannot reach this one.
#
# The shape under test is a circuit written with segment 2 first, since an
# Infrahub relationship hands back a set. Reading the payload order renders the
# chain backwards and raises nothing.


def test_the_trace_renders_a_chain_as_segments_in_order_with_the_junction_between() -> None:
    """US2 scenario 1. Two segments, each with its carrier and its container."""
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, CHAINED["service_trace"])
    assert result["segment_count"] == 2
    assert result["regenerated"] is True
    assert [row["sequence"] for row in result["segments"]] == [1, 2], "sorted on segment_sequence, not payload order"
    assert [row["carrier"]["name"] for row in result["segments"]] == ["oc-mad-fra", "oc-fra-waw"]
    assert [row["carrier"]["containers"][0]["name"] for row in result["segments"]] == ["odu-chain-1", "odu-chain-2"]
    assert [row["path"]["name"] for row in result["segments"]] == ["path-chain-1", "path-chain-2"]
    junction = result["segments"][0]["junction"]
    assert junction["device"] == "oeo-fra"
    assert junction["site"] == "fra"
    assert junction["switching_mode"] == "regenerator"
    assert result["segments"][1]["junction"] is None, "nothing regenerates light nobody carries on"
    assert result["junctions"] == [junction], "one fewer junction than segments"
    assert all(row["junction_note"] is None for row in result["segments"])


def test_a_chained_trace_quotes_no_route_margin_and_pairs_each_one_with_its_segment() -> None:
    """FR-014, and the constraint `budget.RouteBudget` exists to enforce.

    A single `+4.2 dB` on a regenerated circuit reads as the route's headroom and
    two segments closing is not that claim. So there is no route margin here at
    all: the margins come out paired with their segment numbers, exactly as
    `RouteBudget.segment_margins_mdb` returns them, and the verdict is a
    conjunction. `sole_segment` raises for the same reason one level down.
    """
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, CHAINED["service_trace"])
    assert result["route"]["segment_margins"] == [
        {"sequence": 1, "osnr_margin_display": "+2.745 dB"},
        {"sequence": 2, "osnr_margin_display": "+5.740 dB"},
    ]
    assert "osnr_margin_display" not in result["route"], "no route margin exists to print"
    assert "osnr_margin_display" not in result
    assert result["route"]["osnr_positive_on_every_segment"] is True
    assert result["path"] is None, "a chain has no one path, and segment 1 is not the route"
    assert result["carrier"] is None
    assert result["hops"] is None
    assert "no single route margin" in result["route_note"]
    # The two figures that do total across a regeneration, and the framing delay
    # of the one device, which is the term a reader cannot find anywhere else.
    assert result["route"]["total_length_display"] == "320.000 km"
    assert result["route"]["latency_ns"] == 783_000 * 2 + 1200
    assert result["route"]["conduits"] == ["cd-iberia", "cd-north"]
    assert result["hop_count"] == 8


def test_a_single_segment_circuit_traces_what_it_traced_before_segments_existed() -> None:
    """US2 scenario 2. The compatibility half, and the reason it is asserted.

    Every circuit in the shipped dataset has one segment, so this is the render
    the demo guide and the artifact already consume. `path`, `carrier` and `hops`
    stay at the top level and hold what they held; the segment list is the new
    key beside them and it holds the same objects.
    """
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, RICH["service_trace"])
    assert result["segment_count"] == 1
    assert result["regenerated"] is False
    assert result["junctions"] == []
    assert result["route_note"] is None
    assert result["path"]["name"] == "path-svc-a"
    assert result["path"]["osnr_margin_display"] == "+2.284 dB"
    assert result["path"] is result["segments"][0]["path"]
    assert result["carrier"] is result["segments"][0]["carrier"]
    assert result["hops"] is result["segments"][0]["hops"]
    assert result["hop_count"] == 4
    assert result["conduits"] == ["cd-north"]
    assert result["segments"][0]["junction"] is None
    assert result["segments"][0]["junction_note"] is None, "a last segment is not a missing junction"


def test_a_chain_whose_carriers_record_no_shared_device_is_labelled_not_rendered() -> None:
    """R-008's phantom junction, in a report rather than in a traversal.

    Two carriers meeting with nothing recorded that terminates both is not a
    chain the model can verify. The trace names the boundary and says the two
    segments are joined by nothing, rather than printing a junction row with a
    blank in it.
    """
    file_path, class_name = _entry("service_trace")
    result = _run(file_path, class_name, {"OtnService": edges(_chained_service(junction=False))})
    assert result["segment_count"] == 2
    assert result["segments"][0]["junction"] is None
    assert "joined by nothing" in result["segments"][0]["junction_note"]
    assert result["junctions"] == []


def test_the_impact_report_says_the_circuit_is_down_and_which_segment_the_cut_took() -> None:
    """US2 scenario 3, and the wording that must not drift either way.

    The cut takes segment 1 of a two-segment circuit. The circuit is down, and
    saying so first is what stops a surviving segment reading as protection. The
    surviving segment is still named, because it is the only fact an operator can
    act on: one segment has to be re-provisioned, not the route.
    """
    file_path, class_name = _entry("impact_report")
    result = _run(file_path, class_name, CHAINED["impact_report"])
    row = next(item for item in result["latency_sensitive_services"] if item["service"] == "svc-chain")
    assert row["regenerated"] is True
    assert row["segment_sequence"] == 1
    assert row["segment_count"] == 2
    assert row["segments_cut"] == [1]
    assert row["segments_surviving"] == [{"sequence": 2, "carrier": "oc-fra-waw"}]
    assert row["segment_note"].startswith("The circuit is down.")
    assert "segment 1 of 2" in row["segment_note"]
    assert "segment 2 on oc-fra-waw is still lit" in row["segment_note"]
    assert "no protection switching and no restoration" in row["segment_note"]
    assert "only the cut segment has to be re-provisioned" in row["segment_note"]
    # The overclaim this wording exists to avoid. This model cannot reroute, and
    # a report hinting that it might would be worse than one that flattened the
    # distinction away.
    assert not any(
        word in row["segment_note"].lower() for word in ("reroute", "protected", "resilient", "may fail over")
    )
    assert result["chained_service_count"] == 1
    assert result["chained_services"][0]["service"] == "svc-chain"
    assert "not carrying the service" in result["chained_note"]


def test_a_single_segment_circuit_is_not_reported_as_a_partial_loss() -> None:
    """The other half of the distinction. A cut here takes the whole circuit.

    Flattening the two cases together is the failure this pins from the other
    side: a report that said "one segment of one is cut and the others are lit"
    would be technically true and read as protection.
    """
    file_path, class_name = _entry("impact_report")
    result = _run(file_path, class_name, RICH["impact_report"])
    row = next(item for item in result["latency_sensitive_services"] if item["service"] == "svc-a")
    assert row["regenerated"] is False
    assert row["segment_count"] == 1
    assert row["segments_surviving"] == []
    assert "rides one wavelength" in row["segment_note"]
    assert result["chained_service_count"] == 0
    assert result["chained_note"] is None


def test_the_exposure_report_unions_the_ducts_across_a_chains_segments() -> None:
    """FR-019, and the pair that a first-segment-only walk never finds.

    The chain's first segment is alone in `cd-iberia` and its second shares
    `cd-north` with `svc-a`. A report reading the lowest segment answers that the
    two services share nothing, which is not an error a reader can see: it is a
    narrower answer that looks like a clean bill of health.
    """
    file_path, class_name = _entry("srlg_exposure")
    result = _run(file_path, class_name, CHAINED["srlg_exposure"])
    chain = next(row for row in result["services"] if row["service"] == "svc-chain")
    assert chain["conduits"] == ["cd-iberia", "cd-north"]
    assert chain["segment_count"] == 2
    assert chain["span_count"] == 2, "one span per segment, counted over both"
    assert "union across its segments" in chain["segment_note"]
    assert result["regenerated_service_count"] == 1
    assert result["pair_count"] == 1
    assert result["pairs"][0]["service_a"] == "svc-a"
    assert result["pairs"][0]["service_b"] == "svc-chain"
    assert result["pairs"][0]["shared_conduits"] == ["cd-north"]
    shared = next(item for item in result["conduits"] if item["conduit"] == "cd-north")
    assert shared["services"] == ["svc-a", "svc-chain"]
    assert result["not_exposed"] == []


def test_the_exposure_report_still_pairs_services_that_declared_no_group() -> None:
    """FR-018, unconditional, and a later chunk's check depends on it staying so.

    Neither service here declares a diversity group, and both are still paired.
    `checks/diversity.py` is the half that is silent without a declaration,
    because a check that blocked a merge on accepted exposure would block it on a
    decision somebody already made. The report is the place the exposure stays
    visible.
    """
    file_path, class_name = _entry("srlg_exposure")
    result = _run(file_path, class_name, CHAINED["srlg_exposure"])
    assert not any("diversity_group" in row for row in result["services"])
    assert result["pair_count"] == 1
    assert "share cd-north" in result["pairs"][0]["finding"]
