"""The shared drawing sequence, driven by a dialect.

Two maps here draw the same picture of Europe from different records and word
it differently. What they share is an order of operations: classify a section
into a colour band, order the sections, place them on the frame, draw the
routes, then the panel beside them. That order lives in this file.

Everything one map does differently arrives as plain data plus small pure
functions. Nothing here asks which map is being drawn, and no dialect carries a
field that would answer: a map's identity is not something the shared sequence
is allowed to branch on.

`mapchrome.py` holds the primitives, the pieces a map calls to put one shape on
the canvas. This file holds the order those calls go in. The import runs one
way only, from a dialect to here and from here to `mapchrome.py`, so a
primitive never reaches back for a type declared below.

Generic in the section type on purpose. The two record types share no base
class and are deliberately not given one, so every entity here that reads a
section is parameterised by `SectionT` and reads it only through the functions
its dialect supplies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Generic, Sequence, TypeVar

from infrahub_demo_otn.cartography import Frame, Placer, Rect
from infrahub_demo_otn.mapchrome import (
    BODY_TEXT,
    BUSY,
    CASING_FOCUS_WIDTH,
    CASING_WIDTH,
    DOCUMENT_CLOSE,
    FOCUS_FILL,
    MUTED_TEXT,
    PANEL_LEFT,
    PAPER,
    ROUTE_FOCUS_WIDTH,
    ROUTE_WIDTH,
    TABLE_ROW_HEIGHT,
    MapSite,
    SitePlacement,
    SiteProjection,
    document_header,
    draw_base,
    draw_nodes,
    draw_site_labels,
    draw_title_block,
    escape,
    panel_backing,
    panel_caption,
    panel_column_headers,
    panel_heading,
    panel_node_legend,
    panel_rule,
    panel_swatch,
    panel_totals,
    place_site_labels,
    prepare_canvas,
)

SectionT = TypeVar("SectionT")
"""The section record a map bands, orders and tabulates.

