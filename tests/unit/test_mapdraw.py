"""`mapdraw.py`, against the shipped dataset and against the bands themselves.

The renderer is pure presentation, so most of what can go wrong in it is either
arithmetic on a boundary or a label landing on top of something. Both are here.

**The boundary.** One section in the dataset sits at +4.999 dB, a thousandth of
a decibel inside its band. A float compare against 5.0 moves it, and the map
still looks fine, so the band tests pin the edges exactly and the dataset test
pins the resulting counts.

**The placement.** The placer degrades to the cheapest position rather than
raising, so "every label was placed" passes on a map where everything overlaps.
The assertion below is on the result instead: zero overlapping label pairs and
zero labels lying across a route.

**The bytes.** `fixtures/network_map_golden.svg` was captured from this module's
fixtures before the shared drawing code moved into `mapchrome.py`, and is
compared byte for byte. It holds the extraction to being a refactor. A reference
taken after the move would assert only that the moved code matches itself.

Building the fixture needs the optical engine, and that is fine here. The
no-arithmetic rule binds `mapdraw.py`, not the test that feeds it: this module
is standing in for the transform, and it has to produce the same numbers the
transform will.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from infrahub_demo_otn.budget import ModeInput, SectionInput, evaluate_path, span_loss_mdb
from infrahub_demo_otn.cartography import Placer
from infrahub_demo_otn.mapchrome import MapSite, text_width
from infrahub_demo_otn.mapdraw import (
    BAR_WIDTH,
    CHIP_MIN_OCCUPIED_MHZ,
    COLUMN_BAR,
    COLUMN_SPECTRUM,
    MAP_DIALECT,
    MARGIN_BAND_EDGES_MDB,
    MARGIN_BANDS,
    MARGIN_RULE,
    REFERENCE_MODE_NAME,
    UNKNOWN_BAND,
    MapSection,
    layout_map,
    render_map,
    route_segments,
)
from infrahub_demo_otn.mapengine import table_order
from infrahub_demo_otn.plant import build_mode, build_section
from infrahub_demo_otn.units import (
    CBAND_EXTENT_MHZ,
    carrier_interval_mhz,
    channel_to_frequency_mhz,
    free_blocks,
    mhz_to_ghz,
)
from tests.unit.conftest import objects_of_kind


def _facility_by_site() -> dict[str, str]:
    """shortname -> the facility on it, off the `OtnFacility.site` edge.

    Not off a tag name. The six `eurohpc-` tags are still on their sites and
    nothing reads them; `schemas/location.yml` records what a tag read cost.
    """
    return {str(record["site"]): str(record["name"]) for record in objects_of_kind("OtnFacility")}


def _by_name(kind: str) -> dict[str, dict[str, Any]]:
    return {str(record["name"]): dict(record) for record in objects_of_kind(kind)}


def _spans_with_pumps() -> dict[str, dict[str, Any]]:
    """Every span, carrying the pumps that point at it.

    The object files write the edge on the pump, because `OtnRamanPump.span` is
    the mandatory side. Without this walk the pumped section budgets as if it
    were unpumped, both directions agree, and the one asymmetric route on the
    map quietly stops being asymmetric.
    """
    spans = {name: dict(record) for name, record in _by_name("OtnFiberSpan").items()}
    for pump in objects_of_kind("OtnRamanPump"):
        span = spans[str(pump["span"])]
        span.setdefault("raman_pumps", {"edges": []})["edges"].append({"node": dict(pump)})
    return spans


def _section_inputs() -> dict[str, SectionInput]:
    fibers = _by_name("OtnFiberType")
    spans = _spans_with_pumps()
    amplifiers = _by_name("OtnAmplifier")
    roadms = _by_name("OtnRoadm")
    built: dict[str, SectionInput] = {}
    for name, record in _by_name("OtnOpticalMultiplexSection").items():
        built[name] = build_section(
            name=name,
            head=roadms[str(record["roadm_a"])],
            tail=roadms[str(record["roadm_b"])],
            spans=[(spans[str(span)], fibers[str(spans[str(span)]["fiber_type"])]) for span in record["spans"]],
            amplifiers_a2b=[amplifiers[str(amplifier)] for amplifier in record["amplifiers_a2b"]],
            amplifiers_b2a=[amplifiers[str(amplifier)] for amplifier in record["amplifiers_b2a"]],
        )
    return built


def _reference_mode() -> ModeInput:
    return build_mode(_by_name("OtnOpticalMode")[REFERENCE_MODE_NAME])


def _site_by_roadm() -> dict[str, str]:
    """ROADM name to the shortname of the site it stands in.

    Read off the `site` relationship, never off the ROADM's own name. A name is
    an identifier here and nothing recovers meaning from one, which is what
    `test_repository_config.py::test_nothing_reads_a_device_name_for_meaning`
    holds the whole tree to.
    """
    return {name: str(record["site"]) for name, record in _by_name("OtnRoadm").items()}


def _occupancy() -> dict[str, int]:
    """Section name to the spectrum its carriers hold, in MHz.

    The same union the transform computes: the band minus the free blocks, so
    two carriers on overlapping spectrum are counted once. Built from the object
    files rather than typed, because the whole point of the figure is that it
    follows each carrier's symbol rate rather than counting wavelengths.
    """
    baud_of = {str(mode["name"]): int(mode["baud_mbaud"]) for mode in objects_of_kind("OtnOpticalMode")}
    intervals: dict[str, list[Any]] = {}
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        lower, upper = carrier_interval_mhz(
            channel_to_frequency_mhz(int(carrier["channel"])), baud_of[str(carrier["optical_mode"])]
        )
        for section in carrier["sections"]:
            intervals.setdefault(str(section), []).append(_Interval(lower, upper))
    return {
        name: CBAND_EXTENT_MHZ - sum(block.width_mhz for block in free_blocks(held)) for name, held in intervals.items()
    }


@dataclass(frozen=True)
class _Interval:
    """The half-open pair `units.free_blocks` sweeps over, and nothing more."""

    lower_mhz: int
    upper_mhz: int


@cache
def dataset() -> tuple[tuple[MapSite, ...], tuple[MapSection, ...]]:
    """The shipped network, in the shape the transform will hand to the renderer.

    Cached because `objects/` is 11,500 lines and every test below wants the
    same 14 sites and 21 sections.
    """
    section_inputs = _section_inputs()
    mode = _reference_mode()
    used = _occupancy()
    site_of = _site_by_roadm()

    degree: dict[str, int] = {}
    sections: list[MapSection] = []
    for name in sorted(section_inputs):
        built = section_inputs[name]
        site_a = site_of[built.head_node.name]
        site_b = site_of[built.tail_node.name]
        degree[site_a] = degree.get(site_a, 0) + 1
        degree[site_b] = degree.get(site_b, 0) + 1

        total_m = sum(span.length_m for span in built.spans)
        running = 0
        boundaries: list[float] = []
        for span in built.spans[:-1]:
            running += span.length_m
            boundaries.append(running / total_m)

        forward = evaluate_path([built], mode, built.head_node.name)
        reverse = evaluate_path([built], mode, built.tail_node.name)
        sections.append(
            MapSection(
                name=name,
                site_a=site_a,
                site_b=site_b,
                length_m=total_m,
                loss_mdb=sum(span_loss_mdb(span) for span in built.spans),
                margin_a_to_b_mdb=forward.osnr_margin_mdb,
                margin_b_to_a_mdb=reverse.osnr_margin_mdb,
                span_boundaries=tuple(boundaries),
                raman_pumped=any(span.raman_gain_mdb or span.raman_gain_reverse_mdb for span in built.spans),
                occupied_mhz=used.get(name, 0),
                band_extent_mhz=CBAND_EXTENT_MHZ,
            )
        )

    facilities = _facility_by_site()
    sites: list[MapSite] = []
    for record in objects_of_kind("OtnSite"):
        shortname = str(record["shortname"])
        # A site in no section is not on this map. The customer campus is the
        # one the shipped dataset has, and it is correct that it is absent.
        if shortname not in degree:
            continue
        sites.append(
            MapSite(
                name=str(record["name"]),
                shortname=shortname,
                longitude_microdeg=int(record["longitude_microdeg"]),
                latitude_microdeg=int(record["latitude_microdeg"]),
                optical_degree=degree[shortname],
                eurohpc_name=facilities.get(shortname),
            )
        )
    return tuple(sorted(sites, key=lambda site: site.shortname)), tuple(sections)


def named(name: str) -> MapSection:
    _, sections = dataset()
    return next(section for section in sections if section.name == name)


# ---------------------------------------------------------------------------
# The fixture is the dataset the rest of the file claims it is
# ---------------------------------------------------------------------------


def test_the_fixture_is_the_shipped_network() -> None:
    sites, sections = dataset()
    assert len(sites) == 14
    assert len(sections) == 21
    assert sum(section.span_count for section in sections) == 132


# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------


def test_the_band_edges_are_zero_two_and_five_decibels() -> None:
    assert MARGIN_BAND_EDGES_MDB == (0, 2_000, 5_000)


@pytest.mark.parametrize(
    ("margin_mdb", "expected"),
    [
        (-1, "negative"),
        (-535, "negative"),
        (0, "thin"),
        (1_999, "thin"),
        (2_000, "fair"),
        (4_999, "fair"),
        (5_000, "ample"),
        (9_390, "ample"),
    ],
)
def test_a_margin_lands_in_the_band_its_edges_put_it_in(margin_mdb: int, expected: str) -> None:
    """Half open, lower inclusive. The 4,999 case is a real section."""
    assert MARGIN_RULE.band_for(margin_mdb).key == expected


def test_an_absent_margin_is_its_own_colour_and_not_a_passing_one() -> None:
    band = MARGIN_RULE.band_for(None)
    assert band is UNKNOWN_BAND
    assert band not in MARGIN_BANDS
    assert band.colour not in [other.colour for other in MARGIN_BANDS]


def test_every_band_has_its_own_colour() -> None:
    colours = [band.colour for band in MARGIN_BANDS] + [UNKNOWN_BAND.colour]
    assert len(colours) == len(set(colours))


def test_the_legend_wording_comes_off_the_edges() -> None:
    """Stored in millidecibels, said in decibels, and the unknown band says neither."""
    captions = [MARGIN_RULE.caption(band) for band in MARGIN_BANDS]
    assert captions == [
        "below 0 dB, does not close",
        "0 to 2 dB",
        "2 to 5 dB",
        "5 dB and above",
    ]
    assert MARGIN_RULE.caption(UNKNOWN_BAND) == "not computed"


def test_the_worse_direction_decides_and_a_missing_one_makes_it_unknown() -> None:
    """Half an answer painted as a whole one is the failure this pins."""
    both = MapSection("s", "a", "b", 1, 1, 6_000, 1_000, ())
    assert both.worse_margin_mdb == 1_000
    assert MARGIN_RULE.band_for(both.worse_margin_mdb).key == "thin"
    assert both.is_asymmetric

    half = MapSection("s", "a", "b", 1, 1, 6_000, None, ())
    assert half.worse_margin_mdb is None
    assert MARGIN_RULE.band_for(half.worse_margin_mdb) is UNKNOWN_BAND
    assert not half.is_asymmetric


# ---------------------------------------------------------------------------
# What the shipped dataset colours
# ---------------------------------------------------------------------------


def test_the_bands_split_the_shipped_sections_one_two_six_twelve() -> None:
    """The dataset's whole colour story. A seed change that moves a section
    fails here rather than quietly redrawing the map."""
    _, sections = dataset()
    counted = {band.key: 0 for band in MARGIN_BANDS}
    counted[UNKNOWN_BAND.key] = 0
    for section in sections:
        counted[MARGIN_RULE.band_for(section.worse_margin_mdb).key] += 1
    assert counted == {"negative": 1, "thin": 2, "fair": 6, "ample": 12, "unknown": 0}


def test_prague_to_frankfurt_sits_one_thousandth_of_a_decibel_inside_its_band() -> None:
    """The reason the comparison is integer millidecibels and not a float."""
    section = named("oms-prg-fra")
    assert section.worse_margin_mdb == 4_999
    assert MARGIN_RULE.band_for(section.worse_margin_mdb).key == "fair"


def test_vienna_to_milan_is_the_one_asymmetric_section() -> None:
    """The Raman-pumped route. On-off gain is credited one way and the
    combiner's insertion loss is charged both, so the two chains disagree."""
    _, sections = dataset()
    asymmetric = [section.name for section in sections if section.is_asymmetric]
    assert asymmetric == ["oms-vie-mil"]
    pumped = [section.name for section in sections if section.raman_pumped]
    assert pumped == ["oms-vie-mil"]
    section = named("oms-vie-mil")
    spread = abs((section.margin_a_to_b_mdb or 0) - (section.margin_b_to_a_mdb or 0))
    assert spread > 9_000


