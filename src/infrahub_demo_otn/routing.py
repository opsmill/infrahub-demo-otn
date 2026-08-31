"""Choose one route, one mode and one channel, the same way every time.

`budget.py` answers "does this route close". This module answers "which route,
and on what". It is separate from the generator for the same reason `budget.py`
is separate from the check: a pure function over plain data can be tested
against the shipped dataset with no server and no Docker.

Nothing here imports `infrahub_sdk`. Routes arrive as tuples of section names,
occupancy arrives as a mapping of section name to the spectrum in use on it, and
the result is a frozen dataclass. `generators/optical_service.py` is what turns a
GraphQL payload and a path traversal into these inputs.

**A channel is free for a width, not on its own.** Occupancy is a list of
half-open intervals per section rather than a set of channel numbers, because a
wavelength occupies a width of spectrum and the width comes from the mode. A
channel that can anchor a 32 GBd carrier cannot necessarily anchor a 128 GBd one,
so every question about availability here takes a symbol rate.

**Determinism is a requirement, not a nicety.** The demo has to reproduce, so
every ordering in this module is total. Routes are ranked by section count, then
by margin, then by whether they have an anchor wide enough for the mode at all,
then by the channel they would take, and finally by the route's own key, which is
unique. Modes are
ranked by baud rate, then by margin, then by name. No comparison can end in a tie
and no result depends on the order the server returned the paths in.

**A free channel is a precondition for lighting a wavelength, not for using one.**
`choose_route` takes `require_free_channel` because the caller is the only one
that knows which of the two it is doing. See that function for what the flag
changes and why the gate could not stay unconditional.

**The cheapest mode, not the best one.** Ranking modes by margin alone would
always land on DP-QPSK, because halving the constellation buys about five
decibels and doubles the spectrum the carrier occupies. The lower-order fallback
is a cost, so the narrowest mode that closes wins and a wider one is used only
when the narrow one fails.

**Transponder modes only.** The coherent pluggables in the catalog are
terminated by the router itself, and the routers in this dataset carry grey
optics: no centre frequency, -2 dBm, no line side. A ZR wavelength has nowhere
to originate here, so filtering by `mode_class` states that as equipment rather
than discovering it as a strange OSNR result.

**Reach is not a gate.** `OtnOpticalMode.nominal_reach_m` is described in its own
schema as "a starting point for the budget, not a guarantee", and the budget
already accepts the 1010 km Frankfurt route on a mode whose quoted reach is
1000 km. The budget is the gate. Adding a reach gate on top would overrule the
arithmetic with the datasheet it was built to replace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from infrahub_demo_otn.budget import (
    ModeInput,
    PathBudget,
    SectionInput,
    evaluate_both_directions,
    worse_direction,
)
from infrahub_demo_otn.units import (
    GRID_CHANNEL_COUNT,
    FreeBlock,
    SpectralInterval,
    carrier_interval_mhz,
    channel_to_frequency_mhz,
    free_blocks,
    m_to_km,
    mdb_to_db,
    ns_to_us,
    occupied_width_mhz,
)

TRANSPONDER = "transponder"
"""The `mode_class` a service may be provisioned on. See the module docstring."""

REASON_NO_ROUTE = "no-route"
"""Path traversal found nothing between the two endpoints."""

REASON_NO_MODE = "no-mode"
"""No transponder mode in the catalog reaches the requested line rate."""

REASON_BUDGET = "budget"
"""Every route failed OSNR, dispersion or amplifier gain at every mode."""

REASON_CAPACITY = "capacity"
"""Every route had no anchor whose interval was free on all of its sections.

Only reachable when the caller asks for a free channel. A caller that may groom
into a wavelength somebody else already lit passes `require_free_channel=False`
and gets the route back as a candidate with no channel instead of this refusal:
see `choose_route`.

The rejection's `detail` says which of the two conditions below applied, because
"the corridor is full" and "the corridor has room for a narrower wavelength than
the one you asked for" send an operator to two different places.
"""

CHANNEL_NO_SPECTRUM = "no-spectrum"
"""Not one megahertz is free on every section of the route.

