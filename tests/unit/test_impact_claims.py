"""The published impact and capacity numbers, asserted against `objects/`.

Every number this feature prints in a document is asserted here from the object
YAML, with no import of `impact.py`. That is deliberate: a test that computes an
expectation with the code under test agrees with the code under test and notices
nothing.

The claims:

- `oms-fra-mil` holds 4,134,400 MHz of a 4,800,000 MHz C-band, in 40 wavelengths.
- `oms-ams-fra` carries seven wavelengths, which is the fiber-cut headline.
- `oms-ams-bru` at 220 km is the unique shortest section, which is why 400ZR and
  800ZR reach nothing.
- The propagation decomposition is exact, because every span is G.652.D.
- The ZR catalog table on the payloads page is the five pluggable modes, cell
  for cell. That one reads the page rather than the objects alone, because the
  drift it catches is between the two.

Retune the seed in `scripts/generate_geant_dataset.py` and these fail before any
document becomes quietly false. `test_geant_dataset.py` asserts the dataset
regenerates identically; this asserts what it means.
"""

from collections import defaultdict

import pytest

from infrahub_demo_otn.units import (
    CBAND_EXTENT_MHZ,
    GRID_CHANNEL_COUNT,
    FreeBlock,
    anchor_fits_band,
    carrier_interval_mhz,
    channel_to_frequency_mhz,
    free_blocks,
    m_to_km,
    mdb_to_db,
    ns_to_us,
)
from tests.unit.conftest import REPO_ROOT, objects_of_kind

CUT_SECTION = "oms-ams-fra"
"""The section an operator means by "the Frankfurt to Amsterdam fiber"."""

CONGESTED_SECTION = "oms-fra-mil"
"""The corridor the dataset holds at 4,134,400 MHz of 4,800,000 MHz."""

SHORTEST_SECTION = "oms-ams-bru"
"""220 km, and the reason both 120 km pluggables reach nothing."""


def _spans_by_name() -> dict[str, dict[str, object]]:
    return {str(span["name"]): span for span in objects_of_kind("OtnFiberSpan")}


def _section_lengths_m() -> dict[str, int]:
    spans = _spans_by_name()
    return {
        str(section["name"]): sum(int(spans[name]["length_m"]) for name in section["spans"])  # type: ignore[call-overload]
        for section in objects_of_kind("OtnOpticalMultiplexSection")
    }


def _intervals_on(section: str) -> list[tuple[int, int]]:
    """The half-open interval every carrier on `section` occupies, ascending.

    Rebuilt from the mode catalog rather than from `plant.py`, for the reason the
    module docstring gives: a test that derives its expectation with the code
    under test notices nothing.
    """
    bauds = {str(mode["name"]): int(mode["baud_mbaud"]) for mode in objects_of_kind("OtnOpticalMode")}  # type: ignore[call-overload]
    held = [
        carrier_interval_mhz(
            channel_to_frequency_mhz(int(str(carrier["channel"]))), bauds[str(carrier["optical_mode"])]
        )
        for carrier in objects_of_kind("OtnOpticalCarrier")
        if section in (carrier.get("sections") or [])
    ]
    return sorted(held)


def _occupied_width_mhz(section: str) -> int:
    return sum(upper - lower for lower, upper in _intervals_on(section))


def _channels_by_section() -> dict[str, list[int]]:
    used: dict[str, list[int]] = defaultdict(list)
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        channel = int(str(carrier["channel"]))
        for section in carrier.get("sections") or []:
            used[str(section)].append(channel)
    return used


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


