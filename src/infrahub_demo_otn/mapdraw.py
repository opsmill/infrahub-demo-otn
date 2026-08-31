"""The network topology map: layout, then SVG.

Pure presentation. This module does no optical arithmetic and imports neither
`budget.py` nor `infrahub_sdk`. Every optical number arrives already computed,
as a field on `MapSection`, and the only thing done to it here is a conversion
for display through `units.py`.

Three decisions are worth stating up front, because each one is a rule the
drawing code below is not free to relitigate.

**Colour is the worse of the two directional margins.** A section carries an
amplifier chain per direction and the two chains do not have to agree, so it is
budgeted once from each ROADM and arrives with two figures. A service over the
section is limited by the worse one, so that is the one the line is painted
with. When the two fall in different bands the route is also marked asymmetric,
because a reader otherwise cannot tell a route that is uniformly at its colour
from one that is only bad one way. Both figures are in the panel table.

**A margin that could not be computed is its own colour.** Not omitted, and not
folded into a passing band. An absent number is a different statement from a
good one and the map has to make that difference visible.

**A distance label rides its own route.** Rotated to follow it, and collisions
resolve by sliding along the line, never away from it. A label floating beside a
line makes the reader guess which of two crossing routes it belongs to. The map
stays sparse on purpose: exact figures live in the panel table.

Determinism is a requirement, not a nicety. An unchanged network must produce a
byte-identical artifact, or every rebuild looks like a change. Every collection
is sorted before it is walked and every float is formatted at fixed precision.

**What is here, and what is shared.** `mapchrome.py` holds the primitives: the
canvas, the projection, the coastline, the node discs, the site labels, the
title block and the panel's frame, headings, rules and row rhythm. `mapengine.py`
holds the order those calls go in, which is the same order on both maps. What
stays here is this map's dialect, everything that only means something once the
reader knows the colour is an OSNR margin: the bands, the legend and title
wording, the six column offsets, the asymmetry overlay, the distance labels and
the span-boundary dots. Then `render_map`, the one entry point the transform
calls. The golden fixtures under `tests/unit/fixtures/` were captured before each
of those splits and hold them to changing no pixel of this map.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence, TypeAlias

from infrahub_demo_otn.cartography import Placer, Rect
from infrahub_demo_otn.mapchrome import (
    BODY_TEXT,
    BUSY,
    HPC_RING,
    INK_SOFT,
    PANEL_EDGE,
    PANEL_LEFT,
    PAPER,
    UNKNOWN_ROUTE,
    MapSite,
    SiteProjection,
    escape,
    panel_caption,
    panel_swatch,
    text_width,
)
from infrahub_demo_otn.mapengine import (
    Band,
    BandCaptions,
    BandRule,
    BarRule,
    Cell,
    Column,
    Layout,
    MapDialect,
    Placement,
    RouteLabel,
    Slot,
    Title,
    bar_ink,
    build_layout,
    render_layout,
)
from infrahub_demo_otn.units import CBAND_EXTENT_MHZ, db_to_mdb, m_to_km, mdb_to_db, mhz_to_ghz

# ---------------------------------------------------------------------------
# The reference mode and the bands
# ---------------------------------------------------------------------------

REFERENCE_MODE_NAME = "DP-16QAM 64GBd 400G"
"""The mode every route colour is a verdict about.

An OSNR margin means nothing without the mode it is a margin against. This one
separates the shipped dataset properly: raw section loss tracks route length
almost exactly, because the plant is uniform, so a map coloured by loss draws
how long each route is and the distance labels already say that.
"""

MARGIN_BAND_EDGES_DB: tuple[float, float, float] = (0.0, 2.0, 5.0)
"""Where one colour becomes the next, in decibels.

Zero is the only edge the physics chooses; two and five are the operational
reading of "closes but do not touch it" and "closes comfortably".
"""

MARGIN_BAND_EDGES_MDB: tuple[int, ...] = tuple(db_to_mdb(edge) for edge in MARGIN_BAND_EDGES_DB)
"""The same edges in the stored unit, which is the unit the comparison uses.

