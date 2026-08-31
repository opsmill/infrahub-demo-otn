"""Four thousand eight hundred gigahertz, and how much of it will take another wavelength.

The question this report used to answer was "how many of the ninety-six channels
are left", and the answer was wrong in the direction that costs money. A
wavelength does not occupy a channel number, it occupies a width centred on one,
and an anchor is only usable when the whole of that width fits inside free
spectrum. `oms-fra-mil` carries 4,134,400 MHz of the 4,800,000 MHz band and has
665,600 MHz free, which is eight times what a 400G carrier occupies. **One more
400G fits on it.** The other seven do not: that free spectrum is in 26 blocks,
one of 152,800 MHz at the top of the band and twenty-five of 38,000 MHz or less,
and only the first is wide enough to hold a 79,600 MHz carrier. Channel 95 is the
one grid position that centres one inside it.

So the report speaks in spectrum. Occupied and free are megahertz, free is a list
of blocks with widths rather than a count, and the fit question is asked per mode
against the width that mode occupies. There is no `lowest_free_channel`, because
the lowest anchor nobody has claimed is not the lowest anchor anything can use.

**Every mode in the catalog gets a row, including the ones that fit nowhere.**
On the shipped dataset all ten modes fit on `oms-fra-mil`, and saying only that
would hide how narrowly. Twenty-five of its twenty-six free blocks are 38,000 MHz
or less and the narrowest mode occupies 44,400, so every one of the ten is
squeezed into the single 152,800 MHz block above 195,972,200 MHz. Eight of them
anchor on channel 95 and nowhere else; only the two 32 GBd modes at 44,400 MHz
also reach channels 94 and 96, and the 150,000 MHz mode fits on 95 with 0 MHz to
spare at the band edge. Spend that block, which `demo/04_odu_ten_in_one.yml` does
with three 44,400 MHz carriers, and the widest free block drops to 38,000 MHz and
all ten modes fit nowhere at once. A report that listed only the modes that fit
would print ten rows on one branch and none on the other, and read as though
nobody had asked. The refusal is the answer an operator came for, so it is
written out.

**Nothing here derives occupancy a second time.** The intervals come from
`plant.occupancy_from_graphql`, the sweep from `units.free_blocks`, the route's
free spectrum from `routing.route_free_blocks` and the usable anchors from
`routing.fitting_channels`. Those are the same four the collision check and the
generator call. A report and an allocator that compute what is taken separately
is how one hands out spectrum the other says is lit.

**Per section is an upper bound, not the answer.** An operator does not provision
onto a section, they provision onto a route, and a wavelength holds its width for
the whole length of every section it crosses. The report shows the route
intersection for the corridors that matter.

**Contested means overlap, not a shared anchor.** Two carriers used to be in
conflict when they claimed the same channel number. They are in conflict when
their intervals cover the same megahertz, which two different anchors can do: a
128 GBd carrier occupies 150,000 MHz and reaches three grid positions either
side of its own centre. `infrahubctl check channel_collision` is what blocks it.

**It names the branch it read.** Every figure below is a property of one branch.
Provision two services onto the flagship corridor and the same command reports
less free spectrum, and both answers are right.
"""

from typing import Any, Mapping, Sequence

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.impact import kilometres, section_lengths_m
from infrahub_demo_otn.plant import CarrierInterval, modes_from_graphql, occupancy_from_graphql, overlap_range
from infrahub_demo_otn.routing import ModeCandidate, eligible_modes, fitting_channels, route_free_blocks
from infrahub_demo_otn.units import (
    CBAND_EXTENT_MHZ,
    CBAND_LOWER_EDGE_MHZ,
    CBAND_UPPER_EDGE_MHZ,
    GRID_CHANNEL_COUNT,
    GRID_SPACING_MHZ,
    FreeBlock,
    free_blocks,
    occupied_width_mhz,
)

NEXT_SERVICE_RATE_GBPS = 400
"""The rate the demo asks about.

It is no longer true that a channel is a channel. The rate picks the mode, the
mode's symbol rate sets the width, and the width decides which anchors are
usable, so this figure now changes the answer rather than only the story.
"""

