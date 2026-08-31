"""The ODU map: what is inside the wavelengths, on the same frame as the plant.

Pure presentation, and a sibling of `mapdraw.py` rather than a replacement for
it. The canvas, the projection, the coastline, the node discs, the site labels,
the title block and the panel's rhythm come from `mapchrome.py`, and the order
those calls go in comes from `mapengine.py`, so the two maps are the same picture
of Europe drawn the same way. What is here is this map's dialect, everything that
only means something once the reader knows the colour is a tributary-slot
verdict, and `render_odu_map`, the one entry point the transform calls.

Three decisions, each one a rule the drawing code below is not free to
relitigate.

**Colour is the BEST free-slot figure across the section's lit carriers.** Not
the worst, and not an average. The question a colour answers is whether another
circuit can be provisioned on this route, and provisioning goes on the roomiest
wavelength, so the roomiest wavelength is what the line is painted with. The
panel then reports the tightest carrier as its own figure and as the fill bar, so
both facts are on the page and neither is blended into the other. An aggregate
percentage was considered and rejected: a section reading 38 per cent while its
one ODU4 sits at 76 of 80 tells a planner they have room where they have none.

**Unknown is a band, not a gap.** A section with no lit carrier has no free-slot
figure at all, and `containers.largest_fit` refuses an unknown count rather than
answering `None`, because `None` already means "nothing fits". So this module
branches on the unknown case first and paints it in its own grey. A route with no
ODU layer on it must not read as a route with room on it, and that distinction is
the whole reason the fifth band exists.

**No kilometres, no loss, no OSNR margin and no span-boundary dots.** Their
absence is a decision, recorded as R-007, and not an omission waiting to be
tidied up. The section kind carries no total length attribute, every reader sums
it over the spans, and pulling spans in for a kilometre column would drag the
heaviest half of the network map's query back with it. The ODU map makes no
statement about the optical layer; the network map next to it makes every one.

Determinism is a requirement, not a nicety. The records arrive from a GraphQL
response whose ordering is not guaranteed, and an unchanged branch must produce a
byte-identical artifact or every rebuild looks like a change. Every collection
drawn or tabulated is sorted before it is walked and every float is formatted at
fixed precision.

No optical arithmetic, no slot table of its own, and no `infrahub_sdk`. The slot
arithmetic is `containers.py`, which the generator and the capacity check also
call, so the map cannot disagree with them about whether a wavelength is full.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, TypeAlias

from infrahub_demo_otn.cartography import Placer
from infrahub_demo_otn.containers import largest_fit
from infrahub_demo_otn.mapchrome import (
    INK_FAINT,
    PANEL_LEFT,
    PAPER,
    UNKNOWN_ROUTE,
    MapSite,
    SiteProjection,
    escape,
    panel_caption,
    panel_swatch,
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
    Slot,
    Title,
    bar_ink,
    build_layout,
    render_layout,
)

# ---------------------------------------------------------------------------
# The bands
# ---------------------------------------------------------------------------

HEADROOM_BAND_EDGES_SLOTS: tuple[int, int, int] = (1, 8, 80)
"""Where one colour becomes the next, in tributary slots of 1.25 Gbit/s.

Every edge is a client, not a round number. One slot is the smallest client in
the catalog, so below it nothing at all fits. Eight is an ODU2, the 10G
tributary. Eighty is an ODU4, the 100G one. A 40G client sits between them and
gets no edge of its own: STM-256 is 39.8 Gbit/s, so it maps into an ODU3 and
needs 32 slots, which is why the ODU2 band promises a 10G and not a 40G. A band
boundary that was not a client size would colour a route by an arithmetic nobody
provisions against.
"""


HEADROOM_BANDS: tuple[Band, ...] = (
    # Unbounded below rather than pinned at zero, so an overfilled carrier lands
    # here too. `containers.free_slots` returns a negative figure for one instead
    # of clamping, and a band that only matched zero would drop it on the floor.
    Band("full", "#9b2242", None, HEADROOM_BAND_EDGES_SLOTS[0], "nothing fits"),
    Band(
        "odu0",
        "#e0842d",
        HEADROOM_BAND_EDGES_SLOTS[0],
        HEADROOM_BAND_EDGES_SLOTS[1],
        "only a 1G or 2.5G fits",
    ),
    Band(
        "odu2",
        "#3f9fc4",
        HEADROOM_BAND_EDGES_SLOTS[1],
        HEADROOM_BAND_EDGES_SLOTS[2],
        "a 10G fits, not a 100G",
    ),
    Band("odu4", "#1b6b4f", HEADROOM_BAND_EDGES_SLOTS[2], None, "a 100G still fits"),
)
"""The four real bands, tightest first. Order is the legend order, and it is also
increasing headroom, so the legend reads as a scale rather than as a list.

