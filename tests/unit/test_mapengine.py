"""`mapengine.py`, driven by a third dialect that is neither map.

The engine is supposed to know how to draw a map and nothing about which map it
is drawing. That claim is cheap to make and cheap to break: one branch on a
column count, one reach into a section field, and the shared sequence quietly
belongs to one of the two callers again.

So this file builds a third map. Not a mock of one of the two, and not a copy of
either: a conduit lease map, banded on rent in euro cents, with its own record
type, its own unit, its own columns, its own totals and no fill bar at all. If
the engine has learnt what a decibel or a tributary slot is, rendering through
this dialect fails or produces nonsense.

Two arrangements below are deliberately awkward, and both are there to be
discriminating rather than realistic. The columns are declared out of emission
order, so a shared row that walked the tuple instead of reading each column's
declared side would put the rent in the wrong place. And the record carries no
field either shipped map reads, so a shared function reaching past the dialect
raises `AttributeError` instead of drawing something plausible.

This is contract 2 in `specs/020-merge-map-renderers/contracts/dialect.md`, and
the cheapest evidence for SC-005: writing it is the third map in miniature.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Sequence, cast
from xml.etree import ElementTree

import pytest

from infrahub_demo_otn.cartography import Placer
from infrahub_demo_otn.mapchrome import (
    PANEL_LEFT,
    PANEL_WIDTH,
    MapSite,
    SiteProjection,
    panel_caption,
)
from infrahub_demo_otn.mapengine import (
    Band,
    BandCaptions,
    BandRule,
    Cell,
    Column,
    Layout,
    MapDialect,
    Placement,
    Slot,
    Title,
    build_layout,
    render_layout,
    table_order,
)

# ---------------------------------------------------------------------------
# The third map: what it is made of
# ---------------------------------------------------------------------------

LEASE_EDGES_CENTS: tuple[int, int, int] = (50_000, 200_000, 1_000_000)
"""Where one colour becomes the next, in euro cents of rent per month.

A third unit on purpose. The network map bands millidecibels and the ODU map
counts tributary slots, and neither of those numbers means anything here.
"""


@dataclass(frozen=True)
class LeaseSection:
    """One leased conduit, as this map needs it.

    Named fields the other two maps do not have, and none of the ones they do.
    A shared function that read `length_m`, `margin_mdb` or `carriers_lit` off a
    section would raise here rather than draw a wrong number.

    `monthly_cents` is `None` when no lease was ever filed, which is a finding
    and not a rent of zero.
    """

    name: str
    site_a: str
    site_b: str
    owner: str
    term_months: int
    monthly_cents: int | None = None


LEASE_BANDS: tuple[Band, ...] = (
    Band("light", "#1b6b4f", None, LEASE_EDGES_CENTS[0], "cheap to hold"),
    Band("standard", "#3f9fc4", LEASE_EDGES_CENTS[0], LEASE_EDGES_CENTS[1], "the going rate"),
    Band("heavy", "#e0842d", LEASE_EDGES_CENTS[1], LEASE_EDGES_CENTS[2], "worth a second look"),
    Band("critical", "#9b2242", LEASE_EDGES_CENTS[2], None, "renegotiate this one"),
)

NO_LEASE_BAND = Band("no-lease", "#8ba3b1", None, None, "no lease on file")

LEASE_CAPTIONS = BandCaptions(
    unclassified="{caption}",
    below="under {high} a month: {caption}",
    above="{low} a month or more: {caption}",
    between="{low} to {last} a month: {caption}",
    unbounded="any rent at all: {caption}",
    edge=lambda cents: f"EUR {cents // 100:,}",
)
"""This map's own unit, stated once. Cents are stored and euros are read out."""

LEASE_RULE: BandRule[LeaseSection] = BandRule(
    bands=LEASE_BANDS,
    unclassified=NO_LEASE_BAND,
    measure=lambda section: section.monthly_cents,
    captions=LEASE_CAPTIONS,
)

COLUMN_OWNER = 150.0
COLUMN_RENT = 240.0
COLUMN_TERM = 300.0


def _lease_name(section: LeaseSection) -> str:
    return section.name


def _lease_endpoints(section: LeaseSection) -> tuple[str, str]:
    return (section.site_a, section.site_b)


def _rent_text(section: LeaseSection) -> str:
    if section.monthly_cents is None:
        return "not filed"
    return f"EUR {section.monthly_cents // 100:,}"


def _term_text(section: LeaseSection) -> str:
    return f"{section.term_months} mo"