Left unbounded. The two record types carry different fields in different units,
and a base class over them would be either empty or a lie.
"""


@dataclass(frozen=True)
class Band:
    """One colour and the half-open range of figures it covers.

    `low` and `high` are in the map's own unit, and that unit never reaches this
    file: the classifier compares integers and the caption formats them. Which
    is what lets one band table serve a margin in millidecibels and another a
    count of tributary slots.

    `caption` is wording the map states directly, for the half of a legend line
    the edges cannot say on their own. It is `None` where the edges say all of
    it.
    """

    key: str
    colour: str
    low: int | None
    high: int | None
    caption: str | None = None

    def contains(self, figure: int) -> bool:
        """Half open, lower inclusive. `None` on a side means unbounded there."""
        if self.low is not None and figure < self.low:
            return False
        if self.high is not None and figure >= self.high:
            return False
        return True


@dataclass(frozen=True)
class BandCaptions:
    """How one map words a legend line, one wording per shape of edge pair.

    Each wording is a format string over four fields. `low` and `high` are the
    band's edges rendered by `edge`, which is where a stored unit becomes the
    unit the legend states. `last` is the highest figure the band still holds,
    for a countable unit whose ranges read better closed than half open.
    `caption` is whatever the band says for itself.

    A map writes the fields its own wording needs and ignores the rest, which is
    what lets one caption builder serve a legend that derives every word from
    the edges and a legend that only derives half.
    """

    unclassified: str
    below: str
    above: str
    between: str
    unbounded: str
    edge: Callable[[int], str]


@dataclass(frozen=True)
class BandRule(Generic[SectionT]):
    """A band table, the figure it is read against, and the unclassifiable case.

    `unclassified` sits outside `bands` on purpose, so `band_for` can never fall
    into it by accident and `contains` is never asked about it. A figure that
    could not be computed is a different statement from a good one, and every
    map here has to keep the two apart.
    """

    bands: tuple[Band, ...]
    unclassified: Band
    measure: Callable[[SectionT], int | None]
    captions: BandCaptions

    def band_for(self, figure: int | None) -> Band:
        """The band a figure falls in, or the unclassifiable band when there is none."""
        if figure is None:
            return self.unclassified
        for band in self.bands:
            if band.contains(figure):
                return band
        return self.unclassified

    def classify(self, section: SectionT) -> Band:
        """The band a section is painted, from whichever figure this map bands on."""
        return self.band_for(self.measure(section))

    def caption(self, band: Band) -> str:
        """The legend wording for a band, from its edges rather than typed twice.

        Which of the five wordings applies is decided by the edges the band has,
        and the unclassifiable case is decided first, because it is the one band
        whose absent edges mean something rather than nothing.
        """
        if band is self.unclassified:
            wording = self.captions.unclassified
        elif band.low is None and band.high is not None:
            wording = self.captions.below
        elif band.high is None and band.low is not None:
            wording = self.captions.above
        elif band.low is None or band.high is None:
            wording = self.captions.unbounded
        else:
            wording = self.captions.between
        return wording.format(
            low="" if band.low is None else self.captions.edge(band.low),
            high="" if band.high is None else self.captions.edge(band.high),
            last="" if band.high is None else self.captions.edge(band.high - 1),
            caption=band.caption or "",
        )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def ordered_sections(
    sections: Sequence[SectionT],
    name: Callable[[SectionT], str],
) -> tuple[SectionT, ...]:
    """Draw order: by name, always.

    Everything downstream walks this order, so a response that arrives shuffled
    still draws the same bytes. The name is read through the caller's
    accessor because the two record types share no base class to read it off.
    """
    return tuple(sorted(sections, key=name))


def table_order(
    sections: Sequence[SectionT],
    figure: Callable[[SectionT], int | None],
    name: Callable[[SectionT], str],
) -> tuple[SectionT, ...]:
    """Panel table order: the banded figure ascending, the unmeasured first.

    A section nobody could measure leads, because it is the row that needs a
    person and it has no number to sort among the ones that do. Then the
    tightest real figure, because that is the route that runs out first. Name
    breaks every tie, so a shuffled response cannot reorder the table.

    `figure` is the same reading the colour is a verdict about, so the table
    reads down in the order the legend reads down.
    """

    def key(section: SectionT) -> tuple[int, int, str]:
        measured = figure(section)
        if measured is None:
            return (0, 0, name(section))
        return (1, measured, name(section))

    return tuple(sorted(sections, key=key))


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteLabel:
    """A label riding its own route, and the box the placer gave it.

    A map that labels a route rotates the label to follow the line, so `rect` is
    the rotated extent that was reserved against every other label while `width`
    and `height` are the upright box the text is written into.

    `trailing_width` is the room set aside at the far end of that box for
    whatever badge rides alongside the text, and is zero when nothing does.
    """

    centre: tuple[float, float]
    angle_deg: float
    rect: Rect
    width: float
    height: float
    trailing_width: float


@dataclass(frozen=True)
class Placement(Generic[SectionT]):
    """One section positioned on the canvas, banded once.

    The band is settled here rather than while drawing, so nothing downstream
    can reclassify a route into a second colour.

    `overlay_band` is a second band the same route is also in, for a map that
    reads a figure per direction and has to mark the two disagreeing. `label` is
    the box this route's own label was given. Either is `None` on a map with no
    such thing, and that pair of absences is the whole difference between the
    two placements this replaces.
    """

    section: SectionT
    ax: float
    ay: float
    bx: float
    by: float
    band: Band
    touches_focus: bool
    overlay_band: Band | None = None
    label: RouteLabel | None = None


@dataclass(frozen=True)
class Layout(Generic[SectionT]):
    """Everything placed, before a single tag is written.

    Separate from the drawing so the two questions a map gets wrong, where a dot
    lands and where its label lands, can both be asserted without parsing SVG.

    `branch` is which branch the figures were read from. A margin, an occupied
    width and a slot count are each a property of a branch and not of the model,
    so a map that does not say which branch it read is a capacity claim with no
    date on it. Drawn in the footer, and changes no geometry.
    """

    frame: Frame
    sites: tuple[SitePlacement, ...]
    sections: tuple[Placement[SectionT], ...]
    focus: str | None
    branch: str | None = None

    @property
    def label_rects(self) -> tuple[Rect, ...]:
        """Every label rectangle, site and route alike, in draw order."""
        site_rects = [placed.label for placed in self.sites]
        route_rects = [placed.label.rect for placed in self.sections if placed.label is not None]
        return tuple(site_rects + route_rects)


# ---------------------------------------------------------------------------
# The panel table
# ---------------------------------------------------------------------------

BAR_TRACK = "#eaeff3"
BAR_QUIET = "#8ba3b1"
BAR_HALF = 0.5
"""The fill bar's backing, its ink below half full, and where half full is.