WATCHED_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Amsterdam to Milan", ("oms-ams-fra", "oms-fra-mil")),
    ("Frankfurt to Milan direct", ("oms-fra-mil",)),
    ("Frankfurt to Milan via Geneva", ("oms-fra-gva", "oms-gva-mil")),
    ("Berlin to Amsterdam via Hamburg", ("oms-ham-ber", "oms-ams-ham")),
)
"""The corridors the demo talks about, so the route answer is not hypothetical.

Named here rather than discovered, because "which routes matter" is a question
about the demo script and not about the data. A route whose sections the branch
does not carry is reported as unresolvable rather than skipped.
"""

UPPER_BOUND_NOTE = (
    "A per-section free figure is an upper bound for any route through that section. A wavelength holds its "
    "width on every section it crosses, so a route's free spectrum is the band minus the union of its sections' "
    "occupancy, which is never wider and is usually narrower."
)

QUANTISATION_NOTE = (
    "Free spectrum overstates capacity, because anchors are quantised onto the 50 GHz grid. A carrier can only "
    "be centred on a grid position, and it is usable only when the whole width it occupies fits inside one free "
    "block. Dividing free megahertz by a carrier's width counts fragments that no anchor reaches into, so the "
    "anchor count below is the answer and the megahertz are the context."
)

OVERLAP_NOTE = (
    "More than one carrier's interval covers the same spectrum on these sections. Occupancy above counts the "
    "union, so the free figures are still correct and the collision is still real. Two carriers no longer have "
    "to share an anchor to collide: a 128 GBd carrier occupies 150,000 MHz and reaches three grid positions "
    "either side of its own centre. `infrahubctl check channel_collision` is what blocks it."
)

MODE_FIT_NOTE = (
    "`mode_fit` carries one row for every mode in the catalog, on every section and every resolvable route, and "
    "a mode that fits nowhere keeps its row and says so. Omitting it would read as a mode nobody asked about "
    "rather than as a refusal, and the refusal is the answer. Rows are ordered by the width the mode occupies, "
    "narrowest first, because that is the order a planner relaxes a request in."
)

EMPTY_CATALOG_NOTE = (
    "This branch carries no optical mode at all. A fit verdict is a comparison of two widths and one of them is "
    "missing, so `mode_fit` is empty on every section and route below, and that is an unanswerable question "
    "rather than a negative answer."
)

FIT_YES = "fits"
"""At least one anchor puts the whole of this mode's width inside one free block."""

FIT_TOO_NARROW = "too-narrow"
"""No free block is as wide as the mode. A spectrum problem: the answer is a narrower mode or another route."""

FIT_FRAGMENTED = "fragmented"
"""A block is wide enough and no 50 GHz grid position sits inside it. A defrag problem, not a spectrum one."""


def _block_rows(blocks: Sequence[FreeBlock]) -> list[dict[str, Any]]:
    """Free spectrum as blocks with widths, which is what replaced the free count.

    Ascending by lower edge, because that is the order `units.free_blocks`
    produces and a planner reads a spectrum plan from the bottom of the band up.
    """
    return [
        {"lower_mhz": block.lower_mhz, "upper_mhz": block.upper_mhz, "width_mhz": block.width_mhz} for block in blocks
    ]


def _widest(blocks: Sequence[FreeBlock]) -> int:
    """The widest free block, or zero on a section with none.

    Zero rather than `None`: a section with no free block has no spectrum free,
    and every comparison below reads "less than any width" correctly off zero.
    """
    return max((block.width_mhz for block in blocks), default=0)


def _overlap_rows(intervals: Sequence[CarrierInterval]) -> list[dict[str, Any]]:
    """Every pair of carriers on one section whose spectrum overlaps.

    The inner loop stops at the first interval starting at or after the outer
    one's upper edge. `plant.occupancy_from_graphql` sorts by lower edge, so
    everything after that point starts later still and cannot reach back.

    `plant.overlap_range` measures the shared range, and it decides through
    `plant.intervals_overlap`, so a pair that merely touches comes back as no
    overlap here for the same reason it does in the check.
    """
    found: list[dict[str, Any]] = []
    for index, left in enumerate(intervals):
        for right in intervals[index + 1 :]:
            if right.lower_mhz >= left.upper_mhz:
                break
            shared = overlap_range(left, right)
            if shared is None:
                continue
            lower, upper = shared
            found.append(
                {
                    "carriers": [left.carrier, right.carrier],
                    "channels": [left.channel, right.channel],
                    "modes": [left.mode, right.mode],
                    "lower_mhz": lower,
                    "upper_mhz": upper,
                    "width_mhz": upper - lower,
                }
            )
    return found


