#!/usr/bin/env python
"""Generate the GEANT dataset object files from a compact seed.

Roughly 1600 records are produced here, not one of them typed by hand.
`objects/1*.yml` is output, never input.

The seed is the seven tables below. Everything else is arithmetic over them. To
change the network, change a table and re-run; `tests/unit/test_geant_dataset.py`
regenerates and diffs, so a hand edit under `objects/` fails on the next run.

Run with no arguments to write. Run with `--check` to regenerate into a
temporary directory and diff against what is committed, exiting non-zero on any
difference.

Ten rules bind the output, each one a load failure or a lint failure rather than
a style preference, and all ten are enforced by the test module. The ones worth
knowing before editing: a channel reference must be a quoted string, the section
writes the span list rather than the span writing its section, and
`connected_to` is declared on one side only.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import math
import sys
import tempfile
from collections import namedtuple
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from infrahub_demo_otn.budget import (
    LAUNCH_POWER_PER_CHANNEL_MDBM,
    SpanInput,
    cascade_osnr_mdb,
    osnr_stage_mdb,
    span_dispersion_fs_per_nm,
    span_fiber_loss_mdb,
)
from infrahub_demo_otn.containers import LINE_CONTAINER_BY_LINE_RATE_GBPS, slot_capacity
from infrahub_demo_otn.monitors import (
    channels_by_section,
    channels_terminating_by_site,
    degree_port_name,
    monitor_port_name,
)
from infrahub_demo_otn.units import (
    CBAND_EXTENT_MHZ,
    GRID_CHANNEL_COUNT,
    M_PER_KM,
    MDB_PER_DB,
    anchor_fits_band,
    carrier_interval_mhz,
    channel_to_frequency_mhz,
    free_blocks,
    km_to_m,
    occupied_width_mhz,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECT_DIR = REPO_ROOT / "objects"
SCHEMA_DIR = REPO_ROOT / "schemas"
MANIFEST = Path(__file__).resolve().parent / "geant_manifest.json"

# The mode catalog, hand-maintained input to this script. It holds the line rate
# of every mode the carrier plan names, and that rate is what picks a carrier's
# line container type. Read rather than restated: a second copy of a line rate
# in this file is a second place to forget.
OPTICAL_MODES = OBJECT_DIR / "03_optical_modes.yml"

# The fibre catalog, hand-maintained input on the same terms. It holds the
# attenuation and dispersion coefficients the receiver readings are derived
# from, and reading them keeps `objects/01_fiber_types.yml` the one place a
# coefficient is stated.
FIBER_TYPES = OBJECT_DIR / "01_fiber_types.yml"

# The standard group the network_map artifact targets, declared in
# `objects/00_groups.yml` and joined from the site side below. The name is
# repeated in exactly these two places and nowhere else.
SITE_GROUP = "otn_sites"

# --------------------------------------------------------------------------
# Seed table 1: the fourteen sites.
#
# Coordinates are city centres in microdegrees. `shortname` is the
# human-friendly identifier and every other table refers to a site by it.
# --------------------------------------------------------------------------
SITES: list[tuple[str, str, int, int]] = [
    ("Amsterdam", "ams", 52370216, 4895168),
    ("Berlin", "ber", 52520008, 13404954),
    ("Brussels", "bru", 50850346, 4351721),
    ("Copenhagen", "cph", 55676098, 12568337),
    ("Frankfurt", "fra", 50110922, 8682127),
    ("Geneva", "gva", 46204391, 6143158),
    ("Hamburg", "ham", 53551086, 9993682),
    ("London", "lon", 51507351, -127758),
    ("Madrid", "mad", 40416775, -3703790),
    ("Milan", "mil", 45464204, 9189982),
    ("Paris", "par", 48856614, 2352222),
    ("Prague", "prg", 50075538, 14437800),
    ("Vienna", "vie", 48208174, 16373819),
    ("Warsaw", "waw", 52229676, 21012229),
]

# --------------------------------------------------------------------------
# Seed table 2: the twenty-one optical multiplex sections.
#
# Lengths are illustrative fiber route distances, roughly 1.3 times great
# circle. Seven of the twenty-one are load-bearing and must not be changed
# without changing a design claim or an existing test with them:
#
#   ham-ber 330 + ams-ham 470 = 800   Berlin to Amsterdam, route 1
#   ber-fra 540 + ams-fra 470 = 1010  route 2
#   ber-prg 330 + prg-fra 420 + ams-fra 470 = 1220  route 3
#   fra-mil 780                       the AI scenario, direct
#   fra-gva 590 + gva-mil 400 = 990   the AI scenario, via Geneva
#   ams-bru 220                       the unique shortest section, settles 400ZR
#   par-mad 1250                      the longest section
# --------------------------------------------------------------------------
SECTIONS: list[tuple[str, str, int]] = [
    ("ams", "bru", 220),
    ("ams", "lon", 460),
    ("ams", "ham", 470),
    ("ams", "fra", 470),
    ("bru", "par", 320),
    ("lon", "par", 460),
    ("par", "fra", 620),
    ("par", "mad", 1250),
    ("ham", "ber", 330),
    ("ham", "cph", 380),
    ("ber", "cph", 480),
    ("ber", "fra", 540),
    ("ber", "prg", 330),
    ("prg", "fra", 420),
    ("prg", "vie", 340),
    ("prg", "waw", 680),
    ("vie", "waw", 700),
    ("vie", "mil", 800),
    ("fra", "gva", 590),
    ("fra", "mil", 780),
    ("gva", "mil", 400),
]

MAX_SPAN_KM = 90
"""Amplifier spacing ceiling. A section takes the fewest spans that stay under it."""

# --------------------------------------------------------------------------
# Seed table 3: the twelve conduits.
#
# A conduit is one trench leaving one PoP. Membership is on the span, not the
# section: a section crosses several trenches and a trench carries spans
# from several sections. `first` is the span at the section's A end, `last` the
# span at its B end.
#
# Two findings this arrangement exists to produce. Berlin to Amsterdam routes 1
# and 2 share nothing; routes 2 and 3 share both cd-ber-south and the whole of
# the Amsterdam-Frankfurt section. And the two Frankfurt-to-Milan routes share
# cd-fra-south, so an AI service between JUPITER and Leonardo is exposed to one
# backhoe outside Frankfurt whichever route it takes.
# --------------------------------------------------------------------------
CONDUITS: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    ("cd-ams-northeast", "Amsterdam north-east duct", [("ams", "ham", "first")]),
    ("cd-ams-southeast", "Amsterdam south-east duct", [("ams", "fra", "first")]),
    ("cd-ams-west", "Amsterdam western duct", [("ams", "bru", "first"), ("ams", "lon", "first")]),
    ("cd-ber-north", "Berlin northern duct", [("ham", "ber", "last"), ("ber", "cph", "first")]),
    ("cd-ber-south", "Berlin southern duct", [("ber", "fra", "first"), ("ber", "prg", "first")]),
    ("cd-fra-north", "Frankfurt northern duct", [("ams", "fra", "last"), ("par", "fra", "last")]),
    ("cd-fra-south", "Frankfurt southern duct", [("fra", "gva", "first"), ("fra", "mil", "first")]),
    ("cd-lon-channel", "Channel and North Sea crossings", [("ams", "lon", "last"), ("lon", "par", "first")]),
    ("cd-mil-northeast", "Milan north-east duct", [("fra", "mil", "last"), ("vie", "mil", "last")]),
    ("cd-mil-northwest", "Milan north-west duct", [("gva", "mil", "last")]),
    ("cd-par-south", "Paris southern duct", [("par", "mad", "first")]),
    ("cd-prg-east", "Prague eastern duct", [("prg", "vie", "first"), ("prg", "waw", "first")]),
]

# --------------------------------------------------------------------------
# Seed table 4: the EuroHPC attachments.
#
# LUMI is deliberately absent. Finland is outside the fourteen-site subset and
# inventing a PoP to hold it would misrepresent the topology.
#
# A facility is a BuiltinTag on the site plus a dedicated edge router. There is
# no compute-facility kind.
# --------------------------------------------------------------------------
EUROHPC: list[tuple[str, str, str, str]] = [
    ("JUPITER", "jupiter", "fra", "Julich, Germany"),
    ("Karolina", "karolina", "prg", "Ostrava, Czechia"),
    ("Leonardo", "leonardo", "mil", "Bologna, Italy"),
    ("MareNostrum 5", "marenostrum-5", "mad", "Barcelona, Spain"),
    ("MeluXina", "meluxina", "bru", "Luxembourg"),
    ("Vega", "vega", "vie", "Maribor, Slovenia"),
]

# --------------------------------------------------------------------------
# Seed table 5: the carrier plan.
#
# 40 carriers, every one crossing fra-mil. A carrier is an end-to-end wavelength
# holding one channel on every section it crosses, because there is no
# wavelength conversion in a transparent network. That is what lets "40 carriers
# across the network" and "4,134,400 MHz of 4,800,000 MHz occupied on one
# section" both be true.
#
# **Why forty and not seventy-one.** A carrier occupies a width, not a channel
# number. The seventy-one-carrier plan this table used to hold needed 7,306,000
# MHz on fra-mil against the 4,800,000 MHz the modelled C-band has, 52 per cent
# oversubscribed, and `checks/channel_collision.py` reported 91 overlapping
# pairs against it. The mix is kept in proportion and the count comes down:
# 55 per cent at 64 GBd, 37.5 per cent at 128 GBd, 7.5 per cent at 32 GBd,
# against 56.3, 36.6 and 7.0 before. The 128 GBd 400G mode stays, because the
# high-rate story in `ai-payloads.mdx` is what it is there for.
#
# **Why the widest leg is written first.** Anchors are assigned by first fit in
# the order of this table, so the order is part of the seed. A 150,000 MHz
# carrier anchored on channel 2 sits flush against the lower band edge and
# wastes nothing there, and keeping each width in one contiguous run means the
# only spectrum lost is the sliver between two neighbours of the same width.
# Reversing the table is legal and packs worse: the 32 GBd run at the bottom
# loses 2,800 MHz to the edge and the runs no longer abut cleanly.
#
# Each mode is chosen so the route is inside the mode's nominal reach.
# --------------------------------------------------------------------------
CARRIER_PLAN: list[tuple[str, str, list[tuple[str, str]], int, str]] = [
    ("ams", "mil", [("ams", "fra"), ("fra", "mil")], 7, "DP-QPSK 128GBd 400G"),
    ("ber", "mil", [("ber", "fra"), ("fra", "mil")], 5, "DP-QPSK 128GBd 400G"),
    ("par", "mil", [("par", "fra"), ("fra", "mil")], 3, "DP-QPSK 128GBd 400G"),
    ("fra", "mil", [("fra", "mil")], 22, "DP-16QAM 64GBd 400G"),
    ("fra", "vie", [("fra", "mil"), ("vie", "mil")], 3, "DP-QPSK 32GBd 100G"),
]

# --------------------------------------------------------------------------
# Seed table 5a: the O-E-O devices.
#
# Frankfurt and Milan are the two hub sites, and they are hubs by measurement
# rather than by preference. Every one of the 40 wavelengths in the plan above
# crosses `oms-fra-mil`, so those two sites are where wavelengths actually
# terminate: 37 end at Milan and 25 at Frankfurt, against 7 at Amsterdam and
# fewer everywhere else. A cross-connect anywhere else would terminate nothing
# and be inert, which is FR-003 read from the other direction.
#
# The third device is the regenerator, and its site is a measurement too, not a
# choice: SP-002 asked which intermediate site on Madrid to Warsaw carries it,
# and R-013 answered Frankfurt at DP-QPSK 128GBd, +2.745 dB and +5.740 dB.
# Paris and Prague each leave one half short at every mode measured.
#
# Names carry the role, because `switching_mode` is what separates the two and a
# reader scanning `objects/19_geant_odu_switches.yml` should not have to open the
# record to tell a regenerator from a cross-connect. The order matters at
# Frankfurt as well: `chains.py::junction_at` takes the lowest-named device when
# several qualify, so `oeo-fra-01` is chosen over `oxc-fra-01` and the
# regeneration scenario names the device it is about.
# --------------------------------------------------------------------------
ODU_SWITCHES: list[tuple[str, str, str, int]] = [
    ("oeo-fra-01", "fra", "regenerator", 3000),
    ("oxc-fra-01", "fra", "cross_connect", 5000),
    ("oxc-mil-01", "mil", "cross_connect", 5000),
]
"""Name, site, switching mode and framing latency in nanoseconds.

3000 ns on the regenerator is the figure `tests/unit/test_budget_claims.py`
already budgets Madrid to Warsaw with, so the shipped device is what that page's
14,558,963 ns total is computed from rather than a number written twice.

5000 ns on a cross-connect is larger on purpose. A regenerator carries the whole
payload across without looking inside it; a cross-connect demultiplexes to
containers and regroups them, which is strictly more framing work.

