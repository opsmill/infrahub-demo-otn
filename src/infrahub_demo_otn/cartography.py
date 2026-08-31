"""Map geometry: a Mercator projection fitted to its own points, and a label placer.

Geometry only. No colours, no palette, no SVG and no `infrahub_sdk`, so the two
things most likely to be wrong on a map, where a dot lands and where its label
lands, can both be asserted with no server and no renderer.

Two decisions are worth stating, because both were found the hard way in the
approved mockup and neither is obvious from the code alone.

**The frame is fitted, not fixed.** A hard-coded geographic window wastes half
the canvas on empty sea and silently crops a site added outside it. `Frame.fit`
takes the points, pads them, and then grows the *short* axis until the frame
matches the canvas aspect, so the map fills the drawing area at one scale in
both directions.

**North is up because the y axis is flipped once, here.** Mercator y grows
northward and SVG y grows downward, so the projection reads `ymax - my` and not
`my - ymin`. Getting that backwards mirrors the whole map top to bottom, and the
result still looks like a map, which is what makes it expensive. That is why the
frame carries an explicit `ymax` rather than a signed scale factor: a negative
scale would express the same flip in a form nothing can assert.

Everything here is plain float pixel geometry. No value in this module carries a
physical unit, so no conversion belongs in it. Units live in `units.py`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence, TypeVar

MAX_LATITUDE_DEG = 85.05112878
"""Where Mercator is cut off.

`log(tan(pi/4 + lat/2))` runs to infinity at the pole. Web Mercator stops at the
latitude where the projected world is square, and clamping to it turns a pole
into a very high point instead of a domain error. No site in this dataset is
anywhere near it; the clamp is here so a bad input degrades rather than raises.
"""

FIT_MARGIN = 0.08
"""Padding added to each side of the fitted extent, as a fraction of it.

Eight percent is what keeps the outermost site's label inside the drawing area
rather than half off the edge.
"""

DEGENERATE_SPAN = 1e-6
"""The extent given to an axis whose points all share one coordinate.

One site, or several on the same meridian, would otherwise divide by zero. The
value is arbitrary and only has to be non-zero: with a single point there is no
scale the data can imply, so the fit centres it and any scale is as right as any
other.
"""

CROSSING_PENALTY = 900.0
"""Cost charged per route segment a label rectangle crosses.

