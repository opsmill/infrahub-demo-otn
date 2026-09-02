"""Recompute the documented numeric claims from the generated dataset."""

import importlib.util
import json
import subprocess
import sys
import tempfile
from collections import Counter
from functools import cache
from itertools import pairwise
from math import ceil
from pathlib import Path
from typing import Any

import pytest
import yaml

from infrahub_demo_otn.containers import LINE_CONTAINER_BY_LINE_RATE_GBPS, SLOT_TABLE, slot_capacity
from infrahub_demo_otn.units import (
    CBAND_EXTENT_MHZ,
    CBAND_LOWER_EDGE_MHZ,
    CBAND_UPPER_EDGE_MHZ,
    GRID_CHANNEL_COUNT,
    GROUP_INDEX_G652_MILLI,
    FreeBlock,
    anchor_fits_band,
    carrier_interval_mhz,
    channel_to_frequency_mhz,
    free_blocks,
    m_to_km,
    occupied_width_mhz,
    propagation_delay_ns,
)
from tests.unit.conftest import OBJECT_DIR, SCHEMA_DIR, SCRIPT_DIR, object_documents, objects_of_kind

GENERATOR = SCRIPT_DIR / "generate_geant_dataset.py"
MANIFEST = SCRIPT_DIR / "geant_manifest.json"

DISPERSION_FS_PER_NM_KM = 17_000
"""G.652.D, from `objects/01_fiber_types.yml`. Asserted against the catalog below
rather than trusted, so this module cannot drift from the fiber it assumes."""


# ---------------------------------------------------------------------------
# Topology, rebuilt from the object files.
# ---------------------------------------------------------------------------
@cache
def _sections() -> tuple[dict[str, Any], ...]:
    return objects_of_kind("OtnOpticalMultiplexSection")


@cache
def _spans_by_name() -> dict[str, dict[str, Any]]:
    return {str(span["name"]): span for span in objects_of_kind("OtnFiberSpan")}


@cache
def _section_length_m() -> dict[str, int]:
    """Section name -> the sum of its spans' metres. Nothing stores a total."""
    spans = _spans_by_name()
    return {
        str(section["name"]): sum(int(spans[name]["length_m"]) for name in section["spans"]) for section in _sections()
    }


@cache
def _section_endpoints() -> dict[str, tuple[str, str]]:
    """Section name -> its two site shortnames, taken from the spans' endpoints."""
    spans = _spans_by_name()
    endpoints: dict[str, tuple[str, str]] = {}
    for section in _sections():
        first = spans[str(section["spans"][0])]
        endpoints[str(section["name"])] = (str(first["site_a"]), str(first["site_b"]))
    return endpoints


@cache
def _graph() -> dict[str, list[tuple[str, int, str]]]:
    """site -> [(far site, metres, section name)]."""
    lengths = _section_length_m()
    adjacency: dict[str, list[tuple[str, int, str]]] = {}
    for name, (a, b) in _section_endpoints().items():
        adjacency.setdefault(a, []).append((b, lengths[name], name))
        adjacency.setdefault(b, []).append((a, lengths[name], name))
    return adjacency


@cache
def _simple_paths(start: str, end: str, max_hops: int = 6) -> tuple[tuple[int, tuple[str, ...], tuple[str, ...]], ...]:
    """Every simple path, as (metres, sites, sections), shortest first.

    Enumerated exhaustively rather than shortest-path only. The design claims a
    *ranking* of three routes, and a ranking cannot be checked by finding one.
    """
    adjacency = _graph()
    found: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []

    def walk(node: str, visited: set[str], metres: int, sites: tuple[str, ...], sections: tuple[str, ...]) -> None:
        if node == end:
            found.append((metres, sites, sections))
            return
        if len(sites) > max_hops:
            return
        for far, length, section in adjacency.get(node, []):
            if far in visited:
                continue
            visited.add(far)
            walk(far, visited, metres + length, (*sites, far), (*sections, section))
            visited.remove(far)

    walk(start, {start}, 0, (start,), ())
    return tuple(sorted(found, key=lambda entry: entry[0]))


def _route_km(start: str, end: str, rank: int) -> int:
    return round(m_to_km(_simple_paths(start, end)[rank][0]))