def test_paris_to_madrid_is_red_both_ways_and_is_not_marked_asymmetric() -> None:
    """Uniformly bad is a different statement from bad one way, and the map
    must not blur the two."""
    section = named("oms-par-mad")
    assert MARGIN_RULE.band_for(section.margin_a_to_b_mdb).key == "negative"
    assert MARGIN_RULE.band_for(section.margin_b_to_a_mdb).key == "negative"
    assert not section.is_asymmetric
    assert not section.raman_pumped


def test_frankfurt_to_milan_is_the_only_section_with_an_occupancy_chip() -> None:
    """The flagship corridor, measured in spectrum rather than in wavelengths.

    Forty carriers hold 4,134,400 MHz of the 4,800,000 MHz band, which is the
    figure the panel prints and the bar draws. The next busiest section carries
    seven carriers and 1,050,000 MHz, under the quarter-band threshold, so one
    route wears a chip and twenty do not.
    """
    _, sections = dataset()
    chipped = [section.name for section in sections if section.has_chip]
    assert chipped == ["oms-fra-mil"]
    section = named("oms-fra-mil")
    assert section.occupied_mhz == 4_134_400
    assert section.band_extent_mhz == 4_800_000
    assert CHIP_MIN_OCCUPIED_MHZ == 1_200_000
    assert named("oms-ams-fra").occupied_mhz == 1_050_000