def _band_ink(section: LeaseSection, band: Band, bar: Cell) -> Cell:
    del section, bar
    return Cell(band.colour, "600")


LEASE_COLUMNS: tuple[Column[LeaseSection], ...] = (
    Column("RENT", COLUMN_RENT, _rent_text, _band_ink, 10.0, Slot.AFTER_BAR),
    Column("OWNER", COLUMN_OWNER, lambda section: section.owner, size=10.0, slot=Slot.BEFORE_BAR),
    Column("TERM", COLUMN_TERM, _term_text, size=10.0, slot=Slot.AFTER_BAR),
)
"""Declared out of emission order. Emission is owner, then rent, then term, and
nothing about the tuple order says so. The panel's right edge is the widest
offset of the three, which is not read off the last entry either."""


def _place_leases(
    sections: tuple[LeaseSection, ...],
    projection: SiteProjection,
    placer: Placer,
    focus: str | None,
) -> tuple[Placement[LeaseSection], ...]:
    """Straight onto the line. The placer is taken and handed on unspent."""
    del placer
    placed: list[Placement[LeaseSection]] = []
    for section in sections:
        ax, ay = projection.position[section.site_a]
        bx, by = projection.position[section.site_b]
        placed.append(
            Placement(
                section=section,
                ax=ax,
                ay=ay,
                bx=bx,
                by=by,
                band=LEASE_RULE.classify(section),
                touches_focus=focus is not None and focus in (section.site_a, section.site_b),
            )
        )
    return tuple(placed)


def _lease_notes(y: float) -> tuple[list[str], float]:
    out = [panel_caption(y, "rent is the monthly figure on the current lease", 0.0)]
    y += 16.0
    out.append(panel_caption(y, "grey is a conduit nobody could find a lease for", 0.0))
    return out, y


def _lease_totals(layout: Layout[LeaseSection]) -> tuple[tuple[str, str], ...]:
    sections = [placed.section for placed in layout.sections]
    filed = [section for section in sections if section.monthly_cents is not None]
    rent = sum(section.monthly_cents or 0 for section in filed)
    dear = [section for section in filed if (section.monthly_cents or 0) >= LEASE_EDGES_CENTS[2]]
    return (
        ("Conduits with a lease on file", f"{len(filed)} of {len(sections)}"),
        ("Rent per month", f"EUR {rent // 100:,}"),
        ("Conduits to renegotiate", f"{len(dear)}"),
    )


def _lease_made_of(layout: Layout[LeaseSection]) -> str:
    return f"{len(layout.sites)} sites, {len(layout.sections)} leased conduits. Line colour is the monthly rent."


LEASE_TITLE: Title[LeaseSection] = Title(
    heading="Conduit lease exposure",
    made_of=_lease_made_of,
    how_to_read="Rent is per month, per conduit, on the lease currently in force.",
)

LEASE_DIALECT: MapDialect[LeaseSection] = MapDialect(
    name=_lease_name,
    endpoints=_lease_endpoints,
    title=LEASE_TITLE,
    place_sections=_place_leases,
    band_rule=LEASE_RULE,
    columns=LEASE_COLUMNS,
    bar=None,
    legend_heading="Monthly rent",
    legend_subheading="per leased conduit",
    legend_notes=_lease_notes,
    table_note=lambda layout: "dearest first" if layout.sections else "nothing leased",
    totals=_lease_totals,
)
"""No fill bar, no dash, no overlay and no route labels.

Every one of those is a default, so the whole of what this map had to state is
the fourteen fields above it. The absent bar is also a path neither shipped map
exercises: both draw one, so the engine's no-bar branch has no golden over it.
"""


def render_lease_map(
    sites: Sequence[MapSite],
    sections: Sequence[LeaseSection],
    focus: str | None = None,
    branch: str | None = None,
) -> str:
    """The third map's entry point, which is two calls and no third file."""
    return render_layout(LEASE_DIALECT, build_layout(LEASE_DIALECT, sites, sections, focus, branch))


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _sites() -> tuple[MapSite, ...]:
    return (
        MapSite("Amsterdam", "ams", 4_895_168, 52_370_216, 3),
        MapSite("Frankfurt", "fra", 8_682_127, 50_110_922, 4),
        MapSite("Paris", "par", 2_352_222, 48_856_614, 3),
        MapSite("Milan", "mil", 9_189_982, 45_464_204, 2, eurohpc_name="CINECA"),
    )