# ---------------------------------------------------------------------------
# The generator is the source; the files are output.
# ---------------------------------------------------------------------------
def test_the_committed_files_match_a_fresh_generator_run() -> None:
    """Without this, "generated, not hand-edited" is a claim nothing enforces."""
    result = subprocess.run([sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"objects/ has drifted from the seed:\n{result.stdout}{result.stderr}"


@cache
def _generator_module() -> Any:
    """The dataset generator, loaded from its path.

    `scripts/` is not on the import path and must not be put there: adding it
    changes import resolution for every module the suite loads afterwards.
    """
    spec = importlib.util.spec_from_file_location(GENERATOR.stem, GENERATOR)
    assert spec and spec.loader, f"{GENERATOR} could not be loaded"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generator_is_deterministic() -> None:
    """Two runs, byte for byte. A generator nobody dares re-run is not a seed."""
    module = _generator_module()

    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        module.generate(Path(first))
        module.generate(Path(second))
        for name in module.GENERATED_NAMES:
            assert (Path(first) / name).read_bytes() == (Path(second) / name).read_bytes(), name


def test_no_generated_numeric_value_is_a_float() -> None:
    """Infrahub has no Float attribute kind, so a decimal in the seed data would be
    rejected by a Number attribute at load time, which is a slow way to find it."""
    offenders: list[str] = []
    for path in sorted(OBJECT_DIR.glob("1*.yml")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            key, _, value = line.partition(": ")
            if value and value.replace("-", "", 1).replace(".", "", 1).isdigit() and "." in value:
                offenders.append(f"{path.name}:{number} {key.strip()} = {value}")
    assert not offenders, "decimal values in generated YAML: " + "; ".join(offenders)


def test_object_counts_match_the_manifest() -> None:
    """The manifest is the one place a per-kind count lives.

    The docs do not read it. They quote it, and `test_doc_claims.py` reads the
    pages back and fails when a quoted figure and this manifest disagree.
    """
    manifest = json.loads(MANIFEST.read_text())
    actual = {kind: len(objects_of_kind(kind)) for kind in manifest}
    assert actual == manifest


def test_the_inventory_the_installation_page_promises_is_what_loads() -> None:
    """`installation-setup.mdx` and `provisioning-scenarios.mdx` print this inventory."""
    devices = (
        "OtnRouter",
        "OtnTransponder",
        "OtnRoadm",
        "OtnAmplifier",
        "OtnMuxDemux",
        "OtnPatchPanel",
        "OtnRamanPump",
        "OtnOduSwitch",
    )
    ports = (
        "OtnRouterPort",
        "OtnClientPort",
        "OtnLinePort",
        "OtnRoadmAddDropPort",
        "OtnRoadmDegreePort",
        "OtnAmplifierPort",
        "OtnTributaryPort",
        "OtnAmplifierMonitor",
        "OtnRoadmDegreeMonitor",
        "OtnMuxDemuxMonitor",
        "OtnRamanMonitor",
        "OtnReceiverMonitor",
    )
    total = sum(len((document.get("spec") or {}).get("data") or []) for document in object_documents())
    assert total == 2344, "the pages say the load is 2344 objects"
    assert sum(len(objects_of_kind(kind)) for kind in devices) == 441, "the pages say 441 devices"
    assert sum(len(objects_of_kind(kind)) for kind in ports) == 1490, "the pages say 1490 ports"


# ---------------------------------------------------------------------------
# Structural invariants.
# ---------------------------------------------------------------------------
def test_the_fourteen_pops_are_the_fourteen_the_design_names_plus_one_campus() -> None:
    """Fourteen PoPs, each with coordinates, and one customer site."""
    expected = {
        "Amsterdam", "Berlin", "Brussels", "Copenhagen", "Frankfurt", "Geneva", "Hamburg",
        "London", "Madrid", "Milan", "Paris", "Prague", "Vienna", "Warsaw",
    }  # fmt: skip
    sites = objects_of_kind("OtnSite")
    pops = {str(site["name"]) for site in sites if site["site_type"] == "pop"}
    customers = {str(site["name"]) for site in sites if site["site_type"] == "customer"}
    assert pops == expected
    assert customers == {"Amsterdam Science Park"}
    for site in sites:
        assert "latitude_microdeg" in site and "longitude_microdeg" in site, site["name"]
        assert "shortname" in site, site["name"]


def test_there_are_twenty_one_sections_each_with_two_roadms() -> None:
    """Twenty-one sections, each terminating on two different ROADMs."""
    sections = _sections()
    assert len(sections) == 21
    for section in sections:
        assert section["roadm_a"] != section["roadm_b"], section["name"]
        assert section["spans"], section["name"]
        assert section["amplifiers_a2b"], section["name"]
        assert section["amplifiers_b2a"], section["name"]


def test_every_span_is_under_the_amplifier_spacing_ceiling() -> None:
    """About 80 km spacing, with 90 km as the hard ceiling."""
    lengths = [int(span["length_m"]) for span in objects_of_kind("OtnFiberSpan")]
    # 132 core spans and the 18.4 km CWDM tail. The mean moves from 83,636 m to
    # 83,146 m, which stays inside the band below, so only the count changes.
    assert len(lengths) == 133
    assert max(lengths) <= 90_000, f"longest span is {max(lengths)} m"
    assert 78_000 <= sum(lengths) / len(lengths) <= 88_000, "mean span has drifted away from 80 km"


# ---------------------------------------------------------------------------
# The CWDM tail, and the boundary that keeps it out of the budget engine.
# ---------------------------------------------------------------------------
TAIL_SPAN = "span-asp-ams-01"
TAIL_MULTIPLEXERS = {"mux-asp-01", "mux-ams-02"}
TAIL_WAVELENGTHS = ["1471", "1491", "1511", "1531"]


def _customer_shortnames() -> set[str]:
    return {str(site["shortname"]) for site in objects_of_kind("OtnSite") if site["site_type"] == "customer"}


def test_the_cwdm_tail_span_carries_no_amplification_and_no_section() -> None:
    """One span, one fiber type, one length, and nothing else attached to it."""
    span = _spans_by_name()[TAIL_SPAN]
    assert span["length_m"] == 18_400
    assert span["fiber_type"] == "G.652.D"
    assert span["site_a"] in _customer_shortnames()
    assert span["site_b"] == "ams"
    assert "oms" not in span
    assert "conduit" not in span
    assert TAIL_SPAN not in {str(pump["span"]) for pump in objects_of_kind("OtnRamanPump")}
    assert TAIL_SPAN not in {name for section in _sections() for name in section["spans"]}


def test_no_span_touching_a_customer_site_declares_an_oms() -> None:
    """The one guard holding the coarse tail out of the budget engine. Measured."""
    customers = _customer_shortnames()
    assert customers, "no customer site in the dataset, so this guard is vacuous"
    offenders = [
        str(span["name"])
        for span in objects_of_kind("OtnFiberSpan")
        if (str(span["site_a"]) in customers or str(span["site_b"]) in customers) and "oms" in span
    ]
    assert not offenders, "spans on a customer site declaring an oms: " + "; ".join(offenders)


def test_only_the_two_tail_multiplexers_light_a_cwdm_wavelength() -> None:
    """The lit-channel set is the tail and nothing else."""
    multiplexers = objects_of_kind("OtnMuxDemux")
    lit = {str(device["name"]): device.get("cwdm_channels") or [] for device in multiplexers}
    assert {name for name, channels in lit.items() if channels} == TAIL_MULTIPLEXERS
    assert len(multiplexers) == 16, "fourteen dense multiplexers and the two coarse ones"
    for name in TAIL_MULTIPLEXERS:
        assert lit[name] == TAIL_WAVELENGTHS, name


def test_the_cwdm_tail_multiplexers_are_passive_and_one_sits_at_the_campus() -> None:
    """`role` is mandatory and its vocabulary holds no metro, so passive it is."""
    by_name = {str(device["name"]): device for device in objects_of_kind("OtnMuxDemux")}
    assert {by_name[name]["role"] for name in TAIL_MULTIPLEXERS} == {"passive"}
    assert {str(by_name[name]["site"]) for name in TAIL_MULTIPLEXERS} == {"asp", "ams"}


def test_no_object_writes_element_class() -> None:
    """The value is determined by the kind, so it belongs to the kind."""
    offenders = [
        f"{document['spec']['kind']} {entry.get('name')}"
        for document in object_documents()
        for entry in (document.get("spec") or {}).get("data") or []
        if isinstance(entry, dict) and "element_class" in entry
    ]
    assert not offenders, "objects writing element_class: " + "; ".join(offenders)


def test_the_dispersion_constant_this_module_uses_matches_the_catalog() -> None:
    """This module computes a dispersion finding. If the catalog moves, it must
    fail here rather than quietly compute the finding against a stale number."""
    catalog = {str(entry["name"]): entry for entry in objects_of_kind("OtnFiberType")}
    assert catalog["G.652.D"]["dispersion_fs_per_nm_km"] == DISPERSION_FS_PER_NM_KM


def test_span_positions_within_a_section_are_exactly_one_to_n() -> None:
    """The schema cannot enforce this: a uniqueness constraint cannot reference
    an optional relationship, and a duplicate position is accepted at HTTP 200
    with ok: true. Only a test can catch it."""
    spans = _spans_by_name()
    for section in _sections():
        positions = sorted(int(spans[str(name)]["oms_sequence"]) for name in section["spans"])
        assert positions == list(range(1, len(positions) + 1)), f"{section['name']} has positions {positions}"


def test_spans_sum_exactly_to_their_section() -> None:
    """Integer metres divided n ways must not lose the remainder."""
    for name, metres in _section_length_m().items():
        assert metres % 1000 == 0, f"{name} sums to {metres} m, not a whole number of km"


def _chains_of(section: dict[str, Any]) -> dict[str, list[str]]:
    """The section's two amplifier lists.

    Which chain an amplifier is in is which relationship holds it, so this reads
    the section and nothing on the amplifier. Nothing here parses a name.
    """
    return {direction: [str(name) for name in section[f"amplifiers_{direction}"]] for direction in ("a2b", "b2a")}


def _port_roles_by_device() -> dict[str, set[str]]:
    """Every amplifier's ports, keyed by device, as a set of roles.

    The booster and preamp vocabulary lives on `OtnGenericPort.role`, not on the
    device and not in the name. This is the only place a test can read it from.
    """
    roles: dict[str, set[str]] = {}
    for port in objects_of_kind("OtnAmplifierPort"):
        roles.setdefault(str(port["device"]), set()).add(str(port["role"]))
    return roles


def test_the_dataset_ships_two_amplifiers_per_position() -> None:
    """306, and the doubling is not a coincidence to be asserted twice."""
    amplifiers = objects_of_kind("OtnAmplifier")
    assert len(amplifiers) == 306
    per_direction = Counter(
        direction for section in _sections() for direction, chain in _chains_of(section).items() for _ in chain
    )
    assert per_direction == {"a2b": 153, "b2a": 153}


def test_no_amplifier_name_carries_a_direction_and_all_306_are_distinct() -> None:
    """A name is an identifier and carries nothing a query needs."""
    names = [str(device["name"]) for device in objects_of_kind("OtnAmplifier")]
    assert len(names) == len(set(names)) == 306
    offenders = [name for name in names if "a2b" in name or "b2a" in name]
    assert not offenders, "amplifier names carrying a direction: " + "; ".join(offenders)
    assert "direction" not in objects_of_kind("OtnAmplifier")[0]


def test_every_section_has_a_booster_a_preamp_and_one_inline_per_span_gap() -> None:
    """Per direction: one booster, one pre-amplifier, one inline per span gap."""
    amplifiers = {str(device["name"]): device for device in objects_of_kind("OtnAmplifier")}
    roles = _port_roles_by_device()
    for section in _sections():
        chains = _chains_of(section)
        expected = len(section["spans"]) + 1
        assert sum(len(chain) for chain in chains.values()) == 2 * expected, section["name"]
        for direction, chain in chains.items():
            where = f"{section['name']} {direction}"
            assert len(chain) == expected, where
            positions = {name: int(amplifiers[name]["oms_sequence"]) for name in chain}
            assert sum(position == 1 for position in positions.values()) == 1, where
            assert sum(position == expected for position in positions.values()) == 1, where
            for name, position in positions.items():
                want = "booster" if position == 1 else "preamp" if position == expected else "line"
                assert roles[name] == {want}, f"{name} at position {position} has port roles {roles[name]}"
                # A booster or a pre-amplifier is at a section endpoint and an
                # inline amplifier is in a hut, which is not a PoP.
                assert ("site" in amplifiers[name]) is (want != "line"), name


def test_amplifier_positions_within_a_chain_are_exactly_one_to_n_plus_one() -> None:
    """The same hole as the span's positions, one kind over."""
    amplifiers = {str(device["name"]): device for device in objects_of_kind("OtnAmplifier")}
    for section in _sections():
        expected = list(range(1, len(section["spans"]) + 2))
        for direction, chain in _chains_of(section).items():
            where = f"{section['name']} {direction}"
            positions = sorted(int(amplifiers[name]["oms_sequence"]) for name in chain)
            assert positions == expected, f"{where} has amplifier positions {positions}"


def test_every_amplifier_carries_a_noise_figure_and_a_gain() -> None:
    """Both are mandatory with a default, so a missing one loads as
    the default rather than failing, which is exactly how a silent wrong answer
    gets into an OSNR budget. The generator writes both on all 306."""
    for device in objects_of_kind("OtnAmplifier"):
        name = str(device["name"])
        assert "noise_figure_mdb" in device, name
        assert "gain_mdb" in device, name
        assert 3_000 <= int(device["noise_figure_mdb"]) <= 10_000, name
        assert 0 <= int(device["gain_mdb"]) <= 40_000, name


def test_the_raman_pumps_ship_on_vienna_to_milan_and_nowhere_else() -> None:
    """Main ships Raman so the report and rendering paths run on the default."""
    section = {str(name) for name in next(s for s in _sections() if s["name"] == "oms-vie-mil")["spans"]}
    pumps = objects_of_kind("OtnRamanPump")
    assert len(pumps) == 9
    assert {str(pump["span"]) for pump in pumps} == section
    # All nine are counter-propagating at the B end, which is A to B by the
    # derivation. No pump stores that conclusion.
    assert {str(pump["injection_end"]) for pump in pumps} == {"site_b"}
    assert {str(pump["propagation"]) for pump in pumps} == {"counter"}

    names = [str(pump["name"]) for pump in pumps]
    assert len(set(names)) == 9
    assert not [name for name in names if "a2b" in name or "b2a" in name]

    for pump in pumps:
        name = str(pump["name"])
        # Mandatory with a default, which is the dangerous pair. A pump that took
        # the default would credit gain nobody chose to a walk nobody chose.
        assert "on_off_gain_mdb" in pump, name
        assert 0 < int(pump["on_off_gain_mdb"]) <= 15_000, name
        assert "insertion_loss_mdb" in pump, name


def test_no_pump_leaves_its_injection_end_to_the_schema_default() -> None:
    """`injection_end` is mandatory *and* defaulted, which is the dangerous pair."""
    offenders = [str(pump["name"]) for pump in objects_of_kind("OtnRamanPump") if "injection_end" not in pump]
    assert not offenders, "pumps with no injection end of their own: " + "; ".join(offenders)
    assert {str(pump["injection_end"]) for pump in objects_of_kind("OtnRamanPump")} <= {"site_a", "site_b"}


def test_no_pumped_span_is_driven_below_zero_loss() -> None:
    """FR-012's floor exists, and no shipped span comes near it."""
    spans = _spans_by_name()
    for pump in objects_of_kind("OtnRamanPump"):
        span = spans[str(pump["span"])]
        fiber_loss = int(span["length_m"]) // 1000 * 200 + int(span["splice_count"]) * int(span["splice_loss_mdb"])
        credit = int(pump["on_off_gain_mdb"]) - int(pump["insertion_loss_mdb"])
        assert credit < fiber_loss, f"{pump['name']} credits {credit} mdB against {fiber_loss} mdB of fibre"


def test_the_three_odu_switches_are_two_hub_cross_connects_and_one_regenerator() -> None:
    """The devices ship where the measurements put them, not where they look tidy."""
    switches = objects_of_kind("OtnOduSwitch")
    assert len(switches) == 3, f"{len(switches)} O-E-O devices, the dataset ships three"

    by_name = {str(switch["name"]): switch for switch in switches}
    assert sorted(by_name) == ["oeo-fra-01", "oxc-fra-01", "oxc-mil-01"]
    assert [str(by_name[name]["site"]) for name in sorted(by_name)] == ["fra", "fra", "mil"]
    assert [str(by_name[name]["switching_mode"]) for name in sorted(by_name)] == [
        "regenerator",
        "cross_connect",
        "cross_connect",
    ]
    assert min(by_name) == "oeo-fra-01", "the regenerator no longer wins the tie-break at Frankfurt"

    # 3000 ns is the figure `tests/unit/test_budget_claims.py` budgets Madrid to
    # Warsaw with, and its 14,558,963 ns route total is computed from it. The two
    # would drift silently apart without this line.
    assert int(by_name["oeo-fra-01"]["framing_latency_ns"]) == 3_000
    # A cross-connect demultiplexes to containers and regroups them, which is
    # strictly more framing work than passing a whole payload through.
    for name in ("oxc-fra-01", "oxc-mil-01"):
        assert int(by_name[name]["framing_latency_ns"]) > int(by_name["oeo-fra-01"]["framing_latency_ns"]), name
        assert 0 < int(by_name[name]["framing_latency_ns"]) <= 100_000, name


def test_every_odu_switch_terminates_wavelengths_that_exist() -> None:
    """FR-003 from both sides: the edges resolve, and no device is inert."""
    carriers = {str(carrier["name"]) for carrier in objects_of_kind("OtnOpticalCarrier")}
    expected = {"oeo-fra-01": 25, "oxc-fra-01": 25, "oxc-mil-01": 37}

    for switch in objects_of_kind("OtnOduSwitch"):
        name = str(switch["name"])
        terminated = [str(entry) for entry in switch.get("carriers") or []]
        assert terminated, f"{name} terminates no wavelength, so it is never a junction"
        missing = sorted(set(terminated) - carriers)
        assert not missing, f"{name} names wavelengths that do not exist: {', '.join(missing)}"
        assert len(set(terminated)) == len(terminated), f"{name} names a wavelength twice"
        assert len(terminated) == expected[name], f"{name} terminates {len(terminated)}, expected {expected[name]}"


def test_no_odu_switch_writes_the_reverse_side_of_the_carrier_edge() -> None:
    """The edge is written once, from the device."""
    offenders = [str(carrier["name"]) for carrier in objects_of_kind("OtnOpticalCarrier") if "odu_switches" in carrier]
    assert not offenders, "carriers writing the reverse side of the ODU switch edge: " + "; ".join(offenders)


def test_routers_carry_no_element_class() -> None:
    """Light terminates at a router, so a router contributes no insertion loss.
    OtnRouter does not inherit OtnOpticalElement, and this asserts that the data
    agrees."""
    offenders = [str(router["name"]) for router in objects_of_kind("OtnRouter") if "element_class" in router]
    assert not offenders, "routers claiming to be optical elements: " + "; ".join(offenders)


def test_the_connected_to_edge_is_declared_on_one_side_only() -> None:
    """Declaring both sides splits one edge into two phantom one-way links."""
    add_drop = [port for port in objects_of_kind("OtnRoadmAddDropPort") if "connected_to" in port]
    assert not add_drop, "connected_to declared on the ROADM side as well as the line side"

    transponder_ports = _line_ports_on_transponders()
    linked = [port for port in transponder_ports if "connected_to" in port]
    assert len(linked) == len(transponder_ports), "some transponder line ports are unconnected"
    targets = [tuple(port["connected_to"]) for port in linked]
    assert len(targets) == len(set(targets)), "two line ports share one add/drop port"

    patched = [str(port["name"]) for port in _line_ports_on_odu_switches() if "connected_to" in port]
    assert not patched, "an O-E-O line port claims an add/drop port the plant has not got: " + "; ".join(patched)


def test_the_eurohpc_attachments_are_the_six_the_design_names() -> None:
    """Six EuroHPC attachments. LUMI is excluded: Finland is outside the
    fourteen-site subset."""
    tags = {str(tag["name"]) for tag in objects_of_kind("BuiltinTag")}
    assert tags == {
        "eurohpc-jupiter", "eurohpc-karolina", "eurohpc-leonardo",
        "eurohpc-marenostrum-5", "eurohpc-meluxina", "eurohpc-vega",
    }  # fmt: skip

    tagged = {str(site["shortname"]): site.get("tags", []) for site in objects_of_kind("OtnSite")}
    assert tagged["fra"] == ["eurohpc-jupiter"]
    assert tagged["mil"] == ["eurohpc-leonardo"]
    assert tagged["mad"] == ["eurohpc-marenostrum-5"]
    assert tagged["prg"] == ["eurohpc-karolina"]
    assert tagged["bru"] == ["eurohpc-meluxina"]
    assert tagged["vie"] == ["eurohpc-vega"]

    facility_routers = [str(r["name"]) for r in objects_of_kind("OtnRouter") if r.get("role") == "edge"]
    assert len(facility_routers) == 6

    # Over the parsed records, not the raw bytes: `10_geant_tags.yml` carries a
    # comment saying LUMI is absent, and a byte-level check flags the very note
    # that documents the exclusion.
    values = " ".join(
        str(value)
        for kind in ("BuiltinTag", "OtnSite", "OtnRouter")
        for record in objects_of_kind(kind)
        for value in record.values()
    )
    assert "lumi" not in values.lower(), "LUMI is deliberately outside the modelled topology"


# ---------------------------------------------------------------------------
# Claim 1: three Berlin-to-Amsterdam routes. CONFIRMED.
# ---------------------------------------------------------------------------
def test_the_three_berlin_to_amsterdam_routes_are_what_the_design_claims() -> None:
    """The documentation names three Berlin-to-Amsterdam routes at 800, 1010 and
    1220 km. Computed over the loaded sections those figures are exact, they are
    ranks one to three, and the fourth is 1330 km via Copenhagen.
    """
    routes = _simple_paths("ber", "ams")
    assert [round(m_to_km(metres)) for metres, _, _ in routes[:4]] == [800, 1010, 1220, 1330]
    assert [list(sites) for _, sites, _ in routes[:4]] == [
        ["ber", "ham", "ams"],
        ["ber", "fra", "ams"],
        ["ber", "prg", "fra", "ams"],
        ["ber", "cph", "ham", "ams"],
    ]


def test_the_berlin_to_amsterdam_routes_have_materially_different_budgets() -> None:
    """Materially different needs a number, so: each rung is at least 200 km
    longer than the one below it."""
    lengths = [round(m_to_km(metres)) for metres, _, _ in _simple_paths("ber", "ams")[:3]]
    assert all(later - earlier >= 200 for earlier, later in pairwise(lengths))


def test_the_nominal_reach_comparison_for_the_three_routes() -> None:
    """Nominal reach is not the pass criterion; the OSNR margin is. What the
    catalog does settle is that the 1010 km route sits one percent past the
    16QAM reach figure, which is close enough that only a budget can call it.
    """
    modes = {str(mode["name"]): mode for mode in objects_of_kind("OtnOpticalMode")}
    sixteen_qam = int(modes["DP-16QAM 64GBd 400G"]["nominal_reach_m"])
    qpsk = int(modes["DP-QPSK 128GBd 400G"]["nominal_reach_m"])
    assert sixteen_qam == 1_000_000 and qpsk == 2_500_000

    winner, middle, longest = (_route_km("ber", "ams", rank) for rank in (0, 1, 2))
    assert winner * 1000 < sixteen_qam, "the winner must be inside 16QAM reach"
    assert middle * 1000 > sixteen_qam, "viable at 16QAM, but past nominal reach"
    assert (middle * 1000 - sixteen_qam) / sixteen_qam < 0.02, (
        "and it is past it by under two percent, so only OSNR can settle it"
    )
    assert longest * 1000 > sixteen_qam, "this one fails at 16QAM"
    assert longest * 1000 < qpsk, "and is viable at QPSK"


# ---------------------------------------------------------------------------
# Claim 2: Frankfurt to Milan, and the 1028 microsecond penalty. CONFIRMED.
# ---------------------------------------------------------------------------
def test_the_frankfurt_to_milan_pair_is_780_and_990_km() -> None:
    """The seed data must produce the two quoted lengths exactly, not merely be
    consistent with them."""
    routes = _simple_paths("fra", "mil")
    assert [round(m_to_km(metres)) for metres, _, _ in routes[:2]] == [780, 990]
    assert [list(sites) for _, sites, _ in routes[:2]] == [["fra", "mil"], ["fra", "gva", "mil"]]
    assert round(m_to_km(routes[2][0])) - 990 >= 500, "the third route must not be a near-tie"


def test_the_detour_fits_five_milliseconds_which_is_why_the_design_was_corrected() -> None:
    """A 5 ms latency budget does not make the Geneva detour unprovisionable:
    4.85 ms is inside 5 ms with 152 µs to spare. Four milliseconds is the budget
    that actually separates the two routes. This test exists so the figure
    cannot drift back to 5 ms.
    """
    detour_ns = propagation_delay_ns(_simple_paths("fra", "mil")[1][0], GROUP_INDEX_G652_MILLI)
    direct_ns = propagation_delay_ns(_simple_paths("fra", "mil")[0][0], GROUP_INDEX_G652_MILLI)
    assert detour_ns < 5_000_000, "a 5 ms budget does not separate the two routes"
    assert direct_ns < 4_000_000 < detour_ns, "4 ms is the budget that separates the two routes"


# ---------------------------------------------------------------------------
# Claim 4: the chromatic dispersion gate. FALSIFIED, and the design corrected.
# ---------------------------------------------------------------------------
def test_paris_to_madrid_is_the_longest_section_not_the_longest_route() -> None:
    """The longest single section and the longest end-to-end route are two
    different things, and this pins the first of them."""
    lengths = _section_length_m()
    longest = max(lengths, key=lambda name: lengths[name])
    assert longest == "oms-par-mad"
    assert round(m_to_km(lengths[longest])) == 1250


def test_the_longest_route_trips_the_dispersion_gate() -> None:
    """The chromatic dispersion gate does trip in this topology. The longest."""
    families = {str(span["fiber_type"]) for span in objects_of_kind("OtnFiberSpan")}
    assert families == {"G.652.D"}, "one dispersion constant only holds while the plant is one fiber family"

    sites = sorted({site for site in _graph()})
    worst_km: int = 0
    worst_sites: tuple[str, ...] = ()
    for index, start in enumerate(sites):
        for end in sites[index + 1 :]:
            paths = _simple_paths(start, end, max_hops=7)
            if paths and round(m_to_km(paths[0][0])) > worst_km:
                worst_km, worst_sites = round(m_to_km(paths[0][0])), paths[0][1]

    assert worst_km == 2970
    assert list(worst_sites) == ["mad", "par", "fra", "prg", "waw"]

    accumulated_fs_per_nm = worst_km * DISPERSION_FS_PER_NM_KM
    assert accumulated_fs_per_nm == 50_490_000

    modes = {str(mode["name"]): int(mode["cd_tolerance_fs_per_nm"]) for mode in objects_of_kind("OtnOpticalMode")}
    assert accumulated_fs_per_nm > modes["DP-16QAM 64GBd 400G"], "the gate does trip at 400G 16QAM"
    assert accumulated_fs_per_nm > modes["DP-QPSK 128GBd 400G"], "and at 400G QPSK"
    assert accumulated_fs_per_nm < modes["DP-QPSK 32GBd 100G"], "but not at 100G, which is why it is a marginal finding"


# ---------------------------------------------------------------------------
# Capacity, and the corrected section 3.8 reading.
# ---------------------------------------------------------------------------
def test_channel_references_are_quoted_strings() -> None:
    """The human-friendly identifier is a Number attribute and a bare integer is."""
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        assert isinstance(carrier["channel"], str), (
            f"{carrier['name']} references its channel as {type(carrier['channel'])}"
        )
    for device in objects_of_kind("OtnMuxDemux"):
        for wavelength in device.get("cwdm_channels") or []:
            assert isinstance(wavelength, str), (
                f"{device['name']} references wavelength {wavelength} as {type(wavelength)}"
            )


@cache
def _seeded_intervals() -> dict[str, tuple[tuple[int, int], ...]]:
    """Section name -> the half-open interval each carrier on it occupies.

    Rebuilt from `objects/` rather than from the generator's own packer, so this
    module measures what shipped and not what the packer intended.
    """
    bauds = {str(mode["name"]): int(mode["baud_mbaud"]) for mode in objects_of_kind("OtnOpticalMode")}
    held: dict[str, list[tuple[int, int]]] = {str(section["name"]): [] for section in _sections()}
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        center = channel_to_frequency_mhz(int(str(carrier["channel"])))
        interval = carrier_interval_mhz(center, bauds[str(carrier["optical_mode"])])
        for section in carrier["sections"]:
            held[str(section)].append(interval)
    return {name: tuple(sorted(intervals)) for name, intervals in held.items()}


def test_the_seeded_plan_fits_the_c_band_on_every_section() -> None:
    """FR-020. The plan cannot ask for more spectrum than a section has."""
    for name, intervals in _seeded_intervals().items():
        occupied = sum(upper - lower for lower, upper in intervals)
        assert occupied <= CBAND_EXTENT_MHZ, f"{name} asks for {occupied} MHz of a {CBAND_EXTENT_MHZ} MHz band"
        for lower, upper in intervals:
            assert lower >= CBAND_LOWER_EDGE_MHZ, f"{name} holds a carrier starting at {lower} MHz, below the band"
            assert upper <= CBAND_UPPER_EDGE_MHZ, f"{name} holds a carrier ending at {upper} MHz, above the band"
        for earlier, later in pairwise(intervals):
            assert earlier[1] <= later[0], f"{name} holds overlapping carriers {earlier} and {later}"

    busiest = max(_seeded_intervals().items(), key=lambda item: sum(upper - lower for lower, upper in item[1]))
    assert busiest[0] == "oms-fra-mil", "fra-mil is meant to be the section every carrier crosses"
    assert sum(upper - lower for lower, upper in busiest[1]) == 4_134_400


def test_the_free_spectrum_on_the_busiest_corridor_is_fragmented() -> None:
    """FR-014. A demo whose free spectrum is one clean block teaches the wrong."""
    blocks = free_blocks([FreeBlock(lower, upper) for lower, upper in _seeded_intervals()["oms-fra-mil"]])
    assert len(blocks) == 26
    assert sum(block.width_mhz for block in blocks) == 665_600

    widest = max(block.width_mhz for block in blocks)
    assert widest == 152_800
    narrowest_mode = occupied_width_mhz(32_000)
    widest_mode = occupied_width_mhz(128_000)
    assert sum(1 for block in blocks if block.width_mhz < widest_mode) == 25, "all but one block refuses the 128 GBd"
    assert sum(1 for block in blocks if block.width_mhz >= narrowest_mode) == 1, "one block takes the 32 GBd"

    anchorable = [
        channel
        for channel in range(1, GRID_CHANNEL_COUNT + 1)
        for baud in (64_000,)
        if anchor_fits_band(channel_to_frequency_mhz(channel), baud)
        and any(
            block.lower_mhz <= carrier_interval_mhz(channel_to_frequency_mhz(channel), baud)[0]
            and carrier_interval_mhz(channel_to_frequency_mhz(channel), baud)[1] <= block.upper_mhz
            for block in blocks
        )
    ]
    assert anchorable == [95], "exactly one anchor is left for a 400G at 64 GBd, and demo/90 is what spends it"


def test_occupancy_is_uneven_and_sixteen_sections_are_empty() -> None:
    """Deliberate, and stated rather than hidden: the 40 carriers are spent
    making one corridor congested, because a congested corridor is what the
    latency and capacity findings need."""
    loaded: dict[str, int] = {str(section["name"]): 0 for section in _sections()}
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        for section in carrier["sections"]:
            loaded[str(section)] += 1

    assert loaded["oms-fra-mil"] == 40
    assert sorted(count for count in loaded.values() if count) == [3, 3, 5, 7, 40]
    assert sum(1 for count in loaded.values() if count == 0) == 16


def test_every_carrier_is_inside_its_modes_nominal_reach() -> None:
    """A pre-provisioned wavelength that could not physically exist would make
    every capacity number a fiction."""
    modes = {str(mode["name"]): int(mode["nominal_reach_m"]) for mode in objects_of_kind("OtnOpticalMode")}
    lengths = _section_length_m()
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        metres = sum(lengths[str(section)] for section in carrier["sections"])
        reach = modes[str(carrier["optical_mode"])]
        assert metres <= reach, f"{carrier['name']} runs {metres} m on a {reach} m mode"


# ---------------------------------------------------------------------------
# The ODU layer on the base dataset.
# ---------------------------------------------------------------------------
def test_every_pre_provisioned_wavelength_arrives_lit_and_empty() -> None:
    """One line container per carrier, sized from the table, holding nothing."""
    carriers = {str(carrier["name"]) for carrier in objects_of_kind("OtnOpticalCarrier")}
    containers = objects_of_kind("OtnContainer")

    assert len(containers) == len(carriers), f"{len(containers)} line containers for {len(carriers)} carriers"
    assert {str(container["carrier"]) for container in containers} == carriers, "every carrier carries exactly one"

    for container in containers:
        name = str(container["name"])
        odu_type = str(container["odu_type"])
        assert name == f"odu-line-{container['carrier']}", f"{name} is not named after its carrier"
        assert odu_type in SLOT_TABLE, f"{name} is an {odu_type}, which is not one of the container types"
        assert container["tributary_slot_capacity"] == slot_capacity(odu_type), (
            f"{name} offers {container['tributary_slot_capacity']} slots, "
            f"and an {odu_type} offers {slot_capacity(odu_type)}"
        )
        assert container["tributary_slots"] == 0, f"{name} occupies slots in a parent it has none of"
        assert "client_signal" not in container, f"{name} names a client, and a line container carries a wavelength"
        assert "parent_container" not in container, f"{name} nests inside another container"


def test_every_line_container_is_the_type_its_carriers_line_rate_calls_for() -> None:
    """A 100G wavelength gets an ODU4 and a 400G one an ODUC4."""
    rates = {str(mode["name"]): int(mode["line_rate_gbps"]) for mode in objects_of_kind("OtnOpticalMode")}
    carriers = {str(carrier["name"]): carrier for carrier in objects_of_kind("OtnOpticalCarrier")}

    for container in objects_of_kind("OtnContainer"):
        carrier = carriers[str(container["carrier"])]
        rate = rates[str(carrier["optical_mode"])]
        assert rate in LINE_CONTAINER_BY_LINE_RATE_GBPS, (
            f"{carrier['name']} runs at {rate} Gbit/s, which no line container type covers"
        )
        expected = LINE_CONTAINER_BY_LINE_RATE_GBPS[rate]
        assert str(container["odu_type"]) == expected, (
            f"{container['name']} is an {container['odu_type']} on a {rate} Gbit/s carrier, which wants an {expected}"
        )


# ---------------------------------------------------------------------------
# Shared risk.
# ---------------------------------------------------------------------------
def _conduits_on(sections: list[str]) -> set[str]:
    spans = _spans_by_name()
    by_section = {str(section["name"]): [str(name) for name in section["spans"]] for section in _sections()}
    return {
        str(spans[name]["conduit"]) for section in sections for name in by_section[section] if "conduit" in spans[name]
    }


def test_the_berlin_routes_include_both_a_diverse_pair_and_a_shared_pair() -> None:
    """A diversity report needs both outcomes present to be worth running."""
    routes = _simple_paths("ber", "ams")
    first, second, third = (set(route[2]) for route in routes[:3])

    assert not first & second, "routes 1 and 2 must share no section"
    assert not _conduits_on(sorted(first)) & _conduits_on(sorted(second)), "routes 1 and 2 must be fully diverse"

    assert second & third, "routes 2 and 3 share the Amsterdam-Frankfurt section"
    assert "cd-ber-south" in _conduits_on(sorted(second)) & _conduits_on(sorted(third))


def test_the_two_frankfurt_to_milan_routes_share_the_frankfurt_exit_trench() -> None:
    """The shared-risk case the exposure report exists to flag: an AI service
    between JUPITER and Leonardo meets one backhoe on either route."""
    direct, detour = (set(route[2]) for route in _simple_paths("fra", "mil")[:2])
    assert "cd-fra-south" in _conduits_on(sorted(direct)) & _conduits_on(sorted(detour))


def test_most_spans_are_in_no_conduit() -> None:
    """If every span is in a conduit, every route is non-diverse and the report
    says nothing."""
    spans = objects_of_kind("OtnFiberSpan")
    in_conduit = [span for span in spans if "conduit" in span]
    assert len(in_conduit) == 20
    assert len(in_conduit) / len(spans) < 0.25


def test_madrid_is_single_homed() -> None:
    """The diversity report needs a genuine zero, and MareNostrum 5 is it."""
    assert len(_graph()["mad"]) == 1
    assert len(_simple_paths("par", "mad")) == 1


@pytest.mark.parametrize(
    "site", ["ams", "ber", "bru", "cph", "fra", "gva", "ham", "lon", "mil", "par", "prg", "vie", "waw"]
)
def test_every_site_except_madrid_has_at_least_two_degrees(site: str) -> None:
    assert len(_graph()[site]) >= 2


# ---------------------------------------------------------------------------
# What the channel monitors report.
# ---------------------------------------------------------------------------
MONITOR_KINDS = (
    "OtnAmplifierMonitor",
    "OtnRoadmDegreeMonitor",
    "OtnMuxDemuxMonitor",
    "OtnRamanMonitor",
    "OtnReceiverMonitor",
)


def _channel_counts(kind: str) -> dict[tuple[str, str], int]:
    """(device, monitor name) -> the channel count it reports, for one kind."""
    return {
        (str(record["device"]), str(record["name"])): int(record["channel_count"])
        for record in objects_of_kind(kind)
        if "channel_count" in record
    }


def test_no_monitor_reports_seventy_one_channels() -> None:
    """The regression test for the defect feature 024 exists to fix."""
    offenders = [
        f"{kind} {device} {name}"
        for kind in MONITOR_KINDS
        for (device, name), count in _channel_counts(kind).items()
        if count == 71
    ]
    assert not offenders, "monitors still reporting the pre-021 carrier plan:\n" + "\n".join(offenders)


def test_every_degree_monitor_reports_the_light_on_the_section_it_faces() -> None:
    """42 degree monitors, and the distribution is the carrier plan's, not a constant."""
    counts = _channel_counts("OtnRoadmDegreeMonitor")
    assert len(counts) == 42
    assert sorted(Counter(counts.values()).items()) == [(0, 32), (3, 4), (5, 2), (7, 2), (40, 2)]


def test_each_dense_multiplexer_monitor_reports_the_channels_terminating_at_its_site() -> None:
    """The fourteen AWG multiplexers, by name, because nothing else can check them."""
    counts = {device: count for (device, _), count in _channel_counts("OtnMuxDemuxMonitor").items()}
    cwdm = {"mux-ams-02", "mux-asp-01"}
    dense = {device: count for device, count in counts.items() if device not in cwdm}
    expected = {"mux-mil-01": 37, "mux-fra-01": 25, "mux-ams-01": 7, "mux-ber-01": 5, "mux-par-01": 3, "mux-vie-01": 3}

    assert len(dense) == 14
    for device, channels in expected.items():
        assert dense[device] == channels, device
    assert sorted(device for device, count in dense.items() if count == 0) == sorted(set(dense) - set(expected))


def test_the_two_cwdm_multiplexer_monitors_report_their_four_wavelengths() -> None:
    """The one case where the relationship exists, so the count is read off it."""
    counts = {device: count for (device, _), count in _channel_counts("OtnMuxDemuxMonitor").items()}
    assert counts["mux-ams-02"] == 4
    assert counts["mux-asp-01"] == 4


@cache
def _terminations_from_the_object_files() -> dict[str, int]:
    """Site shortname -> the carrier ends that land there, rebuilt from objects/."""
    endpoints = _section_endpoints()
    counts = {str(site["shortname"]): 0 for site in objects_of_kind("OtnSite")}
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        along: Counter[str] = Counter()
        for section in carrier["sections"]:
            along.update(endpoints[str(section)])
        ends = sorted(site for site, times in along.items() if times == 1)
        assert len(ends) == 2, f"{carrier['name']} rides a route with {len(ends)} ends, not two"
        for site in ends:
            counts[site] += 1
    return counts


@cache
def _transponders_by_site() -> dict[str, int]:
    counts: Counter[str] = Counter(str(box["site"]) for box in objects_of_kind("OtnTransponder"))
    return dict(counts)


@cache
def _line_ports_on_transponders() -> tuple[dict[str, Any], ...]:
    """The line ports that hang off a transponder, which used to be all of them."""
    transponders = {str(box["name"]) for box in objects_of_kind("OtnTransponder")}
    return tuple(port for port in objects_of_kind("OtnLinePort") if str(port["device"]) in transponders)


@cache
def _line_ports_on_odu_switches() -> tuple[dict[str, Any], ...]:
    switches = {str(box["name"]) for box in objects_of_kind("OtnOduSwitch")}
    return tuple(port for port in objects_of_kind("OtnLinePort") if str(port["device"]) in switches)


def test_every_line_port_sits_on_a_transponder_or_an_odu_switch() -> None:
    """The two populations account for all of them, so neither test can miss one."""
    counted = len(_line_ports_on_transponders()) + len(_line_ports_on_odu_switches())
    assert counted == len(objects_of_kind("OtnLinePort")), "a line port sits on neither a transponder nor an O-E-O"


def test_a_regenerator_carries_two_dark_line_ports_and_a_cross_connect_none() -> None:
    """An O-E-O regenerator receives a wavelength and transmits a new one."""
    by_device: dict[str, list[str]] = {}
    for port in _line_ports_on_odu_switches():
        by_device.setdefault(str(port["device"]), []).append(str(port["name"]))

    modes = {str(box["name"]): str(box["switching_mode"]) for box in objects_of_kind("OtnOduSwitch")}
    regenerators = sorted(name for name, mode in modes.items() if mode == "regenerator")
    cross_connects = sorted(name for name, mode in modes.items() if mode == "cross_connect")

    assert regenerators == ["oeo-fra-01"], "the shipped dataset holds one regenerator"
    assert cross_connects == ["oxc-fra-01", "oxc-mil-01"]

    assert sorted(by_device) == regenerators, "line ports on an O-E-O device that does not regenerate"
    assert all(sorted(names) == ["L1", "L2"] for names in by_device.values()), by_device

    named = _line_ports_by_carrier()
    lit = [
        f"{port['device']}/{port['name']}"
        for port in _line_ports_on_odu_switches()
        if (str(port["device"]), str(port["name"])) in named or "center_frequency_mhz" in port
    ]
    assert not lit, "a shipped regenerator port is bound to a wavelength that already has two ends: " + "; ".join(lit)


def test_each_pop_carries_a_transponder_per_two_terminations_above_a_floor_of_two() -> None:
    """The placement rule, `max(2, ceil(terminations / 2))`, checked against the data."""
    terminations = _terminations_from_the_object_files()
    placed = _transponders_by_site()

    assert terminations["asp"] == 0
    assert "asp" not in placed, "Amsterdam Science Park is a CWDM tail campus and carries no transponder"

    expected = {site: max(2, ceil(count / 2)) for site, count in terminations.items() if site != "asp"}
    assert placed == expected, f"transponder placement disagrees with the rule: {placed} against {expected}"
    assert sum(placed.values()) == 59


@cache
def _line_ports_by_carrier() -> dict[tuple[str, str], str]:
    """(transponder, line port) -> the wavelength that names it, read from the carriers."""
    named: dict[tuple[str, str], str] = {}
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        for device, port in carrier["line_ports"]:
            key = (str(device), str(port))
            assert key not in named, f"{device}/{port} is named by both {named.get(key)} and {carrier['name']}"
            named[key] = str(carrier["name"])
    return named


def test_every_bound_line_port_is_tuned_to_its_wavelengths_channel() -> None:
    """A port and the channel object behind it cannot disagree."""
    channel_of = {str(carrier["name"]): int(carrier["channel"]) for carrier in objects_of_kind("OtnOpticalCarrier")}
    named = _line_ports_by_carrier()
    assert len(named) == 80, f"forty wavelengths at two ends each is eighty bound ports, not {len(named)}"

    mismatched: list[str] = []
    dark_but_coloured: list[str] = []
    bound_but_dark: list[str] = []
    for port in objects_of_kind("OtnLinePort"):
        key = (str(port["device"]), str(port["name"]))
        carrier = named.get(key)
        if carrier is None:
            if "center_frequency_mhz" in port:
                dark_but_coloured.append(f"{key[0]}/{key[1]}")
            continue
        if "center_frequency_mhz" not in port:
            bound_but_dark.append(f"{key[0]}/{key[1]} terminates {carrier}")
            continue
        expected = channel_to_frequency_mhz(channel_of[carrier])
        if int(port["center_frequency_mhz"]) != expected:
            mismatched.append(f"{key[0]}/{key[1]} on {carrier}: {port['center_frequency_mhz']} against {expected}")

    assert not bound_but_dark, "line ports terminate a wavelength and state no frequency: " + "; ".join(bound_but_dark)
    assert not dark_but_coloured, "line ports state a frequency and terminate nothing: " + "; ".join(dark_but_coloured)
    assert not mismatched, "line port frequency disagrees with its wavelength's channel: " + "; ".join(mismatched)


def test_thirty_eight_line_ports_are_dark_and_the_arithmetic_says_which() -> None:
    """A dark line port is a legal state, and where the 38 sit follows from the floor."""
    terminations = _terminations_from_the_object_files()
    placed = _transponders_by_site()
    named = _line_ports_by_carrier()

    site_of = {str(box["name"]): str(box["site"]) for box in objects_of_kind("OtnTransponder")}
    dark: Counter[str] = Counter()
    for port in _line_ports_on_transponders():
        if (str(port["device"]), str(port["name"])) not in named:
            dark[site_of[str(port["device"])]] += 1

    expected = {site: 2 * count - terminations[site] for site, count in placed.items()}
    assert dict(dark) == {site: count for site, count in expected.items() if count}
    assert sum(dark.values()) == 38, f"38 dark transponder line ports out of 118, not {sum(dark.values())}"

    assert sorted(site for site, count in dark.items() if count == 4) == [
        "bru",
        "cph",
        "gva",
        "ham",
        "lon",
        "mad",
        "prg",
        "waw",
    ]
    assert sorted(site for site, count in dark.items() if count == 1) == ["ams", "ber", "fra", "mil", "par", "vie"]
    assert all(terminations[site] % 2 for site, count in dark.items() if count == 1)


def test_no_pop_drops_below_the_floor_and_a_dark_pop_sits_on_it() -> None:
    """The floor is what keeps the shipped demo scenarios lightable."""
    terminations = _terminations_from_the_object_files()
    placed = _transponders_by_site()

    below = {site: count for site, count in placed.items() if count < 2}
    assert not below, f"PoPs below the floor of two transponders: {below}"

    dark = sorted(site for site in placed if terminations[site] == 0)
    assert dark == ["bru", "cph", "gva", "ham", "lon", "mad", "prg", "waw"]
    assert all(placed[site] == 2 for site in dark), {site: placed[site] for site in dark}

    site_of = {str(box["name"]): str(box["site"]) for box in objects_of_kind("OtnTransponder")}
    unlit = [port for port in _line_ports_on_transponders() if site_of[str(port["device"])] in set(dark)]
    assert len(unlit) == 4 * len(dark), "a dark PoP should carry four line ports, two per transponder"
    named = _line_ports_by_carrier()
    bound = [
        f"{port['device']}/{port['name']}"
        for port in unlit
        if (str(port["device"]), str(port["name"])) in named or "center_frequency_mhz" in port
    ]
    assert not bound, "line ports at a PoP that terminates nothing are bound to a wavelength: " + "; ".join(bound)


def test_the_add_drop_client_and_line_port_populations_stay_one_to_one() -> None:
    """Every line port needs an add/drop port to patch into and a client port beside it."""
    line = _line_ports_on_transponders()
    assert len(objects_of_kind("OtnRoadmAddDropPort")) == len(line)
    assert len(objects_of_kind("OtnClientPort")) == len(line)

    per_device = {
        "OtnLinePort": Counter(str(port["device"]) for port in line),
        "OtnClientPort": Counter(str(port["device"]) for port in objects_of_kind("OtnClientPort")),
    }
    assert set(per_device["OtnLinePort"]) == set(per_device["OtnClientPort"])
    assert set(per_device["OtnLinePort"].values()) == {2}
    assert set(per_device["OtnClientPort"].values()) == {2}

    add_drop: dict[str, list[str]] = {}
    for port in objects_of_kind("OtnRoadmAddDropPort"):
        add_drop.setdefault(str(port["device"]), []).append(str(port["name"]))
    misnumbered = {
        roadm: sorted(names)
        for roadm, names in add_drop.items()
        if sorted(names) != [f"AD-{index:02d}" for index in range(1, len(names) + 1)]
    }
    assert not misnumbered, f"add/drop numbering does not start at AD-01 and run contiguous: {misnumbered}"

    assert not [port for port in objects_of_kind("OtnRoadmAddDropPort") if "connected_to" in port]
    assert all("connected_to" in port for port in line)


# ---------------------------------------------------------------------------
# What the receiver monitors report.
#
# Before feature 025 every one of them reported the same six numbers, so a
# receiver on a dark transponder in Copenhagen claimed the identical 25.1 dB as
# one terminating a wavelength in Milan. These tests are what stops that coming
# back: the readings now follow the route, and a transponder with no wavelength
# says it is dark.
# ---------------------------------------------------------------------------
RECEIVER_READINGS = (
    "rx_power_mdbm",
    "measured_osnr_mdb",
    "pre_fec_ber_ppb",
    "q_factor_mdb",
    "cd_fs_per_nm",
    "dgd_fs",
)


@cache
def _receiver_monitors() -> dict[str, dict[str, Any]]:
    """Transponder name -> its one receiver monitor. One per device, and the kind
    is mandatory on all six readings, so a missing key is a fault rather than a gap."""
    monitors = {str(record["device"]): record for record in objects_of_kind("OtnReceiverMonitor")}
    assert len(monitors) == len(objects_of_kind("OtnReceiverMonitor")), "a transponder carries two receiver monitors"
    assert set(monitors) == {str(box["name"]) for box in objects_of_kind("OtnTransponder")}
    return monitors


@cache
def _receiver_bounds() -> dict[str, tuple[int, int]]:
    """Reading -> (min, max), read out of `schemas/otn_ports.yml`."""
    document = yaml.safe_load((SCHEMA_DIR / "otn_ports.yml").read_text())
    definitions = [node for node in document.get("nodes") or [] if node["name"] == "ReceiverMonitor"]
    assert len(definitions) == 1, "schemas/otn_ports.yml defines ReceiverMonitor other than once"
    bounds = {
        attribute["name"]: (attribute["parameters"]["min_value"], attribute["parameters"]["max_value"])
        for attribute in definitions[0]["attributes"]
        if "parameters" in attribute
    }
    assert set(RECEIVER_READINGS) <= set(bounds), f"a reading lost its declared bounds: {sorted(bounds)}"
    return bounds


@cache
def _lit_transponders() -> set[str]:
    """The transponders holding at least one wavelength, from the carrier side of the edge."""
    return {device for device, _ in _line_ports_by_carrier()}


def test_every_receiver_reading_is_inside_the_bounds_its_schema_attribute_declares() -> None:
    """FR-019, over all 59 monitors and both branches of the derivation."""
    bounds = _receiver_bounds()
    offenders = [
        f"{device}.{reading} = {record[reading]}, outside {bounds[reading]}"
        for device, record in sorted(_receiver_monitors().items())
        for reading in RECEIVER_READINGS
        if not bounds[reading][0] <= int(record[reading]) <= bounds[reading][1]
    ]
    assert not offenders, "receiver readings outside their declared bounds: " + "; ".join(offenders)


def test_the_lit_receivers_report_five_dispersion_figures_for_the_five_routes() -> None:
    """FR-016 and SC-005. Dispersion follows the route, so five routes give five figures."""
    lengths = _section_length_m()
    by_carrier = {
        str(carrier["name"]): sum(lengths[str(section)] for section in carrier["sections"])
        for carrier in objects_of_kind("OtnOpticalCarrier")
    }
    expected = {round(m_to_km(metres)) * DISPERSION_FS_PER_NM_KM for metres in by_carrier.values()}
    assert len(expected) == 5, f"the carrier plan no longer walks five distinct routes: {sorted(expected)}"

    monitors = _receiver_monitors()
    reported = {int(monitors[device]["cd_fs_per_nm"]) for device in _lit_transponders()}
    assert reported == expected, f"lit receivers report {sorted(reported)}, the routes give {sorted(expected)}"

    assert 2 * min(reported) < max(reported) < 3 * min(reported), (
        f"the longest route should carry about twice the dispersion of the shortest, not "
        f"{max(reported)} against {min(reported)}"
    )


def test_amsterdam_to_milan_and_berlin_to_milan_report_different_osnr() -> None:
    """The regression test for critique finding E-002, and the reason the cascade is."""
    monitors = _receiver_monitors()
    # A transponder holds one monitor and can hold two wavelengths, and the
    # monitor derives from the one on L1. So the reading is attributed to the L1
    # wavelength's leg and to no other, which is what keeps a hub site's
    # transponders out of a leg they only pass through.
    osnr: dict[str, set[int]] = {}
    for (device, port), carrier in _line_ports_by_carrier().items():
        if port != "L1":
            continue
        leg = "-".join(str(carrier).rsplit("-", 2)[-2:])
        osnr.setdefault(leg, set()).add(int(monitors[device]["measured_osnr_mdb"]))

    assert len(osnr["ams-mil"]) == 1 and len(osnr["ber-mil"]) == 1, (
        f"one route should give one OSNR figure: {osnr['ams-mil']} and {osnr['ber-mil']}"
    )
    ams = osnr["ams-mil"].pop()
    ber = osnr["ber-mil"].pop()
    assert ams != ber, "Amsterdam to Milan and Berlin to Milan report the same OSNR: the cascade has been flattened"
    assert ams > ber, f"Amsterdam has the shorter spans and should read better than Berlin, not {ams} against {ber}"


def test_every_dark_transponder_reports_loss_of_signal() -> None:
    """FR-008a. Sixteen transponders hold no wavelength, and none of them claims health."""
    monitors = _receiver_monitors()
    dark = sorted(set(monitors) - _lit_transponders())
    assert len(dark) == 16, f"the floor of two leaves sixteen transponders unlit, not {len(dark)}: {dark}"

    expected = {
        "rx_power_mdbm": -40_000,
        "measured_osnr_mdb": 0,
        "pre_fec_ber_ppb": 500_000_000,
        "q_factor_mdb": 0,
        "cd_fs_per_nm": 0,
        "dgd_fs": 0,
    }
    for device in dark:
        actual = {reading: int(monitors[device][reading]) for reading in RECEIVER_READINGS}
        assert actual == expected, f"{device} holds no wavelength and does not report loss of signal: {actual}"

    claiming = [device for device in dark if int(monitors[device]["measured_osnr_mdb"]) != 0]
    assert not claiming, f"a dark transponder reports a non-zero OSNR: {claiming}"

    assert all(int(monitors[device]["measured_osnr_mdb"]) > 0 for device in _lit_transponders()), (
        "a lit transponder reports no OSNR, which would make the dark reading meaningless"
    )


def _first_lit_carrier() -> Any:
    """One carrier off the seed, as `_receiver_readings` is handed it."""
    module = _generator_module()
    carriers = module.build_carriers()
    assert carriers, "the carrier plan is empty, so this guard is vacuous"
    return carriers[0]


def test_a_carrier_that_misses_its_required_osnr_reports_a_floored_reading_and_not_a_crash() -> None:
    """The generator has to be able to emit a wavelength that fails its mode."""
    module = _generator_module()
    carrier = _first_lit_carrier()
    subject = str(carrier["name"])
    mode = str(carrier["optical_mode"])
    healthy = module._receiver_readings(carrier)
    assert healthy["q_factor_mdb"] > 0 and healthy["measured_osnr_mdb"] > 0, healthy

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "_mode_required_osnr_mdb", lambda: {mode: 10_000_000})
        readings = module._guard_receiver_readings(subject, module._receiver_readings(carrier))
    assert readings["q_factor_mdb"] == 0, readings
    assert readings["pre_fec_ber_ppb"] == module.NOISE_LIMIT_BER_PPB, readings
    assert readings["measured_osnr_mdb"] == healthy["measured_osnr_mdb"], readings

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "TRANSPONDER_IMPLEMENTATION_PENALTY_MDB", 10_000_000)
        readings = module._guard_receiver_readings(subject, module._receiver_readings(carrier))
    assert readings["measured_osnr_mdb"] == 0, readings
    assert readings["q_factor_mdb"] == 0, readings


def test_the_receiver_guard_raises_when_it_cannot_resolve_the_bounds_it_guards_against() -> None:
    """A guard that cannot find its bounds must fail, not pass everything."""
    module = _generator_module()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "_schema_bounds", dict)
        with pytest.raises(ValueError, match="declares no min_value or max_value"):
            module._guard_receiver_readings("a carrier", dict(module.DARK_RECEIVER_READINGS))