def test_the_panel_table_leads_with_the_section_that_does_not_close() -> None:
    _, sections = dataset()
    ordered = table_order(sections, MARGIN_RULE.measure, MAP_DIALECT.name)
    assert ordered[0].name == "oms-par-mad"
    margins = [section.worse_margin_mdb for section in ordered]
    assert margins == sorted(margins, key=lambda value: (0, 0) if value is None else (1, value))


# ---------------------------------------------------------------------------
# The output
# ---------------------------------------------------------------------------


def test_the_output_parses_as_xml() -> None:
    sites, sections = dataset()
    root = ElementTree.fromstring(render_map(sites, sections, "par"))
    assert root.tag.endswith("svg")


def test_the_map_is_not_an_empty_frame() -> None:
    """XML parses on a blank document. This is what says a map came out.

    Every country in the basemap draws one path, every route draws a line and
    every site draws a disc, so the counts below are floors that a base layer
    silently dropping out would fall through.
    """
    sites, sections = dataset()
    root = ElementTree.fromstring(render_map(sites, sections, "par"))
    tags = [child.tag.rsplit("}", 1)[-1] for child in root.iter()]
    assert tags.count("path") >= 40
    assert tags.count("line") >= len(sections)
    assert tags.count("circle") >= len(sites)
    assert tags.count("text") >= len(sites) + len(sections)