Large against a plausible overlap area in square pixels, on purpose. Two labels
touching at a corner is untidy; a label lying across a fiber route makes both the
label and the route unreadable, so the placer gives up a lot of overlap before it
accepts one crossing.
"""

Rect = tuple[float, float, float, float]
"""`(x, y, width, height)`, in pixels, y downward."""

Payload = TypeVar("Payload")


def mercator(lon_deg: float, lat_deg: float) -> tuple[float, float]:
    """Spherical Mercator, in radians. y grows northward.

    The flip to screen coordinates does not happen here. This is the projection
    on its own so it can be asserted on its own: y must rise with latitude.
    """
    lat = max(-MAX_LATITUDE_DEG, min(MAX_LATITUDE_DEG, lat_deg))
    return math.radians(lon_deg), math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


@dataclass(frozen=True)
class Frame:
    """A projected window and the transform that puts it on the canvas.

    The bounds are in Mercator radians and the offsets and scale are in pixels.
    `scale` is positive in both axes; the north-up flip lives in `project`.
    """

    xmin: float
    xmax: float
    ymin: float
    ymax: float
    scale: float
    offset_x: float
    offset_y: float

    @property
    def width_rad(self) -> float:
        return self.xmax - self.xmin

    @property
    def height_rad(self) -> float:
        return self.ymax - self.ymin

    @property
    def aspect(self) -> float:
        """Projected width over projected height. Matches the drawing area after a fit."""
        return self.width_rad / self.height_rad

    @classmethod
    def fit(
        cls,
        points: Sequence[tuple[float, float]],
        *,
        width: float,
        height: float,
        pad_left: float = 0.0,
        pad_right: float = 0.0,
        pad_top: float = 0.0,
        pad_bottom: float = 0.0,
        margin: float = FIT_MARGIN,
    ) -> Frame:
        """Fit the projection to `points`, then grow the short axis to the canvas aspect.

        `points` are `(lon, lat)` in degrees. The paddings carve the drawing area
        out of the `width` by `height` canvas, which is how the side panel and
        the title bar keep the map off themselves.

        Order matters. Pad first, then grow: growing first and padding after
        would push the outermost points back towards the edge and undo the
        aspect match.
        """
        if not points:
            raise ValueError("a frame cannot be fitted to no points")
        inner_width = width - pad_left - pad_right
        inner_height = height - pad_top - pad_bottom
        if inner_width <= 0 or inner_height <= 0:
            raise ValueError("the padding leaves no drawing area inside the canvas")

        projected = [mercator(lon, lat) for lon, lat in points]
        xmin = min(point[0] for point in projected)
        xmax = max(point[0] for point in projected)
        ymin = min(point[1] for point in projected)
        ymax = max(point[1] for point in projected)

        # An axis with no extent has no implied scale. Give it one rather than
        # dividing by zero below.
        if xmax - xmin <= 0:
            xmin, xmax = xmin - DEGENERATE_SPAN / 2, xmax + DEGENERATE_SPAN / 2
        if ymax - ymin <= 0:
            ymin, ymax = ymin - DEGENERATE_SPAN / 2, ymax + DEGENERATE_SPAN / 2

        pad_x, pad_y = (xmax - xmin) * margin, (ymax - ymin) * margin
        xmin, xmax = xmin - pad_x, xmax + pad_x
        ymin, ymax = ymin - pad_y, ymax + pad_y

        aspect = inner_width / inner_height
        if (xmax - xmin) / (ymax - ymin) < aspect:
            grow = ((ymax - ymin) * aspect - (xmax - xmin)) / 2
            xmin, xmax = xmin - grow, xmax + grow
        else:
            grow = ((xmax - xmin) / aspect - (ymax - ymin)) / 2
            ymin, ymax = ymin - grow, ymax + grow

        scale = min(inner_width / (xmax - xmin), inner_height / (ymax - ymin))
        return cls(
            xmin=xmin,
            xmax=xmax,
            ymin=ymin,
            ymax=ymax,
            scale=scale,
            offset_x=pad_left + (inner_width - (xmax - xmin) * scale) / 2,
            offset_y=pad_top + (inner_height - (ymax - ymin) * scale) / 2,
        )

    def project(self, lon_deg: float, lat_deg: float) -> tuple[float, float]:
        """Degrees to pixels, y downward, north up.

        `ymax - my` is the flip, and it is the only one. Read it as the distance
        down from the top of the frame.
        """
        mx, my = mercator(lon_deg, lat_deg)
        return self.offset_x + (mx - self.xmin) * self.scale, self.offset_y + (self.ymax - my) * self.scale

    def contains(self, lon_deg: float, lat_deg: float) -> bool:
        """Whether a point falls inside the fitted window, in projected space."""
        mx, my = mercator(lon_deg, lat_deg)
        return self.xmin <= mx <= self.xmax and self.ymin <= my <= self.ymax


class Placer:
    """Greedy rectangle placement against two kinds of obstacle.

    Obstacles are rectangles already placed, and the route segments themselves. A
    label lying across a fiber route is as unreadable as one lying across another
    label, so both are hard constraints first: the first candidate clean of both
    wins outright. Only when nothing clean is left does the placer fall back to
    scoring, at which point overlap area and `CROSSING_PENALTY` per crossed
    segment decide it.

    A label is never dropped. An unplaced site label is a hole in the map that no
    reader can tell apart from a site that is not there, so a bad position beats
    no position and `place` always returns one.

    Candidates are swept in the order given and ties go to the earlier one, so
    the same input places the same way every run.
    """

    def __init__(self) -> None:
        self.taken: list[Rect] = []
        self.segments: list[tuple[float, float, float, float, object]] = []

    def block(self, x: float, y: float, width: float, height: float) -> None:
        """Reserve a rectangle. Used for placed labels and for permanent furniture."""
        self.taken.append((x, y, width, height))

    def add_segment(self, x1: float, y1: float, x2: float, y2: float, key: object) -> None:
        """Add a route segment as an obstacle.

        `key` names what the segment belongs to. A distance label riding its own
        route passes that key as `ignore`, so the segment it is meant to sit on
        does not count against it.
        """
        self.segments.append((x1, y1, x2, y2, key))

    @staticmethod
    def _segment_hits_rect(x1: float, y1: float, x2: float, y2: float, rect: Rect) -> bool:
        x, y, width, height = rect
        if x <= x1 <= x + width and y <= y1 <= y + height:
            return True
        if x <= x2 <= x + width and y <= y2 <= y + height:
            return True

        def orient(px: float, py: float, qx: float, qy: float, rx: float, ry: float) -> int:
            value = (qx - px) * (ry - py) - (qy - py) * (rx - px)
            return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)

        def crosses(ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float) -> bool:
            first = orient(ax, ay, bx, by, cx, cy)
            second = orient(ax, ay, bx, by, dx, dy)
            third = orient(cx, cy, dx, dy, ax, ay)
            fourth = orient(cx, cy, dx, dy, bx, by)
            return first != second and third != fourth

        edges = (
            (x, y, x + width, y),
            (x + width, y, x + width, y + height),
            (x + width, y + height, x, y + height),
            (x, y + height, x, y),
        )
        return any(crosses(x1, y1, x2, y2, *edge) for edge in edges)

    def crossings(self, rect: Rect, ignore: object = None) -> int:
        """How many route segments the rectangle sits across.

        A segment whose key equals `ignore` is skipped. `ignore=None` skips
        nothing, so a segment registered with no key still counts.
        """
        total = 0
        for x1, y1, x2, y2, key in self.segments:
            if ignore is not None and key == ignore:
                continue
            if self._segment_hits_rect(x1, y1, x2, y2, rect):
                total += 1
        return total

    def free(self, rect: Rect, pad: float = 3.0) -> bool:
        """Whether the rectangle clears every placed rectangle, with breathing room."""
        x, y, width, height = rect
        for a, b, c, d in self.taken:
            if x < a + c + pad and a < x + width + pad and y < b + d + pad and b < y + height + pad:
                return False
        return True

    def overlap(self, rect: Rect) -> float:
        """Total area shared with placed rectangles, in square pixels."""
        x, y, width, height = rect
        total = 0.0
        for a, b, c, d in self.taken:
            shared_x = min(x + width, a + c) - max(x, a)
            shared_y = min(y + height, b + d) - max(y, b)
            if shared_x > 0 and shared_y > 0:
                total += shared_x * shared_y
        return total

    def cost(self, rect: Rect, ignore: object = None) -> float:
        """What a dirty candidate is worth: overlap area plus a heavy per-crossing charge."""
        return self.overlap(rect) + CROSSING_PENALTY * self.crossings(rect, ignore)

    def place(self, candidates: Sequence[tuple[Rect, Payload]], ignore: object = None) -> tuple[Rect, Payload]:
        """Take the first clean candidate, else the cheapest, and reserve it.

        `candidates` are `(rect, payload)` in sweep order, where the payload is
        whatever the caller needs back with the position, such as the anchor and
        the text baseline that go with it.

        Raises on an empty sequence. That is a caller with nothing to place, not
        a label the placer failed to fit, and returning `None` for it would put a
        `None` check on the one path that is meant to be total.
        """
        if not candidates:
            raise ValueError("place needs at least one candidate position")
        for rect, payload in candidates:
            if self.free(rect) and not self.crossings(rect, ignore):
                self.block(*rect)
                return rect, payload
        rect, payload = min(candidates, key=lambda candidate: self.cost(candidate[0], ignore))
        self.block(*rect)
        return rect, payload