The comparison has to be exact integer millidecibels. One section in the shipped
dataset sits at +4.999 dB, a thousandth of a decibel inside its band, and a
float compare against 5.0 is exactly the kind of thing that moves it.
"""


MARGIN_BANDS: tuple[Band, ...] = (
    Band("negative", "#cf4b46", None, MARGIN_BAND_EDGES_MDB[0]),
    Band("thin", "#dfa032", MARGIN_BAND_EDGES_MDB[0], MARGIN_BAND_EDGES_MDB[1]),
    Band("fair", "#86b13a", MARGIN_BAND_EDGES_MDB[1], MARGIN_BAND_EDGES_MDB[2]),
    Band("ample", "#2f9e63", MARGIN_BAND_EDGES_MDB[2], None),
)
"""The four bands, worst first. Order is the legend order."""

UNKNOWN_BAND = Band("unknown", UNKNOWN_ROUTE, None, None)
"""The colour for a section whose margin is not available.

Deliberately outside `MARGIN_BANDS`, so `MARGIN_RULE` can never fall into it by
accident and `contains` is never asked about it.
"""


def _margin_edge(margin_mdb: int) -> str:
    """A band edge in whole decibels, which is the unit the legend states."""
    return f"{mdb_to_db(margin_mdb):.0f}"


MARGIN_CAPTIONS = BandCaptions(
    unclassified="not computed",
    below="below {high} dB, does not close",
    above="{low} dB and above",
    between="{low} to {high} dB",
    unbounded="any margin",
    edge=_margin_edge,
)
"""The legend, said entirely in edges.

A decibel of margin needs no gloss: a reader who knows the colour is an OSNR
margin already knows what more of it is worth. The one band with no edges is the
one that has to say something instead.
"""

MARGIN_RULE: BandRule[MapSection] = BandRule(
    bands=MARGIN_BANDS,
    unclassified=UNKNOWN_BAND,
    measure=lambda section: section.worse_margin_mdb,
    captions=MARGIN_CAPTIONS,
)
"""What this map bands on: the worse of the two directional margins.

A service over the section is limited by the worse direction, so that figure is
what the route colour is a verdict about. The better direction is banded too,
for the asymmetry marker and for its own table column, but through `band_for`
on a figure rather than through `classify` on the section.
"""


# ---------------------------------------------------------------------------
# What this map puts on the shared canvas
# ---------------------------------------------------------------------------

CHIP_MIN_OCCUPIED_MHZ = CBAND_EXTENT_MHZ // 4
"""Spectrum in use before a section is worth flagging on the map itself.

A quarter of the modelled C-band, 1,200,000 MHz. Below this the figure is in the
panel table and nowhere else: a chip on every route would be twenty-one more
boxes competing with the distance labels.

A quarter of the band rather than a quarter of the ninety-six anchors, which is
what this threshold used to be. The two are the same fraction and they are not
the same question. Twenty-four anchors is twenty-four 32 GBd carriers, which
occupy 1,065,600 MHz, or twenty-four 128 GBd ones, which want 3,600,000 MHz and
do not fit at all.
"""

SLIDE_OFFSETS: tuple[float, ...] = (
    0.50,
    0.44,
    0.56,
    0.41,
    0.59,
    0.38,
    0.62,
    0.35,
    0.65,
    0.32,
    0.68,
    0.29,
    0.71,
    0.26,
    0.74,
    0.23,
    0.77,
)
"""Where a distance label may sit along its own route, nearest the middle first.

Sliding is the only move a distance label has. It never steps off its line, so
the sweep has to be fine enough to find the gap between two crossing routes,
and it works outward in pairs so a label that fits in the middle stays there.
"""

COLUMN_KM = 110.0
COLUMN_LOSS = 158.0
COLUMN_MARGIN_AB = 206.0
COLUMN_MARGIN_BA = 250.0
COLUMN_BAR = 258.0
COLUMN_SPECTRUM = 316.0
BAR_WIDTH = 22.0
"""The six column offsets, measured from the panel's left edge, and the width of
the occupancy bar. They stay in this file on purpose: kilometres, loss and two
directional margins are this map's vocabulary, and a shared set of offsets would
make the panel code know about OSNR.