def test_the_congested_corridor_holds_4134400_of_4800000_mhz() -> None:
    """The headline capacity claim, restated as spectrum.

    It used to read "96 total, 71 occupied, 25 free", and every one of those
    three numbers counted channel numbers. A carrier occupies a width, so the
    claim the dataset can support is a width: 40 wavelengths holding 4,134,400
    MHz of the 4,800,000 MHz the modelled C-band passes.

    The 96-channel grid is asserted alongside it because it has not moved. The
    grid is where a carrier may anchor; it is not how much room there is.
    """
    occupied = _channels_by_section()[CONGESTED_SECTION]
    assert GRID_CHANNEL_COUNT == 96
    assert len(occupied) == 40, f"{CONGESTED_SECTION} carries {len(occupied)} carriers, the published claim is 40"
    assert len(set(occupied)) == 40, "two carriers hold the same channel on the congested corridor"
    assert _occupied_width_mhz(CONGESTED_SECTION) == 4_134_400
    assert CBAND_EXTENT_MHZ - _occupied_width_mhz(CONGESTED_SECTION) == 665_600


def test_counting_free_channels_on_the_congested_corridor_overstates_it_56_to_1() -> None:
    """The negative result this feature exists to produce.

    Fifty-six channel numbers are unclaimed on the congested corridor and exactly
    one of them can anchor a 400G wavelength. The old reading, "free is 96 minus
    the channel numbers in use", is not conservative-but-wrong: it is wrong by a
    factor of 56 on the one corridor the demo is about.
    """
    occupied = set(_channels_by_section()[CONGESTED_SECTION])
    free = [channel for channel in range(1, GRID_CHANNEL_COUNT + 1) if channel not in occupied]
    assert len(free) == 56
    assert free[:3] == [1, 3, 4]

    blocks = free_blocks([FreeBlock(lower, upper) for lower, upper in _intervals_on(CONGESTED_SECTION)])
    anchorable = [
        channel
        for channel in free
        if anchor_fits_band(channel_to_frequency_mhz(channel), 64_000)
        and any(
            block.lower_mhz <= carrier_interval_mhz(channel_to_frequency_mhz(channel), 64_000)[0]
            and carrier_interval_mhz(channel_to_frequency_mhz(channel), 64_000)[1] <= block.upper_mhz
            for block in blocks
        )
    ]
    assert anchorable == [95]


def test_free_and_occupied_partition_the_grid_on_every_section() -> None:
    used = _channels_by_section()
    for section in _section_lengths_m():
        occupied = set(used.get(section, []))
        free = {channel for channel in range(1, GRID_CHANNEL_COUNT + 1) if channel not in occupied}
        assert occupied & free == set()
        assert occupied | free == set(range(1, GRID_CHANNEL_COUNT + 1))


# ---------------------------------------------------------------------------
# The fiber cut
# ---------------------------------------------------------------------------


def test_the_cut_section_carries_seven_wavelengths() -> None:
    """The fiber-cut headline, and the reason it is worth 2.8 Tbps.

    All seven also cross `oms-fra-mil`, which is what makes them Amsterdam to
    Milan wavelengths rather than Amsterdam to Frankfurt ones. It was twelve and
    4.8 Tbps before the plan was re-seeded to fit the C-band by width.
    """
    crossing = [
        carrier for carrier in objects_of_kind("OtnOpticalCarrier") if CUT_SECTION in (carrier.get("sections") or [])
    ]
    assert len(crossing) == 7
    assert sorted(int(str(carrier["channel"])) for carrier in crossing) == [2, 5, 8, 11, 14, 17, 20]
    assert all(CONGESTED_SECTION in (carrier.get("sections") or []) for carrier in crossing)


def test_no_shipped_carrier_has_a_service_behind_it() -> None:
    """The reason the impact report counts unattached spectrum explicitly.

    The forty shipped carriers are data, not provisioned services. A report that
    dropped a carrier for having no optical path would report an outage of zero
    services where the real answer is seven wavelengths.
    """
    carriers = objects_of_kind("OtnOpticalCarrier")
    assert len(carriers) == 40
    assert not [carrier for carrier in carriers if carrier.get("optical_path")]


# ---------------------------------------------------------------------------
# Reach
# ---------------------------------------------------------------------------


