"""`odudraw.py`, against the shipped dataset and against the bands themselves.

The renderer is pure presentation, so what can go wrong in it is arithmetic on a
band edge, an unknown figure painted as a good one, or two renders of one branch
disagreeing. All three are here.

**The trap.** `containers.largest_fit(0)` returns `None` because nothing fits,
while `largest_fit(None)` raises, because an unknown free count has no answer and
`None` would report it as full. The renderer has to branch on unknown before it
calls, and the two tests that pin the order of that branch are the reason this
file exists rather than a smoke test on the SVG.

**The pair.** Colour is the roomiest lit carrier and the fill bar is the tightest,
and the section that separates them is one whose aggregate reads 39 per cent while
its ODU4 sits at 76 of 80. Averaging the two would tell a planner they have room
where they have none, so a test holds them apart.

**The bytes.** The records come from a GraphQL response whose ordering is not
guaranteed, so the shuffle test feeds the same network in reverse and asserts the
document does not move. Break a sort in `odudraw.py` and it fails.

Building the fixture reads `objects/` and does the slot arithmetic through
`containers.py`. The no-arithmetic rule binds `odudraw.py`, not the test that
feeds it: this module stands in for the transform, and it has to produce the
numbers the transform will.
"""

from collections.abc import Callable
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from infrahub_demo_otn.containers import free_slots, section_headroom, section_tightest, slots_occupied
from infrahub_demo_otn.mapchrome import PANEL_LEFT, PANEL_WIDTH, UNKNOWN_ROUTE, MapSite, text_width
from infrahub_demo_otn.mapengine import table_order
from infrahub_demo_otn.odudraw import (
    FIT_NOTHING,
    FIT_UNKNOWN,
    HEADROOM_BAND_EDGES_SLOTS,
    HEADROOM_BANDS,
    HEADROOM_RULE,
    NO_ODU_BAND,
    ODU_DIALECT,
    OduSection,
    has_no_odu_layer,
    layout_odu_map,
    render_odu_map,
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


def _containers_by_carrier() -> dict[str, list[dict[str, Any]]]:
    """Every container that holds a carrier, keyed by that carrier.

    The line container is the one with a `carrier`; a client container has a
    parent instead. Reading the carrier off the container rather than the other
    way round is what the schema's mandatory side dictates.
    """
    held: dict[str, list[dict[str, Any]]] = {}
    for container in objects_of_kind("OtnContainer"):
        carrier = container.get("carrier")
        if carrier is None:
            continue
        held.setdefault(str(carrier), []).append(dict(container))
    return held


def _children_by_parent() -> dict[str, list[dict[str, Any]]]:
    children: dict[str, list[dict[str, Any]]] = {}
    for container in objects_of_kind("OtnContainer"):
        parent = container.get("parent_container")
        if parent is None:
            continue
        children.setdefault(str(parent), []).append(dict(container))
    return children


def _carrier_free_slots(carrier: str) -> tuple[int, int | None, int | None]:
    """One lit carrier's container count, offered slots and free slots.

    Offered and free are `None` together when the tree holds a type the slot
    table cannot size, because a parent with an unsized child has no free figure
    rather than one computed from its sized siblings.
    """
    line_containers = _containers_by_carrier().get(carrier, [])
    children = _children_by_parent()
    offered = 0
    free: int | None = 0
    for container in line_containers:
        capacity = int(container["tributary_slot_capacity"])
        occupancies = [slots_occupied(str(child["odu_type"])) for child in children.get(str(container["name"]), [])]
        this_free = free_slots(capacity, occupancies)
        offered += capacity
        free = None if this_free is None or free is None else free + this_free
    return len(line_containers), (offered if free is not None else None), free


@cache
def dataset() -> tuple[tuple[MapSite, ...], tuple[OduSection, ...]]:
    """The shipped network, in the shape the transform will hand to the renderer.

    Cached because `objects/` is 11,500 lines and every test below wants the same
    14 sites and 21 sections.
    """
    site_of_roadm = {name: str(record["site"]) for name, record in _by_name("OtnRoadm").items()}
    carriers_on: dict[str, list[str]] = {}
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        for section in carrier["sections"]:
            carriers_on.setdefault(str(section), []).append(str(carrier["name"]))

    degree: dict[str, int] = {}
    sections: list[OduSection] = []
    for name, record in sorted(_by_name("OtnOpticalMultiplexSection").items()):
        site_a = site_of_roadm[str(record["roadm_a"])]
        site_b = site_of_roadm[str(record["roadm_b"])]
        degree[site_a] = degree.get(site_a, 0) + 1
        degree[site_b] = degree.get(site_b, 0) + 1

        lit = 0
        offered: list[int | None] = []
        frees: list[int | None] = []
        for carrier_name in sorted(carriers_on.get(name, [])):
            count, carrier_offered, carrier_free = _carrier_free_slots(carrier_name)
            if count == 0:
                continue
            lit += 1
            offered.append(carrier_offered)
            frees.append(carrier_free)

        known = not any(figure is None for figure in offered + frees) and bool(frees)
        offered_total = sum(figure for figure in offered if figure is not None) if known else None
        free_total = sum(figure for figure in frees if figure is not None) if known else None
        sections.append(
            OduSection(
                name=name,
                site_a=site_a,
                site_b=site_b,
                carriers_lit=lit,
                committed_slots=None if offered_total is None or free_total is None else offered_total - free_total,
                offered_slots=offered_total,
                headroom_slots=section_headroom(frees),
                tightest_free_slots=section_tightest(frees),
            )
        )

    facilities = _facility_by_site()
    sites: list[MapSite] = []
    for record in objects_of_kind("OtnSite"):
        shortname = str(record["shortname"])
        # A site in no section is not on this map. The customer campus is the one
        # the shipped dataset has, and it is correct that it is absent.
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


def named(name: str) -> OduSection:
    _, sections = dataset()
    return next(section for section in sections if section.name == name)


def _count_tag(root: ElementTree.Element, tag: str) -> int:
    return sum(1 for element in root.iter() if element.tag.endswith(tag))


def _texts(svg: str) -> list[str]:
    return [(element.text or "").strip() for element in ElementTree.fromstring(svg).iter()]


# ---------------------------------------------------------------------------
# The fixture is the dataset the rest of the file claims it is
# ---------------------------------------------------------------------------


def test_the_fixture_is_the_shipped_network() -> None:
    sites, sections = dataset()
    assert len(sites) == 14
    assert len(sections) == 21
    # 58, not 40. A wavelength runs end to end over several sections, so each of
    # the 40 carriers is counted by every section it crosses. The count is a
    # per-section figure and the panel says so; summing it over the network is
    # what this line does deliberately and no panel row does.
    assert sum(section.carriers_lit for section in sections) == 58


def test_the_shipped_wavelengths_are_lit_and_empty_where_they_run_at_all() -> None:
    """The base dataset's whole capacity story, in both directions.

    Every pre-provisioned carrier arrives holding one empty line container, so
    where a wavelength runs the headroom is the full offering and nothing is
    committed. Where none runs there is no ODU layer to report, and that is 16 of
    the 21 sections: the shipped carriers cover five route segments, not the whole
    network. A dataset change that stops shipping the line containers fails here
    rather than quietly turning the five coloured routes grey.
    """
    _, sections = dataset()
    lit = [section for section in sections if section.carriers_lit]
    dark = [section for section in sections if not section.carriers_lit]
    assert len(lit) == 5
    assert {section.committed_slots for section in lit} == {0}
    assert all(section.band is NO_ODU_BAND for section in dark)
    # Two offerings, not one. The five carriers over Vienna to Milan are 100G and
    # ride an ODU4 at 80 slots; the other 66 are 400G and ride an ODUC4 at 320.
    assert sorted({section.headroom_slots for section in lit}) == [80, 320]
    assert named("oms-vie-mil").headroom_slots == 80
    # Frankfurt to Milan carries all 71, so its roomiest is an empty ODUC4 and its
    # tightest is an empty ODU4. Both figures real, and they disagree.
    assert (named("oms-fra-mil").headroom_slots, named("oms-fra-mil").tightest_free_slots) == (320, 80)


def test_the_shipped_dataset_splits_sixteen_grey_and_five_roomy() -> None:
    """The base dataset's whole colour story.

    Sixteen sections have no carrier at all, so they are honestly unknown rather
    than empty and available. The five that do are all in the roomiest band,
    because nothing is provisioned on `main`. The four real bands are earned on a
    branch with services on it, which is what the mixed-fill scenario is for; a
    dataset change that moves a section fails here rather than quietly redrawing
    the map.
    """
    _, sections = dataset()
    counted = {band.key: 0 for band in HEADROOM_BANDS}
    counted[NO_ODU_BAND.key] = 0
    for section in sections:
        counted[section.band.key] += 1
    assert counted == {"full": 0, "odu0": 0, "odu2": 0, "odu4": 5, "no-odu": 16}


def test_the_panel_row_carries_all_four_columns_for_a_real_section() -> None:
    """FR-020, on the busiest section in the dataset.

    Frankfurt to Milan carries all 40 wavelengths, so its row is the one where
    every column has something to say: the count, the committed total over the
    offered total, the tightest carrier's figure and the exact type that fits.
    """
    section = named("oms-fra-mil")
    assert (section.carriers_lit, section.committed_slots, section.offered_slots) == (40, 0, 12_080)
    assert section.largest_fit == "ODUC4"

    texts = _texts(render_odu_map(*dataset(), "fra", "main"))
    assert "40" in texts
    assert "0/12080" in texts
    assert "ODUC4" in texts


def test_the_totals_do_not_sum_one_wavelength_over_every_section_it_crosses() -> None:
    """A network offered-slot total would be larger than the network has.

    The 40 carriers over Frankfurt to Milan are the same 40 that Amsterdam to
    Frankfurt counts seven of, so adding the per-section offerings up counts one
    wavelength once per section it runs over. The panel closes on extremes over
    the sections instead, and this is what stops a sum growing back.
    """
    texts = _texts(render_odu_map(*dataset(), "fra", "main"))
    assert texts[texts.index("Sections with a headroom figure") + 1] == "5 of 21"
    assert texts[texts.index("Least headroom on a section") + 1] == "80 slots"
    assert texts[texts.index("Tightest carrier anywhere") + 1] == "80 slots"
    assert texts[texts.index("Sections where nothing fits") + 1] == "0"
    assert "12080" not in [text for text in texts if text.startswith("0 of")]


# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------


def test_the_band_edges_are_one_eight_and_eighty_slots() -> None:
    """Every edge is a client size: one slot, an ODU2, an ODU4."""
    assert HEADROOM_BAND_EDGES_SLOTS == (1, 8, 80)


@pytest.mark.parametrize(
    ("headroom_slots", "expected"),
    [
        (-9, "full"),
        (0, "full"),
        (1, "odu0"),
        (7, "odu0"),
        (8, "odu2"),
        (79, "odu2"),
        (80, "odu4"),
        (320, "odu4"),
        (None, "no-odu"),
    ],
)
def test_a_headroom_figure_lands_in_the_band_its_edges_put_it_in(headroom_slots: int | None, expected: str) -> None:
    """Half open, lower inclusive. The negative case is an overfilled carrier,
    which `containers.free_slots` reports rather than clamping, and it belongs in
    the band that says nothing fits."""
    assert HEADROOM_RULE.band_for(headroom_slots).key == expected


def test_an_unknown_headroom_is_its_own_colour_and_not_a_roomy_one() -> None:
    band = HEADROOM_RULE.band_for(None)
    assert band is NO_ODU_BAND
    assert band not in HEADROOM_BANDS
    assert band.colour not in [other.colour for other in HEADROOM_BANDS]
    assert band.colour == UNKNOWN_ROUTE


def test_every_band_has_its_own_colour() -> None:
    colours = [band.colour for band in HEADROOM_BANDS] + [NO_ODU_BAND.colour]
    assert len(colours) == len(set(colours))


def test_the_legend_wording_comes_off_the_edges() -> None:
    captions = [HEADROOM_RULE.caption(band) for band in HEADROOM_BANDS]
    assert captions == [
        "fewer than 1 free: nothing fits",
        "1 to 7 free: only a 1G or 2.5G fits",
        "8 to 79 free: a 10G fits, not a 100G",
        "80 or more free: a 100G still fits",
    ]
    assert HEADROOM_RULE.caption(NO_ODU_BAND) == "no container on any carrier: not known"


# ---------------------------------------------------------------------------
# The trap: unknown is not full, and the branch comes before the call
# ---------------------------------------------------------------------------


def test_nothing_fits_and_nobody_knows_are_two_different_cells() -> None:
    """`largest_fit(0)` is a verdict and `largest_fit(None)` has none.

    The renderer prints two different words for them. Printing one word for both,
    or a blank, is the failure: a wavelength nobody has measured would read as one
    that is definitively full.
    """
    full = OduSection("oms-a", "fra", "mil", carriers_lit=1, committed_slots=320, offered_slots=320, headroom_slots=0)
    assert full.largest_fit is None
    assert full.band.key == "full"

    unknown = OduSection("oms-b", "fra", "mil", carriers_lit=0)
    assert unknown.band is NO_ODU_BAND
    with pytest.raises(ValueError, match="unknown free-slot count"):
        _ = unknown.largest_fit

    svg = render_odu_map(*_network([full, unknown]), "fra")
    assert FIT_NOTHING in _texts(svg)
    assert FIT_UNKNOWN in _texts(svg)


def test_a_section_holding_an_unsized_container_lands_in_no_odu() -> None:
    """A lit carrier is not the same as a measurable one.

    A VC-4 has no defined slot size, so its parent's free figure is unknown, and
    the section is grey with one carrier lit rather than green with room on it.
    """
    assert slots_occupied("VC-4") is None
    assert free_slots(320, [slots_occupied("VC-4")]) is None

    section = OduSection("oms-fra-mil", "fra", "mil", carriers_lit=1)
    assert section.band is NO_ODU_BAND
    assert section.tightest_fill is None

    svg = render_odu_map(*_network([section]), "fra")
    assert FIT_UNKNOWN in _texts(svg)
    assert "n/a" in _texts(svg)


def test_a_lit_carrier_with_no_headroom_figure_is_not_the_same_as_no_carrier() -> None:
    """Both are `no-odu`, and the panel still says which. One row reads a lit
    carrier it cannot measure; the other reads no carrier at all."""
    unsized = OduSection("oms-a", "fra", "mil", carriers_lit=1)
    dark = OduSection("oms-b", "fra", "mil", carriers_lit=0)
    assert unsized.band is dark.band is NO_ODU_BAND
    assert unsized.carriers_lit == 1
    assert dark.carriers_lit == 0


def test_a_section_with_no_lit_carrier_cannot_claim_headroom() -> None:
    """The two figures come from one walk, so a record where they disagree is a
    transform bug. Caught at the door rather than painted as a real band."""
    with pytest.raises(ValueError, match="no lit carrier"):
        OduSection("oms-a", "fra", "mil", carriers_lit=0, headroom_slots=320)


# ---------------------------------------------------------------------------
# The roomiest decides the colour, the tightest decides the bar
# ---------------------------------------------------------------------------


def test_a_roomy_aggregate_does_not_hide_a_carrier_that_is_nearly_full() -> None:
    """The section the whole colour rule exists for.

    Two lit carriers: an ODUC4 with 240 of 320 free and an ODU4 with 4 of 80. The
    aggregate is 156 of 400 committed, 39 per cent, and a map coloured by that
    would tell a planner the route is a third used. It is: on one wavelength. On
    the other, a 10G no longer fits. Colour reports the roomiest, the bar and the
    figure beside it report the tightest, and neither is averaged into the other.
    """
    section = OduSection(
        name="oms-fra-mil",
        site_a="fra",
        site_b="mil",
        carriers_lit=2,
        committed_slots=156,
        offered_slots=400,
        headroom_slots=240,
        tightest_free_slots=4,
    )
    assert section.band.key == "odu4"
    assert section.largest_fit == "ODUC3"

    assert section.committed_slots is not None and section.offered_slots is not None
    assert section.committed_slots / section.offered_slots < 0.40
    fill = section.tightest_fill
    assert fill is not None and fill > 0.90

    texts = _texts(render_odu_map(*_network([section]), "fra"))
    assert "156/400" in texts
    assert "4" in texts
    assert "ODUC3" in texts


def test_the_bar_does_not_report_an_untouched_wavelength_as_three_quarters_full() -> None:
    """The section that ruled out a per-carrier percentage for the fill bar.

    Frankfurt to Milan carries 37 wavelengths offering 320 slots and three
    offering 80, and on `main` every one of them is empty. The section's offered
    total over its lit-carrier count is 302, so a bar drawn against that mean puts
    an untouched ODU4 at three quarters full and prints its figure in red. The bar
    is scaled to one ODU4 instead, so an empty carrier draws an empty bar.
    """
    section = named("oms-fra-mil")
    assert section.tightest_free_slots == 80
    assert section.offered_slots is not None
    assert section.offered_slots // section.carriers_lit == 302  # the mean that would have lied
    assert section.tightest_fill == 0.0


def test_the_bar_is_clamped_at_both_ends() -> None:
    """An overfilled carrier reports a negative free figure, and a bar drawn past
    its own backing is a rendering bug rather than a stronger warning. The
    negative number is in the column beside it."""
    overfull = OduSection(
        "oms-a",
        "fra",
        "mil",
        carriers_lit=1,
        committed_slots=328,
        offered_slots=320,
        headroom_slots=-8,
        tightest_free_slots=-8,
    )
    assert overfull.tightest_fill == 1.0
    assert overfull.band.key == "full"
    assert "-8" in _texts(render_odu_map(*_network([overfull]), "fra"))

    empty = OduSection(
        "oms-a",
        "fra",
        "mil",
        carriers_lit=1,
        committed_slots=0,
        offered_slots=320,
        headroom_slots=320,
        tightest_free_slots=320,
    )
    assert empty.tightest_fill == 0.0


def test_the_panel_table_leads_with_the_section_that_needs_a_person() -> None:
    """Unknown first, because it has no number to sort among the ones that do,
    then least headroom, because that is the route that runs out first."""
    sections = _one_per_band()
    ordered = table_order(sections, HEADROOM_RULE.measure, ODU_DIALECT.name)
    assert ordered[0].band is NO_ODU_BAND
    headroom = [section.headroom_slots for section in ordered]
    assert headroom == sorted(headroom, key=lambda value: (0, 0) if value is None else (1, value))


# ---------------------------------------------------------------------------
# One section per band, on the real frame
# ---------------------------------------------------------------------------


def _network(sections: list[OduSection]) -> tuple[tuple[MapSite, ...], tuple[OduSection, ...]]:
    """The shipped sites, with the given sections laid over the real route set.

    Every synthetic section above ends on `fra` and `mil`, which are real sites,
    so `prepare_canvas` projects them and the site labels are placed against real
    geometry rather than against an invented frame.
    """
    sites, _ = dataset()
    return sites, tuple(sections)


def _one_per_band() -> tuple[OduSection, ...]:
    """The real 21 sections, with the first five moved to one band each.

    Sorted by name and taken in that order, so the assignment is the same on every
    run. Nothing here changes a route's endpoints, so the map is still the shipped
    network.
    """
    _, sections = dataset()
    figures = (
        ("odu4", 320, 320, 0, 320),
        ("odu2", 40, 320, 280, 12),
        ("odu0", 4, 320, 316, 4),
        ("full", 0, 320, 320, 0),
        ("no-odu", None, None, None, None),
    )
    rewritten: list[OduSection] = []
    for section, (_key, headroom, offered, committed, tightest) in zip(sections, figures, strict=False):
        rewritten.append(
            replace(
                section,
                carriers_lit=0 if headroom is None else 1,
                committed_slots=committed,
                offered_slots=offered,
                headroom_slots=headroom,
                tightest_free_slots=tightest,
            )
        )
    return tuple(rewritten) + tuple(sections[len(figures) :])


def test_every_band_appears_once_when_the_figures_put_it_there() -> None:
    """The five-band render, which is what the mixed-fill scenario produces on a
    live branch. Every band's colour has to reach the document, or a band exists
    in the code and never on the map."""
    sections = _one_per_band()
    keys = [section.band.key for section in sections[:5]]
    assert keys == ["odu4", "odu2", "odu0", "full", "no-odu"]

    svg = render_odu_map(*_network(list(sections)), "fra", "mixed-fill")
    for band in list(HEADROOM_BANDS) + [NO_ODU_BAND]:
        assert svg.count(band.colour) >= 2, band.key  # the swatch, and at least one route


def test_the_output_parses_as_xml_and_is_not_an_empty_frame() -> None:
    """XML parses on a blank document. This is what says a map came out.

    Every country in the basemap draws one path, every route draws a line and
    every site draws a disc, so the counts below are floors that a base layer
    silently dropping out would fall through.
    """
    sites, sections = dataset()
    root = ElementTree.fromstring(render_odu_map(sites, sections, "fra", "main"))
    assert root.tag.endswith("svg")
    assert _count_tag(root, "path") >= 40
    assert _count_tag(root, "line") >= len(sections)
    assert _count_tag(root, "circle") >= len(sites)
    assert _count_tag(root, "text") >= len(sites) + len(sections)


# ---------------------------------------------------------------------------
# The title block, which is how a reader tells the two artifacts apart
# ---------------------------------------------------------------------------


def test_the_title_names_this_map_and_not_the_other_one() -> None:
    """FR-021a. Two SVG artifacts hang off every PoP and both are a map of Europe
    with the same coastline, so the heading is the whole distinction."""
    sites, sections = dataset()
    svg = render_odu_map(sites, sections, "fra", "main")
    texts = _texts(svg)
    heading = next(text for text in texts if text.startswith("ODU"))
    assert heading == "ODU capacity and grooming"
    assert "European optical core" not in svg
    assert "OSNR" not in svg
    # Nothing on this map is a statement about the optical layer.
    assert " km" not in svg
    assert "Seen from Frankfurt" in " ".join(texts)
    assert "Generated from branch main." in " ".join(texts)


def test_no_panel_line_runs_off_the_panel() -> None:
    """The column offsets and the legend wording are measured by hand, so this is
    what catches the caption that grew a clause.

    An overflowing legend line does not crash and does not fail any other test in
    this file. It just prints past the panel's right edge and over the map, on
    fourteen artifacts at once.
    """
    left = PANEL_LEFT - 16.0
    right = PANEL_LEFT - 16.0 + PANEL_WIDTH
    for element in ElementTree.fromstring(render_odu_map(*_network(list(_one_per_band())), "fra", "main")).iter():
        if not element.tag.endswith("text") or not (element.text or "").strip():
            continue
        x = float(element.attrib["x"])
        if x < PANEL_LEFT:
            continue  # the title block and the map's own labels
        width = text_width(element.text or "", float(element.attrib["font-size"]), element.get("font-weight", "400"))
        if element.get("text-anchor") == "end":
            assert x - width >= left, element.text
        else:
            assert x + width <= right, element.text


def test_the_focus_site_changes_the_picture() -> None:
    """Copies that differ only by which dot is larger are copies of one picture."""
    sites, sections = dataset()
    assert render_odu_map(sites, sections, "fra") != render_odu_map(sites, sections, "mil")
    assert render_odu_map(sites, sections, "fra") != render_odu_map(sites, sections, None)


def test_a_section_ending_nowhere_is_refused() -> None:
    sites, _ = dataset()
    orphan = OduSection("oms-nowhere", "fra", "zzz")
    with pytest.raises(ValueError, match="not among the sites"):
        layout_odu_map(sites, [orphan])


# ---------------------------------------------------------------------------
# The empty branch, which is a successful render
# ---------------------------------------------------------------------------


def test_a_branch_with_no_containers_renders_grey_under_a_caption_naming_it() -> None:
    """FR-019 and SC-003. This is a render, not an error.

    The base dataset does not reach this state: its wavelengths ship lit and
    empty. This is the picture of a branch whose containers were removed, and the
    risk is not that it crashes. It is that a reader takes an all-grey map for
    missing data, or worse for an empty network with room everywhere.
    """
    sites, sections = dataset()
    stripped = tuple(
        replace(
            section,
            carriers_lit=0,
            committed_slots=None,
            offered_slots=None,
            headroom_slots=None,
            tightest_free_slots=None,
        )
        for section in sections
    )
    assert all(section.band is NO_ODU_BAND for section in stripped)

    layout = layout_odu_map(sites, stripped, "fra", "stripped-branch")
    assert has_no_odu_layer(layout)

    svg = render_odu_map(sites, stripped, "fra", "stripped-branch")
    joined = " ".join(_texts(svg))
    assert "No ODU layer is provisioned on branch stripped-branch" in joined
    assert "every route is unknown, and none of them is available" in joined

    # No route in a band that reads as available, and the plant is still drawn.
    for band in HEADROOM_BANDS:
        assert svg.count(band.colour) == 1, band.key  # the legend swatch only
    lit = ElementTree.fromstring(render_odu_map(sites, sections, "fra", "main"))
    root = ElementTree.fromstring(svg)
    assert _count_tag(root, "line") == _count_tag(lit, "line")
    assert _count_tag(root, "circle") == _count_tag(lit, "circle")
    assert _count_tag(root, "path") == _count_tag(lit, "path")

    # Every figure is a stated unknown rather than a zero.
    texts = _texts(svg)
    assert texts.count(FIT_UNKNOWN) == len(sections)
    assert texts[texts.index("Sections with a headroom figure") + 1] == f"0 of {len(sections)}"
    assert texts[texts.index("Least headroom on a section") + 1] == "not known anywhere"
    assert texts[texts.index("Tightest carrier anywhere") + 1] == "not known anywhere"
    assert texts[texts.index("Sections where nothing fits") + 1] == "0"


def test_one_stripped_section_does_not_caption_the_whole_branch() -> None:
    """The caption is a statement about the branch, so one grey route must not
    trigger it. A map that cried "no ODU layer" over a single unprovisioned
    section would be wrong on every branch that has one."""
    sites, sections = dataset()
    mixed = (replace(sections[0], carriers_lit=0, headroom_slots=None, tightest_free_slots=None),) + sections[1:]
    layout = layout_odu_map(sites, mixed, "fra", "one-stripped")
    assert not has_no_odu_layer(layout)
    assert "No ODU layer is provisioned" not in render_odu_map(sites, mixed, "fra", "one-stripped")


# ---------------------------------------------------------------------------
# The bytes
# ---------------------------------------------------------------------------


def test_two_renders_of_the_same_input_are_byte_identical() -> None:
    """FR-021 and SC-009. An unchanged branch must not produce a changed artifact.

    The records arrive from a GraphQL response whose ordering is not guaranteed,
    so the reversed feed is the case that matters. Break a sort in `odudraw.py`
    and this is the test that says so.
    """
    sites, sections = dataset()
    first = render_odu_map(sites, sections, "fra", "main")
    assert render_odu_map(sites, sections, "fra", "main") == first
    shuffled = render_odu_map(tuple(reversed(sites)), tuple(reversed(sections)), "fra", "main")
    assert shuffled == first


def test_a_five_band_branch_also_renders_the_same_bytes_in_any_order() -> None:
    """The determinism test with something to sort. Twenty-one identical headroom
    figures cannot detect a table ordered by arrival."""
    sites, _ = dataset()
    sections = _one_per_band()
    first = render_odu_map(sites, sections, "fra", "mixed-fill")
    assert render_odu_map(tuple(reversed(sites)), tuple(reversed(sections)), "fra", "mixed-fill") == first


# ---------------------------------------------------------------------------
# The golden cases
# ---------------------------------------------------------------------------
#
# Four inputs, four fixtures, mirroring the network map's set. Each takes no
# arguments and is deterministic, so each maps to exactly one fixture file. This
# map has shipped with no byte gate at all, which the developer guide records as
# a deliberate absence; these four close it.

GOLDEN_FOCUS = "fra"
GOLDEN_BRANCH = "main"
SECOND_FOCUS = "mil"
MIXED_FILL_BRANCH = "mixed-fill"


def golden_render() -> str:
    """The render this map ships today.

    Focused on one site and read from a named branch, so the focus ring and the
    footer's provenance line are both in the bytes.
    """
    sites, sections = dataset()
    return render_odu_map(sites, sections, GOLDEN_FOCUS, GOLDEN_BRANCH)


def render_with_no_focus() -> str:
    """The whole network with nobody highlighted, and no branch in the footer.

    Both defaults of the four-argument signature at once. Nothing freezes this
    path today, so the absent focus ring and the footer line the branch would
    have written are unpinned in the bytes.
    """
    sites, sections = dataset()
    return render_odu_map(sites, sections)


def render_focused_on_milan() -> str:
    """The same network seen from a second site.

    Moving the focus moves the ring and the label placer searches from a
    different starting cost, so this reaches placements Frankfurt does not.
    """
    sites, sections = dataset()
    return render_odu_map(sites, sections, SECOND_FOCUS, GOLDEN_BRANCH)


def render_with_one_section_per_band() -> str:
    """One section in each of the five bands, `NO_ODU_BAND` among them.

    Stated plainly, because it is a negative result: `main` already colours
    sixteen routes with `NO_ODU_BAND`, so that band alone is not what this case
    adds. What no other case reaches is unknown sitting beside known. The other
    four bands are earned only on a branch with services on it, and their
    colours, their swatches and the row order that puts unknown first all reach
    the bytes here and nowhere else.
    """
    sites, _ = dataset()
    return render_odu_map(sites, _one_per_band(), GOLDEN_FOCUS, MIXED_FILL_BRANCH)


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

GOLDEN_CASES: dict[str, Callable[[], str]] = {
    "odu_map_golden": golden_render,
    "odu_map_no_focus": render_with_no_focus,
    "odu_map_focused_on_milan": render_focused_on_milan,
    "odu_map_one_section_per_band": render_with_one_section_per_band,
}
"""Every gated input, keyed by the fixture file it is compared against.

The key is the file name without its suffix, so a reader who sees a failing case
knows which file to look at and a reader who sees a file knows which builder made
it. The shape mirrors `tests/unit/test_mapdraw.py` on purpose: the two maps are
about to become one engine, and a reader comparing their gates should not first
have to compare their test scaffolding.
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
    return f"uv run python -c \"from tests.unit.test_odudraw import regenerate; regenerate('{case}')\""


@pytest.mark.parametrize("case", sorted(GOLDEN_CASES))
def test_the_odu_map_renders_the_bytes_it_shipped(case: str) -> None:
    """Four inputs, four committed renders, compared byte for byte.

    This map shipped with no byte gate at all. Its determinism was asserted by
    rendering twice and in shuffled order, which says the renderer agrees with
    itself and says nothing about whether it still draws what it drew last week.
    These four fixtures close that gap.

    All four were captured from the renderer as it stood before the two map
    modules were merged, so a failure here is the merge moving a pixel and not
    the moved code agreeing with itself.
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

    The shuffled-order tests above ask whether arrival order reaches the bytes.
    This one asks something narrower and aimed at the fixtures: if a second call
    in the same process differs, whichever render reached the file was an
    accident, and every comparison against it is luck.
    """
    builder = GOLDEN_CASES[case]
    assert builder() == builder()