The first of the two readings of `Selection.channel is None`. It is a fact about
the route and holds for every mode, so no narrower transponder rescues it.
"""

CHANNEL_NO_BLOCK = "no-block-wide-enough"
"""Spectrum is free on the route, and no anchor puts this mode's carrier in it.

The second reading of `Selection.channel is None`, and on a nearly full section
it is the common one. A narrower mode would have fitted, so the answer to an
operator is a different transponder or a different route rather than a wait for
somebody to turn a wavelength down.

**Two things produce it and the message reports the figure that separates them.**
Usually the widest free block is narrower than the mode, and then the width is
the whole story. Sometimes it is not: the anchors sit on a 50 GHz grid, and a
102.8 GHz block that begins 30 GHz above one anchor centre takes no 79.6 GHz
carrier even though it is wider than one. `oms-fra-mil` is in exactly that state
before the re-seed. So the sentence quotes the widest free block and lets the
reader compare it against the width rather than asserting which case it is.
"""

CHANNEL_REASONS = (CHANNEL_NO_SPECTRUM, CHANNEL_NO_BLOCK)
"""The two conditions a `None` channel can mean. Nothing else may be recorded."""

REASON_LATENCY = "latency"
"""A route closed and had spectrum, and was still too slow for the service."""

REASON_PRECEDENCE = (REASON_LATENCY, REASON_CAPACITY, REASON_BUDGET, REASON_NO_MODE, REASON_NO_ROUTE)
"""Which refusal to report when routes were discarded for different reasons.

Latency first, deliberately. A route discarded for latency was otherwise
provisionable: it closed optically and it had spectrum, which is the most
informative thing that can be said about a refusal. Reporting "no capacity" for
a Frankfurt to Milan service when the Geneva route has all 96 channels free
would be a true sentence about the wrong route.
"""

REASON_NO_SLOTS = "no-slots"
"""No line container on any candidate route has room for the client and no new
wavelength can be lit for it either.

Both halves, because grooming and lighting are tried in that order and a refusal
is only reached when both have failed on every route that closed. A separate
token from `REASON_CAPACITY` above, and the two mean different things rather than
overlapping ones. `capacity` is what this module reports to a caller that demands
a free channel. `no-slots` covers both ways lighting can be impossible: no
channel free on the route, and no line container type for the mode's line rate.
`_no_room` names whichever of the two applies rather than assuming the second,
because a reader told the rate has no container type will go looking in
`containers.py` when the real blocker is a full section.

**The one code here that nothing here writes.** `generators/optical_service.py`
is its only writer, in `_no_room`, and it lives here anyway so that the six codes
the schema declares have one home in Python rather than two. Spelled on the
generator, the sixth could only be read by loading an `infrahub_sdk`-importing
module, which is what `tests/unit/test_schema_contract.py` had to do to pin the
vocabulary against the Dropdown.
"""

REJECTION_CODES = (
    REASON_NO_ROUTE,
    REASON_NO_MODE,
    REASON_BUDGET,
    REASON_LATENCY,
    REASON_CAPACITY,
    REASON_NO_SLOTS,
)
"""Every code that may be written to `OtnService.rejection_code`.