The last column moved out from 308 and the bar narrowed by eight when the
occupancy figure stopped being a count. `40` is two glyphs and `4,134` is five,
which `text_width` puts at 29.0px against the 20px the old pair left between the
bar's right end and the column. At 316 and 22 the figure has 29px and a 7px gap,
the same gap the bar has from the margin column to its left, and the panel still
ends inside its 324px box, which `tests/unit/test_mapchrome.py` measures."""

NO_SPECTRUM = "#c2ced5"
"""The occupancy figure on a section with nothing lit.

Paler than every band and paler than the muted figure colour, so an unlit row
reads as empty rather than as a small number. The ODU map's equivalent cell is a
different grey, and the two are not the same value by accident."""


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MapSection:
    """One drawable multiplex section, with its optics already budgeted.

    `margin_a_to_b_mdb` and `margin_b_to_a_mdb` are the two directional OSNR
    margins against `REFERENCE_MODE_NAME`, each of which may be `None` when the
    evaluation could not be run. Both are carried because a route can be good
    one way and thin the other, and the reader needs to see which.

    `span_boundaries` are fractions strictly between 0 and 1, in order, one per
    interior span boundary. They are fractions of route length rather than span
    counts so a route made of unequal spans puts its dots where the huts are.
    """

    name: str
    site_a: str
    site_b: str
    length_m: int
    loss_mdb: int
    margin_a_to_b_mdb: int | None
    margin_b_to_a_mdb: int | None
    span_boundaries: tuple[float, ...] = ()
    raman_pumped: bool = False
    occupied_mhz: int = 0
    band_extent_mhz: int = CBAND_EXTENT_MHZ
    """How much spectrum the carriers crossing this section hold, and how much
    there is to hold.

    A width and not a count of anchors. Ninety-six free anchor numbers are not
    ninety-six provisionable wavelengths, because a carrier occupies its width on
    the 50 GHz grid and a wide mode reaches into its neighbours' positions. The
    union of the occupied intervals is what `units.free_blocks` subtracts from
    the band, so two carriers colliding on the same spectrum are counted once
    here and the collision check is what reports them."""

    @property
    def span_count(self) -> int:
        return len(self.span_boundaries) + 1

    @property
    def worse_margin_mdb(self) -> int | None:
        """The margin the section is limited by, or `None` when it is not known.

        A missing figure in one direction makes the worse of the two unknown,
        not equal to the direction that did evaluate. Half an answer painted as
        a whole one is the failure this returns `None` to avoid.
        """
        if self.margin_a_to_b_mdb is None or self.margin_b_to_a_mdb is None:
            return None
        return min(self.margin_a_to_b_mdb, self.margin_b_to_a_mdb)

    @property
    def better_margin_mdb(self) -> int | None:
        if self.margin_a_to_b_mdb is None or self.margin_b_to_a_mdb is None:
            return None
        return max(self.margin_a_to_b_mdb, self.margin_b_to_a_mdb)

    @property
    def is_asymmetric(self) -> bool:
        """Whether the two directions land in different colour bands.

        Band membership, not raw inequality. Two directions differing by a
        thousandth of a decibel are the same route to a reader; two that differ
        by nine decibels and straddle an edge are not.
        """
        worse, better = self.worse_margin_mdb, self.better_margin_mdb
        if worse is None or better is None:
            return False
        return MARGIN_RULE.band_for(worse) is not MARGIN_RULE.band_for(better)

    @property
    def has_chip(self) -> bool:
        return self.occupied_mhz >= CHIP_MIN_OCCUPIED_MHZ


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


SectionPlacement: TypeAlias = Placement[MapSection]
"""A route on the canvas, with the box its distance label was given.