def test_two_renders_of_the_same_input_are_byte_identical() -> None:
    """An unchanged network must not produce a changed artifact."""
    sites, sections = dataset()
    first = render_map(sites, sections, "par")
    second = render_map(sites, sections, "par")
    assert first == second
    shuffled = render_map(tuple(reversed(sites)), tuple(reversed(sections)), "par")
    assert shuffled == first


# ---------------------------------------------------------------------------
# The extraction invariant
# ---------------------------------------------------------------------------

GOLDEN_PATH = Path(__file__).resolve().parent / "fixtures" / "network_map_golden.svg"
GOLDEN_FOCUS = "par"
GOLDEN_BRANCH = "main"


def golden_render() -> str:
    """The render the committed reference is a copy of.

    Focused on one site and read from a named branch, so the focus ring and the
    footer's provenance line are both in the bytes. Neither is exercised by a
    render with `focus=None` and no branch, and both are shared furniture that a
    refactor of the drawing code can move.
    """
    sites, sections = dataset()
    return render_map(sites, sections, GOLDEN_FOCUS, GOLDEN_BRANCH)


def test_the_extraction_changed_no_pixels() -> None:
    """The network map's output is frozen against a reference committed before
    the shared drawing code moved out of `mapdraw.py`.

    This is not a snapshot test of the whole map's design. It is one invariant
    with one purpose: the chrome extraction is a refactor, so it has to change no
    byte of the existing artifact, and a reference taken after the move would
    only assert that the moved code matches itself.

    A failure here is either the extraction moving a pixel or a deliberate change
    to the map. `docs/docs/developer-guide.mdx` says how to refresh the
    file, and refreshing it is a decision, not a fix.
    """
    assert GOLDEN_PATH.exists(), f"the reference render is missing from {GOLDEN_PATH}"
    assert golden_render() == GOLDEN_PATH.read_text(), (
        f"the render no longer matches {GOLDEN_PATH.name}: the shared drawing code moved a pixel"
    )