The same six the schema declares as Dropdown choices, and the list a consumer
groups or colours by. Wider than `REASON_PRECEDENCE`, which orders only the
five this module itself can report and exists to answer a different question:
which refusal wins when routes were discarded for different reasons.
"""


@dataclass(frozen=True)
class RouteCandidate:
    """One end-to-end route, as the ordered sections a wavelength would cross."""

    key: str
    section_names: tuple[str, ...]
    start_node: str

    @property
    def hop_count(self) -> int:
        """Sections crossed. The first ranking term."""
        return len(self.section_names)


@dataclass(frozen=True)
class ModeCandidate:
    """One catalog mode, with the two fields the selector ranks on."""

    name: str
    mode_class: str
    line_rate_gbps: int
    baud_mbaud: int
    budget_input: ModeInput


@dataclass(frozen=True)
class Rejection:
    """One route that was discarded, and why.

    `shortfall` is how far this route was from being usable, in whatever unit
    its reason measures: nanoseconds over the latency budget, millidecibels of
    missing margin, zero for a route that had no spectrum. It is only
    ever compared against other rejections carrying the same reason, so the
    mixed units never meet.

    It exists so the refusal can name the *nearest* miss rather than an
    arbitrary one. Frankfurt to Milan rejects four routes for latency; the
    useful sentence is about the 990 km Geneva detour that misses by 854
    microseconds, not the 2600 km route through Warsaw that misses by nine
    milliseconds. Ordering by section count instead would not do it: two routes
    of equal length can miss by 2.9 ms and 4.0 ms.
    """

    route_key: str
    reason: str
    detail: str
    hop_count: int
    shortfall: int


@dataclass(frozen=True)
class Selection:
    """The winner: one route, one mode, one channel, and the budget behind it.

    `channel` is the lowest anchor whose interval, at this mode's width, is free
    on every section of the route, or `None` when **no channel is free wide
    enough**. That sentence is the restatement this feature forced and not a
    rewording: the old one said "no channel is free", and under width semantics a
    route can have a quarter of the band free and still take no 128 GBd carrier
    anywhere in it.

    `None` is not a failure and it is not a channel zero. It says the route can
    carry a service that grooms into a wavelength already lit on it and cannot
    carry one that needs a new wavelength, which are two different questions this
    module deliberately no longer conflates. A route only reaches here with
    `channel=None` when the caller passed `require_free_channel=False`.

    `channel_reason` is which of the two conditions produced the `None`, one of
    `CHANNEL_REASONS`. It exists because the two are different answers and the
    operator-facing message has to say which one it is holding: a full corridor is
    somebody else's wavelength to turn down, and a corridor with no block wide
    enough is a different transponder or a different route. The two are enforced
    to travel together below, so a message can never be written from a `None` with
    nothing to explain it.

    `widest_free_mhz` is the widest contiguous run of spectrum free on every
    section of the route, and `0` when there is none. It is the figure that makes
    a refusal actionable, because "the widest free block is 44,400 MHz and this
    mode needs 150,000 MHz" is a sentence an operator can act on and "no channel
    is free" is not. It is populated whether or not a channel was found, because
    it is a true statement about the route either way.
    """

    route: RouteCandidate
    mode: ModeCandidate
    channel: int | None
    budget: PathBudget
    channel_reason: str | None = None
    widest_free_mhz: int = 0

    def __post_init__(self) -> None:
        """A `None` channel always carries its reason, and a channel never does.

        The invariant is enforced rather than documented because the whole point
        of `channel_reason` is that no caller can produce a refusal it cannot
        explain. A hand-built `Selection` with a `None` channel and no reason is
        exactly the message that would reach an operator saying nothing.
        """
        if (self.channel is None) != (self.channel_reason is not None):
            raise ValueError(
                f"{self.route.key}: channel {self.channel!r} and channel_reason {self.channel_reason!r} disagree. "
                "A channel of None carries one of CHANNEL_REASONS saying which condition produced it, and a "
                "channel that was found carries none"
            )
        if self.channel_reason is not None and self.channel_reason not in CHANNEL_REASONS:
            allowed = ", ".join(CHANNEL_REASONS)
            raise ValueError(f"{self.route.key}: channel_reason {self.channel_reason!r} is not one of {allowed}")

    @property
    def rank(self) -> tuple[int, int, bool, int, str]:
        """The total order.

        Fewest sections, then highest margin, then a route with spectrum ahead of
        one with none, then lowest channel, then the route's own key. The key is
        unique, so no two candidates can tie and the same request produces the
        same winner on every run.

        The spectrum term is third rather than first because it is a tie break,
        not a gate. A one-section route whose channels are all taken still beats a
        three-section detour, because grooming into a wavelength already lit on
        the short route is the better answer and needs no channel. What the term
        does say is that between two otherwise equal routes the one that can still
        light a wavelength wins, because that route can carry the service whether
        grooming works or not. `False` sorts before `True`, so `channel is None`
        puts the spectrum-less route second.
        """
        return (
            self.route.hop_count,
            -self.budget.osnr_margin_mdb,
            self.channel is None,
            self.channel if self.channel is not None else 0,
            self.route.key,
        )


@dataclass(frozen=True)
class RoutingResult:
    """What the selector decided, and everything it considered on the way.

    `candidates` is every route that could have been chosen, in rank order, so
    the winner is `candidates[0]` whenever there is one. It exists because
    "rejected" and "not chosen" are different things and the demo needs both: on
    Berlin to Amsterdam nothing is rejected at all, and a log that only reported
    rejections would print one line and explain nothing.
    """

    selection: Selection | None
    candidates: tuple[Selection, ...]
    rejections: tuple[Rejection, ...]
    reason: str | None
    detail: str | None


def route_free_blocks(
    section_names: Sequence[str],
    occupancy: Mapping[str, Sequence[SpectralInterval]],
) -> tuple[FreeBlock, ...]:
    """The contiguous runs of spectrum free on **every** section of a route.

    Capacity is derived, not stored. A megahertz held by a carrier crossing any
    one section of a route is held against the whole route, because a wavelength
    occupies its width for its whole length. So the route's free spectrum is the
    band minus the union of every section's occupancy, which is
    `units.free_blocks` over all of those intervals concatenated. The union is
    what makes concatenation correct: two sections holding the same spectrum
    subtract it once.

    A section absent from `occupancy` contributes nothing, which is the correct
    reading of "no carrier crosses it". That is why occupancy stays a map of what
    is **taken** rather than a map of what is free: an unmentioned section is
    wholly free, and a map of free blocks would read the same absence as a section
    with nothing left.

    The blocks come back clipped to the band edges, so an anchor that fits inside
    one is inside the modelled C-band by construction and needs no second test.
    """
    return free_blocks(interval for name in section_names for interval in occupancy.get(name, ()))


def fitting_channels(
    blocks: Sequence[FreeBlock],
    baud_mbaud: int,
    channel_count: int = GRID_CHANNEL_COUNT,
) -> tuple[int, ...]:
    """Anchors a carrier at this symbol rate could take, ascending.

    A channel is usable when the whole interval the carrier would occupy fits
    inside **one** free block. Inside one and not across two: consecutive blocks
    are separated by a carrier, because `units.free_blocks` merges and never emits
    a zero-width gap, so an interval spanning two blocks is an interval crossing
    somebody's wavelength.

    Containment also settles the band edge without a second test. Every block is
    clipped to the modelled C-band, so a 79.6 GHz carrier on channel 1, which
    reaches 14.8 GHz below the lower edge, is contained in nothing and is not
    offered. That is the narrowing critique E4 asked to be derived rather than
    discovered: the widest and the mid-width modes both lose channels 1 and 96.
    """
    usable: list[int] = []
    for channel in range(1, channel_count + 1):
        lower, upper = carrier_interval_mhz(channel_to_frequency_mhz(channel), baud_mbaud)
        if any(block.lower_mhz <= lower and upper <= block.upper_mhz for block in blocks):
            usable.append(channel)
    return tuple(usable)


def free_channels(
    section_names: Sequence[str],
    occupancy: Mapping[str, Sequence[SpectralInterval]],
    baud_mbaud: int,
    channel_count: int = GRID_CHANNEL_COUNT,
) -> tuple[int, ...]:
    """Channels a carrier at this symbol rate could anchor on, over a whole route.

    **A free channel is no longer a property of a section.** It is a property of a
    section and a width, which is why `baud_mbaud` is an argument rather than a
    detail of the caller. The two halves are `route_free_blocks` and
    `fitting_channels`, split so `choose_route` can compute the blocks once and
    ask about them per mode.
    """
    return fitting_channels(route_free_blocks(section_names, occupancy), baud_mbaud, channel_count)


def eligible_modes(modes: Sequence[ModeCandidate], rate_gbps: int) -> tuple[ModeCandidate, ...]:
    """Transponder modes that reach the requested rate, narrowest spectrum first.

    Sorted by baud rate and then by name, so the caller's preference order is
    fixed before any budget runs and cannot depend on catalog order.
    """
    return tuple(
        sorted(
            (mode for mode in modes if mode.mode_class == TRANSPONDER and mode.line_rate_gbps >= rate_gbps),
            key=lambda mode: (mode.baud_mbaud, mode.name),
        )
    )


def route_sections(route: RouteCandidate, sections: Mapping[str, SectionInput]) -> list[SectionInput]:
    """The route's sections, in order, or a named error.

    A bare `sections[name]` would raise `KeyError` with nothing but the key in
    it, from inside a generator whose whole output is a log line. The traversal
    and the plant payload are two separate reads of the same branch, so they can
    disagree, and when they do the operator needs to be told which section went
    missing rather than handed a traceback. The OSNR check applies the same rule
    one layer up.
    """
    missing = [name for name in route.section_names if name not in sections]
    if missing:
        raise ValueError(f"route {route.key} crosses {', '.join(missing)}, which the plant payload does not contain")
    return [sections[name] for name in route.section_names]


def _evaluate(route: RouteCandidate, sections: Mapping[str, SectionInput], mode: ModeCandidate) -> PathBudget:
    """The worse of the route's two directions.

    A provisioned wavelength is a two-way service and it is only as good as its
    weaker direction, so selecting on the walk that happens to start at
    `endpoint_a` would provision a route the return path cannot carry. The two
    directions read different amplifier chains and are credited different Raman
    gain, so they are genuinely different numbers rather than the same one
    computed twice.
    """
    forward, reverse = evaluate_both_directions(
        route_sections(route, sections), mode.budget_input, start_node=route.start_node
    )
    return worse_direction(forward, reverse)


def _best_mode(
    route: RouteCandidate,
    sections: Mapping[str, SectionInput],
    modes: Sequence[ModeCandidate],
    max_latency_ns: int | None,
) -> list[tuple[ModeCandidate, PathBudget]] | Rejection:
    """Every mode that closes on this route and meets its latency budget, in
    preference order, narrowest spectrum first.

    Two failure modes are distinguished, because they are different answers.
    A route where nothing closes optically is a plant problem. A route where
    something closes and is too slow is a geography problem.

    **The whole ordered list rather than its head**, so `choose_route` can ask the
    spectrum question per mode without re-running the cascade. The head is still
    the answer whenever spectrum does not intervene, and see `choose_route` for
    why walking past it can never improve the anchor.
    """
    closing: list[tuple[ModeCandidate, PathBudget]] = []
    too_slow: list[tuple[ModeCandidate, PathBudget]] = []
    best_margin: tuple[int, str] | None = None
    budget_ns = max_latency_ns

    for mode in modes:
        budget = _evaluate(route, sections, mode)
        if best_margin is None or budget.osnr_margin_mdb > best_margin[0]:
            best_margin = (budget.osnr_margin_mdb, mode.name)
        if not (budget.osnr_ok and budget.cd_ok and budget.gain_ok):
            continue
        if budget_ns is not None and budget.latency_ns > budget_ns:
            too_slow.append((mode, budget))
            continue
        closing.append((mode, budget))

    if closing:
        # Narrowest spectrum first, then the better margin, then the name. The
        # name is the tie-break that makes this total: two modes can share a
        # baud rate, as DP-16QAM 400G and DP-64QAM 600G do at 64 GBd.
        closing.sort(key=lambda pair: (pair[0].baud_mbaud, -pair[1].osnr_margin_mdb, pair[0].name))
        return closing

    if too_slow and budget_ns is not None:
        # `budget_ns is not None` is redundant, because a mode only lands in
        # `too_slow` after that test. It is written out rather than asserted
        # because an `assert` disappears under `-O`.
        mode, budget = min(too_slow, key=lambda pair: (pair[1].latency_ns, pair[0].name))
        over_us = ns_to_us(budget.latency_ns - budget_ns)
        return Rejection(
            route_key=route.key,
            reason=REASON_LATENCY,
            detail=(
                f"closes on {mode.name} over {m_to_km(budget.total_length_m):.0f} km, and takes "
                f"{ns_to_us(budget.latency_ns):.3f} us against a budget of "
                f"{ns_to_us(budget_ns):.3f} us, which it misses by {over_us:.3f} us"
            ),
            hop_count=route.hop_count,
            shortfall=budget.latency_ns - budget_ns,
        )

    shortfall = f"best margin {mdb_to_db(best_margin[0]):+.3f} dB on {best_margin[1]}" if best_margin else "no mode"
    return Rejection(
        route_key=route.key,
        reason=REASON_BUDGET,
        detail=f"closes on no mode: {shortfall}",
        hop_count=route.hop_count,
        shortfall=-best_margin[0] if best_margin else 0,
    )


def no_anchor_detail(route: RouteCandidate, mode: ModeCandidate, reason: str, widest_free_mhz: int) -> str:
    """The sentence to quote about a route that offers this mode no anchor.

    One phrasing for both readings of a `None` channel, used by the `capacity`
    rejection below and read back off the `Selection` by
    `generators/optical_service.py`, so a refusal written here and a log line
    written there cannot describe the same route differently. FR-024a is that the
    operator is told which of the two conditions holds, and a second copy of the
    sentence is how one of them would come to say the other.
    """
    if reason == CHANNEL_NO_SPECTRUM:
        return f"no spectrum at all is free on all {route.hop_count} of its sections: {', '.join(route.section_names)}"
    return (
        f"no anchor puts a {mode.name} carrier, which occupies {occupied_width_mhz(mode.baud_mbaud):,} MHz, inside "
        f"spectrum free on all {route.hop_count} of its sections: the widest free block is {widest_free_mhz:,} MHz"
    )


def choose_route(
    routes: Sequence[RouteCandidate],
    sections: Mapping[str, SectionInput],
    modes: Sequence[ModeCandidate],
    occupancy: Mapping[str, Sequence[SpectralInterval]],
    rate_gbps: int,
    max_latency_ns: int | None = None,
    channel_count: int = GRID_CHANNEL_COUNT,
    require_free_channel: bool = True,
) -> RoutingResult:
    """Pick the route, mode and channel for one service, deterministically.

    **The mode and the anchor are not a joint search, and the reason is
    monotonicity.** A carrier's interval is centred on its anchor and widened by
    its symbol rate, so a narrower mode's interval is contained in a wider one's
    at every anchor. Anything a wide mode fits inside, a narrow mode fits inside
    too. A narrower mode therefore never has **fewer** anchor options than a wider
    one, and the modes are already ranked narrowest first by `eligible_modes`.

    So the loop below walks the closing modes in that existing ascending
    symbol-rate order and takes the first that has an anchor, and that is optimal:
    no wider mode can rescue a narrower one that found nothing, and no wider mode
    can be preferable to a narrower one that found something, because narrow is
    also the cheaper mode. **There is no need to search mode and channel
    together**, which is the reading of FR-023 that would have made this a much
    larger change. In practice the loop stops on its first iteration every time;
    it is written as a loop so the property is exercised rather than assumed.

    Spectrum is now checked in two places rather than one, and the split is not
    arbitrary. **Whether any spectrum is free at all** is a property of the route
    alone, so it is still asked before the budget, where it costs a sweep over a
    list against an amplifier cascade over twenty-five elements. **Whether a block
    is wide enough** cannot be asked until the mode is known, so it is asked after
    the cascade. Every discarded route is reported either way.

    **`require_free_channel` exists because a free channel is a precondition for
    lighting a wavelength and not for using one.** It was an unconditional gate
    here, from when provisioning always lit its own wavelength. That reading is
    now wrong for half the callers: a service that grooms into a line container
    somebody else already lit consumes no channel, so a section holding 96 of 96
    can still carry it. Left as a gate, `oms-fra-mil` under
    `demo/90_fra_mil_saturated.yml` refuses every service with a `capacity` reason
    before grooming is tried at all, which is a true sentence about a question
    nobody asked.

    The default stays `True`, so a caller that has not thought about grooming
    keeps the stricter behaviour rather than silently gaining a route it cannot
    use. `False` makes the route a candidate with `channel=None`, and the caller
    then owns the requirement: it may groom into that route, and it must refuse if
    it needs to light. That is where the requirement belongs, because only the
    caller knows whether this particular service will groom or light.
    """
    usable = eligible_modes(modes, rate_gbps)
    if not usable:
        return RoutingResult(
            selection=None,
            candidates=(),
            rejections=(),
            reason=REASON_NO_MODE,
            detail=f"no transponder mode in the catalog carries {rate_gbps} Gbps",
        )
    if not routes:
        return RoutingResult(
            selection=None,
            candidates=(),
            rejections=(),
            reason=REASON_NO_ROUTE,
            detail="path traversal found no route between the two endpoints",
        )

    selections: list[Selection] = []
    rejections: list[Rejection] = []

    for route in sorted(routes, key=lambda candidate: candidate.key):
        blocks = route_free_blocks(route.section_names, occupancy)
        widest = max((block.width_mhz for block in blocks), default=0)
        if not blocks and require_free_channel:
            # The mode-independent half, and the only half that can be settled
            # before the cascade runs: no transponder is narrow enough when the
            # route holds not one free megahertz. The mode passed below is the
            # narrowest eligible one and the sentence does not quote it, which is
            # the whole point of settling this case without running the budget.
            rejections.append(
                Rejection(
                    route_key=route.key,
                    reason=REASON_CAPACITY,
                    detail=no_anchor_detail(route, usable[0], CHANNEL_NO_SPECTRUM, 0),
                    hop_count=route.hop_count,
                    # A route with no spectrum is not "nearly" anything: there
                    # is no partial channel. Section count breaks the tie.
                    shortfall=0,
                )
            )
            continue

        outcome = _best_mode(route, sections, usable, max_latency_ns)
        if isinstance(outcome, Rejection):
            rejections.append(outcome)
            continue

        # The closing modes narrowest first, and the first one with an anchor
        # wins. See the docstring: the walk cannot improve on its first hit and
        # cannot rescue a miss, so this is one iteration in practice.
        anchored: tuple[ModeCandidate, PathBudget, int] | None = None
        for mode, budget in outcome:
            fitting = fitting_channels(blocks, mode.baud_mbaud, channel_count)
            if fitting:
                anchored = (mode, budget, fitting[0])
                break

        if anchored is None:
            # No anchor for any mode that closes. `blocks` is non-empty here
            # whenever the caller allowed a route with none through, so the two
            # conditions are told apart by the blocks and not by the modes.
            mode, budget = outcome[0]
            reason = CHANNEL_NO_SPECTRUM if not blocks else CHANNEL_NO_BLOCK
            if require_free_channel:
                rejections.append(
                    Rejection(
                        route_key=route.key,
                        reason=REASON_CAPACITY,
                        detail=no_anchor_detail(route, mode, reason, widest),
                        hop_count=route.hop_count,
                        shortfall=0,
                    )
                )
                continue
            # `None` is only reachable under `require_free_channel=False`, and it
            # says the route has spectrum for nothing new at this width: usable by
            # grooming, not by lighting.
            selections.append(
                Selection(
                    route=route,
                    mode=mode,
                    channel=None,
                    budget=budget,
                    channel_reason=reason,
                    widest_free_mhz=widest,
                )
            )
            continue

        mode, budget, channel = anchored
        selections.append(Selection(route=route, mode=mode, channel=channel, budget=budget, widest_free_mhz=widest))

    ranked = tuple(sorted(selections, key=lambda candidate: candidate.rank))
    if ranked:
        return RoutingResult(
            selection=ranked[0],
            candidates=ranked,
            rejections=tuple(rejections),
            reason=None,
            detail=None,
        )

    binding = next((reason for reason in REASON_PRECEDENCE if any(r.reason == reason for r in rejections)), None)
    # The nearest miss, not the first one found: smallest shortfall, then fewest
    # sections, then the key. Total, so the reported sentence is as reproducible
    # as the decision itself.
    nearest = min(
        (r for r in rejections if r.reason == binding),
        key=lambda r: (r.shortfall, r.hop_count, r.route_key),
        default=None,
    )
    return RoutingResult(
        selection=None,
        candidates=(),
        rejections=tuple(rejections),
        reason=binding,
        detail=f"{nearest.route_key} {nearest.detail}" if nearest else None,
    )