`overlay_band` is the better direction's band, which is what the asymmetry
marker is drawn in, and `label` is always set, because every route on this map
carries its distance.
"""

MapLayout: TypeAlias = Layout[MapSection]
"""Everything placed, before a single tag is written."""


def route_segments(layout: MapLayout) -> tuple[tuple[float, float, float, float, str], ...]:
    """Every route as a segment, keyed by section name.

    A section name is not something the engine reads, so this is the map's own
    to state. It is what lets a test replay the routes into a placer and ask
    whether any label lies across one.
    """
    return tuple((p.ax, p.ay, p.bx, p.by, p.section.name) for p in layout.sections)


def _rotated_extent(width: float, height: float, angle_deg: float) -> tuple[float, float]:
    """The axis-aligned box a rotated rectangle needs."""
    radians = math.radians(angle_deg)
    cos, sin = abs(math.cos(radians)), abs(math.sin(radians))
    return width * cos + height * sin, width * sin + height * cos


def _distance_text(section: MapSection) -> str:
    return f"{m_to_km(section.length_m):,.0f} km"


def _chip_text(section: MapSection) -> str:
    """The occupancy chip on a busy route, in gigahertz.

    The band it is a fraction of is on the panel's totals line rather than in
    the chip. `4,134/4,800 GHz` measures 88px and the box it rides in sits on a
    rotated route, so the denominator is the half that goes.
    """
    return f"{mhz_to_ghz(section.occupied_mhz):,.0f} GHz"


def _margin_text(margin_mdb: int | None) -> str:
    """A margin for the panel table, signed, or a dash when there is none."""
    if margin_mdb is None:
        return "n/a"
    return f"{mdb_to_db(margin_mdb):+.2f}"


def _margin_column(heading: str, x: float, read: Callable[[MapSection], int | None]) -> Column[MapSection]:
    """A directional margin column, coloured by its own value's band.

    The row's colour is the worse direction. A cell that took it would paint the
    good direction in the bad direction's colour, which is the one thing the two
    columns exist to keep apart.
    """

    def ink(section: MapSection, band: Band, bar: Cell) -> Cell:
        del band, bar
        return Cell(MARGIN_RULE.band_for(read(section)).colour)

    return Column(heading, x, lambda section: _margin_text(read(section)), ink)


def _spectrum_fill(section: MapSection) -> float | None:
    """How much of the band is lit, or `None` for a section with nothing on it."""
    if not section.occupied_mhz:
        return None
    return section.occupied_mhz / section.band_extent_mhz


NETWORK_COLUMNS: tuple[Column[MapSection], ...] = (
    Column("KM", COLUMN_KM, lambda section: f"{m_to_km(section.length_m):,.0f}"),
    # Directional, and the heading has to say so. A Raman pump is credited to the
    # orientation being walked while its combiner loss is charged both ways, so on
    # a pumped span the two directions do not carry the same loss. Twenty of the
    # twenty-one sections are unpumped and identical either way; the header is for
    # the one that is not.
    Column("LOSS AB", COLUMN_LOSS, lambda section: f"{mdb_to_db(section.loss_mdb):.1f}"),
    _margin_column("A TO B", COLUMN_MARGIN_AB, lambda section: section.margin_a_to_b_mdb),
    _margin_column("B TO A", COLUMN_MARGIN_BA, lambda section: section.margin_b_to_a_mdb),
    Column(
        "GHZ",
        COLUMN_SPECTRUM,
        lambda section: f"{mhz_to_ghz(section.occupied_mhz):,.0f}",
        bar_ink,
        10.0,
        Slot.AFTER_BAR,
    ),
)
"""The table, in emission order: four figures, then the occupancy bar, then the
occupied spectrum that takes the bar's own ink.

Gigahertz, not anchors and not megahertz. The heading has to name a unit now
that the figure is a width: `4,134` under `CH` would read as four thousand
channels on a ninety-six channel grid."""

NETWORK_BAR: BarRule[MapSection] = BarRule(COLUMN_BAR, BAR_WIDTH, _spectrum_fill, NO_SPECTRUM)
"""How much of the band is lit on this section, drawn beside the figure."""


def _section_name(section: MapSection) -> str:
    """How the engine reads a name off this map's record."""
    return section.name