Every band states its own caption, because that sentence is the half of the
legend the edges cannot produce on their own: 8 to 79 slots is a fact, and "a
10G fits and a 100G does not" is what a reader came for.
"""

NO_ODU_BAND = Band("no-odu", UNKNOWN_ROUTE, None, None, "not known")
"""The colour for a section with no lit carrier, or one holding a container the
slot table cannot size.

Deliberately outside `HEADROOM_BANDS`, so `HEADROOM_RULE` can never fall into it
by accident and `contains` is never asked about it. The grey is the network
map's unknown grey on purpose: a reader who has learnt what that treatment means
on one artifact should not have to learn it twice.
"""

HEADROOM_CAPTIONS = BandCaptions(
    unclassified="no container on any carrier: {caption}",
    below="fewer than {high} free: {caption}",
    above="{low} or more free: {caption}",
    between="{low} to {last} free: {caption}",
    unbounded="any headroom: {caption}",
    edge=str,
)
"""The legend, said in edges and then in what they mean.

The four real bands say the range and then read it out, because a slot count is
an arithmetic nobody provisions against on its own. The unknown band has no
range to say, which is the whole of what it means.

Ranges are stated closed, `1 to 7`, rather than half open, because slots are
counted and a reader would take `1 to 8` as including eight.
"""

HEADROOM_RULE: BandRule[OduSection] = BandRule(
    bands=HEADROOM_BANDS,
    unclassified=NO_ODU_BAND,
    measure=lambda section: section.headroom_slots,
    captions=HEADROOM_CAPTIONS,
)
"""What this map bands on: free slots on the roomiest lit carrier.

The unknown case is checked before anything else, and that ordering is the point
of routing every colour through here. `containers.largest_fit(0)` returns `None`
because nothing fits, while `largest_fit(None)` raises, because an unknown count
has no answer and answering `None` would report an unmeasured wavelength as a
full one. Every caller goes through this rule first, so the two never meet.
"""


# ---------------------------------------------------------------------------
# What this map puts on the shared canvas
# ---------------------------------------------------------------------------

COLUMN_LIT = 118.0
COLUMN_SLOTS = 186.0
COLUMN_BAR = 194.0
COLUMN_TIGHTEST = 254.0
COLUMN_FIT = 308.0
BAR_WIDTH = 30.0
"""The panel's column offsets, measured from its left edge, and the width of the
fill bar. They stay in this file, next to the bands, for the same reason the
network map keeps its own: lit carriers, committed slots and a largest-fit type
are this map's vocabulary, and a shared set of offsets would make the panel code
know what an ODU is."""

FIT_NOTHING = "none"
FIT_UNKNOWN = "no ODU"
"""What the largest-fit column prints for the two answers that are not a type.