def _sections() -> tuple[LeaseSection, ...]:
    return (
        LeaseSection("ams-fra", "ams", "fra", "Relined BV", 60, 42_000),
        LeaseSection("fra-mil", "fra", "mil", "Alpen Fiber", 36, 180_000),
        LeaseSection("ams-par", "ams", "par", "Canal Ducts", 24, 1_450_000),
        LeaseSection("par-mil", "par", "mil", "Unknown", 12, None),
    )


@pytest.fixture(name="svg")
def _svg() -> str:
    return render_lease_map(_sites(), _sections(), focus="fra", branch="main")


# ---------------------------------------------------------------------------
# The engine draws a map it has never heard of
# ---------------------------------------------------------------------------


def test_a_third_dialect_renders_a_whole_document(svg: str) -> None:
    """A map that is neither of the two comes out as one parseable SVG."""
    assert svg.startswith("<svg ")
    assert svg.endswith("</svg>")
    assert ElementTree.fromstring(svg).tag.endswith("svg")


def test_the_engine_states_no_wording_of_its_own(svg: str) -> None:
    """Nothing either shipped map says reaches a document neither one drew.

    Every word above is this dialect's. If the engine carried a heading, a unit
    or a legend line of its own, it would show up here as the third map speaking
    in the first map's voice.
    """
    for borrowed in ("OSNR", "margin", "ODU", "tributary", "slots", "dB", "km", "channel"):
        assert borrowed not in svg


def test_the_legend_is_read_out_in_this_map_s_own_unit(svg: str) -> None:
    """Euros, from cents, through the dialect's own edge formatter."""
    assert "under EUR 500 a month: cheap to hold" in svg
    assert "EUR 500 to EUR 1,999 a month: the going rate" in svg
    assert "EUR 10,000 a month or more: renegotiate this one" in svg
    assert "no lease on file" in svg


def test_every_band_gets_a_swatch_in_legend_order(svg: str) -> None:
    """Four real bands then the unclassifiable one, down the panel in that order.

    The unclassifiable band sits outside the band table, so the swatch loop has
    to add it back at the end. A legend that drops it is a map whose greyest
    routes are unexplained.
    """
    panel = svg[svg.index(LEASE_DIALECT.legend_heading) :]
    swatches = [panel.index(band.colour) for band in list(LEASE_BANDS) + [NO_LEASE_BAND]]
    assert swatches == sorted(swatches)


def test_each_route_is_drawn_in_the_band_its_own_rent_put_it_in(svg: str) -> None:
    """The colour on the line is the colour the panel is a verdict about.

    Read off the document rather than off the layout, because reclassifying
    while drawing is how a route ends up one colour on the map and another in
    the table, and only the document shows both. Anything left of the panel is
    on the map; the swatches are lines too.
    """
    colours = {band.colour for band in list(LEASE_BANDS) + [NO_LEASE_BAND]}
    drawn = sorted(
        element.get("stroke", "")
        for element in ElementTree.fromstring(svg).iter("{http://www.w3.org/2000/svg}line")
        if element.get("stroke") in colours and float(element.get("x1", "0")) < PANEL_LEFT
    )
    assert drawn == sorted(LEASE_RULE.classify(section).colour for section in _sections())


def test_the_unmeasured_conduit_lands_in_the_unclassifiable_band() -> None:
    """No lease on file is its own band, not the cheapest one."""
    unfiled = LeaseSection("par-mil", "par", "mil", "Unknown", 12, None)
    assert LEASE_RULE.classify(unfiled) is NO_LEASE_BAND
    assert LEASE_RULE.classify(unfiled) is not LEASE_BANDS[0]


def test_cells_are_emitted_on_the_side_their_column_declared(svg: str) -> None:
    """Owner before rent, though the tuple declares rent first.

    The discriminator for a shared row that walked the column list instead of
    reading each column's declared side. With no fill bar between them, the two
    loops are the only thing holding the order.
    """
    row = svg[svg.index("Alpen Fiber") :]
    assert row.index("Alpen Fiber") < row.index("EUR 1,800")
    assert row.index("EUR 1,800") < row.index("36 mo")


def test_a_map_with_no_fill_bar_draws_no_bar(svg: str) -> None:
    """The no-bar path, which neither shipped map's goldens reach.

    Both maps draw a bar, so the branch that omits one has no byte gate over it.
    The rounded 7-high rectangle is the bar's own shape and nothing else uses it.
    """
    assert 'height="7" rx="3.5"' not in svg


