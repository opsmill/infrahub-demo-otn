"""The furniture every map of this network is drawn on, with no verdict in it.

Two artifacts now draw Europe. The network map colours each route by its OSNR
margin; the ODU map colours the same route by the largest container that still
fits on it. They share a canvas, a projection, a coastline, a node glyph, a site
label, a title block and a side panel, and they share nothing at all about what a
colour means.

That split is the whole reason this module exists, and it is what decides where
each piece of code lives. Anything that would make the shared frame know about
OSNR margins, tributary slots, kilometres or spectrum stays with the map that
owns the vocabulary. So the bands, the legend wording, the column offsets, the
row contents and the route colours are declared in `mapdraw.py` and
`odudraw.py`, `mapengine.py` holds the order they are drawn in, and the frame,
the basemap, the graticule, the node discs, the site labels, the text metrics
and the panel's frame, headings, rules and row rhythm are here.

**The panel was a rhythm and not a table, and it is now both.** This module used
to argue that a shared table would need a column vocabulary, and that a column
vocabulary is a verdict about what matters, so each caller kept its own offsets
and rendered its own rows. Feature 020 reversed that position. `mapengine.Column`
names a heading, an offset, a text function, an ink function and which side of
the fill bar the cell sits on, and it names nothing else: no unit, no field and
no map. The vocabulary turned out to be about the shape of a column rather than
about what a column means, which is what makes it worth its cost. It replaced
the same row renderer written twice, and a third map's table is now a tuple
rather than a file.

So a third map declares a dialect and lets the shared sequence draw its rows.
Hand-rolling a panel renderer against the primitives below is the older, longer
way, and the row rhythm here exists to serve `mapengine.py` rather than to be
re-assembled by hand.

**The layout seam takes endpoint pairs, not sections.** `prepare_canvas` wants
the name of each edge and the two site shortnames it ends on, and nothing else.
The network map's sections carry span boundaries, lengths and two directional
margins; the ODU map's carry none of that. Handing over the pair is what lets one
projection serve both without either map's record shape leaking into the other's.

Determinism is a requirement, not a nicety. An unchanged network must produce a
byte-identical artifact, or every rebuild looks like a change. Every collection is
sorted before it is walked and every float is formatted at fixed precision.
`tests/unit/fixtures/network_map_golden.svg` was captured before this module
existed and holds the extraction to changing no pixel of the network map.

Pure presentation. No optical arithmetic, no slot arithmetic, and no
`infrahub_sdk`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from infrahub_demo_otn.basemap import COUNTRIES
from infrahub_demo_otn.cartography import Frame, Placer, Rect
from infrahub_demo_otn.units import microdeg_to_deg

# ---------------------------------------------------------------------------
# The canvas
# ---------------------------------------------------------------------------

CANVAS_WIDTH = 1680.0
CANVAS_HEIGHT = 1080.0
PAD_LEFT = 34.0
PAD_RIGHT = 380.0
PAD_TOP = 96.0
PAD_BOTTOM = 44.0
"""The drawing area is the canvas less these. The right pad is the side panel
and the top pad is the title block, so the projection never runs under either.
"""

# ---------------------------------------------------------------------------
# The palette
# ---------------------------------------------------------------------------

INK = "#0d3550"
INK_SOFT = "#5a6d78"
INK_FAINT = "#93a5b0"
NODE_FILL = "#124b6e"
FOCUS_FILL = "#1f2937"
HPC_RING = "#7c4dbe"
HPC_TEXT = "#d9c9f2"
BUSY = "#c94a45"
SEA_TOP = "#eaf1f6"
SEA_BOTTOM = "#dfeaf1"
LAND_FILL = "#f7f4ec"
LAND_LINE = "#cfc9ba"
GRATICULE = "#c8d8e2"
PAPER = "#ffffff"
RULE = "#e2e9ee"
PANEL_EDGE = "#d5dfe6"
BODY_TEXT = "#3a4c56"
MUTED_TEXT = "#54666f"
LEGEND_TEXT = "#41535e"
UNKNOWN_ROUTE = "#8ba3b1"
"""One palette for both maps. A second copy is how the two artifacts end up with
two shades of the same sea.