Both are stated figures rather than vendor ones, in the manner of
`AMPLIFIER_NOISE_FIGURE_MDB`, and both are well inside the 100000 ns ceiling
`framing_latency_ns` shares with `fec_latency_ns`.
"""

# --------------------------------------------------------------------------
# Seed table 6: the CWDM tail.
#
# One customer campus, one short span into Amsterdam, one passive multiplexer at
# each end, four lit coarse wavelengths. No ROADM, no amplifier, no section, no
# pump, no carrier.
#
# **It has its own table and must not join SITES.** That table is iterated in
# `build_devices` and `build_ports` to build a full device set and a full port
# set per site, and in `degrees()` to build the routing adjacency. A campus
# placed there arrives with a ROADM, amplifiers, transponders, a patch panel and
# forty-odd ports, none of which a customer campus has. This seed feeds
# `build_sites`, `build_spans` and the multiplexer block, and nothing else.
#
# **The span deliberately has no `oms`.** That absence is the entire boundary
# between the coarse tail and the budget engine. `build_span` is reached from
# `build_section` alone, which is reached from `sections_from_graphql` alone,
# which walks the sections and reads `spans` off each one. Set `oms` here and the
# tail is in the marshaller on the next run, budgeted at a 1550 nm attenuation
# coefficient that understates its loss by 0.92 dB at 1471 nm. The guard in
# `tests/unit/test_geant_dataset.py` is what holds the boundary.
# --------------------------------------------------------------------------
CWDM_TAIL_SITE = ("Amsterdam Science Park", "asp", 52356400, 4955200)
"""The customer campus, in the shape of a SITES row. `site_type: customer`."""

CWDM_TAIL_PEER = "ams"
"""The PoP the tail lands on."""

CWDM_TAIL_LENGTH_M = 18_400
"""18.4 km. Metro distance, which is what a coarse plan is for.

Well under MAX_SPAN_KM, so it is one span. It moves the mean span from 83,636 m
to 83,146 m, which stays inside the band the span test asserts.
"""

CWDM_TAIL_WAVELENGTHS = [1471, 1491, 1511, 1531]
"""The four lit coarse wavelengths, referenced by human-friendly identifier.

Three S band and one C band, which is the set a real four-channel metro tail
uses and which makes the band point without a footnote: only 1531 is inside the
erbium window, so only one of the four could be amplified even if an amplifier
were fitted.

Written as quoted strings on the object. `center_wavelength_nm` is the
human-friendly identifier and a bare number in YAML is an integer, which the
reference resolver rejects.
"""

CWDM_TAIL_MODEL = "M-CWDM4"
"""A four-channel thin-film filter, not the 40-channel AWG the PoPs carry."""

# --------------------------------------------------------------------------
# Seed table 7: fixed values that are the same everywhere.
# --------------------------------------------------------------------------
FIBER_TYPE = "G.652.D"
"""Every span. Mixing families would change the group index and therefore every
propagation figure tests/unit/test_units.py already asserts, while exercising no
extra code path in the budget engine."""

TRIBUTARY_SITES = ["lon", "mil", "par", "waw"]
"""Where the legacy E1 handoff lives. OtnTributaryPort is the only copper port
kind in the model and would otherwise have no instance."""

SPLICE_KM_PER_DRUM = 4
"""One fusion splice per drum of cable."""

DEVICE_PROFILE: dict[str, dict[str, Any]] = {
    "OtnRoadm": {"insertion_loss_mdb": 7000, "role": "core", "model": "R-9D"},
    "OtnTransponder": {"insertion_loss_mdb": 0, "role": "edge", "model": "T-400G"},
    "OtnAmplifier": {"insertion_loss_mdb": 0, "role": "core", "model": "A-EDFA"},
    "OtnMuxDemux": {"insertion_loss_mdb": 4000, "role": "passive", "model": "M-AWG40"},
    "OtnPatchPanel": {"insertion_loss_mdb": 500, "role": "passive", "model": "P-ODF"},
    # The insertion loss is the WDM combiner that puts the pump light on the
    # fibre, not the pump laser. It is charged in both directions of travel
    # because the combiner is in line and both directions pass through it.
    "OtnRamanPump": {"insertion_loss_mdb": 500, "role": "core", "model": "RP-C14"},
    # Zero insertion loss, for the reason a transponder has zero: the device
    # terminates the light instead of passing it through, so there is no line
    # path across it to attenuate. The inherited attribute still exists and
    # still applies to the incoming segment, which is why `budget.py`'s
    # `RegeneratorInput` leaves it out of any term spanning both segments.
    "OtnOduSwitch": {"insertion_loss_mdb": 0, "role": "core", "model": "X-ODU8"},
    # No insertion loss, no vendor and no model: all three live on
    # OtnOpticalElement, which OtnRouter does not inherit because light
    # terminates at a router. One inheritance left off, three attributes gone
    # with it.
    #
    # element_class lives there too and is written by nobody. Each concrete kind
    # declares it with a default_value matching its own kind, so a value here
    # would be the same fact written twice.
    "OtnRouter": {"role": "core"},
}

VENDOR = "Generic"
"""Not a real vendor. Inventing plausible names in a public repository would
attribute performance figures to companies that never supplied them."""

AMPLIFIER_NOISE_FIGURE_MDB = 4000
"""4.0 dB on every amplifier. A good low-noise EDFA, at the favourable end of
realistic, which `docs/docs/link-budget.mdx` states rather than hides."""

PMD_COEFFICIENT_FS_PER_ROOT_KM = 100
"""0.1 ps per root kilometre, the polarisation mode dispersion of the fibre.

Differential group delay grows with the square root of length rather than with
length, which is what makes it worth a coefficient of its own instead of another
per-kilometre figure beside attenuation and dispersion. 0.1 ps/sqrt(km) is the
ITU-T G.652.D link design value for fibre drawn since the late 1990s, and it is
a stated figure in the same sense `AMPLIFIER_NOISE_FIGURE_MDB` is: plausible,
unsourced, and the same on every span so no route gets a private allowance.

It is a physical parameter of the fibre, not a unit conversion, so it belongs
here rather than in `units.py`.
"""

TRANSPONDER_IMPLEMENTATION_PENALTY_MDB = 400
"""How far below the cascade a real receiver lands, in millidecibels.

0.4 dB of implementation penalty: the transponder's own optics and DSP are not
ideal, so the OSNR it reports is a little worse than the fibre plant delivers.

Named `OSNR_DRIFT_MDB` until feature 025, with a docstring saying it was "the gap
the drift report exists to show". That was never true. `transforms/monitor_drift.py`
reads `OtnAmplifier` and `OtnRamanPump`, and `queries/monitor_drift.gql` names
`OtnAmplifierMonitor` and `OtnRamanMonitor`; no receiver kind appears in either,
so nothing has ever reported this figure as drift. It is a penalty, and the name
now says so.
"""

FEC_THRESHOLD_Q_MDB = 5700
"""5.7 dB, the Q factor at which the soft-decision FEC stops correcting.

The offset that turns an OSNR margin into a Q reading: a carrier sitting exactly
at its mode's `required_osnr_mdb` reports this Q, so a reader can tell at a
glance how far a wavelength is from the cliff. Above it the FEC closes the
errors; below it the post-FEC traffic breaks.
"""

FEC_THRESHOLD_BER_PPB = 20000000
"""2.0e-2, the pre-FEC bit error rate a receiver sees at `FEC_THRESHOLD_Q_MDB`.

The anchor for the stated BER relation in `_receiver_readings`. A 15% overhead
soft-decision FEC corrects a pre-FEC error rate of about 2e-2 down to below
1e-15, and that is the pairing quoted for coherent line cards of this class.
"""

AMPLIFIER_GAIN_MDB = 22000
"""22.0 dB on every amplifier.

Covers the largest fiber loss on any shipped span, 21.2 dB on `oms-ber-fra`,
with 0.8 dB to spare. The gain gate therefore passes on this data and fires the
moment somebody stretches a span past roughly 93 km.

**Uniform on purpose.** Giving the booster a lower gain and a higher noise
figure than the line amplifiers looks more realistic and is wrong here, because
an amplifier hut is bidirectional and this model gives it one object. A section
stored `roadm_a -> roadm_b` and traversed the other way reverses its amplifier
chain, so the booster's module ends up behind a 90 km span it was never sized
for, and the budget then depends on which ROADM the section happens to be stored
under.
"""

DIRECTION_A_TO_B = "a_to_b"
DIRECTION_B_TO_A = "b_to_a"
"""Which way a chain amplifies. No device name carries either token, and no
device stores one. A chain is identified by the relationship holding it."""

RAMAN_SECTION = ("vie", "mil")
"""The one pumped section on the default branch, Vienna to Milan.

800 km over nine spans at the second-tightest margin in the network, so pumping
it is plausible engineering rather than a prop. Main ships Raman so the report
and rendering paths are exercised here and not only on a demonstration branch,
and so no node kind ships with zero instances.

Paris to Madrid is deliberately left unpumped. It is the one section 16QAM 400G
cannot close, and that is a finding the demo needs to keep. Fixing it is a
branch and a proposed change, not an edit to this table."""

RAMAN_DIRECTION = DIRECTION_A_TO_B
"""Vienna towards Milan. One direction, because a network that pumps one way is
what makes the two chains visibly different in the report."""

RAMAN_ON_OFF_GAIN_MDB = 10000
"""10.0 dB of on-off gain per pumped span.

Under the 15000 mdB the schema enforces, and comfortably under the smallest
fibre loss on any shipped span, 16,167 mdB, so no span in this dataset is driven
anywhere near zero effective loss."""

SCHEMA_HINT = "# yaml-language-server: $schema=https://schema.infrahub.app/infrahub/object/latest.json"
GENERATED_BY = "scripts/generate_geant_dataset.py"


# --------------------------------------------------------------------------
# Derived topology. Nothing below is seed; it is all arithmetic over the tables.
# --------------------------------------------------------------------------
def site_names() -> dict[str, str]:
    """shortname -> display name."""
    return {short: name for name, short, _, _ in SITES}


def eurohpc_by_site() -> dict[str, tuple[str, str, str]]:
    """shortname -> (facility, slug, where it really is)."""
    return {site: (facility, slug, where) for facility, slug, site, where in EUROHPC}


def section_key(a: str, b: str) -> str:
    return f"oms-{a}-{b}"


def span_count(length_km: int) -> int:
    """The fewest spans that keep every span at or under the spacing ceiling."""
    return -(-length_km // MAX_SPAN_KM)


def span_lengths_m(length_km: int, count: int) -> list[int]:
    """Divide the section into whole metres that sum to it exactly.

    Integer division leaves a remainder of up to count-1 metres. Spreading it
    one metre at a time over the leading spans keeps the sum exact, which is
    what lets the test assert equality rather than approximate equality.
    """
    total = km_to_m(length_km)
    base, remainder = divmod(total, count)
    return [base + 1 if index < remainder else base for index in range(count)]


def span_name(a: str, b: str, index: int) -> str:
    return f"span-{a}-{b}-{index:02d}"


def span_fiber_geometry(length_m: int) -> dict[str, int]:
    """The loss-bearing fields of one span, from its length alone.

    One statement of the splice count, the two connectors and the ageing
    allowance, shared by the spans this script writes and by the `SpanInput`
    the receiver readings are derived through. Written twice, the two could
    disagree and the readings would then describe fibre that is not in the
    dataset.
    """
    return {
        "splice_count": round(length_m / M_PER_KM / SPLICE_KM_PER_DRUM),
        "splice_loss_mdb": 50,
        "connector_count": 2,
        "connector_loss_mdb": 300,
        "aging_margin_mdb": 1500,
    }


@lru_cache(maxsize=1)
def section_span_lengths_m() -> dict[str, list[int]]:
    """Section key -> its span lengths in metres, in sequence order.

    The same division `build_spans` writes, taken once so a route can be walked
    without rebuilding every span record in the network.
    """
    return {section_key(a, b): span_lengths_m(km, span_count(km)) for a, b, km in SECTIONS}


def carrier_route_spans_m(carrier: dict[str, Any]) -> list[int]:
    """The ordered span lengths a carrier's light crosses, end to end.

    Derived from the sections `CARRIER_PLAN` gives the wavelength, in the order
    it gives them, then from `SECTIONS` through `span_count` and
    `span_lengths_m`. Nothing is parsed back out of a name and no length is
    restated.

    This is what makes two routes of equal span count differ. Amsterdam to Milan
    and Berlin to Milan are both fifteen spans; Amsterdam's first six are 78.3 km
    and Berlin's first six are 90 km, so the losses differ and so does every
    reading derived from them.
    """
    lengths = section_span_lengths_m()
    return [length for key in carrier["sections"] for length in lengths[key]]


def _route_span_inputs(carrier: dict[str, Any]) -> list[SpanInput]:
    """One `SpanInput` per span along a carrier's route, in order.

    Built from the same geometry `build_spans` writes and the coefficients of
    the fibre those spans name, so `budget.py` sees exactly the plant that is in
    the dataset. No Raman gain is credited: the pumped section is one direction
    of `oms-vie-mil` and no carrier in the plan rides it, so crediting it here
    would be inventing a gain the light never sees.
    """
    fiber = _fiber_coefficients()
    if FIBER_TYPE not in fiber:
        raise ValueError(f"the spans name fiber type {FIBER_TYPE}, which {FIBER_TYPES.name} does not define")
    coefficients = fiber[FIBER_TYPE]
    return [
        SpanInput(
            name=f"{carrier['name']} span {index}",
            length_m=length,
            attenuation_mdb_per_km=coefficients["attenuation_mdb_per_km"],
            dispersion_fs_per_nm_km=coefficients["dispersion_fs_per_nm_km"],
            group_index_milli=coefficients["group_index_milli"],
            **span_fiber_geometry(length),
        )
        for index, length in enumerate(carrier_route_spans_m(carrier), start=1)
    ]


def degrees() -> dict[str, list[str]]:
    """shortname -> the far-end shortnames it has a section to, sorted."""
    neighbours: dict[str, list[str]] = {short: [] for _, short, _, _ in SITES}
    for a, b, _ in SECTIONS:
        neighbours[a].append(b)
        neighbours[b].append(a)
    return {site: sorted(peers) for site, peers in neighbours.items()}


def degree_sections() -> dict[tuple[str, str], str]:
    """(ROADM name, degree port name) -> the section that degree faces.

    Built from the two seed tables rather than recovered from the port name. The
    generator holds the near site and the far site while it is writing the port,
    so the section is known here and nothing has to be taken apart afterwards.
    `monitors.far_site_of_degree` is the reverse trip, and only a check reading
    the graph back needs it.

    Both orders of every pair are keyed because `SECTIONS` names each fibre once
    and both of its ends face it.
    """
    keys: dict[tuple[str, str], str] = {}
    for a, b, _ in SECTIONS:
        keys[(a, b)] = keys[(b, a)] = section_key(a, b)
    neighbours = degrees()
    return {
        (f"roadm-{short}-01", degree_port_name(far)): keys[(short, far)]
        for _, short, _, _ in SITES
        for far in neighbours[short]
    }


@lru_cache(maxsize=1)
def _terminations_by_site() -> dict[str, int]:
    """shortname -> how many carrier ends land there, across the whole plan.

    Memoised because `transponder_count` reads it once per site from both
    `build_devices` and `build_ports`, and neither of those is memoised.
    """
    counts = {short: 0 for _, short, _, _ in SITES}
    for record in carrier_endpoints(build_carriers()):
        for site in record["endpoints"]:
            counts[site] += 1
    return counts


def transponder_count(site: str) -> int:
    """`max(2, ceil(terminations / 2))`, because a transponder holds two line ports.

    The count now follows the wavelengths that actually terminate at the site,
    read from `carrier_endpoints(build_carriers())`. It used to be four where a
    EuroHPC facility attaches and two everywhere else, which tied the transponder
    population to a compute facility that says nothing about how much light a
    site terminates: Milan terminates 37 wavelengths and had four transponders,
    Brussels terminates none and had four as well.

    The floor of two is not a rounding convenience. Five of the eight sites that
    terminate nothing are endpoints of shipped demo scenarios: Madrid and Warsaw
    carry the headline refusal in `demo/06_mad_waw_16qam.yml`, Prague the
    InfiniBand service in `demo/03_infiniband_service.yml`, London the
    ten-into-one grooming runbook in `demo/04_odu_ten_in_one.yml`, and Geneva two
    HPC services in `demo/00_services.yml`.

    The floor is a modelling argument and not a functional one, and the
    difference is worth stating so nobody defends it on the wrong ground.
    `generators/optical_service.py` names no line port when it provisions a
    service, so those scenarios would still run against a site that has no
    transponder. What would not survive is the network they are read against: a
    PoP that can never terminate a wavelength is not a credible node, and a
    reader who provisions a 400G service at Madrid and finds no transponder in
    Madrid's inventory is looking at an incoherent network. Those scenarios live
    under `demo/`, loaded by hand onto a branch and never by git sync, so nothing
    in `objects/` or in this file points at them.

    Amsterdam Science Park is outside this rule. It is a customer campus on a
    CWDM tail from `CWDM_TAIL_SITE`, it belongs to no section, and `build_devices`
    does not iterate it, so it carries no transponder and keeps none.
    """
    terminations = _terminations_by_site()
    if site not in terminations:
        raise ValueError(
            f"{site} is not one of the PoPs in SITES, so it has no termination count and no transponders. "
            "Amsterdam Science Park sits in CWDM_TAIL_SITE and is deliberately not one of them."
        )
    return max(2, -(-terminations[site] // 2))


def conduit_for_span() -> dict[str, str]:
    """span name -> conduit name, for the nineteen spans that are in one."""
    lengths = {section_key(a, b): span_count(km) for a, b, km in SECTIONS}
    mapping: dict[str, str] = {}
    for conduit, _, members in CONDUITS:
        for a, b, end in members:
            index = 1 if end == "first" else lengths[section_key(a, b)]
            mapping[span_name(a, b, index)] = conduit
    return mapping


_Interval = namedtuple("_Interval", ("lower_mhz", "upper_mhz"))
"""A half-open slice of spectrum, so `units.free_blocks` can sweep the plan.