def _section_endpoints(section: MapSection) -> tuple[str, str]:
    """The two sites this map's record runs between."""
    return (section.site_a, section.site_b)


def _place_distance_labels(
    sections: tuple[MapSection, ...],
    projection: SiteProjection,
    placer: Placer,
    focus: str | None,
) -> tuple[SectionPlacement, ...]:
    """Slide each distance label along its own route until it fits.

    Longest route first: it has the most room to slide, so letting it go last
    would waste the only freedom it has on a route that had none.
    """
    placed: list[SectionPlacement] = []
    for section in sorted(sections, key=lambda item: (-item.length_m, item.name)):
        ax, ay = projection.position[section.site_a]
        bx, by = projection.position[section.site_b]
        text = _distance_text(section)
        chip = _chip_text(section) if section.has_chip else None
        chip_width = (text_width(chip, 9.5, "700") + 12.0) if chip else 0.0
        badge_width = 16.0 if section.raman_pumped else 0.0
        box_width = text_width(text, 10.5) + 14.0 + (chip_width + 4.0 if chip else 0.0) + badge_width
        box_height = 17.0

        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy) or 1.0
        angle = math.degrees(math.atan2(dy, dx))
        if angle > 90.0:
            angle -= 180.0
        elif angle < -90.0:
            angle += 180.0
        extent_w, extent_h = _rotated_extent(box_width, box_height, angle)

        # Never far enough along to sit on an end node, however crowded it gets.
        t_min = min(0.45, (projection.radius[section.site_a] + box_width / 2 + 10.0) / length)
        t_max = max(0.55, 1.0 - (projection.radius[section.site_b] + box_width / 2 + 10.0) / length)
        candidates: list[tuple[Rect, tuple[float, float]]] = []
        for offset in SLIDE_OFFSETS:
            t = min(max(offset, t_min), t_max)
            centre_x, centre_y = ax + dx * t, ay + dy * t
            rect = (centre_x - extent_w / 2, centre_y - extent_h / 2, extent_w, extent_h)
            candidates.append((rect, (centre_x, centre_y)))
        rect, centre = placer.place(candidates, ignore=section.name)

        band = MARGIN_RULE.classify(section)
        other = MARGIN_RULE.band_for(section.better_margin_mdb)
        placed.append(
            SectionPlacement(
                section=section,
                ax=ax,
                ay=ay,
                bx=bx,
                by=by,
                band=band,
                touches_focus=focus is not None and focus in (section.site_a, section.site_b),
                overlay_band=other,
                label=RouteLabel(
                    centre=centre,
                    angle_deg=angle,
                    rect=rect,
                    width=box_width,
                    height=box_height,
                    trailing_width=chip_width,
                ),
            )
        )
    return tuple(sorted(placed, key=lambda item: item.section.name))


def _legend_notes(y: float) -> tuple[list[str], float]:
    """What the band swatches cannot say: the asymmetry marker and the Raman ring.

    Returns the lines and the y the last of them landed on, because the node
    legend under this block keeps counting from there.
    """
    left = PANEL_LEFT
    out = [panel_swatch(y, INK_SOFT)]
    # The dashed overlay in paper white over the solid stroke, which is the
    # asymmetry marker itself rather than a colour from the palette.
    out.append(
        f'<line x1="{left:.1f}" y1="{y - 4:.1f}" x2="{left + 28:.1f}" y2="{y - 4:.1f}" stroke="{PAPER}" '
        f'stroke-width="3.6" stroke-dasharray="7 9"/>'
    )
    out.append(panel_caption(y, "dashed: directions in different bands"))
    y += 20.0
    out.append(
        f'<circle cx="{left + 8:.1f}" cy="{y - 4:.1f}" r="6.5" fill="none" stroke="{HPC_RING}" stroke-width="1.4"/>'
    )
    out.append(
        f'<text x="{left + 8:.1f}" y="{y - 0.6:.1f}" text-anchor="middle" fill="{HPC_RING}" font-size="9" '
        f'font-weight="700">R</text>'
    )
    # Two lines, because one does not fit. The panel is 340px wide and a caption
    # starts 38px in, so 286px is all there is; this sentence measured 499.7px
    # and printed over the map itself for the length of the legend. Every
    # caption is now measured by
    # tests/unit/test_mapchrome.py::test_no_panel_text_overflows_the_panel.
    out.append(panel_caption(y, "Raman pump on one of its spans, so it is"))
    y += 14.0
    out.append(panel_caption(y, "asymmetric: its B to A loss differs"))
    return out, y