`UNKNOWN_ROUTE` is the exception that proves the rule. A route colour is a verdict
and every verdict belongs to the map that owns the vocabulary, but "this figure is
not available" is the one statement both maps make in the same words: the network
map for a section whose budget would not evaluate, the ODU map for a section with
no lit carrier. A reader who has learnt that treatment on one artifact should not
have to learn it twice, so the two share the grey and neither is free to pick its
own."""

# ---------------------------------------------------------------------------
# Geometry of the furniture
# ---------------------------------------------------------------------------

ROUTE_WIDTH = 3.4
ROUTE_FOCUS_WIDTH = 5.2
CASING_WIDTH = 7.6
CASING_FOCUS_WIDTH = 9.6
NODE_BASE_RADIUS = 6.5
NODE_RADIUS_PER_DEGREE = 1.5
HPC_RING_GAP = 6.0
FOCUS_RING_GAP = 9.5
GRATICULE_STEP_DEG = 5
"""Route widths live here although each map strokes its own routes. The colour is
a verdict and belongs to the caller; the width is furniture, and two maps of one
network drawn at two line weights read as two networks."""

PANEL_GUTTER = 28.0
PANEL_WIDTH = 340.0
PANEL_TOP = 92.0
TABLE_ROW_HEIGHT = 15.6
PANEL_LEFT = CANVAS_WIDTH - PAD_RIGHT + PANEL_GUTTER
"""Where the panel starts, which is inside the right pad the projection is kept
out of. Every panel helper below is measured from it, so a caller passes column
offsets and never absolute x."""


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MapSite:
    """One drawable site, on either map.

    Coordinates are microdegrees, which is what the graph stores and therefore
    what the transform can hand over without touching them. Degrees are read
    back through `units.py`, the only place a scale factor is allowed.

    `eurohpc_name` doubles as the flag and the caption: a site is a EuroHPC host
    exactly when it has one, and the map draws the name inside the dashed ring.

    Shared rather than redefined per map. Both maps select the same sites from
    the same query, and a second record with the same five fields would drift on
    the first one that gained a sixth.
    """

    name: str
    shortname: str
    longitude_microdeg: int
    latitude_microdeg: int
    optical_degree: int
    eurohpc_name: str | None = None

    @property
    def is_eurohpc(self) -> bool:
        return self.eurohpc_name is not None

    @property
    def longitude_deg(self) -> float:
        return microdeg_to_deg(self.longitude_microdeg)

    @property
    def latitude_deg(self) -> float:
        return microdeg_to_deg(self.latitude_microdeg)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SitePlacement:
    """A site on the canvas, with the rectangle its label was given."""

    site: MapSite
    x: float
    y: float
    radius: float
    label: Rect
    is_focus: bool


@dataclass(frozen=True)
class SiteProjection:
    """The frame and where every node landed in it.

    Handed from `prepare_canvas` to whatever places the edge labels, which is the
    one part of layout each map does for itself. `sites` is sorted by shortname
    and is the order everything downstream walks.
    """

    frame: Frame
    sites: tuple[MapSite, ...]
    position: Mapping[str, tuple[float, float]]
    radius: Mapping[str, float]


def ordered_sites(sites: Sequence[MapSite]) -> tuple[MapSite, ...]:
    return tuple(sorted(sites, key=lambda site: site.shortname))


def prepare_canvas(
    sites: Sequence[MapSite],
    edges: Sequence[tuple[str, str, str]],
    focus: str | None = None,
) -> tuple[SiteProjection, Placer]:
    """Fit the frame, project the nodes, and block out everything a label cannot sit on.

    `edges` is one `(name, site_a, site_b)` per drawn route, in draw order.
    Endpoint pairs and a name, not sections: the two maps carry different section
    records and neither shape belongs in here.

    The returned placer already holds the side panel, the title block, every
    route as a named segment and a halo around every node. What it does not hold
    is any label, because the order labels are placed in is the caller's
    decision and it is the decision that determines whether they fit.

    `focus` is a site shortname or `None`. It changes no geometry: the halo a
    focused node would need is reserved around every node, so the same network
    lays out identically whichever site the map is being rendered for, and the
    fourteen artifacts differ only where they are meant to.
    """
    placed_sites = ordered_sites(sites)
    if not placed_sites:
        raise ValueError("a map needs at least one site")

    by_shortname = {site.shortname: site for site in placed_sites}
    for name, site_a, site_b in edges:
        for end in (site_a, site_b):
            if end not in by_shortname:
                raise ValueError(f"section {name} ends on {end}, which is not among the sites")

    # A focus naming nobody is a whole map with nothing highlighted, which is
    # the one thing every copy of this artifact is supposed to differ by. The
    # transform already refuses a site its query did not match; this refuses the
    # case where the query matched a site the map does not draw, which the
    # `site_type: "pop"` filter on the drawn set makes reachable.
    if focus is not None and focus not in by_shortname:
        raise ValueError(f"the map is focused on {focus}, which is not among the sites it draws")

    frame = Frame.fit(
        [(site.longitude_deg, site.latitude_deg) for site in placed_sites],
        width=CANVAS_WIDTH,
        height=CANVAS_HEIGHT,
        pad_left=PAD_LEFT,
        pad_right=PAD_RIGHT,
        pad_top=PAD_TOP,
        pad_bottom=PAD_BOTTOM,
    )
    position = {site.shortname: frame.project(site.longitude_deg, site.latitude_deg) for site in placed_sites}
    radius = {site.shortname: NODE_BASE_RADIUS + site.optical_degree * NODE_RADIUS_PER_DEGREE for site in placed_sites}

    placer = Placer()
    # The panel and the title block are permanent obstacles. Without them a
    # label slides off the map and under the table.
    placer.block(CANVAS_WIDTH - PAD_RIGHT + 6.0, 0.0, PAD_RIGHT, CANVAS_HEIGHT)
    placer.block(0.0, 0.0, CANVAS_WIDTH, PAD_TOP - 26.0)
    for name, site_a, site_b in edges:
        ax, ay = position[site_a]
        bx, by = position[site_b]
        placer.add_segment(ax, ay, bx, by, name)
    for site in placed_sites:
        x, y = position[site.shortname]
        reserved = radius[site.shortname] + FOCUS_RING_GAP
        placer.block(x - reserved, y - reserved, 2 * reserved, 2 * reserved)

    projection = SiteProjection(frame=frame, sites=placed_sites, position=position, radius=radius)
    return projection, placer


def place_site_labels(
    projection: SiteProjection,
    placer: Placer,
    focus: str | None = None,
) -> tuple[SitePlacement, ...]:
    """Sweep sixteen bearings on four rings around each node.

    Busiest node first, because a degree-six node has the fewest wedges left
    between its own routes and the last site to be placed gets whatever is
    still free.

    Called after the caller has placed its own edge labels, never before. An edge
    label has one degree of freedom and a site label has sixty-four, so placing
    the free one first spends the crowded one's only option on it.
    """
    placed: list[SitePlacement] = []
    for site in sorted(projection.sites, key=lambda item: (-item.optical_degree, item.shortname)):
        x, y = projection.position[site.shortname]
        node_radius = projection.radius[site.shortname]
        box_width = text_width(site.name, 12.0, "600") + 18.0
        if site.eurohpc_name is not None:
            box_width = max(box_width, text_width(site.eurohpc_name.upper(), 9.0, "700") + 18.0)
        box_height = 20.0 + (12.0 if site.is_eurohpc else 0.0)
        gap = node_radius + (12.0 if site.is_eurohpc else 3.0) + 7.0
        candidates: list[tuple[Rect, None]] = []
        for ring in (1.0, 1.35, 1.8, 2.4):
            for step in range(16):
                bearing = math.radians(step * 22.5 - 90.0)
                cos, sin = math.cos(bearing), math.sin(bearing)
                reach = gap + (abs(cos) * box_width + abs(sin) * box_height) / 2
                centre_x, centre_y = x + cos * reach * ring, y + sin * reach * ring
                candidates.append(((centre_x - box_width / 2, centre_y - box_height / 2, box_width, box_height), None))
        rect, _ = placer.place(candidates)
        placed.append(
            SitePlacement(
                site=site,
                x=x,
                y=y,
                radius=node_radius,
                label=rect,
                is_focus=site.shortname == focus,
            )
        )
    return tuple(sorted(placed, key=lambda item: item.site.shortname))


# ---------------------------------------------------------------------------
# Text metrics
# ---------------------------------------------------------------------------


def text_width(text: str, size: float, weight: str = "400") -> float:
    """Rough advance width in pixels.

    Bold and uppercase run wider. Underestimating here is what makes a label
    overflow the box the placer reserved for it, so the estimate leans high.
    """
    em = 0.55
    if weight in ("600", "700"):
        em += 0.03
    upper = sum(1 for character in text if character.isupper())
    if text and upper / len(text) > 0.6:
        em += 0.08
    return len(text) * size * em


def escape(text: object) -> str:
    """Every value that reaches a text node goes through here.

    Site names and facility names come out of the graph, so an ampersand in one
    is a broken document rather than a broken label.
    """
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# The document and the base layer
# ---------------------------------------------------------------------------

DOCUMENT_CLOSE = "</svg>"


def document_header() -> list[str]:
    """The opening tag and the three definitions both maps use.

    The sea gradient, the node drop shadow and the clip path that keeps the
    basemap out of the title block. Named identically in both artifacts, which
    matters because a reader opens the two side by side.
    """
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS_WIDTH:.0f} {CANVAS_HEIGHT:.0f}" '
        f'width="{CANVAS_WIDTH:.0f}" height="{CANVAS_HEIGHT:.0f}" '
        f'font-family="Inter, -apple-system, Helvetica Neue, Arial, sans-serif">',
        "<defs>",
        '<linearGradient id="sea" x1="0" y1="0" x2="0" y2="1">',
        f'<stop offset="0" stop-color="{SEA_TOP}"/><stop offset="1" stop-color="{SEA_BOTTOM}"/>',
        "</linearGradient>",
        '<filter id="soft" x="-40%" y="-40%" width="180%" height="180%">',
        '<feDropShadow dx="0" dy="1" stdDeviation="1.5" flood-color="#0b2b3a" flood-opacity="0.25"/>',
        "</filter>",
        f'<clipPath id="frame"><rect x="0" y="{PAD_TOP - 40:.0f}" width="{CANVAS_WIDTH:.0f}" '
        f'height="{CANVAS_HEIGHT - PAD_TOP + 40:.0f}"/></clipPath>',
        "</defs>",
    ]


def graticule_ticks(low: float, high: float, step: int) -> list[int]:
    """Every multiple of `step` that falls inside `[low, high]`, and none outside."""
    first = math.ceil(low / step) * step
    return [tick for tick in range(first, int(math.floor(high / step)) * step + 1, step)]


def _inverse_mercator_lat_deg(y: float) -> float:
    """Projected y back to latitude. Only the graticule needs this."""
    return math.degrees(2.0 * math.atan(math.exp(y)) - math.pi / 2)


def draw_base(frame: Frame) -> list[str]:
    """Sea, graticule, land and borders, clipped to the drawing area."""
    out: list[str] = [
        f'<rect width="{CANVAS_WIDTH:.0f}" height="{CANVAS_HEIGHT:.0f}" fill="{PAPER}"/>',
        f'<rect width="{CANVAS_WIDTH:.0f}" height="{CANVAS_HEIGHT:.0f}" fill="url(#sea)"/>',
    ]

    lat_low = _inverse_mercator_lat_deg(frame.ymin)
    lat_high = _inverse_mercator_lat_deg(frame.ymax)
    lon_low, lon_high = math.degrees(frame.xmin), math.degrees(frame.xmax)
    # Clipped, and bounded to the frame rather than rounded outwards from it.
    # The old `int(low // step) * step` to `int(high) + step` ranges overshot by
    # up to a full step at each end, so the render carried meridians beyond the
    # canvas and one under the side panel: markup that draws nothing, in a file
    # asserted to be byte-stable.
    out.append(f'<g clip-path="url(#frame)" stroke="{GRATICULE}" stroke-width="0.7" opacity="0.7">')
    step = GRATICULE_STEP_DEG
    for lon in graticule_ticks(lon_low, lon_high, step):
        start, end = frame.project(lon, lat_low), frame.project(lon, lat_high)
        out.append(f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}"/>')
    for lat in graticule_ticks(lat_low, lat_high, step):
        start, end = frame.project(lon_low, lat), frame.project(lon_high, lat)
        out.append(f'<line x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{end[0]:.1f}" y2="{end[1]:.1f}"/>')
    out.append("</g>")

    out.append('<g clip-path="url(#frame)">')
    for _name, rings in sorted(COUNTRIES.items()):
        parts: list[str] = []
        for ring in rings:
            points = [frame.project(lon, lat) for lon, lat in ring]
            parts.append("M" + " ".join(f"{x:.1f},{y:.1f}" for x, y in points) + "Z")
        out.append(
            f'<path d="{"".join(parts)}" fill="{LAND_FILL}" fill-rule="evenodd" '
            f'stroke="{LAND_LINE}" stroke-width="0.9" stroke-linejoin="round"/>'
        )
    out.append("</g>")
    return out


# ---------------------------------------------------------------------------
# Nodes and their labels
# ---------------------------------------------------------------------------


def draw_nodes(placements: Sequence[SitePlacement]) -> list[str]:
    """A disc per site, its optical degree inside it.

    Radius grows with degree, so the shape of the core is readable before a
    single label is. A EuroHPC site gets a dashed violet ring; the focus site
    gets a solid dark one. Identical on both maps: the node is the network, and
    only the routes between them carry a verdict.
    """
    out: list[str] = ["<g>"]
    for placed in placements:
        site = placed.site
        if site.is_eurohpc:
            out.append(
                f'<circle cx="{placed.x:.1f}" cy="{placed.y:.1f}" r="{placed.radius + HPC_RING_GAP:.1f}" '
                f'fill="none" stroke="{HPC_RING}" stroke-width="2" stroke-dasharray="3.5 3" opacity="0.95"/>'
            )
        if placed.is_focus:
            out.append(
                f'<circle cx="{placed.x:.1f}" cy="{placed.y:.1f}" r="{placed.radius + FOCUS_RING_GAP:.1f}" '
                f'fill="none" stroke="{FOCUS_FILL}" stroke-width="2.6"/>'
            )
        fill = FOCUS_FILL if placed.is_focus else NODE_FILL
        out.append(
            f'<circle cx="{placed.x:.1f}" cy="{placed.y:.1f}" r="{placed.radius:.1f}" fill="{fill}" '
            f'stroke="{PAPER}" stroke-width="2.5" filter="url(#soft)"/>'
        )
        out.append(
            f'<text x="{placed.x:.1f}" y="{placed.y + 3.6:.1f}" text-anchor="middle" fill="{PAPER}" '
            f'font-size="10" font-weight="700">{site.optical_degree}</text>'
        )
    out.append("</g>")
    return out


def draw_site_labels(placements: Sequence[SitePlacement]) -> list[str]:
    """The name box, plus a leader line when the placer had to move it far."""
    out: list[str] = [f'<g stroke="{NODE_FILL}" stroke-width="1" opacity="0.45">']
    for placed in placements:
        x, y, width, height = placed.label
        centre_x, centre_y = x + width / 2, y + height / 2
        if math.hypot(centre_x - placed.x, centre_y - placed.y) > placed.radius + 24.0:
            out.append(f'<line x1="{placed.x:.1f}" y1="{placed.y:.1f}" x2="{centre_x:.1f}" y2="{centre_y:.1f}"/>')
    out.append("</g>")

    out.append("<g>")
    for placed in placements:
        x, y, width, height = placed.label
        fill = FOCUS_FILL if placed.is_focus else NODE_FILL
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" rx="9" '
            f'fill="{fill}" opacity="0.96"/>'
        )
        out.append(
            f'<text x="{x + width / 2:.1f}" y="{y + 14.0:.1f}" text-anchor="middle" fill="{PAPER}" '
            f'font-size="12" font-weight="600">{escape(placed.site.name)}</text>'
        )
        if placed.site.eurohpc_name is not None:
            out.append(
                f'<text x="{x + width / 2:.1f}" y="{y + 26.0:.1f}" text-anchor="middle" fill="{HPC_TEXT}" '
                f'font-size="9" font-weight="700">{escape(placed.site.eurohpc_name.upper())}</text>'
            )
    out.append("</g>")
    return out


# ---------------------------------------------------------------------------
# The title block
# ---------------------------------------------------------------------------


def draw_title_block(heading: str, subject: str, footer: str) -> list[str]:
    """Three lines at three sizes: what this is, what it is made of, how to read it.

    The wording is the caller's, all of it. Two SVG artifacts now hang off every
    point of presence and both are a map of Europe, so the heading has to name
    the map's own subject or a reader cannot tell them apart without opening
    both. Escaping is the caller's too, because the caller is the one
    interpolating graph values into these strings.
    """
    return [
        f'<text x="{PAD_LEFT:.0f}" y="44" fill="{INK}" font-size="25" font-weight="700">{heading}</text>',
        f'<text x="{PAD_LEFT:.0f}" y="68" fill="{INK_SOFT}" font-size="13">{subject}</text>',
        f'<text x="{PAD_LEFT:.0f}" y="{CANVAS_HEIGHT - 16:.0f}" fill="{INK_FAINT}" font-size="11">{footer}</text>',
    ]


# ---------------------------------------------------------------------------
# The panel: frame, headings, rules and row rhythm
# ---------------------------------------------------------------------------


def panel_backing(bottom: float) -> str:
    """The rectangle everything in the panel sits on.

    Written last and inserted first, because its height is not known until the
    last total has been laid out. Both callers reserve the slot the same way.
    """
    return (
        f'<rect x="{PANEL_LEFT - 16:.1f}" y="{PANEL_TOP:.0f}" width="{PANEL_WIDTH:.0f}" '
        f'height="{bottom - PANEL_TOP - 4:.0f}" rx="12" fill="{PAPER}" opacity="0.97" stroke="{PANEL_EDGE}"/>'
    )


def panel_heading(y: float, title: str, note: str, note_column: float) -> list[str]:
    """A section heading on the left and a right-aligned note against a column."""
    return [
        f'<text x="{PANEL_LEFT:.1f}" y="{y:.1f}" fill="{INK}" font-size="13" font-weight="700">{title}</text>',
        f'<text x="{PANEL_LEFT + note_column:.1f}" y="{y:.1f}" text-anchor="end" fill="{INK_SOFT}" '
        f'font-size="10.5">{note}</text>',
    ]


def panel_swatch(y: float, colour: str) -> str:
    """The short stroke a legend row is keyed by. Same weight as no route on the
    map, deliberately: the legend is a key to a colour, not a scale drawing."""
    return (
        f'<line x1="{PANEL_LEFT:.1f}" y1="{y - 4:.1f}" x2="{PANEL_LEFT + 28:.1f}" y2="{y - 4:.1f}" '
        f'stroke="{colour}" stroke-width="3.6" stroke-linecap="round"/>'
    )


def panel_caption(y: float, text: str, offset: float = 38.0) -> str:
    """One line of legend prose, indented past whatever it captions."""
    return f'<text x="{PANEL_LEFT + offset:.1f}" y="{y:.1f}" fill="{LEGEND_TEXT}" font-size="11.5">{text}</text>'


def panel_rule(y: float, width: float) -> str:
    """The hairline under a header row and above the totals."""
    return (
        f'<line x1="{PANEL_LEFT:.1f}" y1="{y:.1f}" x2="{PANEL_LEFT + width:.1f}" y2="{y:.1f}" '
        f'stroke="{RULE}" stroke-width="1"/>'
    )


def panel_node_legend(y: float) -> tuple[list[str], float]:
    """The node glyph key: degree, EuroHPC ring, focus ring. Two rows, 30 apart.

    Wording included, unlike every other legend row, because the node glyph is
    this module's and so is what it means. Returns the lines and the y the second
    row landed on, so the caller keeps counting from there.
    """
    out = [
        f'<circle cx="{PANEL_LEFT + 11:.1f}" cy="{y - 4:.1f}" r="10" fill="{NODE_FILL}" stroke="{PAPER}" '
        f'stroke-width="2.2"/>',
        f'<text x="{PANEL_LEFT + 11:.1f}" y="{y - 0.6:.1f}" text-anchor="middle" fill="{PAPER}" font-size="9.5" '
        f'font-weight="700">4</text>',
        panel_caption(y, "optical degree", 30.0),
        f'<circle cx="{PANEL_LEFT + 152:.1f}" cy="{y - 4:.1f}" r="10" fill="{NODE_FILL}" stroke="{PAPER}" '
        f'stroke-width="2.2"/>',
        f'<circle cx="{PANEL_LEFT + 152:.1f}" cy="{y - 4:.1f}" r="15.5" fill="none" stroke="{HPC_RING}" '
        f'stroke-width="2" stroke-dasharray="3.5 3"/>',
        panel_caption(y, "EuroHPC", 172.0),
    ]
    y += 30.0
    out.extend(
        [
            f'<circle cx="{PANEL_LEFT + 11:.1f}" cy="{y - 4:.1f}" r="10" fill="{FOCUS_FILL}" stroke="{PAPER}" '
            f'stroke-width="2.2"/>',
            f'<circle cx="{PANEL_LEFT + 11:.1f}" cy="{y - 4:.1f}" r="14.5" fill="none" stroke="{FOCUS_FILL}" '
            f'stroke-width="2.6"/>',
            panel_caption(y, "the site this map belongs to", 30.0),
        ]
    )
    return out, y


def panel_column_headers(y: float, row_label: str, columns: Sequence[tuple[str, float]]) -> list[str]:
    """The header row: one left-aligned label, then one right-aligned per column.

    The offsets are the caller's and stay there. A shared set of them would make
    this module know that one map measures kilometres and the other slots.
    """
    out = [
        f'<text x="{PANEL_LEFT:.1f}" y="{y:.1f}" fill="{INK_FAINT}" font-size="9.5" font-weight="700" '
        f'letter-spacing="0.6">{row_label}</text>'
    ]
    for label, column in columns:
        out.append(
            f'<text x="{PANEL_LEFT + column:.1f}" y="{y:.1f}" text-anchor="end" fill="{INK_FAINT}" font-size="9.5" '
            f'font-weight="700" letter-spacing="0.6">{label}</text>'
        )
    return out


def panel_totals(y: float, rows: Sequence[tuple[str, str]], width: float) -> tuple[list[str], float]:
    """The label-and-figure rows the panel closes on, 20 apart.

    Returns the y after the last row, which is what `panel_backing` needs.
    """
    out: list[str] = []
    for label, value in rows:
        out.append(f'<text x="{PANEL_LEFT:.1f}" y="{y:.1f}" fill="{INK_SOFT}" font-size="11.5">{escape(label)}</text>')
        out.append(
            f'<text x="{PANEL_LEFT + width:.1f}" y="{y:.1f}" text-anchor="end" fill="{NODE_FILL}" '
            f'font-size="11.5" font-weight="600">{escape(value)}</text>'
        )
        y += 20.0
    return out, y