`units.SpectralInterval` is a protocol and `plant.CarrierInterval` is built from a
GraphQL payload this script never has. Two fields is the whole of what the sweep
reads.

The functional form rather than `@dataclass` or a class-based `NamedTuple`, and
that is a constraint rather than a taste: `tests/unit/test_geant_dataset.py` loads
this file through `importlib.util.module_from_spec` without registering it in
`sys.modules`, and both of those decorators look the module up there to resolve
the annotations `from __future__ import annotations` postponed. The lookup returns
`None` and the import fails before a single test runs.
"""


def carrier_anchors() -> list[int]:
    """One channel anchor per carrier, in the order `CARRIER_PLAN` writes them.

    First fit: each carrier takes the lowest channel whose occupied interval both
    lies inside the modelled C-band and clears every interval already assigned on
    a section it shares. The band test is `units.anchor_fits_band`, so a 150,000
    MHz carrier is refused channel 1 and channel 96 here rather than by the check
    afterwards, and a 79,600 MHz one is refused the same two.

    **Deterministic and order-dependent, on purpose.** Fitting inside the C-band
    admits many packings, and `tests/unit/test_geant_dataset.py` regenerates and
    diffs, so a packing that depended on set iteration or on a hash would fail
    the suite intermittently and read as a dataset bug. The plan's order is the
    only input, the scan is ascending, and the result is byte-identical on every
    run. Reordering the plan is how the packing is changed.

    Raises rather than skipping when a carrier has nowhere to go. A plan that
    does not fit is a plan to correct, not a wavelength to drop quietly.
    """
    bauds = _mode_bauds()
    occupied: dict[str, list[tuple[int, int]]] = {}
    anchors: list[int] = []
    for a, b, sections, count, mode in CARRIER_PLAN:
        if mode not in bauds:
            raise ValueError(f"the carrier plan names optical mode {mode}, which {OPTICAL_MODES.name} does not define")
        baud = bauds[mode]
        keys = [section_key(x, y) for x, y in sections]
        for index in range(count):
            for channel in range(1, GRID_CHANNEL_COUNT + 1):
                center = channel_to_frequency_mhz(channel)
                if not anchor_fits_band(center, baud):
                    continue
                lower, upper = carrier_interval_mhz(center, baud)
                if any(
                    lower < held_upper and held_lower < upper
                    for key in keys
                    for held_lower, held_upper in occupied.get(key, ())
                ):
                    continue
                for key in keys:
                    occupied.setdefault(key, []).append((lower, upper))
                anchors.append(channel)
                break
            else:
                raise ValueError(
                    f"carrier {index + 1} of {count} on the {a} to {b} leg, mode {mode} at "
                    f"{occupied_width_mhz(baud)} MHz wide, has no anchor left on "
                    f"{'|'.join(keys)}. The plan asks for more spectrum than the "
                    f"{CBAND_EXTENT_MHZ} MHz C-band holds. Reduce a count in CARRIER_PLAN."
                )
    return anchors


def carrier_plan_fit_report() -> str:
    """Occupied width per section against the C-band, one line each, widest first.

    What `quickstart.md` runs to answer "does the plan fit". Every line must
    report an occupied width at or under `CBAND_EXTENT_MHZ`; `carrier_anchors`
    raises before this can print otherwise, so a printed report is already a
    passing one and the figures are there to be read rather than to be trusted.
    """
    bauds = _mode_bauds()
    anchors = carrier_anchors()
    intervals: dict[str, list[tuple[int, int]]] = {}
    cursor = 0
    for _, _, sections, count, mode in CARRIER_PLAN:
        for _ in range(count):
            lower, upper = carrier_interval_mhz(channel_to_frequency_mhz(anchors[cursor]), bauds[mode])
            for x, y in sections:
                intervals.setdefault(section_key(x, y), []).append((lower, upper))
            cursor += 1

    lines = [f"Carrier plan fit, {len(anchors)} carriers against a {CBAND_EXTENT_MHZ} MHz C-band:"]
    ordered = sorted(intervals.items(), key=lambda item: (-sum(u - lo for lo, u in item[1]), item[0]))
    for key, held in ordered:
        occupied = sum(upper - lower for lower, upper in held)
        blocks = free_blocks([_Interval(lower, upper) for lower, upper in held])
        widest = max((block.width_mhz for block in blocks), default=0)
        lines.append(
            f"  {key}: {len(held)} carriers, {occupied} MHz occupied, "
            f"{CBAND_EXTENT_MHZ - occupied} MHz free in {len(blocks)} block(s), widest {widest} MHz"
        )
    empty = sorted(section_key(a, b) for a, b, _ in SECTIONS if section_key(a, b) not in intervals)
    lines.append(f"  {len(empty)} section(s) carry no wavelength: {', '.join(empty)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Emission.
# --------------------------------------------------------------------------
def _header(purpose: list[str]) -> str:
    """The file preamble: provenance, then what this file is for.

    Four fixed lines and no more. What a single kind explains about itself
    travels as a `notes` entry instead, so it lands on the `kind:` key it is
    about rather than on the pile every reader scrolls past first.
    """
    lines = [
        "---",
        SCHEMA_HINT,
        "#",
        f"# GENERATED by {GENERATED_BY}. Edit the generator, not this file:",
        "# tests/unit/test_geant_dataset.py regenerates and diffs on every run.",
        "#",
    ]
    lines += [f"# {line}" if line else "#" for line in purpose]
    return "\n".join(lines)


def _note(kind: str, lines: list[str], text: str) -> str:
    """Insert a note under the `kind:` key of an already-serialised document."""
    marker = f"  kind: {kind}\n"
    block = "\n".join(f"  # {line}" if line else "  #" for line in lines)
    return text.replace(marker, f"{marker}{block}\n", 1)


# --------------------------------------------------------------------------
# Default suppression.
#
# A quarter of the lines this generator used to emit repeated a value the
# schema already declares as the attribute's `default_value`. Three of them say
# nothing an operator wants from a data file: a port is enabled, its
# administrative state is up, a device is active. Those three are dropped when
# they equal the default and Infrahub supplies them on load, which was checked
# by loading the stripped dataset onto a branch and reading every one back.
#
# Every physical value stays written even when it matches. An amplifier's gain
# and noise figure, a span's splice loss, connector count and ageing margin are
# what the budget engine works on, and reading a device file to learn what an
# amplifier does should not mean opening a schema file to finish the sentence.
# --------------------------------------------------------------------------
SUPPRESSED_WHEN_DEFAULT = frozenset({"enabled", "admin_state", "status"})

# Two attributes match their default and are written anyway, and a test says so
# for each. `OtnRamanPump.injection_end` has a default only because a mandatory
# attribute needs one for the schema to load at all, so the value on the page
# has to come from the seed rather than from that loading device, and
# `test_no_pump_leaves_its_injection_end_to_the_schema_default` requires it.
# `OtnSite.site_type` matches on the fourteen pops and
# `test_the_fourteen_pops_are_the_fourteen_the_design_names_plus_one_campus`
# reads it straight off the YAML. Neither is in the set above today; naming
# them keeps that true if the set ever grows.
NEVER_SUPPRESSED = frozenset({"injection_end", "site_type"})


@lru_cache(maxsize=1)
def _schema_defaults() -> dict[str, dict[str, Any]]:
    """Every `default_value` the schema declares, resolved per node kind.

    Read out of `schemas/` rather than restated here. A default changed in the
    schema then changes what this generator writes on the next run, instead of
    leaving two copies of one fact to disagree quietly.
    """
    generics: dict[str, dict[str, Any]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    inherits: dict[str, list[str]] = {}

    for path in sorted(SCHEMA_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text())
        for section, bucket in (("generics", generics), ("nodes", nodes)):
            for definition in document.get(section) or []:
                kind = f"{definition['namespace']}{definition['name']}"
                bucket[kind] = {
                    attribute["name"]: attribute["default_value"]
                    for attribute in definition.get("attributes") or []
                    if "default_value" in attribute
                }
                inherits[kind] = list(definition.get("inherit_from") or [])

    resolved: dict[str, dict[str, Any]] = {}
    for kind, own in nodes.items():
        merged: dict[str, Any] = {}
        for parent in inherits.get(kind, []):
            merged.update(generics.get(parent, {}))
        merged.update(own)
        resolved[kind] = merged
    return resolved


@lru_cache(maxsize=1)
def _mode_line_rates() -> dict[str, int]:
    """Optical mode name to its line rate in Gbit/s, read out of the mode catalog.

    The carrier plan names a mode; the ODU layer needs the rate behind it. Taking
    it from `objects/03_optical_modes.yml` keeps one statement of what a mode
    runs at, the same way `_schema_defaults` takes defaults from `schemas/`.
    """
    rates: dict[str, int] = {}
    for document in yaml.safe_load_all(OPTICAL_MODES.read_text()):
        spec = (document or {}).get("spec") or {}
        if spec.get("kind") != "OtnOpticalMode":
            continue
        for record in spec.get("data") or []:
            rates[str(record["name"])] = int(record["line_rate_gbps"])
    return rates


def _mode_bauds() -> dict[str, int]:
    """Optical mode name to its symbol rate in MBd, read out of the mode catalog.

    The symbol rate is what sets the occupied width, and `units.occupied_width_mhz`
    is the only place that conversion happens. Read here rather than restated, the
    same way `_mode_line_rates` reads the line rate: a width written twice is a
    width that can disagree with the check.
    """
    bauds: dict[str, int] = {}
    for document in yaml.safe_load_all(OPTICAL_MODES.read_text()):
        spec = (document or {}).get("spec") or {}
        if spec.get("kind") != "OtnOpticalMode":
            continue
        for record in spec.get("data") or []:
            bauds[str(record["name"])] = int(record["baud_mbaud"])
    return bauds


@lru_cache(maxsize=1)
def _mode_required_osnr_mdb() -> dict[str, int]:
    """Optical mode name to the OSNR it needs to close, read out of the mode catalog.

    Same pattern and same reason as `_mode_bauds` and `_mode_line_rates`. A
    receiver's Q factor is its delivered OSNR measured against this figure, so a
    second copy here is a second place for the Q readings and the margin check to
    disagree about what a mode requires.
    """
    required: dict[str, int] = {}
    for document in yaml.safe_load_all(OPTICAL_MODES.read_text()):
        spec = (document or {}).get("spec") or {}
        if spec.get("kind") != "OtnOpticalMode":
            continue
        for record in spec.get("data") or []:
            required[str(record["name"])] = int(record["required_osnr_mdb"])
    return required


@lru_cache(maxsize=1)
def _fiber_coefficients() -> dict[str, dict[str, int]]:
    """Fibre type name to its attenuation, dispersion and group index.

    Read out of `objects/01_fiber_types.yml` rather than restated, the same way
    `_mode_bauds` reads the mode catalog. The spans this script writes name
    `FIBER_TYPE` and carry no coefficients of their own, so the receiver
    derivation has to fetch them from the catalog the spans point at.
    """
    coefficients: dict[str, dict[str, int]] = {}
    for document in yaml.safe_load_all(FIBER_TYPES.read_text()):
        spec = (document or {}).get("spec") or {}
        if spec.get("kind") != "OtnFiberType":
            continue
        for record in spec.get("data") or []:
            coefficients[str(record["name"])] = {
                "attenuation_mdb_per_km": int(record["attenuation_mdb_per_km"]),
                "dispersion_fs_per_nm_km": int(record["dispersion_fs_per_nm_km"]),
                "group_index_milli": int(record["group_index_milli"]),
            }
    return coefficients


@lru_cache(maxsize=1)
def _schema_bounds() -> dict[str, dict[str, tuple[int | None, int | None]]]:
    """Every `min_value` and `max_value` the schema declares, per node kind.

    Read out of `schemas/` for the reason `_schema_defaults` is: a bound stated
    twice is a bound that can disagree with itself. `_receiver_readings` guards
    against these rather than against literals, so widening an attribute in the
    schema widens the guard on the next run and narrowing it fails the
    regeneration instead of the load.
    """
    bounds: dict[str, dict[str, tuple[int | None, int | None]]] = {}
    for path in sorted(SCHEMA_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text())
        for section in ("generics", "nodes"):
            for definition in document.get(section) or []:
                kind = f"{definition['namespace']}{definition['name']}"
                for attribute in definition.get("attributes") or []:
                    parameters = attribute.get("parameters") or {}
                    if "min_value" in parameters or "max_value" in parameters:
                        bounds.setdefault(kind, {})[attribute["name"]] = (
                            parameters.get("min_value"),
                            parameters.get("max_value"),
                        )
    return bounds


def _strip_defaults(kind: str, data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the status-shaped fields of `kind` that only restate the schema."""
    defaults = _schema_defaults().get(kind, {})
    candidates = {name: defaults[name] for name in SUPPRESSED_WHEN_DEFAULT - NEVER_SUPPRESSED if name in defaults}
    if not candidates:
        return data
    return [
        {key: value for key, value in record.items() if key not in candidates or value != candidates[key]}
        for record in data
    ]