def test_the_shortest_section_is_amsterdam_to_brussels_at_220_km() -> None:
    lengths = _section_lengths_m()
    assert len(lengths) == 21
    shortest = min(lengths.items(), key=lambda item: item[1])
    assert shortest == (SHORTEST_SECTION, 220_000)
    assert sorted(lengths.values())[:3] == [220_000, 320_000, 330_000]
    assert len([length for length in lengths.values() if length == 220_000]) == 1


# ---------------------------------------------------------------------------
# Fiber and conduits
# ---------------------------------------------------------------------------


def test_every_span_is_g652d_so_the_propagation_split_is_exact() -> None:
    """FR-013a's premise, asserted rather than assumed.

    The latency report sums propagation per span at that span's own group index.
    While every span is G.652.D that equals one calculation over the total
    length, and the catalog already carries G.654.E at 1467 for the day it does
    not. This is the test that notices.
    """
    spans = objects_of_kind("OtnFiberSpan")
    # 132 core spans and the CWDM tail, which is the same fiber family.
    assert len(spans) == 133
    assert {str(span["fiber_type"]) for span in spans} == {"G.652.D"}
    indices = {str(fiber["name"]): int(fiber["group_index_milli"]) for fiber in objects_of_kind("OtnFiberType")}
    assert indices["G.652.D"] == 1468
    assert indices["G.654.E"] != indices["G.652.D"]


def test_the_two_conduits_that_make_the_diversity_finding_real() -> None:
    """The pairs the SRLG report is supposed to find.

    `oms-fra-mil` and `oms-fra-gva` are different corridors out of Frankfurt and
    they share `cd-fra-south`. That is the pair a route map calls diverse.
    """
    spans = _spans_by_name()
    sections_by_conduit: dict[str, set[str]] = defaultdict(set)
    for section in objects_of_kind("OtnOpticalMultiplexSection"):
        for name in section["spans"]:
            conduit = spans[str(name)].get("conduit")
            if conduit:
                sections_by_conduit[str(conduit)].add(str(section["name"]))

    assert {"oms-fra-mil", "oms-fra-gva"} <= sections_by_conduit["cd-fra-south"]
    assert {"oms-fra-mil", "oms-vie-mil"} <= sections_by_conduit["cd-mil-northeast"]
    assert len(sections_by_conduit) == 12


def test_most_spans_are_unducted_and_that_is_reported_not_grouped() -> None:
    """A null conduit is not a conduit named null.

    Grouping unducted spans under a single missing key would invent the largest
    shared-risk group in the network out of the absence of data.
    """
    spans = objects_of_kind("OtnFiberSpan")
    unducted = [span for span in spans if not span.get("conduit")]
    assert unducted, "the dataset has always had spans outside any conduit"
    assert len(unducted) < len(spans)


# ---------------------------------------------------------------------------
# The published ZR catalog
# ---------------------------------------------------------------------------

CATALOG_PAGE = REPO_ROOT / "docs" / "docs" / "ai-payloads.mdx"
"""The page that prints the coherent pluggable catalog as a table."""

CATALOG_HEADING = "## The ZR catalog"
"""The heading the table sits under. Renaming it must fail loudly, not silently
return no rows."""

CATALOG_COLUMNS = ("Mode", "Rate", "Required OSNR", "Nominal reach", "FEC", "FEC latency")
"""Every column of the published table. Five of the six come from
`objects/03_optical_modes.yml` and the sixth is the join key."""