def test_the_occupancy_figure_clears_the_bar_it_sits_beside() -> None:
    """The panel's own edges are measured elsewhere; this is the gap inside it.

    `tests/unit/test_mapchrome.py` catches a cell that runs off the panel. It
    cannot catch one that runs backwards over the occupancy bar, because both
    stay inside the box. The figure went from two glyphs to five when it stopped
    being a count of anchors, and the bar had to narrow for it to fit.
    """
    widest = max(mhz_to_ghz(section.occupied_mhz) for _, sections in [dataset()] for section in sections)
    cell = text_width(f"{widest:,.0f}", 10.0, "700")
    gap = COLUMN_SPECTRUM - cell - (COLUMN_BAR + BAR_WIDTH)
    assert gap > 0.0, f"the occupancy figure overlaps the bar by {-gap:.1f}px"


# ---------------------------------------------------------------------------
# The golden cases
# ---------------------------------------------------------------------------
#
# Four inputs, four fixtures. `golden_render` above is the first of them, and it
# is the render this map ships today. The three below reach the paths it does
# not. Each takes no arguments and is deterministic, so each maps to exactly one
# fixture file.

SECOND_FOCUS = "mil"

UNKNOWN_MARGIN_SECTION = "oms-vie-mil"
"""The section given an unevaluated direction, so a route lands in `UNKNOWN_BAND`."""


def render_with_no_focus() -> str:
    """The whole network with nobody highlighted, and no branch in the footer.

    Both defaults of the four-argument signature at once. Nothing freezes this
    path today, so the absent focus ring and the footer line the branch would
    have written are unpinned in the bytes.
    """
    sites, sections = dataset()
    return render_map(sites, sections)


def render_focused_on_milan() -> str:
    """The same network seen from a second site.

    Moving the focus moves the ring and the label placer searches from a
    different starting cost, so this reaches placements Paris does not.
    """
    sites, sections = dataset()
    return render_map(sites, sections, SECOND_FOCUS, GOLDEN_BRANCH)


def render_with_an_unknown_margin() -> str:
    """One section whose reverse direction did not evaluate.

    The shipped dataset puts nothing in `UNKNOWN_BAND`, so that band's colour and
    its legend swatch reach no fixture otherwise. Vienna to Milan is the section
    chosen because it is the one asymmetric route: dropping a direction there
    drops the asymmetry marker too, so one input covers both.
    """
    sites, sections = dataset()
    unknown = tuple(
        replace(section, margin_b_to_a_mdb=None) if section.name == UNKNOWN_MARGIN_SECTION else section
        for section in sections
    )
    return render_map(sites, unknown, GOLDEN_FOCUS, GOLDEN_BRANCH)