def _document(kind: str, data: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": "infrahub.app/v1",
        "kind": "Object",
        "spec": {"kind": kind, "data": _strip_defaults(kind, data)},
    }


class _IndentedDumper(yaml.SafeDumper):
    """PyYAML writes a sequence flush with its parent key; yamllint rejects that.

    `yamllint -s` promotes its default `indentation` rule to an error, and that
    rule wants a sequence indented inside the mapping key that owns it. PyYAML's
    default is the other convention. Nothing about the parsed YAML changes; only
    two spaces per level do.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _dump(documents: list[dict[str, Any]], notes: dict[str, list[str]]) -> str:
    """Serialise, with the key order the dicts were built in.

    `width` is set well above the 180-character ceiling `yamllint -s` enforces so
    that PyYAML never introduces a line break of its own; every emitted field is
    short enough that the ceiling is met by construction instead.
    """
    chunks = []
    for document in documents:
        text = yaml.dump(
            document, Dumper=_IndentedDumper, sort_keys=False, default_flow_style=False, width=4096, allow_unicode=True
        )
        kind = str(document["spec"]["kind"])
        if kind in notes:
            text = _note(kind, notes[kind], text)
        chunks.append(text)
    return "\n---\n".join(chunks)


def _write(
    path: Path,
    purpose: list[str],
    documents: list[dict[str, Any]],
    notes: dict[str, list[str]] | None = None,
) -> None:
    path.write_text(f"{_header(purpose)}\n{_dump(documents, notes or {})}")


def build_tags() -> list[dict[str, Any]]:
    names = site_names()
    return [
        {"name": f"eurohpc-{slug}", "description": f"EuroHPC {facility}, {where}. Modelled attachment: {names[site]}."}
        for facility, slug, site, where in sorted(EUROHPC, key=lambda row: row[1])
    ]


def build_facilities() -> list[dict[str, Any]]:
    """The six EuroHPC facilities, each on the PoP it hands traffic to.

    `name` is the slug and nothing prettier. `mapchrome.py` upper-cases it for
    the caption on the node disc, and both committed map renders hold the
    upper-cased slug, so `marenostrum-5` is the value and `MareNostrum 5` would
    move a golden. The display name lives in `description` instead.
    """
    names = site_names()
    return [
        {
            "name": slug,
            "description": f"EuroHPC {facility}, {where}. Modelled attachment: {names[site]}.",
            "site": site,
        }
        for facility, slug, site, where in sorted(EUROHPC, key=lambda row: row[1])
    ]


def build_sites() -> list[dict[str, Any]]:
    """The fourteen PoPs and the one customer campus behind the CWDM tail.

    `site_type` is written on all fifteen even though the schema defaults it to
    `pop`. A discriminator that a guard test reads should arrive from the seed,
    not from a default.

    Only the PoPs join `otn_sites`. Membership is written here, on the site,
    because `CoreGroup.members` peers the `CoreNode` generic, which has no human
    friendly ID for a `members:` list to resolve against. It is written by the
    branch that emits a PoP rather than by a filter over the finished list, so
    the campus cannot pick it up: `OtnSite` inherits `CoreArtifactTarget`, so an
    unscoped group would render the network map onto a site the map does not
    draw, on a tail that belongs to no section.
    """
    attachments = eurohpc_by_site()
    records: list[dict[str, Any]] = []
    for name, short, latitude, longitude in SITES:
        record: dict[str, Any] = {
            "name": name,
            "shortname": short,
            "description": f"{name} PoP. {len(degrees()[short])} optical degrees.",
            "site_type": "pop",
            "latitude_microdeg": latitude,
            "longitude_microdeg": longitude,
        }
        if short in attachments:
            record["tags"] = [f"eurohpc-{attachments[short][1]}"]
        record["member_of_groups"] = [SITE_GROUP]
        records.append(record)

    name, short, latitude, longitude = CWDM_TAIL_SITE
    records.append(
        {
            "name": name,
            "shortname": short,
            "description": (
                f"Customer campus. {len(CWDM_TAIL_WAVELENGTHS)} CWDM wavelengths into "
                f"{site_names()[CWDM_TAIL_PEER]} over one metro span."
            ),
            "site_type": "customer",
            "latitude_microdeg": latitude,
            "longitude_microdeg": longitude,
        }
    )
    return records


def build_conduits() -> list[dict[str, Any]]:
    return [
        {"name": name, "owner": "GEANT", "description": f"{description}. Shared-risk group."}
        for name, description, _ in CONDUITS
    ]


def _device(name: str, kind: str, site: str | None) -> dict[str, Any]:
    """Build one device record.

    There is no `description`. `OtnGenericDevice` has a name, a status and a
    role and nothing else. Every device name here therefore has to carry its own
    meaning, which is why an inline amplifier is `amp-fra-mil-il03` and not
    `amp-0117`. Adding a description is rejected by `infrahubctl object
    validate` for all 255 devices at once.
    """
    profile = DEVICE_PROFILE[kind]
    record: dict[str, Any] = {"name": name, "status": "active", "role": profile["role"]}
    if site is not None:
        record["site"] = site
    # insertion_loss_mdb, vendor and model all come from OtnOpticalElement, which
    # OtnRouter does not inherit, so a router gets none of them. Setting `model`
    # on one is rejected with "model is not a valid attribute or relationship for
    # OtnRouter".
    if "insertion_loss_mdb" in profile:
        record["insertion_loss_mdb"] = profile["insertion_loss_mdb"]
        record["vendor"] = VENDOR
        record["model"] = profile["model"]
    return record


def _amplifier(name: str, site: str | None, sequence: int) -> dict[str, Any]:
    """One amplifier, with the three attributes the budget engine reads.

    No role attribute. `oms_sequence` already carries it: position 1 is the
    booster of this chain and position N+1 is its pre-amplifier. Storing both
    would be two copies of one fact.

    No direction attribute either, and none to write. Which chain an amplifier
    is in is the section relationship holding it, `amplifiers_a2b` or
    `amplifiers_b2a`, and the section file in 16 writes both lists.
    """
    record = _device(name, "OtnAmplifier", site)
    record["noise_figure_mdb"] = AMPLIFIER_NOISE_FIGURE_MDB
    record["gain_mdb"] = AMPLIFIER_GAIN_MDB
    record["oms_sequence"] = sequence
    return record


def amplifier_name(a: str, b: str, position: int, forward: bool) -> str:
    """Name one amplifier by where it sits, and by nothing else.

    `position` is the hut index counting from the A end: 0 is the A-end site,
    1 to N-1 are the amplifier huts between the spans, and N is the B-end site.
    A section of N spans therefore has N+1 huts and two amplifiers in each, so
    ordinals run 01 to 2N+2 and hut k takes 2k+1 and 2k+2.

    The forward chain takes the odd ordinal of each pair. Any fixed rule works;
    what matters is that it is fixed, so the generator reproduces its output
    byte for byte, and that the two objects for one hut stay adjacent in the
    file, which is what the old paired naming was for.

    The name says nothing else. It carries no direction, because the
    relationship holding the amplifier says which chain it is in; no position in
    the chain, because `oms_sequence` says that; and no role, because the
    amplifier's own IN and OUT ports read booster, line and preamp. A name that
    has to be parsed to be understood is an attribute wearing a disguise.
    """
    return f"amp-{a}-{b}-{2 * position + (1 if forward else 2):02d}"


def amplifier_chain(a: str, b: str, direction: str, spans: int) -> list[tuple[str, str | None, int, str]]:
    """One chain as (name, site, oms_sequence, port_role), in sequence order.

    An `a_to_b` chain starts with its booster at site `a` and walks the huts
    upwards. A `b_to_a` chain starts with its booster at site `b` and walks them
    downwards, so hut h is at sequence N-h+1 rather than h+1. Both end with a
    pre-amplifier at the far site at sequence N+1.

    The role is returned rather than recovered from the name later. This
    function is the one place that knows both the position and the chain length,
    so it is the only place the booster-or-preamp question can be answered
    without reading a string back. The role goes on the amplifier's IN and OUT
    ports, not on the device, which is where `OtnGenericPort.role` already holds
    the booster, line and preamp vocabulary.

    Inline amplifiers carry no site. An amplifier hut is not a PoP, and the site
    relationship is optional exactly for this case.
    """
    forward = direction == DIRECTION_A_TO_B
    positions = range(0, spans + 1) if forward else range(spans, -1, -1)
    chain: list[tuple[str, str | None, int, str]] = []
    for sequence, position in enumerate(positions, start=1):
        site = a if position == 0 else b if position == spans else None
        role = "booster" if sequence == 1 else "preamp" if sequence == spans + 1 else "line"
        chain.append((amplifier_name(a, b, position, forward), site, sequence, role))
    return chain


def amplifier_port_roles() -> dict[str, str]:
    """Every amplifier name against the role its IN and OUT ports carry.

    Built from the same `amplifier_chain` calls `build_devices` makes, because
    the role is a fact about position in a chain and that is where position is
    assigned. Nothing reads it back out of a device name.
    """
    roles: dict[str, str] = {}
    for a, b, km in SECTIONS:
        spans = span_count(km)
        for direction in (DIRECTION_A_TO_B, DIRECTION_B_TO_A):
            for name, _, _, role in amplifier_chain(a, b, direction, spans):
                roles[name] = role
    return roles


def build_devices() -> dict[str, list[dict[str, Any]]]:
    # OtnOduSwitch is absent, and not by oversight. Its `carriers` relationship
    # names wavelengths, which are written in 17, and this function feeds 13. A
    # switch emitted here would reference a carrier that does not exist yet and
    # the load would fail on the reference. `build_odu_switches` takes the
    # carrier records and 19 writes them, the same argument that put the Raman
    # pumps in 15 beside their spans.
    devices: dict[str, list[dict[str, Any]]] = {kind: [] for kind in DEVICE_PROFILE if kind != "OtnOduSwitch"}

    for _, short, _, _ in SITES:
        devices["OtnRoadm"].append(_device(f"roadm-{short}-01", "OtnRoadm", short))
        devices["OtnRouter"].append(_device(f"rtr-{short}-01", "OtnRouter", short))
        devices["OtnMuxDemux"].append(_device(f"mux-{short}-01", "OtnMuxDemux", short))
        devices["OtnPatchPanel"].append(_device(f"odf-{short}-01", "OtnPatchPanel", short))
        for index in range(1, transponder_count(short) + 1):
            devices["OtnTransponder"].append(_device(f"xpdr-{short}-{index:02d}", "OtnTransponder", short))

    for _, slug, site, _ in sorted(EUROHPC, key=lambda row: row[1]):
        record = _device(f"rtr-{slug}-{site}-01", "OtnRouter", site)
        record["role"] = "edge"
        devices["OtnRouter"].append(record)

    devices["OtnMuxDemux"].extend(_cwdm_tail_multiplexers())

    # Two chains per section, one per direction of travel, N+1 amplifiers each.
    # An amplifier hut is bidirectional and this model gives each direction its
    # own object, so the hut at position 3 of a nine-span section is two records
    # that differ in name and in `oms_sequence`. Which chain each is in is
    # written by the section in 16, not by the device here.
    #
    # oms_sequence is what makes a chain walkable. Infrahub relationships carry
    # no order, so each of the section's two amplifier lists is a set by the
    # time the budget engine reads it, and the engine sorts each on the
    # sequence. Amplifier k feeds span k of its own walk; amplifier N+1 is the
    # pre-amplifier at the far end.
    for a, b, km in SECTIONS:
        spans = span_count(km)
        for direction in (DIRECTION_A_TO_B, DIRECTION_B_TO_A):
            for name, at_site, sequence, _ in amplifier_chain(a, b, direction, spans):
                devices["OtnAmplifier"].append(_amplifier(name, at_site, sequence))

    devices["OtnRamanPump"].extend(build_raman_pumps())

    return {kind: sorted(records, key=lambda record: str(record["name"])) for kind, records in devices.items()}


def _cwdm_tail_multiplexers() -> list[dict[str, Any]]:
    """The two ends of the CWDM tail, each lighting the same four wavelengths.

    Named to the existing convention, so the coarse filter at the Amsterdam PoP
    is `mux-ams-02` beside the dense AWG that is already `mux-ams-01`. The
    campus end is the campus's only device.

    `role` comes from the device profile and is `passive`, which is the correct
    value and also the only one available: the vocabulary is access, core, edge
    and passive, with no metro in it.

    The wavelengths are quoted strings. `center_wavelength_nm` is the
    human-friendly identifier of `OtnCwdmChannel`, and a bare number in YAML is
    an integer, which the reference resolver rejects.
    """
    campus = CWDM_TAIL_SITE[1]
    records = []
    for name, site in ((f"mux-{campus}-01", campus), (f"mux-{CWDM_TAIL_PEER}-02", CWDM_TAIL_PEER)):
        record = _device(name, "OtnMuxDemux", site)
        record["model"] = CWDM_TAIL_MODEL
        record["cwdm_channels"] = [str(nm) for nm in CWDM_TAIL_WAVELENGTHS]
        records.append(record)
    return records


def build_raman_pumps() -> list[dict[str, Any]]:
    """One counter-propagating pump on every span of the pumped section.

    Placement is the stored fact and the direction is what follows from it. A
    counter-propagating pump fires against the signal, so it sits at the far end
    of the span it pumps and its light travels back up the fibre. Every pump
    here serves `a_to_b`, so every one is injected at the B end of its span and
    `injection_end` is `site_b` on all nine. The marshaller derives the
    direction from that and `propagation`, so nothing stores a value that could
    contradict the two facts beside it.

    `injection_end` is written even though the schema defaults it. The default
    exists only because a mandatory attribute needs one for the schema to load,
    and a value that arrived from a loading device rather than from the seed
    credits gain to a walk nobody chose.

    Names are position-based: `pump-<a>-<b>-<NN>`, ordered by span index and
    then by injection end. One pump per span here, so the span index alone
    orders them.
    """
    a, b = RAMAN_SECTION
    spans = span_count(dict(((x, y), km) for x, y, km in SECTIONS)[(a, b)])
    forward = RAMAN_DIRECTION == DIRECTION_A_TO_B
    records: list[dict[str, Any]] = []
    for index in range(1, spans + 1):
        far_end = index if forward else index - 1
        site = a if far_end == 0 else b if far_end == spans else None
        record = _device(f"pump-{a}-{b}-{index:02d}", "OtnRamanPump", site)
        record["on_off_gain_mdb"] = RAMAN_ON_OFF_GAIN_MDB
        record["injection_end"] = "site_b" if forward else "site_a"
        record["propagation"] = "counter"
        record["span"] = span_name(a, b, index)
        records.append(record)
    return records


def _port(name: str, device: str, role: str, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "device": device,
        "role": role,
        "enabled": True,
        "admin_state": "up",
        "oper_state": "up",
    }
    record.update(extra)
    return record


def build_ports() -> dict[str, list[dict[str, Any]]]:
    ports: dict[str, list[dict[str, Any]]] = {
        "OtnRouterPort": [],
        "OtnClientPort": [],
        "OtnRoadmDegreePort": [],
        "OtnRoadmAddDropPort": [],
        "OtnLinePort": [],
        "OtnAmplifierPort": [],
        "OtnTributaryPort": [],
        "OtnAmplifierMonitor": [],
        "OtnRoadmDegreeMonitor": [],
        "OtnMuxDemuxMonitor": [],
        "OtnReceiverMonitor": [],
    }
    neighbours = degrees()
    routers = [record["name"] for record in build_devices()["OtnRouter"]]
    # The colour a line port is tuned to, for the eighty that terminate a
    # wavelength. It comes from `channel_to_frequency_mhz` applied to the
    # carrier's channel, which is the same call `carrier_anchors` made when it
    # chose that channel, so the port and the channel object cannot disagree.
    bindings = line_port_bindings(build_carriers())

    for router in routers:
        for index in (1, 2):
            ports["OtnRouterPort"].append(
                _port(
                    f"1/1/{index}",
                    router,
                    "client",
                    tx_power_mdbm=-2000,
                    rx_sensitivity_mdbm=-14000,
                    connector_type="LC",
                )
            )

    for _, short, _, _ in SITES:
        roadm = f"roadm-{short}-01"
        for far in neighbours[short]:
            ports["OtnRoadmDegreePort"].append(
                _port(
                    degree_port_name(far),
                    roadm,
                    "degree",
                    tx_power_mdbm=0,
                    rx_sensitivity_mdbm=-20000,
                    connector_type="LC",
                )
            )

        add_drop = 0
        for index in range(1, transponder_count(short) + 1):
            transponder = f"xpdr-{short}-{index:02d}"
            for client in (1, 2):
                ports["OtnClientPort"].append(
                    _port(
                        f"C{client}",
                        transponder,
                        "client",
                        tx_power_mdbm=-1000,
                        rx_sensitivity_mdbm=-14000,
                        connector_type="LC",
                    )
                )
            for line in (1, 2):
                add_drop += 1
                target = f"AD-{add_drop:02d}"
                ports["OtnRoadmAddDropPort"].append(
                    _port(target, roadm, "add_drop", tx_power_mdbm=0, rx_sensitivity_mdbm=-20000, connector_type="LC")
                )
                # Declared on this side only. The edge is symmetric and
                # self-referencing, so declaring both ends splits it in two.
                #
                # A dark port omits center_frequency_mhz rather than writing a
                # null. The attribute is optional and a port tuned to nothing has
                # no colour to state; writing zero would be a frequency outside
                # the declared band, and writing null is a value the loader has
                # to strip anyway.
                bound = bindings.get((transponder, f"L{line}"))
                colour: dict[str, Any] = (
                    {"center_frequency_mhz": channel_to_frequency_mhz(int(bound["channel"]))} if bound else {}
                )
                ports["OtnLinePort"].append(
                    _port(
                        f"L{line}",
                        transponder,
                        "line",
                        tx_power_mdbm=1000,
                        rx_sensitivity_mdbm=-18000,
                        connector_type="LC",
                        connected_to=[roadm, target],
                        **colour,
                    )
                )
            if index == 1 and short in TRIBUTARY_SITES:
                for tributary in (1, 2):
                    ports["OtnTributaryPort"].append(
                        _port(
                            f"E1-{tributary:02d}",
                            transponder,
                            "tributary",
                            speed_kbps=2048,
                            impedance_ohm=120,
                            connector_type="RJ48",
                        )
                    )

    # An O-E-O regenerator receives a wavelength and transmits a new one, so it
    # has line-side optics. Until this loop existed it had none, and a
    # regenerated circuit read as two half-dark wavelengths: the outer end of
    # each segment terminated on a transponder and the inner end terminated on
    # nothing at all.
    #
    # Keyed on `switching_mode`, not on the device name. A cross-connect grooms
    # ODU containers behind a transponder at the electrical layer and terminates
    # no wavelength, so it gets no line port. Giving it one would put a third
    # terminator on all 37 wavelengths `oxc-mil-01` is patched to, which is the
    # over-termination that made counting `odu_switches` unworkable in the first
    # place. A fourth O-E-O device added to ODU_SWITCHES gets ports if it
    # regenerates and none if it does not, which is the rule rather than a list.
    #
    # **Both ports are dark, and that is the shipped truth rather than an
    # omission.** All 25 wavelengths `oeo-fra-01` is patched to run Frankfurt to
    # Milan and already terminate on transponders at both ends, so no two of them
    # form a chain and nothing is regenerated at Frankfurt in this dataset. The
    # regeneration happens on the scenario branches, where `demo/` lights
    # wavelengths that meet at a regenerator. Binding either port here would put
    # a third terminator on a wavelength that has two.
    #
    # A dark port omits center_frequency_mhz rather than writing a null, the same
    # way the 38 dark transponder ports above do. It also omits connected_to:
    # every add/drop port at Frankfurt is patched to a transponder line port, and
    # inventing a patch that the plant does not hold would be worse than saying
    # nothing.
    for switch_name, _, mode, _ in ODU_SWITCHES:
        if mode != "regenerator":
            continue
        for line in (1, 2):
            ports["OtnLinePort"].append(
                _port(
                    f"L{line}",
                    switch_name,
                    "line",
                    tx_power_mdbm=1000,
                    rx_sensitivity_mdbm=-18000,
                    connector_type="LC",
                )
            )

    # The role comes from the chain, not from the name. `amplifier_chain` knows
    # both the position and the chain length, so it is the only place the
    # booster-or-preamp question can be answered, and nothing here parses a
    # device name for meaning.
    roles = amplifier_port_roles()
    for amplifier in build_devices()["OtnAmplifier"]:
        name = str(amplifier["name"])
        role = roles[name]
        ports["OtnAmplifierPort"].append(_port("IN", name, role, rx_sensitivity_mdbm=-28000, connector_type="LC"))
        ports["OtnAmplifierPort"].append(_port("OUT", name, role, tx_power_mdbm=17000, connector_type="LC"))

    for kind, monitors in build_monitoring_ports(ports).items():
        ports[kind].extend(monitors)

    return ports


# Every reading below is derived from the device's own configured value with a
# fixed offset. Nothing is random, because the generator has to reproduce its
# output byte for byte, and nothing is invented, because a reading that does not
# follow from the configuration cannot be compared against the computed budget.
MEASURED_AT = "2026-08-26T06:00:00Z"

GAIN_DROOP_MDB = 300
"""How far below its configured gain an amplifier is actually running. A real
stage sits slightly under target as pumps age."""

DEGRADED_DROOP_MDB = 1400
"""A stage that has drooped far enough to be worth a maintenance visit. Applied
to every seventeenth amplifier so the drift report has something real to find:
a uniform droop across all 306 would prove the comparison runs and prove
nothing about whether it discriminates."""

DEGRADED_EVERY = 17

# A transponder whose line ports hold no wavelength. Loss of signal, and it says
# so.
#
# **What this buys.** Before feature 025 every receiver in the dataset reported
# the same healthy 25.1 dB, whether or not there was any light on the fibre in
# front of it. Under the floor of two there are 38 dark line ports, so that
# uniform block would now be claiming health on transponders that terminate
# nothing at all. A dark receiver reporting darkness is what makes the floor
# honest rather than a retreat: the floor keeps the transponders the demo
# scenarios need, and these readings stop those transponders pretending to carry
# traffic.
#
# Each value was checked against `schemas/otn_ports.yml` before being chosen, and
# `_guard_receiver_readings` re-checks all six on every run. None had to be
# clamped and none is an invented plausible-looking figure.
#
#   rx_power_mdbm     -40,000  the floor of the declared range, which is what a
#                              coherent front end reports below its detection
#                              threshold
#   measured_osnr_mdb       0  nothing to measure a signal against noise on
#   q_factor_mdb            0  same
#   pre_fec_ber_ppb   5e8      a bit error rate of 0.5, which is what pure noise
#                              gives, and half the 1e9 ppb ceiling
#   cd_fs_per_nm            0  nothing propagated, so nothing accumulated
#   dgd_fs                  0  same
DARK_RECEIVER_READINGS: dict[str, int] = {
    "rx_power_mdbm": -40000,
    "measured_osnr_mdb": 0,
    "pre_fec_ber_ppb": 500000000,
    "q_factor_mdb": 0,
    "cd_fs_per_nm": 0,
    "dgd_fs": 0,
}

NOISE_LIMIT_BER_PPB = DARK_RECEIVER_READINGS["pre_fec_ber_ppb"]
"""A bit error rate of 0.5. Nothing reads worse: a receiver guessing at random
is already wrong half the time, so the stated BER relation stops here."""


def _ber_from_q_margin_ppb(margin_mdb: int) -> int:
    """Pre-FEC bit error rate in parts per billion, from the Q margin.

    **A stated relation, not a physical model, and the label matters.** The
    physically correct route is `BER = 0.5 * erfc(Q / sqrt(2))`, and it is closed
    to this generator: `math.erfc` resolves to the platform libm, so macOS and
    the CI image can differ in the last unit in the last place. That difference
    survives rounding to parts per billion, and it would land as a regeneration
    diff that reads as a dataset bug and reproduces on nobody's machine.
    `tests/unit/test_geant_dataset.py` regenerates and diffs, so a figure that
    is nearly deterministic is a figure that fails intermittently.

    So the relation is stated instead: **the error rate halves for every
    decibel of Q margin**, anchored at `FEC_THRESHOLD_BER_PPB` where the margin
    is zero, and interpolated linearly inside each decibel. It is exact integer
    arithmetic, monotone decreasing in the margin, and identical on every
    machine. It is the right shape and it is not the right curve, and a reader
    comparing these figures against a vendor's BER-versus-Q plot will find them
    optimistic in the tail. That is the honest trade and it is written here
    rather than left to be discovered.

    A negative margin runs the same relation the other way, doubling per
    decibel of shortfall, and stops at `NOISE_LIMIT_BER_PPB`.
    """
    steps, part = divmod(margin_mdb, MDB_PER_DB)
    at_step = FEC_THRESHOLD_BER_PPB >> steps if steps >= 0 else FEC_THRESHOLD_BER_PPB << -steps
    at_next = FEC_THRESHOLD_BER_PPB >> (steps + 1) if steps + 1 >= 0 else FEC_THRESHOLD_BER_PPB << -(steps + 1)
    return min(at_step - (at_step - at_next) * part // MDB_PER_DB, NOISE_LIMIT_BER_PPB)


def _guard_receiver_readings(subject: str, readings: dict[str, int]) -> dict[str, int]:
    """Raise if any reading falls outside the bounds the schema declares.

    Raised here rather than clamped, and raised here rather than left to the
    load. A clamped reading is a wrong reading that looks right, and a reading
    that only fails at `infrahubctl object load` fails a long way from the line
    that derived it. `build_line_containers` raises the same way for a line rate
    with no container type, for the same reason: a silent gap in the dataset is
    the one failure this script cannot afford.

    The bounds come from `schemas/otn_ports.yml` through `_schema_bounds`, so
    widening an attribute widens this guard on the next run.

    A reading whose bounds cannot be resolved is a failure and not a pass.
    `_schema_bounds` keys on the concrete kind and reads only the attributes
    declared inline on it, so moving the six readings onto an inherited generic,
    the pattern `OtnGenericPort` and `OtnOpticalPort` already use, would empty
    `bounds["OtnReceiverMonitor"]` and let every value through. That is the exact
    failure this function promises to prevent, so the absence is raised on
    rather than skipped over.
    """
    bounds = _schema_bounds().get("OtnReceiverMonitor", {})
    unbounded = sorted(set(readings) - set(bounds))
    if unbounded:
        raise ValueError(
            f"schemas/otn_ports.yml declares no min_value or max_value for OtnReceiverMonitor.{unbounded[0]}"
            + (f" (and {len(unbounded) - 1} more: {', '.join(unbounded[1:])})" if len(unbounded) > 1 else "")
            + f", so the readings derived for {subject} cannot be guarded. If the attributes moved onto an "
            "inherited generic, teach _schema_bounds to resolve inheritance. Passing an unbounded reading "
            "would make this guard silently vacuous."
        )
    for name, value in readings.items():
        low, high = bounds[name]
        if (low is not None and value < low) or (high is not None and value > high):
            raise ValueError(
                f"the receiver reading {name}={value}, derived for {subject}, is outside the {low} to {high} "
                f"range schemas/otn_ports.yml declares for OtnReceiverMonitor.{name}. Correct the derivation "
                "or the schema. Clamping it would hide a wrong figure behind a legal one."
            )
    return readings


def _receiver_readings(carrier: dict[str, Any]) -> dict[str, int]:
    """The six readings a receiver takes on a wavelength, from that wavelength's route.

    Every figure follows from the plant the carrier crosses, so two wavelengths
    on two routes report two different sets of numbers. That is the whole point
    of the change: the readings used to be one literal block repeated on every
    transponder in the network.

    The keys are ordered as the records were written before feature 025, so the
    regeneration diff is values and nothing else.
    """
    spans = _route_span_inputs(carrier)
    route_km = sum(span.length_m for span in spans) // M_PER_KM

    # FR-016. Summed over the real spans through the same function the budget
    # engine uses, rather than multiplied out fresh. The two agree exactly here
    # because `span_lengths_m` sums to the section, and summing is what keeps
    # them agreeing if a section ever divides unevenly.
    cd_fs_per_nm = sum(span_dispersion_fs_per_nm(span) for span in spans)

    # FR-017. Polarisation mode dispersion accumulates with the square root of
    # length, not with length, so a route four times as long carries twice the
    # differential group delay. `math.isqrt` is exact integer arithmetic and
    # touches no platform library, which is why it is allowed where `math.sqrt`
    # is not.
    dgd_fs = PMD_COEFFICIENT_FS_PER_ROOT_KM * math.isqrt(route_km)

    # ------------------------------------------------------------------
    # FR-021. Read this before building any report on `measured_osnr_mdb`.
    #
    # **This figure must never be compared against the budget engine and called
    # evidence.** It is computed here by `budget.cascade_osnr_mdb`, over the same
    # spans, with the same noise figure, that `budget.evaluate_path` walks when a
    # check or a transform asks whether a wavelength closes. Compare the two and
    # the difference is `TRANSPONDER_IMPLEMENTATION_PENALTY_MDB` and nothing
    # else. That is not a measurement agreeing with a prediction. It is one
    # subtraction, dressed up as a validation, and it will look like a passing
    # report for as long as nobody checks where both sides came from.
    #
    # **Gain drift is different, and the difference is the reason that report is
    # allowed to exist.** An amplifier's `gain_mdb` is a configured equipment
    # spec: an input to the model, written on the device because somebody set the
    # amplifier to that gain. `measured_gain_mdb` on its monitor is what the
    # device reports back afterwards. Those are two independent facts about the
    # world and their disagreement carries real information, which is why
    # `transforms/monitor_drift.py` reads amplifiers and Raman pumps and reads no
    # receiver at all.
    #
    # **OSNR has no second source here.** In a real network an optical spectrum
    # analyser measures it, independently of whatever the planning tool
    # predicted. In a synthetic one there is only the model, so a receiver's OSNR
    # and the engine's OSNR are the same number twice. Use these readings to show
    # that a long route is worse than a short one, that a dark receiver is dark,
    # and that a Q factor tracks a mode's requirement. Do not use them to show
    # that the budget engine is right.
    #
    # One stage per amplifier, and each amplifier sits at the far end of one
    # span, so its input is the per-channel launch less that span's own loss.
    # **Do not collapse this to one stage repeated N times.** Span loss runs from
    # 17,267 mdB on Amsterdam to Frankfurt up to 19,700 mdB on Berlin to
    # Frankfurt, a spread of 2.4 dB, and Amsterdam to Milan and Berlin to Milan
    # are both fifteen spans. Flatten the list and those two routes report the
    # same quality, which is exactly the fault this feature exists to remove,
    # reintroduced one layer down where every figure still looks reasonable.
    # ------------------------------------------------------------------
    stages = [
        osnr_stage_mdb(LAUNCH_POWER_PER_CHANNEL_MDBM - span_fiber_loss_mdb(span), AMPLIFIER_NOISE_FIGURE_MDB)
        for span in spans
    ]
    # Floored at the zero `schemas/otn_ports.yml` declares as the minimum, for
    # the reason spelled out at the Q factor below. A route that delivers less
    # noise margin than the penalty costs is a receiver with no signal to
    # measure, and zero is what the schema can say about it.
    measured_osnr_mdb = max(0, cascade_osnr_mdb(stages) - TRANSPONDER_IMPLEMENTATION_PENALTY_MDB)

    # FR-018. The preamplifier at the far end holds its output at the same
    # per-channel launch every other amplifier does, and what the receiver sees
    # is that output less the two passive stages between them: the ROADM the
    # wavelength drops through and the multiplexer behind it. Both losses are
    # already in DEVICE_PROFILE, where the budget engine charges them too. It
    # does not vary by route, because the preamplifier is regulated: a longer
    # route costs OSNR, not received power, and saying so is more useful than
    # inventing a slope.
    rx_power_mdbm = (
        LAUNCH_POWER_PER_CHANNEL_MDBM
        - int(DEVICE_PROFILE["OtnRoadm"]["insertion_loss_mdb"])
        - int(DEVICE_PROFILE["OtnMuxDemux"]["insertion_loss_mdb"])
    )

    # The Q factor is the OSNR margin against what the mode needs, offset so a
    # carrier sitting exactly at its requirement reads the FEC threshold. A
    # reader then sees at a glance how far a wavelength is from the cliff, in the
    # unit a line card actually reports. The requirement is read through
    # `_mode_required_osnr_mdb`, the same pattern `_mode_bauds` uses, so the mode
    # catalog stays the one statement of what a mode needs.
    mode = str(carrier["optical_mode"])
    required = _mode_required_osnr_mdb()
    if mode not in required:
        raise ValueError(
            f"{carrier['name']} rides optical mode {mode}, which {OPTICAL_MODES.name} does not define, so there "
            "is no required OSNR to measure its Q factor against"
        )
    margin_mdb = measured_osnr_mdb - required[mode]

    # The Q reading is floored at zero, the minimum `schemas/otn_ports.yml`
    # declares, rather than allowed to go negative and fail
    # `_guard_receiver_readings`. A wavelength more than 5.7 dB below its mode's
    # required OSNR reports zero Q and a bit error rate at the noise limit, which
    # is the floor of what the schema can express and is a truthful description
    # of a receiver that far under the FEC threshold. The generator's job is to
    # emit that state, not to refuse to emit a dataset because of it: a
    # wavelength that misses its requirement is a state this repository models on
    # purpose, and `checks/osnr_margin.py` is what reports the margin failure.
    # Today's tightest carrier, `oc-ch047-fra-mil`, reads 9,225, so the floor is
    # unreachable on the shipped plan and this changes no committed figure.
    return {
        "rx_power_mdbm": rx_power_mdbm,
        "measured_osnr_mdb": measured_osnr_mdb,
        "pre_fec_ber_ppb": _ber_from_q_margin_ppb(margin_mdb),
        "q_factor_mdb": max(0, FEC_THRESHOLD_Q_MDB + margin_mdb),
        "cd_fs_per_nm": cd_fs_per_nm,
        "dgd_fs": dgd_fs,
    }


def build_monitoring_ports(ports: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """One monitor per device that has a monitoring interface, keyed by kind.

    A monitor carries the readings its family reports and nothing else. The
    schema enforces that now: the readings are mandatory on each kind, and a
    kind has no field for a reading its hardware cannot take. No record carries
    a monitor_type, because the kind is the type.

    The port name is unchanged by the split: MON on the device, and
    MON-<degree> on a ROADM that has more than one. The name comes from
    `monitors.monitor_port_name`, which is the same function the completeness
    check uses to pair a monitor with its port, so the two cannot drift. The
    uniqueness constraint on OtnGenericPort is (device, name) and it binds
    across every kind that inherits the generic, so the four kinds below share
    one namespace per device.

    Where each reading comes from:

    - An amplifier's gain is its configured gain less a droop, with a heavier
      droop every DEGRADED_EVERY units so the drift report has something to
      find. Its input power is a fixed launch and its output is the sum.
    - A degree monitor's `channel_count` is the distinct channels riding the
      section that degree faces, from `monitors.channels_by_section` over the
      carrier plan. A dark degree reports 0, which is the schema minimum and a
      real reading: 32 of the 42 degrees carry no light at all.
    - A dense multiplexer's `channel_count` is the distinct channels
      terminating at its site, from `monitors.channels_terminating_by_site`.
      The graph holds no relationship from a multiplexer to the wavelengths it
      lights, so no check can verify these and
      `tests/unit/test_geant_dataset.py` asserts them by name instead.
    - A CWDM multiplexer's `channel_count` is the length of its own
      `cwdm_channels` list, because there the relationship does exist.
    - A receiver's six readings come from the route of the wavelength its
      transponder terminates, in `_receiver_readings`, and a transponder that
      terminates none reports `DARK_RECEIVER_READINGS`. They were one literal
      block on all forty monitors until feature 025, which is how every
      receiver in the network came to claim the same healthy 25.1 dB.

    Both channel counts were the literal 71 until feature 024. That was the
    size of the carrier plan before feature 021 cut it to 40, and it stayed
    behind on 56 of the 58 monitors as a number nothing derived.
    """
    monitors: dict[str, list[dict[str, Any]]] = {
        "OtnAmplifierMonitor": [],
        "OtnRoadmDegreeMonitor": [],
        "OtnMuxDemuxMonitor": [],
        "OtnReceiverMonitor": [],
    }
    devices = build_devices()

    for index, amplifier in enumerate(devices["OtnAmplifier"]):
        name = str(amplifier["name"])
        droop = DEGRADED_DROOP_MDB if index % DEGRADED_EVERY == 0 else GAIN_DROOP_MDB
        gain = int(amplifier["gain_mdb"]) - droop
        launch = -3500
        monitors["OtnAmplifierMonitor"].append(
            _port(
                "MON",
                name,
                "monitor",
                measured_at=MEASURED_AT,
                input_power_mdbm=launch,
                output_power_mdbm=launch + gain,
                measured_gain_mdb=gain,
                tilt_mdb=-400,
            )
        )

    carriers = build_carriers()
    on_section = channels_by_section(carriers, [section_key(a, b) for a, b, _ in SECTIONS])
    faced = degree_sections()

    for degree in ports["OtnRoadmDegreePort"]:
        port = str(degree["name"])
        roadm = str(degree["device"])
        section = faced.get((roadm, port))
        if section is None:
            raise ValueError(
                f"{port} on {roadm} faces no section in the seed tables, so there is no fibre to count the "
                "light on. A degree port and its section are both derived from SECTIONS and one of them moved"
            )
        monitors["OtnRoadmDegreeMonitor"].append(
            _port(
                monitor_port_name(port),
                roadm,
                "monitor",
                measured_at=MEASURED_AT,
                total_power_mdbm=15200,
                channel_count=on_section[section],
            )
        )

    terminating = channels_terminating_by_site(
        carrier_endpoints(carriers), [str(mux["site"]) for mux in devices["OtnMuxDemux"]]
    )
    for mux in devices["OtnMuxDemux"]:
        cwdm = mux.get("cwdm_channels") or []
        lit = len(cwdm) if cwdm else terminating[str(mux["site"])]
        monitors["OtnMuxDemuxMonitor"].append(
            _port(
                "MON",
                str(mux["name"]),
                "monitor",
                measured_at=MEASURED_AT,
                total_power_mdbm=9800,
                channel_count=lit,
            )
        )

    by_name = {str(carrier["name"]): carrier for carrier in carriers}
    bindings = line_port_bindings(carriers)
    for transponder in devices["OtnTransponder"]:
        name = str(transponder["name"])
        # One monitor per device, not one per line port, because the receiver is
        # part of the transponder and the schema hangs the readings off the
        # device. A transponder whose two line ports hold two different
        # wavelengths therefore has one monitor and two carriers, and the monitor
        # derives from the carrier on L1: that is the first slot
        # `line_port_bindings` fills, so it is the wavelength the transponder has
        # held longest and the one a reader looking at the device will find
        # first. A transponder with one bound port and one dark port is lit, and
        # derives from the wavelength it has.
        bound = next((bindings[(name, line)] for line in ("L1", "L2") if (name, line) in bindings), None)
        # FR-008a. No wavelength on either port is loss of signal, not a healthy
        # reading on a fibre with no light on it. The monitor still exists:
        # `checks/monitor_completeness.py` gates on a transponder that has none,
        # and all six readings are mandatory on the kind, so darkness has to be
        # said rather than left out.
        readings = _receiver_readings(by_name[str(bound["name"])]) if bound else dict(DARK_RECEIVER_READINGS)
        monitors["OtnReceiverMonitor"].append(
            _port(
                "MON",
                name,
                "monitor",
                measured_at=MEASURED_AT,
                **_guard_receiver_readings(name, readings),
            )
        )

    return monitors


def build_raman_monitors() -> list[dict[str, Any]]:
    """Pump monitors, emitted with the pumps rather than with the other ports.

    A monitor cannot load before the device it hangs off, and pumps are written
    in the span file because a pump cannot load before its span. Object files
    load in filename order, so a pump monitor in the port file names a device
    that does not exist yet. The kind split changes none of that.
    """
    return [
        _port(
            "MON",
            str(pump["name"]),
            "monitor",
            measured_at=MEASURED_AT,
            pump_power_mdbm=30000,
            measured_gain_mdb=int(pump["on_off_gain_mdb"]) - GAIN_DROOP_MDB,
            back_reflection_mdb=40000,
        )
        for pump in build_raman_pumps()
    ]


def build_spans() -> list[dict[str, Any]]:
    conduits = conduit_for_span()
    names = site_names()
    records: list[dict[str, Any]] = []
    for a, b, km in SECTIONS:
        count = span_count(km)
        for index, length in enumerate(span_lengths_m(km, count), start=1):
            record: dict[str, Any] = {
                "name": span_name(a, b, index),
                "description": f"{names[a]} to {names[b]}, span {index} of {count}.",
                # No element_class. It is inherited from OtnOpticalElement and
                # OtnFiberSpan defaults it to fiber_span, so writing it here
                # would put the same fact on all 132 spans.
                "oms_sequence": index,
                "length_m": length,
                **span_fiber_geometry(length),
                "fiber_type": FIBER_TYPE,
                "site_a": a,
                "site_b": b,
            }
            if record["name"] in conduits:
                record["conduit"] = conduits[str(record["name"])]
            # No `oms`. The section writes the edge and it reads back here.
            records.append(record)
    records.append(_cwdm_tail_span())
    return records


def _cwdm_tail_span() -> dict[str, Any]:
    """The one span of the CWDM tail. The 133rd, and the only one in no section.

    **No `oms`, deliberately, and no conduit.** The missing `oms` is the whole
    boundary between the coarse tail and the budget engine: the marshaller
    reaches a span only through the section that lists it, so a tail attached to
    no section is a tail the engine never sees. Filling this in would budget an
    18.4 km link at a 1550 nm attenuation coefficient and understate its loss by
    0.92 dB at 1471 nm, silently. The other spans omit `oms` too, but for the
    opposite reason: their section writes the edge from the far side.

    `oms_sequence` is written even though nothing in the tail reads it. The
    attribute is optional in the schema and `plant._sequence` raises for a
    missing value, so a span that ever did join a section would fail there
    rather than here.
    """
    campus = CWDM_TAIL_SITE[1]
    names = site_names()
    return {
        "name": span_name(campus, CWDM_TAIL_PEER, 1),
        "description": (
            f"{CWDM_TAIL_SITE[0]} to {names[CWDM_TAIL_PEER]}, the CWDM tail. One span, in no optical multiplex section."
        ),
        "oms_sequence": 1,
        "length_m": CWDM_TAIL_LENGTH_M,
        **span_fiber_geometry(CWDM_TAIL_LENGTH_M),
        "fiber_type": FIBER_TYPE,
        "site_a": campus,
        "site_b": CWDM_TAIL_PEER,
    }


def build_sections() -> list[dict[str, Any]]:
    names = site_names()
    records: list[dict[str, Any]] = []
    for a, b, km in SECTIONS:
        count = span_count(km)
        # One list per direction of travel, in sequence order within each. This
        # is where the chain an amplifier belongs to is recorded, and it is the
        # only place: no amplifier stores a direction.
        #
        # Each list is exhaustive on purpose: an object load sets a relationship
        # to exactly what the file names and detaches everything else, so a
        # section that lists half a chain silently loses the rest of it.
        chains = {
            direction: [name for name, _, _, _ in amplifier_chain(a, b, direction, count)]
            for direction in (DIRECTION_A_TO_B, DIRECTION_B_TO_A)
        }
        records.append(
            {
                "name": section_key(a, b),
                "description": f"{names[a]} to {names[b]}, {km} km over {count} spans.",
                "roadm_a": f"roadm-{a}-01",
                "roadm_b": f"roadm-{b}-01",
                "spans": [span_name(a, b, index) for index in range(1, count + 1)],
                "amplifiers_a2b": chains[DIRECTION_A_TO_B],
                "amplifiers_b2a": chains[DIRECTION_B_TO_A],
            }
        )
    return records


@lru_cache(maxsize=1)
def build_carriers() -> list[dict[str, Any]]:
    """The forty pre-provisioned wavelengths, in the order `CARRIER_PLAN` writes them.

    Memoised for the same reason `_schema_defaults` and `_mode_line_rates` are:
    `transponder_count` now derives from it through `_terminations_by_site`, and
    `transponder_count` is called once per site from both `build_devices` and
    `build_ports`. Nothing mutates the returned records, so one shared list is
    safe; `_strip_defaults` rebuilds every record it filters.
    """
    names = site_names()
    channels = iter(carrier_anchors())
    records: list[dict[str, Any]] = []
    for a, b, sections, count, mode in CARRIER_PLAN:
        for _ in range(count):
            channel = next(channels)
            records.append(
                {
                    "name": f"oc-ch{channel:03d}-{a}-{b}",
                    "description": f"{names[a]} to {names[b]} on channel {channel}.",
                    # Quoted. The human-friendly identifier is a Number
                    # attribute and a bare integer is rejected before the
                    # write.
                    "channel": str(channel),
                    "optical_mode": mode,
                    "sections": [section_key(x, y) for x, y in sections],
                }
            )
    return records


def carrier_endpoints(carriers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each carrier's channel anchor against the two sites its route ends at.

    `monitors.channels_terminating_by_site` needs the ends and the carrier
    record does not hold them: a wavelength stores the sections it rides, and
    where it terminates is the plan's. The plan's ends are read here rather than
    parsed back out of the carrier name, the same argument `build_odu_switches`
    makes, and the pairing is by position with the same length guard.
    """
    ends = [(a, b) for a, b, _, count, _ in CARRIER_PLAN for _ in range(count)]
    if len(ends) != len(carriers):
        raise ValueError(
            f"the carrier plan describes {len(ends)} wavelengths and build_carriers emitted {len(carriers)}, "
            "so the endpoints below would be attached to the wrong ones"
        )
    return [
        {"name": carrier["name"], "channel": carrier["channel"], "endpoints": [a, b]}
        for carrier, (a, b) in zip(carriers, ends)
    ]