Both maps already drew these three the same way. What they never agreed on is
the colour of the cell beside a bar that is not there, which is why that one is
a field on `BarRule` and these are constants.
"""


class Slot(Enum):
    """Which side of the fill bar a cell is emitted on.

    The output is appended fragments, so emission order is the whole of byte
    identity. Declaring the side per column means the order is stated rather
    than inherited from whatever order the column list happens to be walked in.

    An enum rather than two strings, because a cell is emitted only when its
    slot matches one of these two. A misspelt string would print its heading and
    then nothing under it on every row, and no exception anywhere. The name is
    resolved where it is written, so mypy refuses the typo at the dialect.
    """

    BEFORE_BAR = "before_bar"
    AFTER_BAR = "after_bar"


@dataclass(frozen=True)
class Cell:
    """The ink one table cell is written in.

    `weight` is `None` where the map sets no `font-weight` at all, which is a
    different document from one that sets the default weight explicitly.
    """

    colour: str
    weight: str | None = None


CellInk = Callable[[SectionT, Band, Cell], Cell]
"""How one column decides its cell's ink, from the row and the bar beside it.

The three arguments are everything a cell has ever been coloured from here: the
section, the band the row was classified into, and the ink the fill bar took. A
column that wants a fixed colour ignores all three, and with no bar rule the
third is the plain figure ink, so there is always something to ignore.
"""


def muted_ink(section: SectionT, band: Band, bar: Cell) -> Cell:
    """The plain figure colour, whatever the row is."""
    del section, band, bar
    return Cell(MUTED_TEXT)


def bar_ink(section: SectionT, band: Band, bar: Cell) -> Cell:
    """Whatever the fill bar took, absent colour included."""
    del section, band
    return bar


@dataclass(frozen=True)
class Column(Generic[SectionT]):
    """One right-aligned panel column: its heading, its offset and its cell.

    `x` is measured from the panel's left edge and is carried across from the
    offsets each map already declared. A rounded or recomputed `x` moves a pixel.
    """

    heading: str
    x: float
    text: Callable[[SectionT], str]
    ink: CellInk[SectionT] = muted_ink
    size: float = 11.0
    slot: Slot = Slot.BEFORE_BAR

    def __post_init__(self) -> None:
        """Refuse a slot that is not one of the two, loudly and at declaration.

        mypy already refuses it where the dialect is written. This catches the
        caller that is not type checked, because the alternative is a heading
        with an empty column under it on every row and no error at all.
        """
        if not isinstance(self.slot, Slot):
            raise TypeError(f"Column {self.heading!r} declares slot {self.slot!r}, which is not a Slot")


@dataclass(frozen=True)
class BarRule(Generic[SectionT]):
    """The fill bar: identical geometry on both maps, different inputs.

    `fill` is a fraction from 0 to 1, or `None` for a row with no bar at all. No
    bar is not an empty bar: an empty bar says there is room, and these rows have
    nothing to measure.

    `absent_colour` is what the cell after the bar is written in when there is
    none. It differs per map and stays a declared value for that reason.
    """

    x: float
    width: float
    fill: Callable[[SectionT], float | None]
    absent_colour: str


PlaceSections = Callable[
    [tuple[SectionT, ...], SiteProjection, Placer, str | None],
    tuple[Placement[SectionT], ...],
]
"""How one map turns ordered sections into placements, placer included.

Handed the placer that `prepare_canvas` returned, and free to spend it or not.
A map that rides a label on each route places those labels here, before the site
labels get their turn; a map that rides none hands the placer straight on. Which
of the two is happening is the caller's own business and not a flag the shared
sequence reads, because the difference is not a switch inside one algorithm: it
is which of two algorithms consumes a fixed budget first.
"""

RouteDash = Callable[[Placement[SectionT]], str]
"""Whatever a map appends to its route stroke, dash pattern included.

An attribute fragment rather than a pattern, because a solid route sets no
attribute at all and a `stroke-dasharray` of nothing is a different document
from no `stroke-dasharray`.
"""

RouteMarks = Callable[[Placement[SectionT], str, float], list[str]]
"""Whatever one map rides on top of a drawn route.

Handed the placement, the geometry fragment the line was drawn from and the
stroke width it was drawn at, so an overlay lands on exactly the line under it
rather than on a second computation of where that line went.
"""

DistanceLabels = Callable[[Layout[SectionT]], list[str]]
"""The pass that writes a map's route labels, between the routes and the nodes.