FIXTURE_DIR = GOLDEN_PATH.parent

GOLDEN_CASES: dict[str, Callable[[], str]] = {
    "network_map_golden": golden_render,
    "network_map_no_focus": render_with_no_focus,
    "network_map_focused_on_milan": render_focused_on_milan,
    "network_map_unknown_margin": render_with_an_unknown_margin,
}
"""Every gated input, keyed by the fixture file it is compared against.

The key is the file name without its suffix, so a reader who sees a failing case
knows which file to look at and a reader who sees a file knows which builder made
it. `network_map_golden` is the render that already shipped, and its bytes are
the ones `test_the_extraction_changed_no_pixels` has always compared.
"""


def golden_path(case: str) -> Path:
    """The committed fixture one case is compared against."""
    return FIXTURE_DIR / f"{case}.svg"


def regenerate(case: str) -> None:
    """Overwrite one fixture from a fresh render.

    Never called by a test. It exists so the failure message below can name a
    command that works, and calling it is a decision that belongs in its own
    commit.
    """
    golden_path(case).write_text(GOLDEN_CASES[case]())


def regeneration_command(case: str) -> str:
    """The command that regenerates one fixture, spelled into the failure message.

    A reader who hits a red golden should not have to open the developer guide to
    find out what the deliberate path is.
    """
    return f"uv run python -c \"from tests.unit.test_mapdraw import regenerate; regenerate('{case}')\""


@pytest.mark.parametrize("case", sorted(GOLDEN_CASES))
def test_the_network_map_renders_the_bytes_it_shipped(case: str) -> None:
    """Four inputs, four committed renders, compared byte for byte.

    One render gates one path. The focused case leaves `focus=None` unpinned, a
    single focus site leaves the label placer's crossing penalty free to score
    differently, and the shipped dataset puts nothing in `UNKNOWN_BAND` at all.
    Each case above reaches what the others do not.

    All four fixtures were captured from the renderer as it stood before the two
    map modules were merged, so a failure here is the merge moving a pixel and
    not the moved code agreeing with itself.
    """
    path = golden_path(case)
    assert path.exists(), f"the committed render is missing from {path}"
    assert GOLDEN_CASES[case]() == path.read_text(), (
        f"the render no longer matches {path.name}. If a drawing change was "
        f"deliberate, refresh the fixture in its own commit:\n    {regeneration_command(case)}"
    )


@pytest.mark.parametrize("case", sorted(GOLDEN_CASES))
def test_a_golden_case_renders_the_same_bytes_twice_in_one_process(case: str) -> None:
    """A fixture captured from a renderer that varies run to run gates nothing.

    This is the difference between the fixtures testing determinism and the
    fixtures testing capture luck: if a second call in the same process differs,
    whichever render reached the file was an accident.
    """
    builder = GOLDEN_CASES[case]
    assert builder() == builder()


def test_the_focus_site_changes_the_picture() -> None:
    """Copies that differ only by which dot is larger are copies of one picture."""
    sites, sections = dataset()
    assert render_map(sites, sections, "par") != render_map(sites, sections, "mil")
    assert render_map(sites, sections, "par") != render_map(sites, sections, None)


def test_a_section_ending_nowhere_is_refused() -> None:
    sites, _ = dataset()
    orphan = MapSection("oms-nowhere", "par", "zzz", 1_000, 1_000, 1_000, 1_000, ())
    with pytest.raises(ValueError, match="not among the sites"):
        layout_map(sites, [orphan])


# ---------------------------------------------------------------------------
# Placement, asserted on the result
# ---------------------------------------------------------------------------