def _made_of(layout: MapLayout) -> str:
    """One line of what this render is made of, and what its colour is about."""
    spans = sum(placed.section.span_count for placed in layout.sections)
    return (
        f"{len(layout.sites)} PoPs, {len(layout.sections)} multiplex sections, {spans} fiber spans. "
        f"Line colour is OSNR margin at {escape(REFERENCE_MODE_NAME)}."
    )


MAP_TITLE: Title[MapSection] = Title(
    heading="European optical core",
    made_of=_made_of,
    how_to_read=(
        "The number inside a site is its optical degree; dots along a route are "
        "span boundaries. Coastlines: Natural Earth."
    ),
)
"""The wording above the map, which is what tells a reader which of the two
artifacts hanging off this site they have open."""


def _draw_distance_labels(layout: MapLayout) -> list[str]:
    """The distance on its own route, rotated to follow it.

    The Raman badge and the occupancy chip ride in the same box, so a marker is
    never floating free of the route it is a statement about.
    """
    out: list[str] = ["<g>"]
    for placed in layout.sections:
        label = placed.label
        if label is None:
            continue
        section = placed.section
        centre_x, centre_y = label.centre
        half_width = label.width / 2
        chip = _chip_text(section) if section.has_chip else None
        badge_width = 16.0 if section.raman_pumped else 0.0
        out.append(f'<g transform="translate({centre_x:.1f},{centre_y:.1f}) rotate({label.angle_deg:.1f})">')
        out.append(
            f'<rect x="{-half_width:.1f}" y="{-label.height / 2:.1f}" width="{label.width:.1f}" '
            f'height="{label.height:.1f}" rx="8" fill="{PAPER}" opacity="0.97" '
            f'stroke="{PANEL_EDGE}" stroke-width="0.9"/>'
        )
        trailing = label.trailing_width + (4.0 if chip else 0.0) + badge_width
        text_x = -half_width + (label.width - trailing) / 2
        out.append(
            f'<text x="{text_x:.1f}" y="3.6" text-anchor="middle" fill="{BODY_TEXT}" '
            f'font-size="10.5">{escape(_distance_text(section))}</text>'
        )
        cursor = half_width - 3.0
        if chip:
            out.append(
                f'<rect x="{cursor - label.trailing_width:.1f}" y="{-label.height / 2 + 3:.1f}" '
                f'width="{label.trailing_width:.1f}" height="{label.height - 6:.1f}" rx="5.5" fill="{BUSY}"/>'
            )
            out.append(
                f'<text x="{cursor - label.trailing_width / 2:.1f}" y="3.4" text-anchor="middle" fill="{PAPER}" '
                f'font-size="9.5" font-weight="700">{escape(chip)}</text>'
            )
            cursor -= label.trailing_width + 3.0
        if section.raman_pumped:
            out.append(
                f'<circle cx="{cursor - 6.5:.1f}" cy="0" r="6.5" fill="none" stroke="{HPC_RING}" stroke-width="1.4"/>'
            )
            out.append(
                f'<text x="{cursor - 6.5:.1f}" y="3.4" text-anchor="middle" fill="{HPC_RING}" '
                f'font-size="9" font-weight="700">R</text>'
            )
        out.append("</g>")
    out.append("</g>")
    return out