Two words rather than one blank. A blank cell in that column is read as a value
nobody got round to filling in, and both of these are findings.
"""


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OduSection:
    """One drawable multiplex section, with its slot figures already counted.

    A carrier is lit when it holds at least one container. A dark carrier
    contributes to no numerator and no denominator, because slot capacity exists
    once a container is written on the wavelength; counting it as zero free would
    make an unprovisioned section look full, and counting it as fully free would
    make it look available.

    `committed_slots`, `offered_slots`, `headroom_slots` and
    `tightest_free_slots` are each `None` when the figure is not known, which
    happens when the section has no lit carrier or when one of its containers is
    of a type the slot table cannot size. Unknown is never zero here, in either
    direction.

    There is no `length_m`, no `loss_mdb`, no margin and no span boundary, and
    that is R-007 rather than an oversight. This map makes no statement about the
    optical layer, and the section kind has no total length attribute for it to
    make one from.
    """

    name: str
    site_a: str
    site_b: str
    carriers_lit: int = 0
    committed_slots: int | None = None
    offered_slots: int | None = None
    headroom_slots: int | None = None
    tightest_free_slots: int | None = None

    def __post_init__(self) -> None:
        """A section with no lit carrier cannot have a headroom figure.

        The two arrive from the same walk over the same carriers, so a record
        where they disagree is a transform bug, and the map would paint it as a
        real band. Caught at the door rather than drawn.
        """
        if self.carriers_lit == 0 and self.headroom_slots is not None:
            raise ValueError(f"section {self.name} has no lit carrier but reports {self.headroom_slots} free slots")

    @property
    def band(self) -> Band:
        """The colour this route is painted, from the roomiest lit carrier."""
        return HEADROOM_RULE.classify(self)

    @property
    def largest_fit(self) -> str | None:
        """The exact type name of the largest container that still fits, or `None`.

        `None` means nothing fits, which is a verdict. An unknown headroom has no
        verdict, so this raises for one, exactly as `containers.largest_fit` does.
        Derived rather than carried on the record on purpose: a transform that
        computed the headroom and then wrote a type name beside it would have two
        figures free to drift, and the drift would show as a route coloured "room
        for a 100G" whose panel row says nothing fits.
        """
        return largest_fit(self.headroom_slots)

    @property
    def tightest_fill(self) -> float | None:
        """How much of one 100G client's room is gone on the tightest carrier.

        Zero when the tightest carrier still has a full ODU4 free, one when it has
        nothing left, and `None` when there is no figure to draw. The scale is one
        ODU4, 80 slots, which is the same yardstick the top colour band uses, so
        the bar and the colour are measuring the section with one ruler at two
        ends: the colour on the roomiest carrier, the bar on the tightest.

        **The obvious scale was a per-carrier percentage and it is not available
        here, nor should it be faked.** The record carries the section's offered
        total and its lit-carrier count, and dividing one by the other gives a
        mean that is wrong on any section mixing line rates. Frankfurt to Milan in
        the shipped dataset is such a section: 37 carriers offering 320 slots and
        three offering 80, all of them empty. Its tightest carrier is an untouched
        ODU4, and the mean would draw that bar three quarters full and print the
        figure in red. Measured, not supposed, which is why the denominator is a
        client size instead.

        Clamped at both ends. An overfilled carrier reports a negative free
        figure, and a bar drawn past its own backing is a rendering bug rather
        than a stronger warning; the negative number is in the column beside it.
        """
        if self.tightest_free_slots is None:
            return None
        room = min(HEADROOM_BAND_EDGES_SLOTS[2], max(0, self.tightest_free_slots))
        return 1.0 - room / HEADROOM_BAND_EDGES_SLOTS[2]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


OduSectionPlacement: TypeAlias = Placement[OduSection]
"""A route on the canvas, in the band its headroom put it in.

No label and no overlay band. This map puts no label on a route: the network map
rides a distance on each line and pays for it with a placement sweep, while the
figures here are all in the panel, and a second set of boxes competing with the
site labels would buy nothing. The band is read once per section, not once per
direction, so there is no second band to overlay either.
"""

OduLayout: TypeAlias = Layout[OduSection]
"""Everything placed, before a single tag is written."""


def has_no_odu_layer(layout: OduLayout) -> bool:
    """Whether not one carrier anywhere on this branch holds a container.

    The empty-branch render is a success, not an error, and this is what tells
    the title block to say so. Every route grey because every route is
    unprovisioned is a different picture from every route grey because the
    transform fell over, and only one of them has a caption.

    A fact this map reads off its own records, so it is stated here rather than
    on the shared layout, which is not allowed to know what an ODU is.
    """
    return bool(layout.sections) and all(placed.section.carriers_lit == 0 for placed in layout.sections)


def _section_name(section: OduSection) -> str:
    """How the engine reads a name off this map's record."""
    return section.name


def _section_endpoints(section: OduSection) -> tuple[str, str]:
    """The two sites this map's record runs between."""
    return (section.site_a, section.site_b)


def _place_sections(
    sections: tuple[OduSection, ...],
    projection: SiteProjection,
    placer: Placer,
    focus: str | None,
) -> tuple[OduSectionPlacement, ...]:
    """Straight onto the line, and the placer is handed on untouched.

    `placer` is taken and not used, and that is the point of it appearing here.
    This map puts no label on a route, so every position the placer still has is
    left for the site labels. Spending none of it is a decision this map makes,
    not an absence the shared sequence infers.
    """
    del placer
    placed: list[OduSectionPlacement] = []
    for section in sections:
        ax, ay = projection.position[section.site_a]
        bx, by = projection.position[section.site_b]
        placed.append(
            OduSectionPlacement(
                section=section,
                ax=ax,
                ay=ay,
                bx=bx,
                by=by,
                band=section.band,
                touches_focus=focus is not None and focus in (section.site_a, section.site_b),
            )
        )
    return tuple(placed)


def _fit_text(section: OduSection) -> str:
    """The largest-fit column for one section, with the unknown case first.

    The order of these two branches is the trap this whole module is written
    around. `containers.largest_fit` raises on an unknown count rather than
    answering `None`, so the guard has to come before the call and not after it.
    """
    if section.headroom_slots is None:
        return FIT_UNKNOWN
    fits = section.largest_fit
    return fits if fits is not None else FIT_NOTHING


