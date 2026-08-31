"""The panel is a fixed-width box, and everything drawn in it has to fit.

An SVG `<text>` node does not wrap and does not clip. A string wider than the
box it was written into simply keeps going, over whatever is beside it, and
nothing anywhere reports it. `mapchrome.text_width` already exists and the label
placer already calls it, but no caller checked the panel, so a legend line
measuring 499.7px was drawn into 286px of space and printed across the map for
the length of the legend. It shipped, and it was found by a reader looking at
the documentation site rather than by anything here.

So this module measures the rendered output rather than the source. Both maps,
every text node inside the panel, against the panel's own edges. A new caption,
a longer site name or a widened column all reach it, which a test over string
literals in `mapdraw.py` would not.

The measurement is `text_width`, the same estimate the placer trusts, and its
docstring says it leans high. That is the correct direction for a guard: a
caption this module passes is one the real font renders narrower still.
"""

from __future__ import annotations

from xml.etree import ElementTree

import pytest

from infrahub_demo_otn.mapchrome import PANEL_LEFT, PANEL_WIDTH, text_width
from tests.unit.test_mapdraw import dataset as network_dataset
from tests.unit.test_mapdraw import golden_render
from tests.unit.test_odudraw import dataset as odu_dataset

from infrahub_demo_otn.odudraw import render_odu_map  # isort: skip

SVG_NS = "{http://www.w3.org/2000/svg}"

# The backing rectangle `panel_backing` draws: 16px left of PANEL_LEFT, and
# PANEL_WIDTH across. Nothing the panel draws may leave it.
PANEL_X0 = PANEL_LEFT - 16.0
PANEL_X1 = PANEL_X0 + PANEL_WIDTH


def _panel_texts(svg: str) -> list[tuple[str, float, float, str, str]]:
    """Every text node whose anchor sits inside the panel.

    Returned as (text, x, size, weight, anchor). Nodes left of the panel belong
    to the map, the title block or the footer, and they are measured against a
    different edge or against none at all.
    """
    found: list[tuple[str, float, float, str, str]] = []
    for element in ElementTree.fromstring(svg).iter(f"{SVG_NS}text"):
        content = "".join(element.itertext())
        if not content.strip():
            continue
        try:
            x = float(element.get("x", ""))
        except ValueError:
            continue
        if x < PANEL_X0:
            continue
        size = float(element.get("font-size", "11.5"))
        weight = element.get("font-weight", "400")
        anchor = element.get("text-anchor", "start")
        found.append((content, x, size, weight, anchor))
    return found


def _extent(x: float, width: float, anchor: str) -> tuple[float, float]:
    """The left and right edge a string occupies, given where it is anchored."""
    if anchor == "end":
        return x - width, x
    if anchor == "middle":
        return x - width / 2, x + width / 2
    return x, x + width


RENDERS = {
    "network-map": lambda: golden_render(),
    "odu-map": lambda: render_odu_map(*odu_dataset(), "fra", "main"),
}


@pytest.mark.parametrize("name", sorted(RENDERS))
def test_no_panel_text_overflows_the_panel(name: str) -> None:
    """Nothing drawn in the panel may cross either of its edges.

    The failure this catches is silent in every other test: the render succeeds,
    the bytes are deterministic, the golden matches itself, and the picture is
    unreadable. Only a measurement says so.
    """
    texts = _panel_texts(RENDERS[name]())
    assert texts, f"{name} drew no panel text, so this test measured nothing"

    overflowing = []
    for content, x, size, weight, anchor in texts:
        left, right = _extent(x, text_width(content, size, weight), anchor)
        if left < PANEL_X0 - 0.5 or right > PANEL_X1 + 0.5:
            overflowing.append(f"  {right - PANEL_X1:+7.1f}px past the right edge: {content!r}")

    assert not overflowing, (
        f"{name} draws {len(overflowing)} text node(s) outside its "
        f"{PANEL_WIDTH:.0f}px panel, which prints over the map:\n" + "\n".join(overflowing)
    )


def test_the_measurement_would_catch_a_caption_that_does_not_fit() -> None:
    """The guard above only means something if it can fail.

    A caption starts 38px into the panel, so the room a legend line has is
    PANEL_WIDTH minus that minus the 16px the backing extends left. This asserts
    the arithmetic rather than trusting it, and it is what makes the numbers in
    `mapdraw.py`'s comment checkable.
    """
    room = PANEL_X1 - (PANEL_LEFT + 38.0)
    assert 280.0 < room < 290.0, f"the caption budget moved to {room:.1f}px; the comments quoting 286 are now wrong"

    # The line that shipped over the map, measured at the size it was drawn.
    shipped = "Raman pump on one of its spans, so it is asymmetric and its B to A loss differs"
    assert text_width(shipped, 11.5) > room, "the caption that overflowed no longer measures as overflowing"


def test_both_maps_share_one_panel_geometry() -> None:
    """The two renderers draw their panels from the same constants.

    If they ever stop, the parametrised test above is measuring one map against
    the other's box and would pass while a caption ran off the page.
    """
    network_panel = {round(x) for _, x, _, _, _ in _panel_texts(golden_render())}
    odu_panel = {round(x) for _, x, _, _, _ in _panel_texts(render_odu_map(*odu_dataset(), "fra", "main"))}
    assert network_panel and odu_panel
    assert min(network_panel) == min(odu_panel), (
        "the two panels no longer start at the same x, so they no longer share PANEL_LEFT"
    )


def test_the_network_dataset_is_the_one_the_panel_measures() -> None:
    """A guard on the import above, which is easy to point at the wrong fixture."""
    sites, sections = network_dataset()
    assert len(sites) == 14, f"expected the fourteen PoPs, got {len(sites)}"
    assert len(sections) == 21, f"expected the 21 multiplex sections, got {len(sections)}"
