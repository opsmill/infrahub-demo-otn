"""`cartography.py`, against points chosen for the failure they catch.

Two bugs are what this file exists for, and both cost real time in the mockup.

A mirrored map. Mercator y grows north, SVG y grows down, and the flip has to
happen exactly once. Done twice, or expressed as a negative scale factor, the
map renders north-down and still looks entirely plausible. Every assertion about
ordering below is there to make that loud.

A dropped label. The placer's fallback is the whole point of it: a site whose
label could not be placed is indistinguishable from a site that is not on the
map, so the tests pin that a position always comes back, and that when the
fallback runs it is the cheapest one.
"""

import math

import pytest

from infrahub_demo_otn.cartography import (
    CROSSING_PENALTY,
    MAX_LATITUDE_DEG,
    Frame,
    Placer,
    mercator,
)

# The mockup canvas: the side panel takes the right-hand 356 px and the title
# bar the top 96, so the drawing area is 1290 by 940 and is not square.
CANVAS = {"width": 1680.0, "height": 1080.0, "pad_left": 34.0, "pad_right": 356.0, "pad_top": 96.0, "pad_bottom": 44.0}
INNER_WIDTH = 1680.0 - 34.0 - 356.0
INNER_HEIGHT = 1080.0 - 96.0 - 44.0

# Five of the real sites, far enough apart to fill a frame.
SITES = [
    (-9.14, 38.72),  # Lisbon
    (4.90, 52.37),  # Amsterdam
    (12.57, 55.68),  # Copenhagen
    (24.94, 60.17),  # Helsinki
    (9.19, 45.46),  # Milan
]


def fitted(points: list[tuple[float, float]] | None = None) -> Frame:
    return Frame.fit(points if points is not None else SITES, **CANVAS)


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


def test_mercator_grows_east_and_north() -> None:
    """The raw projection, before any screen flip. y must rise with latitude."""
    assert mercator(0.0, 0.0) == pytest.approx((0.0, 0.0), abs=1e-12)
    assert mercator(10.0, 0.0)[0] > mercator(-10.0, 0.0)[0]
    assert mercator(0.0, 60.0)[1] > mercator(0.0, 40.0)[1] > mercator(0.0, 0.0)[1]
    assert mercator(0.0, -60.0)[1] < 0.0


def test_mercator_is_monotonic_across_a_sweep_in_both_axes() -> None:
    xs = [mercator(lon, 45.0)[0] for lon in range(-170, 180, 10)]
    ys = [mercator(0.0, float(lat))[1] for lat in range(-80, 81, 10)]
    assert xs == sorted(xs)
    assert ys == sorted(ys)
    assert len(set(xs)) == len(xs)
    assert len(set(ys)) == len(ys)


def test_the_poles_clamp_instead_of_raising() -> None:
    """`log(tan(pi/4 + lat/2))` runs to infinity at 90. No site is near it; nothing raises either."""
    assert mercator(0.0, 90.0) == mercator(0.0, MAX_LATITUDE_DEG)
    assert mercator(0.0, -90.0) == mercator(0.0, -MAX_LATITUDE_DEG)
    assert math.isfinite(mercator(0.0, 90.0)[1])


def test_north_is_up_on_the_canvas() -> None:
    """The bug this whole file is named after.

    Screen y grows downward, so the northern point must come out with the
    *smaller* y. A second flip, or a negative scale, reverses this and draws a
    map that looks fine until someone reads a city name.
    """
    frame = fitted()
    helsinki_y = frame.project(24.94, 60.17)[1]
    lisbon_y = frame.project(-9.14, 38.72)[1]
    assert helsinki_y < lisbon_y


def test_projected_order_follows_geographic_order_in_both_axes() -> None:
    frame = fitted()
    xs = [frame.project(lon, 50.0)[0] for lon in (-10.0, -5.0, 0.0, 5.0, 10.0, 20.0)]
    ys = [frame.project(10.0, lat)[1] for lat in (60.0, 55.0, 50.0, 45.0, 40.0)]
    assert xs == sorted(xs)
    assert ys == sorted(ys)


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------


def test_the_fitted_frame_contains_every_input_point() -> None:
    frame = fitted()
    for lon, lat in SITES:
        assert frame.contains(lon, lat)
        x, y = frame.project(lon, lat)
        assert CANVAS["pad_left"] < x < CANVAS["width"] - CANVAS["pad_right"]
        assert CANVAS["pad_top"] < y < CANVAS["height"] - CANVAS["pad_bottom"]