def _slots_text(section: OduSection) -> str:
    """Committed over offered, across the section's lit carriers."""
    if section.committed_slots is None or section.offered_slots is None:
        return "n/a"
    return f"{section.committed_slots}/{section.offered_slots}"


def _tightest_text(section: OduSection) -> str:
    if section.tightest_free_slots is None:
        return "n/a"
    return f"{section.tightest_free_slots}"


def _fit_ink(section: OduSection, band: Band, bar: Cell) -> Cell:
    """The largest-fit cell restates the route's own colour.

    The only cell on either map that repeats the swatch. It is the column the
    colour on the map is a verdict about, so a reader can carry one back to the
    other without counting slots.
    """
    del section, bar
    return Cell(band.colour, "600")


ODU_COLUMNS: tuple[Column[OduSection], ...] = (
    Column("LIT", COLUMN_LIT, lambda section: f"{section.carriers_lit}"),
    Column("SLOTS", COLUMN_SLOTS, _slots_text),
    # The tightest carrier, not the section. The bar to its left is the same
    # figure as a shape, and the colour on the map is the other end of the range,
    # so the header has to say which end this is.
    Column("TIGHT", COLUMN_TIGHTEST, _tightest_text, bar_ink, 10.0, Slot.AFTER_BAR),
    Column("FITS", COLUMN_FIT, _fit_text, _fit_ink, 10.0, Slot.AFTER_BAR),
)
"""The table, in emission order: two counts, then the fill bar, then the tightest
figure and what still fits."""

ODU_BAR: BarRule[OduSection] = BarRule(COLUMN_BAR, BAR_WIDTH, lambda section: section.tightest_fill, INK_FAINT)
"""How much of one 100G client's room is gone on the tightest carrier.

No bar at all rather than an empty one when there is no figure. An empty bar is a
carrier with room; those rows have no carrier to measure, and `INK_FAINT` is what
they say so in."""


def _legend_notes(y: float) -> tuple[list[str], float]:
    """What the band swatches cannot say: the dash, and the two ends of the range.

    Returns the lines and the y the last of them landed on, because the node
    legend under this block keeps counting from there.
    """
    # The dash is the unknown case's second signal. Stated in the legend because
    # on an unprovisioned branch every route wears it and there is no other
    # colour on the map to read it against.
    out = [panel_swatch(y, NO_ODU_BAND.colour)]
    out.append(
        f'<line x1="{PANEL_LEFT:.1f}" y1="{y - 4:.1f}" x2="{PANEL_LEFT + 28:.1f}" y2="{y - 4:.1f}" '
        f'stroke="{PAPER}" stroke-width="3.6" stroke-dasharray="8 6"/>'
    )
    out.append(panel_caption(y, "dashed: no container on any carrier here"))
    y += 20.0
    # The two ends of the range, said in the legend rather than left to the
    # column headers. A reader who takes the colour for the whole section reads a
    # route with one full wavelength as a route with room on it.
    out.append(panel_caption(y, "grey is not empty: nothing is known here", 0.0))
    y += 16.0
    out.append(panel_caption(y, "colour is the roomiest carrier on the route", 0.0))
    y += 16.0
    out.append(panel_caption(y, "the bar and the figure are the tightest", 0.0))
    y += 16.0
    out.append(panel_caption(y, "a full bar: no 100G fits on the tightest", 0.0))
    return out, y


def _table_note(layout: OduLayout) -> str:
    """The remark beside the section heading, which an empty branch changes."""
    return "no ODU layer on this branch" if has_no_odu_layer(layout) else "least headroom first"


def _odu_totals(layout: OduLayout) -> tuple[tuple[str, str], ...]:
    """The four figures the panel closes on. Four per-section facts, and no sum
    across sections.

    A network total of offered slots was the obvious fourth line and it is wrong.
    A wavelength runs end to end over several sections and each of them counts it,
    so summing the per-section offerings counts one carrier once per section it
    crosses and reports more capacity than the network has. The per-section
    figures in the table above are the honest ones, and these are the extremes
    over them.

    The last two are the negative results, and they are counts rather than a
    percentage because a percentage of twenty-one sections hides which ones.
    """
    sections = [placed.section for placed in layout.sections]
    measured = [section for section in sections if section.band is not NO_ODU_BAND]
    headroom = [section.headroom_slots for section in measured if section.headroom_slots is not None]
    tightest = [section.tightest_free_slots for section in measured if section.tightest_free_slots is not None]
    nothing_fits = sum(1 for section in sections if section.band.key == "full")
    return (
        # "with a headroom figure", not "with an ODU layer". A section can have a
        # lit carrier and still be unknown, because one container of a type the
        # slot table cannot size makes its parent's free figure unknown.
        ("Sections with a headroom figure", f"{len(measured)} of {len(sections)}"),
        ("Least headroom on a section", f"{min(headroom)} slots" if headroom else "not known anywhere"),
        ("Tightest carrier anywhere", f"{min(tightest)} slots" if tightest else "not known anywhere"),
        ("Sections where nothing fits", f"{nothing_fits}"),
    )