@lru_cache(maxsize=1)
def line_port_slots() -> dict[str, list[tuple[str, str]]]:
    """shortname -> its line ports in the order a wavelength takes them.

    Transponders ascending by name, and L1 before L2 inside each. That order is
    the whole of the assignment rule and it is derived from the seed alone:
    `transponder_count` sizes the list and the two names are fixed, so two runs
    of this script hand the same wavelength the same port and the committed
    files diff clean.
    """
    return {
        short: [
            (f"xpdr-{short}-{index:02d}", f"L{line}")
            for index in range(1, transponder_count(short) + 1)
            for line in (1, 2)
        ]
        for _, short, _, _ in SITES
    }


def line_port_bindings(carriers: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """(transponder, line port) -> the carrier endpoint record that port terminates.

    Eighty entries against 118 line ports: forty wavelengths, each terminated at
    both of its ends. The remaining 38 ports are dark and are absent from this
    mapping rather than present with a null.

    Carriers are walked in `CARRIER_PLAN` order and each end takes the lowest
    free slot at its site, so the result is a function of the seed tables and of
    nothing else. `transponder_count` and this loop read the same termination
    counts, which is why the slots cannot run out; the raise is there for the
    case where one of the two is changed and the other is not.
    """
    slots = line_port_slots()
    used = dict.fromkeys(slots, 0)
    bindings: dict[tuple[str, str], dict[str, Any]] = {}
    for record in carrier_endpoints(carriers):
        for site in record["endpoints"]:
            free = slots[site]
            if used[site] >= len(free):
                raise ValueError(
                    f"{site} terminates more wavelengths than its {len(free) // 2} transponders hold. "
                    "transponder_count and this assignment read the same termination counts, so the two "
                    "have been changed apart."
                )
            bindings[free[used[site]]] = record
            used[site] += 1
    return bindings


def carriers_with_line_ports(carriers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The carrier records with the two ports terminating each one written on them.

    **The edge is written from this side, and that is a load-order decision
    rather than a modelling one.** `OtnLinePort.carrier` is the natural place to
    put it and it cannot go there: `infrahubctl object load` resolves a
    human-friendly identifier at insert time with no deferred second pass, and
    `14_geant_ports.yml` loads before `17_geant_carriers.yml`, so a port naming a
    wavelength names one that does not exist yet and the whole batch fails. The
    file already makes the same argument twice, for the add/drop port a line port
    patches into and for the wavelengths an ODU switch terminates. The
    relationship is one edge on one identifier, so writing it here populates
    `OtnLinePort.carrier` on read exactly as writing it there would have.

    Two ports per wavelength, the near end first, because `carrier_endpoints`
    returns the plan's two ends in order and `line_port_bindings` assigns them in
    that order.
    """
    bound: dict[str, list[list[str]]] = {str(carrier["name"]): [] for carrier in carriers}
    for (device, port), record in line_port_bindings(carriers).items():
        bound[str(record["name"])].append([device, port])

    wrong = {name: len(ports) for name, ports in bound.items() if len(ports) != 2}
    if wrong:
        raise ValueError(f"every wavelength is terminated at both ends, so two line ports each, not {wrong}")

    return [{**carrier, "line_ports": bound[str(carrier["name"])]} for carrier in carriers]


def build_line_containers(carriers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One line container per carrier, lit and empty, sized from its line rate.

    A line container belongs to a wavelength and not to any service, which is why
    it is written here. Measured in R-011: generator tracking is keyed per
    service, so a container shared by two services joins both tracking groups and
    the next run of either that stops writing it deletes it while the other still
    holds a child underneath.

    A rate with no line container type raises rather than skipping the carrier.
    Skipping would leave a wavelength that the ODU map draws as unlit forever, and
    a silent gap in the dataset is the one failure this pass cannot afford.
    """
    rates = _mode_line_rates()
    records: list[dict[str, Any]] = []
    for carrier in carriers:
        name = str(carrier["name"])
        mode = str(carrier["optical_mode"])
        if mode not in rates:
            raise ValueError(f"{name} rides optical mode {mode}, which {OPTICAL_MODES.name} does not define")
        rate = rates[mode]
        if rate not in LINE_CONTAINER_BY_LINE_RATE_GBPS:
            known = ", ".join(str(gbps) for gbps in sorted(LINE_CONTAINER_BY_LINE_RATE_GBPS))
            raise ValueError(
                f"{name} runs at {rate} Gbit/s on mode {mode}, and no line container type is defined for that "
                f"rate. Defined rates are {known}. Add the rate to LINE_CONTAINER_BY_LINE_RATE_GBPS or take the "
                "mode off the carrier plan. Emitting the carrier without a container would leave the wavelength "
                "unlit on the ODU map."
            )
        odu_type = LINE_CONTAINER_BY_LINE_RATE_GBPS[rate]
        records.append(
            {
                "name": f"odu-line-{name}",
                "description": f"{odu_type} on {name}. Lit and empty until a service grooms into it.",
                "odu_type": odu_type,
                "tributary_slots": 0,
                "tributary_slot_capacity": slot_capacity(odu_type),
                "carrier": name,
            }
        )
    return records


def build_odu_switches(carriers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The three O-E-O devices, each naming the wavelengths it terminates.

    `carriers` is the whole of the junction predicate `chains.py` evaluates, so a
    device that names none contributes no junction at all: FR-003. That is why
    the list is derived from the carrier plan rather than typed out. A wavelength
    terminates at the two sites the plan gives as its ends, and the plan's ends
    are read here rather than parsed back out of the carrier name, which would be
    an attribute wearing a disguise.

    Both Frankfurt devices name the same 45 wavelengths and that is the correct
    reading, not a duplicate. Two O-E-O shelves at one PoP patched to the same
    add/drop bank is a redundant pair, and `chains.py::junction_at` documents the
    tie-break it needs: the lowest-named device wins, which is `oeo-fra-01`.

    The relationship is written from this side only. `OtnOpticalCarrier`'s
    `odu_switches` is the inverse on the same identifier, and an object load sets
    a relationship to exactly what the file names, so writing both sides would
    have each load overwrite the other's view of the same edge.
    """
    ends = [(a, b) for a, b, _, count, _ in CARRIER_PLAN for _ in range(count)]
    if len(ends) != len(carriers):
        raise ValueError(
            f"the carrier plan describes {len(ends)} wavelengths and build_carriers emitted {len(carriers)}, "
            "so the endpoints below would be attached to the wrong ones"
        )

    terminated: dict[str, list[str]] = {}
    for carrier, (a, b) in zip(carriers, ends):
        for site in (a, b):
            terminated.setdefault(site, []).append(str(carrier["name"]))

    records: list[dict[str, Any]] = []
    for name, site, mode, framing_ns in ODU_SWITCHES:
        wavelengths = sorted(terminated.get(site, []))
        if not wavelengths:
            raise ValueError(
                f"{name} sits at {site}, where no wavelength in the carrier plan terminates. A device with an "
                "empty carriers list is never a junction, so it would load and do nothing"
            )
        record = _device(name, "OtnOduSwitch", site)
        record["switching_mode"] = mode
        record["framing_latency_ns"] = framing_ns
        record["carriers"] = wavelengths
        records.append(record)
    return sorted(records, key=lambda record: str(record["name"]))


def generate(target: Path) -> dict[str, int]:
    """Write every generated file into `target` and return the per-kind counts."""
    target.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    tags = build_tags()
    _write(
        target / "10_geant_tags.yml",
        [
            "The six EuroHPC facilities, as tags on their PoP.",
            "LUMI is absent: Finland is outside the fourteen-site subset.",
        ],
        [_document("BuiltinTag", tags)],
    )
    counts["BuiltinTag"] = len(tags)

    sites = build_sites()
    facilities = build_facilities()
    _write(
        target / "11_geant_sites.yml",
        [
            "The fourteen European PoPs and the one customer campus behind the",
            "CWDM tail. shortname is the addressing key everywhere else, and",
            "site_type is what keeps 'the fourteen PoPs' a filter rather than a",
            "count that a fifteenth site silently breaks.",
            "The PoPs join otn_sites from this side: CoreGroup.members peers",
            "CoreNode, which has no human friendly ID to name them by.",
        ],
        [_document("OtnSite", sites), _document("OtnFacility", facilities)],
        {
            "OtnFacility": [
                "The six EuroHPC facilities, on an edge rather than in a tag",
                "name. The eurohpc- tags in 10_geant_tags.yml stay where an",
                "operator wrote them; nothing reads them back any more.",
                "",
                "name is the tag suffix and has to stay it. The map renderer",
                "upper-cases it for the caption on the node disc.",
            ]
        },
    )
    counts["OtnSite"] = len(sites)
    counts["OtnFacility"] = len(facilities)

    conduits = build_conduits()
    _write(
        target / "12_geant_conduits.yml",
        ["Twelve trenches. Membership is recorded on the span, not here."],
        [_document("OtnConduit", conduits)],
    )
    counts["OtnConduit"] = len(conduits)

    devices = build_devices()
    pumps = devices.pop("OtnRamanPump")
    _write(
        target / "13_geant_devices.yml",
        [
            "Every racked device except the Raman pumps, which are emitted with",
            "their spans in 15_geant_spans.yml and say why there.",
            "",
            "No device writes element_class: each kind defaults it, and no device",
            "writes status: active either, for the same reason.",
        ],
        [_document(kind, records) for kind, records in sorted(devices.items())],
        {
            "OtnAmplifier": [
                "Boosters and pre-amplifiers are sited at their section endpoint;",
                "inline amplifiers are not, because an amplifier hut is not a PoP.",
                "Two amplifiers per hut, one per direction of travel, taking the two",
                "ordinals of that hut. Which chain each is in is written by its",
                "section in 16, not here, and no amplifier name carries a direction.",
            ],
            "OtnMuxDemux": [
                "Sixteen, not fourteen. The two extra are the coarse thin-film",
                "filters at the ends of the CWDM tail, and they are the only devices",
                "in this file that light a CWDM wavelength.",
            ],
        },
    )
    counts.update({kind: len(records) for kind, records in devices.items()})

    ports = build_ports()
    _write(
        target / "14_geant_ports.yml",
        [
            "Every port. ROADM add/drop ports are emitted before transponder line",
            "ports because the line port declares the connected_to edge and its",
            "target must already exist. The edge is declared on one side only.",
            "",
            "No port writes enabled or admin_state. OtnGenericPort defaults them",
            "to true and up, which is what every port here is. oper_state is",
            "written, because what a port is doing is not what it was asked to do.",
        ],
        [
            _document(kind, ports[kind])
            for kind in (
                "OtnRouterPort",
                "OtnClientPort",
                "OtnRoadmDegreePort",
                "OtnRoadmAddDropPort",
                "OtnLinePort",
                "OtnAmplifierPort",
                "OtnTributaryPort",
                "OtnAmplifierMonitor",
                "OtnRoadmDegreeMonitor",
                "OtnMuxDemuxMonitor",
                "OtnReceiverMonitor",
            )
        ],
    )
    counts.update({kind: len(records) for kind, records in ports.items()})

    spans = build_spans()
    _write(
        target / "15_geant_spans.yml",
        [
            "No span writes element_class: OtnFiberSpan defaults it to fiber_span.",
            "No span writes its oms either; the section writes that edge.",
            "",
            "The CWDM tail is the exception and the difference matters. It has no",
            "oms because it belongs to no section at all, and that absence is the",
            "only thing keeping a coarse link out of the budget engine, which",
            "would price it at a 1550 nm attenuation coefficient.",
        ],
        [
            _document("OtnFiberSpan", spans),
            _document("OtnRamanPump", pumps),
            _document("OtnRamanMonitor", build_raman_monitors()),
        ],
        {
            "OtnRamanPump": [
                "The pumps follow the spans in this file rather than sitting with",
                "the other devices in 13. OtnRamanPump.span is mandatory, so a pump",
                "cannot load before the span it pumps, and the pump writes the edge",
                "because the span's raman_pumps side is the inverse.",
            ],
        },
    )
    counts["OtnFiberSpan"] = len(spans)
    counts["OtnRamanPump"] = len(pumps)
    # The pump monitors ship in the span file, so they are not in `ports` and
    # the line above does not reach them. Counting them here keeps the manifest
    # equal to what actually loads.
    counts["OtnRamanMonitor"] = len(build_raman_monitors())

    sections = build_sections()
    _write(
        target / "16_geant_sections.yml",
        [
            "The twenty-one sections, each writing its ordered spans and its two",
            "amplifier chains. Which chain an amplifier is in is recorded here",
            "and nowhere else: no amplifier stores a direction and no amplifier",
            "name carries one.",
        ],
        [_document("OtnOpticalMultiplexSection", sections)],
    )
    counts["OtnOpticalMultiplexSection"] = len(sections)

    carriers = build_carriers()
    _write(
        target / "17_geant_carriers.yml",
        [
            "Forty pre-provisioned wavelengths, every one crossing fra-mil so that",
            "section holds 4,134,400 MHz of the 4,800,000 MHz C-band, with 665,600",
            "MHz free in 26 blocks and only the widest of those, 152,800 MHz, able",
            "to anchor anything at all. Channel references are quoted strings: a",
            "bare integer is rejected before the write.",
            "",
            "Each wavelength names the two line ports terminating it. The edge is",
            "written here and not on the port, because 14_geant_ports.yml loads",
            "first and a port cannot name a carrier that does not exist yet.",
        ],
        [_document("OtnOpticalCarrier", carriers_with_line_ports(carriers))],
    )
    counts["OtnOpticalCarrier"] = len(carriers)

    line_containers = build_line_containers(carriers)
    _write(
        target / "18_geant_line_containers.yml",
        [
            "One line container per pre-provisioned wavelength. Every carrier",
            "arrives lit and empty: its container offers the full slot count of the",
            "carrier's line rate, 80 for a 100G ODU4 and 320 for a 400G ODUC4, and",
            "holds no child. So the choice provisioning makes is which wavelength to",
            "groom into, never whether to light one.",
        ],
        [_document("OtnContainer", line_containers)],
        {
            "OtnContainer": [
                "These are dataset objects rather than generator output, and that is",
                "a measurement rather than a preference. Generator tracking is keyed",
                "per service, so a container written by two services joins both",
                "tracking groups, and the next run of either one that stops writing",
                "it deletes it while the other still holds a child underneath.",
                "",
                "No container here writes client_signal: a line container carries a",
                "wavelength, and the client is named by the container nested inside.",
            ],
        },
    )
    counts["OtnContainer"] = len(line_containers)

    odu_switches = build_odu_switches(carriers)
    _write(
        target / "19_geant_odu_switches.yml",
        [
            "Three O-E-O devices, last in the load order: each names the wavelengths",
            "it terminates, and a carrier has to exist before a device references it.",
            "",
            "Which three sites, and why those, is on the kind below.",
        ],
        [_document("OtnOduSwitch", odu_switches)],
        {
            "OtnOduSwitch": [
                "Two cross-connects at the two hub sites, Frankfurt and Milan, which are",
                "hubs by measurement: every one of the 40 wavelengths crosses fra-mil, so",
                "37 terminate at Milan and 25 at Frankfurt and no third site reaches",
                "double figures. One regenerator at Frankfurt, the site",
                "R-013 measured as the only split that closes Madrid to Warsaw.",
                "",
                "Every device names at least one wavelength. A device with an empty",
                "carriers list loads and is never a junction, so an inert one here",
                "would look like a capability the demo does not have.",
                "",
                "No device writes element_class: OtnOduSwitch defaults it to",
                "odu_switch, the tenth choice, added for this kind.",
            ],
        },
    )
    counts["OtnOduSwitch"] = len(odu_switches)

    return dict(sorted(counts.items()))


GENERATED_NAMES = [
    "10_geant_tags.yml",
    "11_geant_sites.yml",
    "12_geant_conduits.yml",
    "13_geant_devices.yml",
    "14_geant_ports.yml",
    "15_geant_spans.yml",
    "16_geant_sections.yml",
    "17_geant_carriers.yml",
    "18_geant_line_containers.yml",
    "19_geant_odu_switches.yml",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="regenerate into a temporary directory and diff, writing nothing"
    )
    arguments = parser.parse_args(argv)

    if not arguments.check:
        counts = generate(OBJECT_DIR)
        MANIFEST.write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
        print(f"wrote {len(GENERATED_NAMES)} files and {MANIFEST.name}: {sum(counts.values())} objects")
        return 0

    with tempfile.TemporaryDirectory() as directory:
        fresh = Path(directory)
        counts = generate(fresh)
        differences = [
            name for name in GENERATED_NAMES if not filecmp.cmp(fresh / name, OBJECT_DIR / name, shallow=False)
        ]
        expected = json.dumps(counts, indent=2, sort_keys=True) + "\n"
        if MANIFEST.read_text() != expected:
            differences.append(MANIFEST.name)

    if differences:
        print("committed output does not match a fresh run: " + ", ".join(differences), file=sys.stderr)
        return 1
    print(f"clean: {len(GENERATED_NAMES)} files and the manifest match a fresh run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