def test_the_frame_aspect_matches_the_drawing_area_not_the_canvas() -> None:
    """The short axis grows until the projected window matches the drawing area.

    Matching the *canvas* instead would let the side panel eat the map, which is
    the same failure as no fit at all.
    """
    frame = fitted()
    assert frame.aspect == pytest.approx(INNER_WIDTH / INNER_HEIGHT)
    assert frame.aspect != pytest.approx(CANVAS["width"] / CANVAS["height"])
    assert frame.width_rad * frame.scale == pytest.approx(INNER_WIDTH)
    assert frame.height_rad * frame.scale == pytest.approx(INNER_HEIGHT)


def test_the_fit_grows_the_short_axis_and_never_shrinks_the_long_one() -> None:
    """Growing is the only move. Shrinking would put a site outside the frame."""
    points = [(0.0, 40.0), (2.0, 60.0)]  # tall and narrow, so x is the short axis
    frame = Frame.fit(points, **CANVAS)
    assert frame.xmin < mercator(0.0, 40.0)[0]
    assert frame.xmax > mercator(2.0, 60.0)[0]
    assert frame.ymin < mercator(0.0, 40.0)[1]
    assert frame.ymax > mercator(2.0, 60.0)[1]
    assert frame.aspect == pytest.approx(INNER_WIDTH / INNER_HEIGHT)


def test_a_wide_point_set_grows_the_other_axis() -> None:
    frame = Frame.fit([(-30.0, 49.0), (40.0, 51.0)], **CANVAS)
    assert frame.aspect == pytest.approx(INNER_WIDTH / INNER_HEIGHT)
    assert frame.ymin < mercator(0.0, 49.0)[1]
    assert frame.ymax > mercator(0.0, 51.0)[1]


def test_no_fixed_geographic_window_is_used() -> None:
    """FR-016. The bounds come out of the points and nothing else.

    Two ways of saying it. A point set nowhere near Europe frames itself just as
    tightly, and moving the same shape east moves the window with it while the
    pixels stay put, which a hard-coded window could not do.
    """
    europe = fitted()
    pacific = Frame.fit([(160.0, -35.0), (175.0, -41.0), (168.0, -45.0)], **CANVAS)
    assert pacific.xmin > europe.xmax
    for lon, lat in [(160.0, -35.0), (175.0, -41.0), (168.0, -45.0)]:
        assert pacific.contains(lon, lat)

    shifted = Frame.fit([(lon + 100.0, lat) for lon, lat in SITES], **CANVAS)
    assert shifted.xmin > europe.xmin
    assert shifted.width_rad == pytest.approx(europe.width_rad)
    for lon, lat in SITES:
        assert europe.project(lon, lat) == pytest.approx(shifted.project(lon + 100.0, lat))


def test_the_frame_pads_beyond_the_extreme_points() -> None:
    """A site sitting on the frame edge has half its label off the canvas."""
    frame = fitted()
    lons = [mercator(lon, lat)[0] for lon, lat in SITES]
    assert frame.xmin < min(lons)
    assert frame.xmax > max(lons)


def test_a_single_point_still_produces_a_usable_frame() -> None:
    """No extent means no implied scale. Centre it rather than divide by zero."""
    frame = Frame.fit([(4.90, 52.37)], **CANVAS)
    x, y = frame.project(4.90, 52.37)
    assert x == pytest.approx(CANVAS["pad_left"] + INNER_WIDTH / 2)
    assert y == pytest.approx(CANVAS["pad_top"] + INNER_HEIGHT / 2)
    assert frame.aspect == pytest.approx(INNER_WIDTH / INNER_HEIGHT)


def test_points_on_one_meridian_still_produce_a_usable_frame() -> None:
    frame = Frame.fit([(10.0, 40.0), (10.0, 60.0)], **CANVAS)
    assert frame.width_rad > 0
    assert frame.project(10.0, 60.0)[1] < frame.project(10.0, 40.0)[1]


def test_fitting_no_points_raises() -> None:
    with pytest.raises(ValueError, match="no points"):
        Frame.fit([], **CANVAS)


def test_padding_wider_than_the_canvas_raises() -> None:
    with pytest.raises(ValueError, match="no drawing area"):
        Frame.fit(SITES, width=100.0, height=100.0, pad_left=60.0, pad_right=60.0)


# ---------------------------------------------------------------------------
# The placer
# ---------------------------------------------------------------------------


def loaded_placer() -> Placer:
    """One placed label at the origin and one vertical route segment at x=200."""
    placer = Placer()
    placer.block(0.0, 0.0, 100.0, 100.0)
    placer.add_segment(200.0, 0.0, 200.0, 300.0, "oms-a")
    return placer