def _fit_reason(width_mhz: int, blocks: Sequence[FreeBlock], anchors: Sequence[int]) -> str:
    """Which of the three outcomes a mode reached against these blocks.

    One decision, read by both sentences below. The headline answer and the
    per-mode row say different things about the same verdict, and deriving that
    verdict twice is how the two come to disagree on the branch nobody tested.

    Three outcomes and not two, because "no" has two readings and they call for
    different work. No block wide enough is a spectrum problem and the answer is
    a narrower mode or a different route. A block wide enough that no grid
    position sits inside is a fragmentation problem, and the answer is a defrag
    of the plan. Collapsing them into one "no" tells an operator to go looking in
    the wrong place.
    """
    if anchors:
        return FIT_YES
    return FIT_TOO_NARROW if _widest(blocks) < width_mhz else FIT_FRAGMENTED


def _mode_verdict(
    mode: ModeCandidate,
    blocks: Sequence[FreeBlock],
    anchors: Sequence[int],
    where: str,
) -> dict[str, Any]:
    """One catalog mode against this free spectrum, whether or not it fits.

    The anchors themselves are not carried. A mode that fits on 25 of the 96
    positions would print 25 numbers per mode per section, and the two figures a
    planner reads off this row are how many there are and which is the lowest.
    `anchors_for_another_400g` on the row above still carries the full list for
    the one rate the demo asks about.
    """
    width = occupied_width_mhz(mode.baud_mbaud)
    reason = _fit_reason(width, blocks, anchors)
    widest = _widest(blocks)
    if reason == FIT_YES:
        verdict = (
            f"Fits {where}, on {len(anchors)} of the {GRID_CHANNEL_COUNT} anchors, the lowest being channel "
            f"{anchors[0]}. A {mode.name} carrier occupies {width:,} MHz."
        )
    elif reason == FIT_TOO_NARROW:
        verdict = (
            f"Does not fit {where}. A {mode.name} carrier occupies {width:,} MHz and the widest free block is "
            f"{widest:,} MHz, so no anchor can take one."
        )
    else:
        verdict = (
            f"Does not fit {where}. A {mode.name} carrier occupies {width:,} MHz and the widest free block is "
            f"{widest:,} MHz, which is wider, but no 50 GHz grid position centres one inside a free block."
        )
    return {
        "mode": mode.name,
        "mode_class": mode.mode_class,
        "line_rate_gbps": mode.line_rate_gbps,
        "baud_mbaud": mode.baud_mbaud,
        "occupied_width_mhz": width,
        "fits": reason == FIT_YES,
        "reason": reason,
        "anchor_count": len(anchors),
        "lowest_anchor": anchors[0] if anchors else None,
        "verdict": verdict,
    }


def _mode_fit_rows(catalog: Sequence[ModeCandidate], blocks: Sequence[FreeBlock], where: str) -> list[dict[str, Any]]:
    """Every mode in the catalog against this free spectrum, narrowest width first.

    Sorted on the occupied width and then the name, so the order is the order a
    planner relaxes a request in and does not depend on catalog order. Two modes
    at one symbol rate occupy the same width and the name breaks the tie.
    """
    ordered = sorted(catalog, key=lambda candidate: (occupied_width_mhz(candidate.baud_mbaud), candidate.name))
    return [_mode_verdict(mode, blocks, fitting_channels(blocks, mode.baud_mbaud), where) for mode in ordered]


def _fit_answer(mode: ModeCandidate | None, blocks: Sequence[FreeBlock], anchors: Sequence[int], where: str) -> str:
    """Whether another carrier at the asked rate fits, and why not when it does not.

    The three outcomes come from `_fit_reason`, which the per-mode rows read too.
    """
    if mode is None:
        return (
            f"Cannot be answered on this branch: no transponder mode in the catalog reaches "
            f"{NEXT_SERVICE_RATE_GBPS}G, so there is no width to test {where} against."
        )
    width = occupied_width_mhz(mode.baud_mbaud)
    free = sum(block.width_mhz for block in blocks)
    reason = _fit_reason(width, blocks, anchors)
    if reason == FIT_YES:
        return (
            f"Yes, on channel {anchors[0]}. A {mode.name} carrier occupies {width:,} MHz, {free:,} MHz is free "
            f"{where} in {len(blocks)} blocks, and {len(anchors)} of the {GRID_CHANNEL_COUNT} anchors can take "
            f"one. Free spectrum divided by width would have said {free // width}."
        )
    widest = _widest(blocks)
    if reason == FIT_TOO_NARROW:
        return (
            f"No. The widest free block {where} is {widest:,} MHz and a {mode.name} carrier occupies "
            f"{width:,} MHz. {free:,} MHz is free in total, in {len(blocks)} blocks, and none of them is wide "
            f"enough."
        )
    return (
        f"No. The widest free block {where} is {widest:,} MHz, which is wider than the {width:,} MHz a "
        f"{mode.name} carrier occupies, but no 50 GHz grid position centres one inside a free block. The "
        f"spectrum is there and it is fragmented."
    )