def _catalog_rows() -> list[dict[str, str]]:
    """The ZR catalog table, one dict per row, read off the page itself.

    Parsed and not restated. A test that wrote 23.0 next to 23000 would assert
    that two constants in this file agree, and the page could still drift.
    `objects/03_optical_modes.yml` is hand-written, so the regenerate-and-diff
    test that guards `objects/1*.yml` never reaches it.

    The heading, the run of table lines and the header cells are each asserted
    before a single row is returned. That is the failure this parser is most
    likely to have one day: a reshaped table yields an empty list, and every
    assertion over an empty list passes.
    """
    lines = CATALOG_PAGE.read_text().splitlines()
    assert CATALOG_HEADING in lines, f"{CATALOG_PAGE.name} no longer carries a {CATALOG_HEADING!r} section"

    table: list[str] = []
    for line in lines[lines.index(CATALOG_HEADING) + 1 :]:
        if line.startswith("|"):
            table.append(line)
        elif table:
            break
    assert len(table) > 2, f"found {len(table)} table lines under {CATALOG_HEADING!r}, expected a header and rows"

    cells = [[cell.strip() for cell in line.strip("|").split("|")] for line in table]
    assert tuple(cells[0]) == CATALOG_COLUMNS, (
        f"the published columns are {cells[0]}, this test reads {CATALOG_COLUMNS}"
    )
    assert set(cells[1]) == {"---"}, f"expected a separator row under the header, got {cells[1]}"
    return [dict(zip(CATALOG_COLUMNS, row, strict=True)) for row in cells[2:]]


def _figure(cell: str, unit: str) -> float:
    """The number out of a cell like `26.0 dB`, with the printed unit asserted.

    The unit is checked rather than stripped. Every scale factor in
    `units.py` is a power of a thousand, so a cell that switched from km to m
    would still hold a number and only the suffix would say it had moved.
    """
    number, _, printed = cell.partition(" ")
    assert printed == unit, f"the cell {cell!r} is not in {unit}"
    return float(number)


def _rate_gbps(cell: str) -> int:
    """The line rate out of a cell like `400G`. No scale factor, gigabits both
    on the page and in the object."""
    assert cell.endswith("G"), f"the rate cell {cell!r} is not in gigabits"
    return int(cell.removesuffix("G"))


def test_the_published_zr_catalog_is_the_five_pluggable_modes() -> None:
    """Which rows the table holds, asserted in both directions.

    Set equality rather than a length check on its own. A pluggable added to
    the catalog and left out of the page fails here, and so does a row on the
    page naming a mode nothing ships.
    """
    rows = _catalog_rows()
    assert len(rows) == 5, f"the published catalog has {len(rows)} rows, expected 5"
    pluggables = [mode for mode in objects_of_kind("OtnOpticalMode") if mode.get("mode_class") == "pluggable"]
    assert len(pluggables) == 5
    assert {row["Mode"] for row in rows} == {str(mode["name"]) for mode in pluggables}


def test_every_published_zr_catalog_cell_matches_the_optical_mode_objects() -> None:
    """Each of the five figures per row, against the scaled integers.

    Conversion runs object to page through `units.py`, which is the only file
    allowed to hold a scale factor. Dividing by 1000 here would put a sixth
    copy of that constant in the repository, in the test whose job is to catch
    the figures moving.
    """
    modes = {str(mode["name"]): mode for mode in objects_of_kind("OtnOpticalMode")}
    checked = 0
    for row in _catalog_rows():
        name = row["Mode"]
        mode = modes[name]
        assert _rate_gbps(row["Rate"]) == int(str(mode["line_rate_gbps"])), f"{name} line rate"
        assert _figure(row["Required OSNR"], "dB") == pytest.approx(mdb_to_db(int(str(mode["required_osnr_mdb"])))), (
            f"{name} required OSNR"
        )
        assert _figure(row["Nominal reach"], "km") == pytest.approx(m_to_km(int(str(mode["nominal_reach_m"])))), (
            f"{name} nominal reach"
        )
        assert row["FEC"] == str(mode["fec_type"]), f"{name} FEC type"
        assert _figure(row["FEC latency"], "µs") == pytest.approx(ns_to_us(int(str(mode["fec_latency_ns"])))), (
            f"{name} FEC latency"
        )
        checked += 1
    assert checked == 5, f"asserted {checked} rows, expected 5"