CROSSING = (190.0, 10.0, 20.0, 20.0)
"""Clear of the placed rectangle, but lying across the route segment."""

NUDGED = (90.0, 90.0, 20.0, 20.0)
"""Clear of the route, overlapping the placed rectangle by 10 by 10."""

BURIED = (50.0, 50.0, 60.0, 60.0)
"""Clear of the route, overlapping the placed rectangle by 50 by 50."""

CLEAN = (400.0, 400.0, 20.0, 20.0)
"""Clear of both."""


def test_a_candidate_across_a_route_is_rejected_and_a_clean_one_taken() -> None:
    """FR-009. A label lying on a fiber route is a hard reject, not a cost.

    The crossing candidate is offered first and is clear of every placed
    rectangle, so only the segment test can turn it down.
    """
    placer = loaded_placer()
    assert placer.free(CROSSING)
    assert placer.crossings(CROSSING) == 1
    rect, payload = placer.place([(CROSSING, "north"), (CLEAN, "south")])
    assert rect == CLEAN
    assert payload == "south"


def test_a_candidate_over_a_placed_label_is_rejected_too() -> None:
    placer = loaded_placer()
    rect, payload = placer.place([(NUDGED, "over"), (CLEAN, "clear")])
    assert rect == CLEAN
    assert payload == "clear"


def test_the_segment_a_label_rides_can_be_ignored() -> None:
    """A distance label belongs on its own route, so that one segment does not count."""
    placer = loaded_placer()
    rect, _ = placer.place([(CROSSING, "on-route")], ignore="oms-a")
    assert rect == CROSSING
    assert placer.crossings(CROSSING, ignore="oms-a") == 0
    assert placer.crossings(CROSSING, ignore="oms-b") == 1


def test_a_position_still_comes_back_when_every_candidate_is_dirty() -> None:
    """The fallback. A dropped label reads as a missing site, so a bad spot beats none."""
    placer = loaded_placer()
    candidates = [(CROSSING, "cross"), (NUDGED, "nudge"), (BURIED, "bury")]
    assert not any(placer.free(rect) and not placer.crossings(rect) for rect, _ in candidates)
    rect, payload = placer.place(candidates)
    assert rect in {CROSSING, NUDGED, BURIED}
    assert payload is not None


def test_the_dirty_position_returned_is_the_cheapest_one() -> None:
    """Overlap area against `CROSSING_PENALTY` per crossed segment.

    The crossing candidate is offered first and would win a first-past-the-post
    fallback. It does not: one crossing costs 900 and the small overlap costs
    100, so the label is nudged onto its neighbour rather than onto the fiber.
    """
    placer = loaded_placer()
    assert placer.cost(CROSSING) == pytest.approx(CROSSING_PENALTY)
    assert placer.cost(NUDGED) == pytest.approx(100.0)
    assert placer.cost(BURIED) == pytest.approx(2500.0)
    rect, payload = placer.place([(CROSSING, "cross"), (NUDGED, "nudge"), (BURIED, "bury")])
    assert rect == NUDGED
    assert payload == "nudge"


def test_a_placed_label_becomes_an_obstacle_for_the_next_one() -> None:
    placer = Placer()
    first, _ = placer.place([(CLEAN, "first")])
    assert first == CLEAN
    assert placer.taken == [CLEAN]
    second, _ = placer.place([(CLEAN, "second"), ((400.0, 500.0, 20.0, 20.0), "below")])
    assert second == (400.0, 500.0, 20.0, 20.0)


def test_the_dirty_fallback_also_reserves_its_rectangle() -> None:
    """Otherwise two labels pile onto the same bad spot instead of two different ones."""
    placer = loaded_placer()
    placer.place([(BURIED, "bury")])
    assert BURIED in placer.taken


def test_a_segment_ending_inside_a_rectangle_counts_as_a_crossing() -> None:
    """A route that stops inside a label crosses no edge. It is still unreadable."""
    placer = Placer()
    placer.add_segment(405.0, 405.0, 500.0, 900.0, "oms-b")
    assert placer.crossings(CLEAN) == 1


def test_a_segment_missing_the_rectangle_entirely_counts_nothing() -> None:
    placer = Placer()
    placer.add_segment(0.0, 0.0, 10.0, 10.0, "oms-c")
    assert placer.crossings(CLEAN) == 0


def test_placing_with_no_candidates_raises() -> None:
    """A caller with nothing to place, not a label the placer could not fit."""
    with pytest.raises(ValueError, match="at least one candidate"):
        Placer().place([])