def _section_row(
    name: str,
    intervals: Sequence[CarrierInterval],
    length_m: int,
    mode: ModeCandidate | None,
    catalog: Sequence[ModeCandidate],
) -> dict[str, Any]:
    blocks = free_blocks(intervals)
    free_mhz = sum(block.width_mhz for block in blocks)
    occupied_mhz = CBAND_EXTENT_MHZ - free_mhz
    anchors = fitting_channels(blocks, mode.baud_mbaud) if mode is not None else ()
    return {
        "section": name,
        "length_m": length_m,
        "length_display": kilometres(length_m),
        "band_extent_mhz": CBAND_EXTENT_MHZ,
        "occupied_mhz": occupied_mhz,
        "free_mhz": free_mhz,
        "utilisation_percent": round(100.0 * occupied_mhz / CBAND_EXTENT_MHZ, 1),
        "carrier_count": len(intervals),
        # The intervals, not a set of anchors. The anchor rides along because it
        # is what an operator says out loud, and the edges are what the plan is
        # drawn against.
        "carriers": [
            {
                "carrier": interval.carrier,
                "channel": interval.channel,
                "mode": interval.mode,
                "center_mhz": interval.center_mhz,
                "lower_mhz": interval.lower_mhz,
                "upper_mhz": interval.upper_mhz,
                "width_mhz": interval.width_mhz,
            }
            for interval in intervals
        ],
        "free_blocks": _block_rows(blocks),
        "free_block_count": len(blocks),
        "widest_free_block_mhz": _widest(blocks),
        "anchors_for_another_400g": list(anchors),
        "another_400g_fits": bool(anchors),
        "answer": _fit_answer(mode, blocks, anchors, "on this section"),
        "mode_fit": _mode_fit_rows(catalog, blocks, "on this section"),
        "overlaps": _overlap_rows(intervals),
    }


def _route_row(
    label: str,
    sections: tuple[str, ...],
    occupancy: Mapping[str, tuple[CarrierInterval, ...]],
    known: frozenset[str],
    mode: ModeCandidate | None,
    catalog: Sequence[ModeCandidate],
) -> dict[str, Any]:
    missing = [name for name in sections if name not in known]
    if missing:
        return {
            "route": label,
            "sections": list(sections),
            "resolvable": False,
            # No `mode_fit` key at all rather than an empty one. An unresolvable
            # route has no free spectrum to test a width against, and an empty
            # list here would read as ten modes that all fit nowhere.
            "answer": f"Cannot be answered on this branch: {', '.join(missing)} is not present.",
        }
    blocks = route_free_blocks(sections, occupancy)
    free_mhz = sum(block.width_mhz for block in blocks)
    per_section = {name: sum(block.width_mhz for block in free_blocks(occupancy.get(name, ()))) for name in sections}
    anchors = fitting_channels(blocks, mode.baud_mbaud) if mode is not None else ()
    return {
        "route": label,
        "sections": list(sections),
        "resolvable": True,
        "free_mhz_per_section": per_section,
        "best_single_section_free_mhz": max(per_section.values()),
        "route_free_mhz": free_mhz,
        "free_blocks": _block_rows(blocks),
        "free_block_count": len(blocks),
        "widest_free_block_mhz": _widest(blocks),
        "anchors_for_another_400g": list(anchors),
        "another_400g_fits": bool(anchors),
        "answer": _fit_answer(mode, blocks, anchors, "on every section of this route"),
        "mode_fit": _mode_fit_rows(catalog, blocks, "on every section of this route"),
    }