def test_the_table_puts_the_unmeasured_first_then_ascending() -> None:
    """One order, from the engine, keyed on whatever this map bands on."""
    ordered = table_order(_sections(), LEASE_RULE.measure, _lease_name)
    assert [section.name for section in ordered] == ["par-mil", "ams-fra", "fra-mil", "ams-par"]


def test_the_panel_stays_inside_its_own_width() -> None:
    """The right edge is the widest column's offset, and it has to fit."""
    assert max(column.x for column in LEASE_COLUMNS) < PANEL_WIDTH


def test_the_panel_rules_reach_the_widest_column_not_the_last_declared() -> None:
    """Declaration order is not x order, so the right edge is taken from x.

    `slot` already decouples the order columns are declared in from the order
    they are emitted in, and nothing then forces declarations to run left to
    right. This map declares the same three columns with the narrowest last, and
    a right edge read off the last entry would stop every rule 150 short.
    """
    rent, owner, term = LEASE_COLUMNS
    shuffled = replace(LEASE_DIALECT, columns=(rent, term, owner))
    svg = render_layout(shuffled, build_layout(shuffled, _sites(), _sections(), "fra", "main"))
    assert f'x2="{PANEL_LEFT + COLUMN_TERM:.1f}"' in svg
    assert f'x2="{PANEL_LEFT + COLUMN_OWNER:.1f}"' not in svg


def test_a_column_whose_slot_is_not_a_slot_is_refused() -> None:
    """A misspelt side is an error, not a column that quietly emits nothing.

    `draw_table_row` emits a cell only where the slot matches one of the two, so
    anything else prints a heading with nothing under it on every row. mypy
    refuses that at the dialect; this is the caller mypy never saw.
    """
    with pytest.raises(TypeError, match="not a Slot"):
        Column("OWNER", COLUMN_OWNER, lambda section: section.owner, slot=cast(Slot, "before_bar"))


# ---------------------------------------------------------------------------
# The properties the two maps assert, asserted on a map that is neither
# ---------------------------------------------------------------------------


def test_the_same_records_render_the_same_bytes(svg: str) -> None:
    assert render_lease_map(_sites(), _sections(), focus="fra", branch="main") == svg


def test_a_shuffled_response_renders_the_same_bytes(svg: str) -> None:
    """Ordering is not guaranteed on the way in and must not reach the output."""
    shuffled = render_lease_map(
        tuple(reversed(_sites())),
        tuple(reversed(_sections())),
        focus="fra",
        branch="main",
    )
    assert shuffled == svg


def test_focus_moves_no_node_and_no_label() -> None:
    """Focus is a highlight, on any map. It changes no geometry."""
    focused = build_layout(LEASE_DIALECT, _sites(), _sections(), focus="fra")
    unfocused = build_layout(LEASE_DIALECT, _sites(), _sections(), focus=None)
    assert [(placed.x, placed.y, placed.label) for placed in focused.sites] == [
        (placed.x, placed.y, placed.label) for placed in unfocused.sites
    ]
    assert focused.label_rects == unfocused.label_rects


def test_a_render_with_nobody_highlighted_still_draws() -> None:
    """`focus=None` is a whole map, not an error."""
    nobody = render_lease_map(_sites(), _sections(), focus=None, branch="main")
    assert "Seen from" not in nobody
    assert "Conduit lease exposure" in nobody


def test_the_branch_is_provenance_and_not_geometry() -> None:
    """Named in the footer, and nothing else moves."""
    named = render_lease_map(_sites(), _sections(), focus="fra", branch="capacity-review")
    unnamed = render_lease_map(_sites(), _sections(), focus="fra", branch=None)
    assert "Generated from branch capacity-review." in named
    assert "Generated from the graph." in unnamed
    assert len(named.splitlines()) == len(unnamed.splitlines())


# ---------------------------------------------------------------------------
# FR-009: there is nothing on the dialect to branch on
# ---------------------------------------------------------------------------


def test_the_dialect_carries_no_field_that_names_a_map() -> None:
    """No identity field, so the shared sequence has nothing to switch on.

    A field list rather than a grep, because a field added later is exactly the
    thing this requirement is about and a grep only runs when someone runs it.
    """
    declared = {field.name for field in fields(MapDialect)}
    assert declared == {
        "name",
        "endpoints",
        "title",
        "place_sections",
        "band_rule",
        "columns",
        "bar",
        "legend_heading",
        "legend_subheading",
        "legend_notes",
        "table_note",
        "totals",
        "route_dash",
        "route_marks",
        "draws_distance_labels",
    }
    for field in declared:
        assert "odu" not in field
        assert "network" not in field
        assert "kind" not in field