def _empty_branch_caption(layout: OduLayout) -> str:
    """The sentence an unprovisioned branch gets, naming the branch.

    FR-019, and it is a caption on a successful render rather than an error. The
    base dataset does not reach this state: every pre-provisioned wavelength ships
    carrying an empty line container, so it is lit and its route lands in a real
    band. This is the picture of a branch whose containers were removed, and the
    caption says which branch so nobody reads it as the model being empty.
    """
    where = f"branch {escape(layout.branch)}" if layout.branch else "the branch this was read from"
    return f"No ODU layer is provisioned on {where}: every route is unknown, and none of them is available."


def _made_of(layout: OduLayout) -> str:
    """One line of what this render is made of, or the empty branch's caption.

    Sections with an ODU layer, not carriers. A wavelength runs over several
    sections and every one of them counts it, so a network carrier total would be
    larger than the network has.
    """
    if has_no_odu_layer(layout):
        return _empty_branch_caption(layout)
    with_odu = sum(1 for placed in layout.sections if placed.section.carriers_lit)
    return (
        f"{len(layout.sites)} PoPs, {len(layout.sections)} multiplex sections, {with_odu} carrying an ODU layer. "
        f"Line colour is the largest ODU that still fits on the roomiest lit carrier."
    )


ODU_TITLE: Title[OduSection] = Title(
    heading="ODU capacity and grooming",
    made_of=_made_of,
    how_to_read=(
        "The number inside a site is its optical degree. The bar and the figure beside it in "
        "the panel are the TIGHTEST carrier on the route, not the roomiest. Coastlines: Natural Earth."
    ),
)
"""The wording above the map.

The heading names this map's own subject and does not share a word with the
network map's. Two SVG artifacts hang off every point of presence, both are a map
of Europe with the same coastline and the same node discs, and the heading is
what a reader opening one of them reads first.
"""


def _route_dash(placed: OduSectionPlacement) -> str:
    """A section with no lit carrier is dashed as well as grey.

    Colour alone carries the unknown case on the network map, where one route in
    twenty-one is grey; here the whole map is grey on an unprovisioned branch,
    and a reader has no second colour to compare against. The dash says "no ODU
    layer" without one.
    """
    return ' stroke-dasharray="8 6"' if placed.band is NO_ODU_BAND else ""


ODU_DIALECT: MapDialect[OduSection] = MapDialect(
    name=_section_name,
    endpoints=_section_endpoints,
    title=ODU_TITLE,
    place_sections=_place_sections,
    band_rule=HEADROOM_RULE,
    columns=ODU_COLUMNS,
    bar=ODU_BAR,
    legend_heading="Largest ODU that fits",
    legend_subheading="on the roomiest lit carrier",
    legend_notes=_legend_notes,
    table_note=_table_note,
    totals=_odu_totals,
    route_dash=_route_dash,
)
"""What the shared layout pass has to be told about this map.

The placement hook spends nothing, so the site labels are placed against the
routes and the nodes and nothing else.
"""


def layout_odu_map(
    sites: Sequence[MapSite],
    sections: Sequence[OduSection],
    focus: str | None = None,
    branch: str | None = None,
) -> OduLayout:
    """Project the sites, place the site labels, and hand back the positions.

    `focus` is a site shortname or `None` and changes no geometry. `branch` is
    provenance and changes none either.
    """
    return build_layout(ODU_DIALECT, sites, sections, focus, branch)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def render_odu_map(
    sites: Sequence[MapSite],
    sections: Sequence[OduSection],
    focus: str | None = None,
    branch: str | None = None,
) -> str:
    """The whole ODU map, as one SVG document.

    `focus` is the shortname of the site the artifact is being generated for, or
    `None` for the network with nobody highlighted. `branch` is the branch the
    figures were read from, drawn in the footer and named in the caption when the
    branch has no ODU layer at all. The same records always produce the same
    bytes, whatever order they arrive in.
    """
    return render_layout(ODU_DIALECT, layout_odu_map(sites, sections, focus, branch))