class CapacityViewTransform(InfrahubTransform):
    query = "channel_occupancy"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        lengths = section_lengths_m(data)
        if not lengths:
            raise ValueError("this branch carries no optical multiplex section, so there is no spectrum to report")
        occupancy = occupancy_from_graphql(data)
        known = frozenset(lengths)

        # The narrowest mode that reaches the rate, which is what a planner would
        # pick and is the most optimistic answer the branch admits. A wider mode
        # at the same rate fits in fewer places, never more, so a "no" here is a
        # "no" for every 400G mode in the catalog.
        # The whole catalog, transponders and pluggables alike, because a
        # pluggable's width is as real as a transponder's and "800ZR fits
        # nowhere on this corridor" is a result rather than a gap. The rate
        # question below narrows to the transponders that reach 400G.
        catalog = modes_from_graphql(data)
        candidates = eligible_modes(catalog, NEXT_SERVICE_RATE_GBPS)
        mode = candidates[0] if candidates else None

        rows = [
            _section_row(name, occupancy.get(name, ()), length_m, mode, catalog) for name, length_m in lengths.items()
        ]
        rows.sort(key=lambda row: (-row["occupied_mhz"], row["section"]))
        routes = [_route_row(label, sections, occupancy, known, mode, catalog) for label, sections in WATCHED_ROUTES]

        busiest = rows[0]
        empty = [row["section"] for row in rows if row["occupied_mhz"] == 0]
        contested = [row for row in rows if row["overlaps"]]

        # A mode that fits on no section fits on no route either, since a route's
        # free spectrum is the intersection of its sections'. Named at the top
        # because it is the one negative result an operator should not have to
        # read twenty-one section rows to find.
        fits_somewhere = {fit["mode"] for row in rows for fit in row["mode_fit"] if fit["fits"]}
        fits_nowhere = [fit["mode"] for fit in busiest["mode_fit"] if fit["mode"] not in fits_somewhere]
        blocked_on_busiest = [fit["mode"] for fit in busiest["mode_fit"] if not fit["fits"]]

        return {
            "branch": self.branch_name,
            "band": {
                "extent_mhz": CBAND_EXTENT_MHZ,
                "lower_edge_mhz": CBAND_LOWER_EDGE_MHZ,
                "upper_edge_mhz": CBAND_UPPER_EDGE_MHZ,
                "grid_spacing_mhz": GRID_SPACING_MHZ,
                "anchor_count": GRID_CHANNEL_COUNT,
                "note": (
                    "Capacity is derived, never stored. Occupied is the union of the intervals the carriers "
                    "crossing a section hold; free is the band minus that union, as blocks. The anchor count is "
                    "how many 50 GHz grid positions a carrier may be centred on, not how many wavelengths fit."
                ),
            },
            "rate_asked_gbps": NEXT_SERVICE_RATE_GBPS,
            "mode_asked_about": (
                None
                if mode is None
                else {
                    "name": mode.name,
                    "baud_mbaud": mode.baud_mbaud,
                    "occupied_width_mhz": occupied_width_mhz(mode.baud_mbaud),
                }
            ),
            "section_count": len(rows),
            "headline": (
                f"{busiest['section']} is the busiest section on {self.branch_name}: "
                f"{busiest['occupied_mhz']:,} of {CBAND_EXTENT_MHZ:,} MHz occupied, {busiest['free_mhz']:,} MHz "
                f"free in {busiest['free_block_count']} blocks, the widest "
                f"{busiest['widest_free_block_mhz']:,} MHz. {len(blocked_on_busiest)} of the {len(catalog)} catalog "
                f"modes fit nowhere on it. {len(empty)} of {len(rows)} sections carry no wavelength at all."
            ),
            "upper_bound_note": UPPER_BOUND_NOTE,
            "quantisation_note": QUANTISATION_NOTE,
            "mode_fit_note": EMPTY_CATALOG_NOTE if not catalog else MODE_FIT_NOTE,
            "mode_catalog_size": len(catalog),
            "modes_that_fit_nowhere": fits_nowhere,
            "modes_blocked_on_busiest_section": blocked_on_busiest,
            "sections": rows,
            "routes": routes,
            "empty_sections": empty,
            "contested": [{"section": row["section"], "overlaps": row["overlaps"]} for row in contested],
            "contested_note": OVERLAP_NOTE if contested else None,
        }