A whole pass rather than a flag, because the labels are boxes in a map's own
vocabulary and the shared sequence has no wording to put in them. What the
sequence decides is only where in the stack they land: over the lines they
belong to, under the discs they must not cover.
"""


def no_dash(placed: Placement[SectionT]) -> str:
    """A solid route, which is what a map with one signal per line draws."""
    del placed
    return ""


def no_marks(placed: Placement[SectionT], geometry: str, width: float) -> list[str]:
    """Nothing on the line beyond the line."""
    del placed, geometry, width
    return []


@dataclass(frozen=True)
class Title(Generic[SectionT]):
    """The three lines above the map, and which of them one render can change.

    `heading` names the artifact. The two maps share no word of it on purpose:
    both are the same picture of Europe with the same coastline and the same
    node discs, and the heading is what tells a reader which of the two hanging
    off a site they have open.

    `made_of` is the sentence about this render's own contents, so it is read
    off the layout rather than stated: counts, and what the colour is a verdict
    about, and on one map a caption for a branch with nothing on it at all.

    `how_to_read` is fixed prose about the drawing itself. No render changes it,
    which is why it is a string and `made_of` is not.
    """

    heading: str
    made_of: Callable[[Layout[SectionT]], str]
    how_to_read: str


@dataclass(frozen=True)
class MapDialect(Generic[SectionT]):
    """Everything one map does differently, as data and pure functions.

    `name` and `endpoints` are the only two fields the shared sequence reads off
    a section directly, and it reads them through here because the two record
    types share no base class. Everything else about a section is read through
    the map's own formatters.

    `columns` is in emission order and `bar` is the fill bar, or `None` for a
    map that draws none.

    `route_dash` and `route_marks` are what a map puts on a route beyond the
    stroke itself: a dash pattern that says the colour is not a measurement, an
    overlay that says the two directions disagree, a dot where a span ends. Both
    default to nothing, because a route drawn plain is a map making no second
    statement rather than a map that forgot one.

    `draws_distance_labels` is the label pass a map runs between the routes and
    the nodes, or `None` for a map that labels no route. Declared here rather
    than decided while drawing, because the sequence is not allowed to work out
    which map it has and then run an extra step for one of them.

    `title` is the wording above the map, and the only place either map states
    its own name.

    `legend_heading` and `legend_subheading` are what the panel opens on, and
    `legend_notes` is the block of prose under the band swatches: a map's own
    markers, its own warnings, and whatever the swatches cannot say. It is handed
    the y it starts at and returns the y it finished on, because a map that says
    four things pushes everything below it four lines down.

    `table_note` is the right-aligned remark beside the section heading, read off
    the layout because one map changes it on an empty branch.

    `totals` is the pairs the panel closes on. It stays per map because the folds
    are not the same fold: one map sums a figure over the sections it drew, and
    the other refuses to, because a wavelength runs over several sections and
    each of them would count it.

    No field on here answers which map is being drawn, and none ever will. The
    sequence is not allowed to branch on a map's identity, and the cheapest way
    to hold that line is to give it nothing to branch on.
    """

    name: Callable[[SectionT], str]
    endpoints: Callable[[SectionT], tuple[str, str]]
    title: Title[SectionT]
    place_sections: PlaceSections[SectionT]
    band_rule: BandRule[SectionT]
    columns: tuple[Column[SectionT], ...]
    bar: BarRule[SectionT] | None
    legend_heading: str
    legend_subheading: str
    legend_notes: Callable[[float], tuple[list[str], float]]
    table_note: Callable[[Layout[SectionT]], str]
    totals: Callable[[Layout[SectionT]], tuple[tuple[str, str], ...]]
    route_dash: RouteDash[SectionT] = no_dash
    route_marks: RouteMarks[SectionT] = no_marks
    draws_distance_labels: DistanceLabels[SectionT] | None = None


def build_layout(
    dialect: MapDialect[SectionT],
    sites: Sequence[MapSite],
    sections: Sequence[SectionT],
    focus: str | None = None,
    branch: str | None = None,
) -> Layout[SectionT]:
    """Project the sites, place everything, and hand back the positions.

    `mapchrome.prepare_canvas` does the projection and the obstacles off the
    endpoint pairs alone, and refuses a section ending on a site the map does
    not draw or a focus naming nobody.

    Then the order that decides where every site label lands: the map's own
    placement pass first, the site labels second. A label riding a route has one
    degree of freedom and a site label has sixty-four, so placing the free one
    first would spend the crowded one's only option on it. A map with no route
    labels spends nothing and the site labels get the whole budget, which is a
    different outcome from the same two calls in the other order.

    `focus` is a site shortname or `None` and changes no geometry. `branch` is
    provenance and changes none either; it is drawn in the footer so a reader
    can tell which branch the figures came from.
    """
    ordered = ordered_sections(sections, dialect.name)
    edges: list[tuple[str, str, str]] = []
    for section in ordered:
        site_a, site_b = dialect.endpoints(section)
        edges.append((dialect.name(section), site_a, site_b))

    projection, placer = prepare_canvas(sites, edges, focus)
    placed_sections = dialect.place_sections(ordered, projection, placer, focus)
    placed_sites = place_site_labels(projection, placer, focus)
    return Layout(
        frame=projection.frame,
        sites=placed_sites,
        sections=placed_sections,
        focus=focus,
        branch=branch,
    )


def _cell(column: Column[SectionT], section: SectionT, y: float, ink: Cell) -> str:
    """One right-aligned figure, at its column, in the ink its rule chose."""
    weight = "" if ink.weight is None else f' font-weight="{ink.weight}"'
    return (
        f'<text x="{PANEL_LEFT + column.x:.1f}" y="{y:.1f}" text-anchor="end" fill="{ink.colour}" '
        f'font-size="{column.size:g}"{weight}>{escape(column.text(section))}</text>'
    )


def draw_table_row(dialect: MapDialect[SectionT], section: SectionT, y: float) -> list[str]:
    """One section across the panel: swatch, route, cells, fill bar, cells.

    The order is the whole point. A band swatch, then the route, then every
    column declared `BEFORE_BAR`, then the bar if this row has one, then every
    column declared `AFTER_BAR`. Nothing is emitted because the loop reached it;
    each fragment is emitted because a column said which side it sits on.

    The bar's own ink is handed to every cell, so a map whose post-bar figure
    repeats the bar's colour asks for it rather than recomputing it, and the
    colour a row gets when it has no bar is the one its map declared.
    """
    band = dialect.band_rule.classify(section)
    site_a, site_b = dialect.endpoints(section)
    route = f"{site_a.upper()} – {site_b.upper()}"
    out = [
        f'<circle cx="{PANEL_LEFT + 4:.1f}" cy="{y - 3.5:.1f}" r="3.2" fill="{band.colour}"/>',
        f'<text x="{PANEL_LEFT + 13:.1f}" y="{y:.1f}" fill="{BODY_TEXT}" font-size="11">{escape(route)}</text>',
    ]
    bar = dialect.bar
    fill = None if bar is None else bar.fill(section)
    if bar is None:
        ink = Cell(MUTED_TEXT)
    elif fill is None:
        ink = Cell(bar.absent_colour)
    else:
        ink = Cell(BUSY if fill > BAR_HALF else BAR_QUIET, "700" if fill > BAR_HALF else "600")

    for column in dialect.columns:
        if column.slot is Slot.BEFORE_BAR:
            out.append(_cell(column, section, y, column.ink(section, band, ink)))
    if bar is not None and fill is not None:
        out.append(
            f'<rect x="{PANEL_LEFT + bar.x:.1f}" y="{y - 7.5:.1f}" width="{bar.width:.0f}" height="7" rx="3.5" '
            f'fill="{BAR_TRACK}"/>'
        )
        out.append(
            f'<rect x="{PANEL_LEFT + bar.x:.1f}" y="{y - 7.5:.1f}" width="{bar.width * fill:.1f}" height="7" '
            f'rx="3.5" fill="{ink.colour}"/>'
        )
    for column in dialect.columns:
        if column.slot is Slot.AFTER_BAR:
            out.append(_cell(column, section, y, column.ink(section, band, ink)))
    return out


def draw_panel(dialect: MapDialect[SectionT], layout: Layout[SectionT]) -> list[str]:
    """The whole side panel: legend, node key, the section table and the totals.

    The rhythm is fixed and the wording is not. Every step below moves `y` by an
    amount both maps already agreed on, and the two places they differ are the
    prose under the swatches and the remark beside the section heading, which
    arrive as dialect hooks.

    The panel's right edge is the furthest column's offset. It is where every
    rule ends and every right-aligned note is anchored, and taking it from the
    column list is what stops a map declaring the same number twice and moving
    one.
    """
    # The furthest offset, not the last declared one. `slot` decouples the order
    # columns are declared in from the order they are emitted in, and nothing
    # requires either to run left to right, so `columns[-1]` would anchor the
    # rules short of the columns they underline.
    right = max(column.x for column in dialect.columns)
    rule = dialect.band_rule
    out: list[str] = [""]
    backing_at = 0

    y = 120.0
    out.extend(panel_heading(y, dialect.legend_heading, dialect.legend_subheading, right))
    for band in list(rule.bands) + [rule.unclassified]:
        y += 21.0
        out.append(panel_swatch(y, band.colour))
        out.append(panel_caption(y, escape(rule.caption(band))))

    y += 26.0
    notes, y = dialect.legend_notes(y)
    out.extend(notes)

    y += 30.0
    node_legend, y = panel_node_legend(y)
    out.extend(node_legend)

    y += 34.0
    out.extend(panel_heading(y, "Sections", dialect.table_note(layout), right))
    y += 17.0
    out.extend(panel_column_headers(y, "ROUTE", tuple((column.heading, column.x) for column in dialect.columns)))
    y += 5.0
    out.append(panel_rule(y, right))

    sections = [placed.section for placed in layout.sections]
    for section in table_order(sections, rule.measure, dialect.name):
        y += TABLE_ROW_HEIGHT
        out.extend(draw_table_row(dialect, section, y))

    y += 24.0
    out.append(panel_rule(y - 12, right))
    total_rows, y = panel_totals(y, dialect.totals(layout), right)
    out.extend(total_rows)

    out[backing_at] = panel_backing(y)
    return out


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def draw_routes(dialect: MapDialect[SectionT], layout: Layout[SectionT]) -> list[str]:
    """One line per section, in the colour its band was settled at.

    A casing under every route, so a line crossing another still reads as two
    lines, and a wider casing and stroke on a route touching the focus site.
    Then whatever the map rides on top, which is the only part that differs.

    The colour is read off the placement rather than classified again here.
    Reclassifying while drawing is how a route ends up one colour on the map and
    another in the panel.
    """
    out: list[str] = ["<g>"]
    for placed in layout.sections:
        casing = FOCUS_FILL if placed.touches_focus else PAPER
        casing_width = CASING_FOCUS_WIDTH if placed.touches_focus else CASING_WIDTH
        width = ROUTE_FOCUS_WIDTH if placed.touches_focus else ROUTE_WIDTH
        geometry = f'x1="{placed.ax:.1f}" y1="{placed.ay:.1f}" x2="{placed.bx:.1f}" y2="{placed.by:.1f}"'
        out.append(
            f'<line {geometry} stroke="{casing}" stroke-width="{casing_width:.1f}" '
            f'stroke-linecap="round" opacity="0.9"/>'
        )
        out.append(
            f'<line {geometry} stroke="{placed.band.colour}" stroke-width="{width:.1f}" '
            f'stroke-linecap="round"{dialect.route_dash(placed)}/>'
        )
        out.extend(dialect.route_marks(placed, geometry, width))
    out.append("</g>")
    return out


def draw_title(dialect: MapDialect[SectionT], layout: Layout[SectionT]) -> list[str]:
    """The title block: what this is, what it is made of, and how to read it.

    Two things a render adds to its map's own wording, and both are the same on
    any map. A map is seen from one site or from nowhere in particular. And its
    figures were read from a named branch or from the graph, which the footer
    has to say: a margin, an occupied width and a slot count are each a property
    of a branch, so a map that does not name one is a capacity claim with no
    date on it.
    """
    title = dialect.title
    focus_site = next((placed.site for placed in layout.sites if placed.is_focus), None)
    subject = f"Seen from {focus_site.name}. " if focus_site is not None else ""
    provenance = f"Generated from branch {escape(layout.branch)}. " if layout.branch else "Generated from the graph. "
    return draw_title_block(title.heading, f"{subject}{title.made_of(layout)}", f"{provenance}{title.how_to_read}")


def render_layout(dialect: MapDialect[SectionT], layout: Layout[SectionT]) -> str:
    """Turn a finished layout into one SVG document.

    The order is the stacking order, and every step of it is settled here. The
    base under everything, the routes over it, then the label pass a map may or
    may not have, then the nodes, because a disc covering a label is legible and
    a label covering a disc is not. The title and the panel go on last.
    """
    out: list[str] = document_header()
    out.extend(draw_base(layout.frame))
    out.extend(draw_routes(dialect, layout))
    if dialect.draws_distance_labels is not None:
        out.extend(dialect.draws_distance_labels(layout))
    out.extend(draw_nodes(layout.sites))
    out.extend(draw_site_labels(layout.sites))
    out.extend(draw_title(dialect, layout))
    out.extend(draw_panel(dialect, layout))
    out.append(DOCUMENT_CLOSE)
    return "\n".join(out)