def _route_marks(placed: SectionPlacement, geometry: str, width: float) -> list[str]:
    """What this map rides on a drawn route: the asymmetry overlay and the dots.

    An asymmetric route carries a dashed overlay in the better direction's
    colour, which is the marker itself: it says the same line is one colour one
    way and another colour the other. A dot marks every interior span boundary.
    """
    section = placed.section
    colour = placed.band.colour
    out: list[str] = []
    if section.is_asymmetric and placed.overlay_band is not None:
        out.append(
            f'<line {geometry} stroke="{placed.overlay_band.colour}" stroke-width="{width:.1f}" '
            f'stroke-linecap="butt" stroke-dasharray="7 9"/>'
        )
    for fraction in section.span_boundaries:
        x = placed.ax + (placed.bx - placed.ax) * fraction
        y = placed.ay + (placed.by - placed.ay) * fraction
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{PAPER}" stroke="{colour}" stroke-width="1.4"/>')
    return out


def _network_totals(layout: MapLayout) -> tuple[tuple[str, str], ...]:
    """The four figures the panel closes on. Counts and sums, no optics.

    The spectrum total is summed over sections, so a wavelength crossing three
    of them is counted three times. That is deliberate and it is what the row it
    replaced did with anchors: the figure is how much of the plant is lit, not
    how many carriers exist, and a wavelength does hold its width on every
    section it crosses.
    """
    sections = [placed.section for placed in layout.sections]
    distance_km = sum(m_to_km(section.length_m) for section in sections)
    spans = sum(section.span_count for section in sections)
    occupied_mhz = sum(section.occupied_mhz for section in sections)
    extent_mhz = sections[0].band_extent_mhz if sections else CBAND_EXTENT_MHZ
    return (
        ("Route distance", f"{distance_km:,.0f} km"),
        ("Fiber spans", f"{spans}"),
        ("Spectrum in use", f"{mhz_to_ghz(occupied_mhz):,.0f} GHz"),
        ("C-band per section", f"{mhz_to_ghz(extent_mhz):,.0f} GHz"),
    )


MAP_DIALECT: MapDialect[MapSection] = MapDialect(
    name=_section_name,
    endpoints=_section_endpoints,
    title=MAP_TITLE,
    place_sections=_place_distance_labels,
    band_rule=MARGIN_RULE,
    columns=NETWORK_COLUMNS,
    bar=NETWORK_BAR,
    legend_heading="OSNR margin",
    legend_subheading=escape(REFERENCE_MODE_NAME),
    legend_notes=_legend_notes,
    table_note=lambda layout: "worse margin first",
    totals=_network_totals,
    route_marks=_route_marks,
    draws_distance_labels=_draw_distance_labels,
)
"""What the shared layout pass has to be told about this map.

The placement hook is the distance-label sweep, which is where this map's share
of the placer goes. Everything the site labels get afterwards is what that sweep
left, which is the whole reason the hook owns the step rather than being a step
the shared sequence runs conditionally.
"""


def layout_map(
    sites: Sequence[MapSite],
    sections: Sequence[MapSection],
    focus: str | None = None,
    branch: str | None = None,
) -> MapLayout:
    """Project the sites, place every label, and hand back the positions.

    `focus` is a site shortname or `None` and changes no geometry. `branch` is
    provenance and changes none either; it is drawn in the footer so a reader can
    tell which branch the figures came from.
    """
    return build_layout(MAP_DIALECT, sites, sections, focus, branch)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def render_map(
    sites: Sequence[MapSite],
    sections: Sequence[MapSection],
    focus: str | None = None,
    branch: str | None = None,
) -> str:
    """The whole map, as one SVG document.

    `focus` is the shortname of the site the artifact is being generated for,
    or `None` for the network with nobody highlighted. `branch` is the branch
    the figures were read from, drawn in the footer. The same arguments always
    produce the same bytes.
    """
    return render_layout(MAP_DIALECT, layout_map(sites, sections, focus, branch))