def test_no_label_overlaps_another_label_on_the_shipped_dataset() -> None:
    """Not "placement returned". The placer always returns."""
    sites, sections = dataset()
    layout = layout_map(sites, sections, "par")
    auditor = Placer()
    for rect in layout.label_rects:
        assert auditor.overlap(rect) == 0.0, rect
        auditor.block(*rect)


def test_no_label_lies_across_a_route_on_the_shipped_dataset() -> None:
    """A distance label rides its own route, so that one segment is excused.
    Every other route crossing every other label is a defect."""
    sites, sections = dataset()
    layout = layout_map(sites, sections, "par")
    auditor = Placer()
    for segment in route_segments(layout):
        auditor.add_segment(*segment)
    for placed in layout.sites:
        assert auditor.crossings(placed.label) == 0, placed.site.shortname
    for placed in layout.sections:
        assert placed.label is not None, placed.section.name
        assert auditor.crossings(placed.label.rect, ignore=placed.section.name) == 0, placed.section.name


def test_every_distance_label_stays_on_its_own_route() -> None:
    """FR-008. Collisions resolve by sliding along the line, never away from it."""
    layout = layout_map(*dataset(), "par")
    for placed in layout.sections:
        assert placed.label is not None, placed.section.name
        centre_x, centre_y = placed.label.centre
        span_x, span_y = placed.bx - placed.ax, placed.by - placed.ay
        offset_x, offset_y = centre_x - placed.ax, centre_y - placed.ay
        cross = abs(span_x * offset_y - span_y * offset_x) / (span_x**2 + span_y**2) ** 0.5
        assert cross == pytest.approx(0.0, abs=1e-6), placed.section.name
        along = (offset_x * span_x + offset_y * span_y) / (span_x**2 + span_y**2)
        assert 0.0 < along < 1.0, placed.section.name


def test_no_label_leaves_the_canvas() -> None:
    layout = layout_map(*dataset(), "par")
    for x, y, width, height in layout.label_rects:
        assert x >= 0.0 and y >= 0.0
        assert x + width <= 1680.0
        assert y + height <= 1080.0


def test_a_branch_with_no_carriers_renders_the_plant_and_no_chips() -> None:
    """`network-map.mdx` promises this state, so it is asserted rather than assumed.

    A branch can hold the whole plant and not one carrier: the sections exist,
    the fiber exists, and nothing is lit yet. The page says the occupancy column
    reads zero on every row, no route wears a chip, and the totals line reads
    zero, and calls that the correct picture rather than a broken render. The
    risk is not that it crashes. It is that an empty occupancy map is mistaken
    for missing data and something is drawn to fill the gap.
    """
    sites, sections = dataset()
    unlit = tuple(replace(section, occupied_mhz=0) for section in sections)

    assert any(section.occupied_mhz for section in sections), "the shipped dataset must have carriers to remove"
    assert not any(section.has_chip for section in unlit)

    svg = render_map(sites, unlit, "par")
    root = ElementTree.fromstring(svg)

    # The plant is still fully drawn. Same routes, same sites, same basemap.
    lit = ElementTree.fromstring(render_map(sites, sections, "par"))
    assert _count_tag(root, "line") == _count_tag(lit, "line")
    assert _count_tag(root, "circle") == _count_tag(lit, "circle")
    assert _count_tag(root, "path") == _count_tag(lit, "path")

    # Every gigahertz figure the render draws. On an unlit branch the only two
    # are the totals rows, so a third would be a chip that outlived its carriers.
    texts = [(element.text or "").strip() for element in root.iter()]
    assert [text for text in texts if text.endswith(" GHz")] == ["0 GHz", "4,800 GHz"], (
        "a chip survived on a branch with no carriers"
    )

    # The totals line reads zero, and the row count is untouched.
    slots = texts.index("Spectrum in use")
    assert texts[slots + 1] == "0 GHz"
    assert len([section for section in table_order(unlit, MARGIN_RULE.measure, MAP_DIALECT.name)]) == len(sections)


def _count_tag(root: ElementTree.Element, tag: str) -> int:
    return sum(1 for element in root.iter() if element.tag.endswith(tag))
