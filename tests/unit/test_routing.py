"""The selector, against fixtures small enough to check by hand.

`test_routing_claims.py` runs the same code over the shipped dataset. This
module runs it over three-span toys where every expected value can be derived on
paper, because a test whose expectation came out of the code under test is a
change detector rather than a test.

The fixtures are deliberately asymmetric. A section chain where every span is
the same length and every ROADM the same loss passes an ordering bug, a
reversal bug and a double-counted node all at once. `budget.py`'s docstring
records where that went wrong the first time.
"""

import pytest

from infrahub_demo_otn.budget import AmplifierInput, ModeInput, NodeInput, SectionInput, SpanInput
from infrahub_demo_otn.plant import CarrierInterval, intervals_overlap
from infrahub_demo_otn.routing import (
    CHANNEL_NO_BLOCK,
    CHANNEL_NO_SPECTRUM,
    REASON_BUDGET,
    REASON_CAPACITY,
    REASON_LATENCY,
    REASON_NO_MODE,
    REASON_NO_ROUTE,
    REASON_PRECEDENCE,
    ModeCandidate,
    RouteCandidate,
    choose_route,
    eligible_modes,
    free_channels,
    route_sections,
)
from infrahub_demo_otn.units import (
    CBAND_LOWER_EDGE_MHZ,
    CBAND_UPPER_EDGE_MHZ,
    GRID_CHANNEL_COUNT,
    carrier_interval_mhz,
    channel_to_frequency_mhz,
)

QPSK_BAUD = 128_000
"""128 GBd, 150.0 GHz wide. Three channels of spectrum on a 50 GHz anchor."""

QAM_BAUD = 64_000
"""64 GBd, 79.6 GHz wide. The mode `MODES` selects for a 400G service."""

NARROW_BAUD = 32_000
"""32 GBd, 44.4 GHz wide, which is the only seeded rate that fits one channel.

Used wherever a test is about *which* channel is taken rather than about width,
because at this rate an occupied anchor costs exactly its own channel and the
arithmetic reads the way the channel-number model used to.
"""


def _span(name: str, km: int) -> SpanInput:
    return SpanInput(
        name=name,
        length_m=km * 1000,
        attenuation_mdb_per_km=200,
        dispersion_fs_per_nm_km=17000,
        splice_count=km // 10,
        splice_loss_mdb=50,
        connector_count=2,
        connector_loss_mdb=300,
        aging_margin_mdb=1500,
    )


def _section(name: str, head: str, tail: str, kms: tuple[int, ...]) -> SectionInput:
    """One section, with an amplifier per span plus a pre-amplifier, each way.

    Both chains carry the same identical units, because routing ranks whole
    routes and has no opinion about which direction an amplifier faces.
    """
    chain = tuple(
        AmplifierInput(name=f"{name}-amp-{index:02d}", noise_figure_mdb=4000, gain_mdb=22000)
        for index in range(len(kms) + 1)
    )
    return SectionInput(
        name=name,
        head_node=NodeInput(name=head, insertion_loss_mdb=7000),
        tail_node=NodeInput(name=tail, insertion_loss_mdb=7000),
        spans=tuple(_span(f"{name}-{index + 1:02d}", km) for index, km in enumerate(kms)),
        amplifiers_a2b=chain,
        amplifiers_b2a=chain,
    )


SHORT = _section("sec-a-b", "roadm-a", "roadm-b", (80, 70, 90))
# No span over 95 km. A 100 km span at 0.2 dB/km plus splices, connectors and
# the ageing allowance comes to 22.6 dB, which a 22 dB amplifier cannot recover,
# and the whole route then fails the gain gate for a reason that has nothing to
# do with what these fixtures are testing.
LONG = _section("sec-b-c", "roadm-b", "roadm-c", (95, 85, 90, 80))
SECTIONS = {SHORT.name: SHORT, LONG.name: LONG}


def _mode(name: str, klass: str, rate: int, baud: int, osnr_mdb: int, cd: int = 50_000_000) -> ModeCandidate:
    return ModeCandidate(
        name=name,
        mode_class=klass,
        line_rate_gbps=rate,
        baud_mbaud=baud,
        budget_input=ModeInput(name=name, required_osnr_mdb=osnr_mdb, cd_tolerance_fs_per_nm=cd, fec_latency_ns=4000),
    )


NARROW = _mode("narrow-400", "transponder", 400, 64_000, 24_500)
WIDE = _mode("wide-400", "transponder", 400, 128_000, 19_000)
GREEDY = _mode("greedy-600", "transponder", 600, 64_000, 29_000)
PLUGGABLE = _mode("plug-400", "pluggable", 400, 59_840, 23_000)
SMALL = _mode("small-100", "transponder", 100, 32_000, 14_000)
MODES = [PLUGGABLE, WIDE, GREEDY, NARROW, SMALL]

ROUTE_SHORT = RouteCandidate(key="sec-a-b", section_names=("sec-a-b",), start_node="roadm-a")
ROUTE_LONG = RouteCandidate(key="sec-a-b|sec-b-c", section_names=("sec-a-b", "sec-b-c"), start_node="roadm-a")


# --------------------------------------------------------------------------
# free_channels
# --------------------------------------------------------------------------


def _held(*channels: int, baud_mbaud: int = NARROW_BAUD) -> tuple[CarrierInterval, ...]:
    """The spectrum a set of carriers holds, one interval per anchor.

    The same shape `plant.occupancy_from_graphql` builds from a payload, so these
    fixtures exercise the type the generator actually passes rather than a
    stand-in that only has the two edges on it.
    """
    built = []
    for channel in channels:
        center = channel_to_frequency_mhz(channel)
        lower, upper = carrier_interval_mhz(center, baud_mbaud)
        built.append(
            CarrierInterval(
                carrier=f"oc-{channel:03d}",
                channel=channel,
                center_mhz=center,
                lower_mhz=lower,
                upper_mhz=upper,
                mode=f"{baud_mbaud // 1000} GBd",
            )
        )
    return tuple(built)


def test_a_route_over_a_section_nothing_crosses_has_every_channel_the_band_takes() -> None:
    """An empty route, and the width is still the thing that decides.

    Every anchor is unoccupied, and a 64 GBd carrier still cannot sit on channel
    1: it is 79.6 GHz wide, channel 1 centres 25 GHz above the lower band edge,
    and it would reach 14.8 GHz past it. A 32 GBd carrier is 44.4 GHz wide and
    fits. Nothing here is about occupancy at all, which is the point.
    """
    assert free_channels(("sec-a-b",), {}, NARROW_BAUD, channel_count=8) == (1, 2, 3, 4, 5, 6, 7, 8)
    assert free_channels(("sec-a-b",), {}, QAM_BAUD, channel_count=8) == (2, 3, 4, 5, 6, 7, 8)


def test_the_band_edge_costs_the_wider_modes_the_first_and_last_anchor() -> None:
    """Critique E4, measured rather than assumed, over the whole 96-channel grid.

    The finding as first written said a 150 GHz carrier loses channels 1, 2, 95
    and 96. It loses 1 and 96 only: at exactly 150.000 GHz it sits flush against
    the edge on channel 2 and on channel 95 with nothing to spare. 64 GBd loses
    the same two, so the widest and the mid-width modes share a usable range.
    """
    assert len(free_channels(("sec-a-b",), {}, NARROW_BAUD)) == GRID_CHANNEL_COUNT
    assert free_channels(("sec-a-b",), {}, QAM_BAUD)[0] == 2
    assert free_channels(("sec-a-b",), {}, QAM_BAUD)[-1] == 95
    assert free_channels(("sec-a-b",), {}, QPSK_BAUD)[0] == 2
    assert free_channels(("sec-a-b",), {}, QPSK_BAUD)[-1] == 95


def test_one_wide_carrier_costs_a_wide_neighbour_more_anchors_than_a_narrow_one() -> None:
    """The architectural finding, in one assertion pair.

    A free channel is not a property of a section. It is a property of a section
    **and a width**. One 128 GBd carrier on channel 40 holds 150 GHz, and what
    that costs the next service depends entirely on what the next service is: a
    32 GBd carrier loses three anchors to it and a 128 GBd carrier loses five.
    Under the channel-number model both answers were one.
    """
    occupancy = {"sec-a-b": _held(40, baud_mbaud=QPSK_BAUD)}
    narrow = free_channels(("sec-a-b",), occupancy, NARROW_BAUD)
    wide = free_channels(("sec-a-b",), occupancy, QPSK_BAUD)
    assert [channel for channel in range(38, 44) if channel not in narrow] == [39, 40, 41]
    assert [channel for channel in range(38, 44) if channel not in wide] == [38, 39, 40, 41, 42]


def test_a_channel_taken_on_one_section_is_unavailable_to_the_whole_route() -> None:
    """The point of design 3.6: a wavelength occupies its width end to end.

    Channel 2 is free on `sec-a-b` and taken on `sec-b-c`. A route crossing both
    cannot use it, and a route crossing only the first can. At 32 GBd an occupied
    anchor costs exactly its own channel, so the claim is about the route and not
    about the width.
    """
    occupancy = {"sec-a-b": _held(1), "sec-b-c": _held(2)}
    assert free_channels(("sec-a-b",), occupancy, NARROW_BAUD, channel_count=4) == (2, 3, 4)
    assert free_channels(("sec-a-b", "sec-b-c"), occupancy, NARROW_BAUD, channel_count=4) == (3, 4)


def test_the_same_channel_on_two_sections_is_one_channel_gone_not_two() -> None:
    occupancy = {"sec-a-b": _held(3), "sec-b-c": _held(3)}
    assert free_channels(("sec-a-b", "sec-b-c"), occupancy, NARROW_BAUD, channel_count=4) == (1, 2, 4)


def test_a_section_absent_from_occupancy_contributes_nothing() -> None:
    """Absent means "no carrier crosses it", not "unknown, assume the worst".

    Occupancy stays a map of what is taken rather than of what is free for this
    exact reason: an unmentioned section is wholly free, and a map of free blocks
    would read the same absence as a section with nothing left on it.
    """
    occupancy = {"sec-a-b": _held(1)}
    assert free_channels(("sec-a-b", "never-heard-of-it"), occupancy, NARROW_BAUD, channel_count=3) == (2, 3)


def test_a_fully_occupied_section_leaves_no_channel() -> None:
    """Four 44.4 GHz carriers on four 50 GHz anchors leave 5.6 GHz gaps between
    them, and nothing provisionable fits in 5.6 GHz."""
    assert free_channels(("sec-a-b",), {"sec-a-b": _held(1, 2, 3, 4)}, NARROW_BAUD, channel_count=4) == ()


# --------------------------------------------------------------------------
# eligible_modes
# --------------------------------------------------------------------------


def test_pluggable_modes_are_not_provisionable() -> None:
    """A ZR wavelength originates in a router port, and every router port
    in this model is grey. `plug-400` has the lowest baud rate of the 400G modes
    and would win on spectral efficiency if it were eligible at all."""
    assert PLUGGABLE not in eligible_modes(MODES, 400)


def test_a_mode_below_the_requested_rate_is_not_eligible() -> None:
    assert SMALL not in eligible_modes(MODES, 400)
    assert SMALL in eligible_modes(MODES, 100)


def test_a_higher_rate_mode_is_eligible_for_a_lower_request() -> None:
    """600G carries 400G. Whether it closes is the budget's problem, not this
    function's."""
    assert GREEDY in eligible_modes(MODES, 400)


def test_eligible_modes_come_back_narrowest_first_and_the_tie_breaks_on_name() -> None:
    """`narrow-400` and `greedy-600` both run at 64 GBd, which is the real
    catalog's situation: DP-16QAM 400G and DP-64QAM 600G share a baud rate. The
    name is what makes the order total."""
    assert [mode.name for mode in eligible_modes(MODES, 400)] == ["greedy-600", "narrow-400", "wide-400"]


def test_no_eligible_mode_refuses_before_any_route_is_budgeted() -> None:
    result = choose_route([ROUTE_SHORT], SECTIONS, MODES, {}, rate_gbps=1600)
    assert result.selection is None
    assert result.reason == REASON_NO_MODE
    assert result.rejections == ()


# --------------------------------------------------------------------------
# route_sections
# --------------------------------------------------------------------------


def test_a_section_missing_from_the_payload_names_itself() -> None:
    """A bare dict lookup raises `KeyError` with nothing but the key in it,
    from inside a generator whose whole output is a log line."""
    broken = RouteCandidate(key="k", section_names=("sec-a-b", "sec-ghost"), start_node="roadm-a")
    with pytest.raises(ValueError, match=r"route k crosses sec-ghost, which the plant payload does not contain"):
        route_sections(broken, SECTIONS)


def test_route_sections_returns_them_in_route_order() -> None:
    assert [section.name for section in route_sections(ROUTE_LONG, SECTIONS)] == ["sec-a-b", "sec-b-c"]


# --------------------------------------------------------------------------
# choose_route
# --------------------------------------------------------------------------


def test_no_route_refuses_without_touching_the_plant() -> None:
    result = choose_route([], SECTIONS, MODES, {}, rate_gbps=400)
    assert result.selection is None
    assert result.reason == REASON_NO_ROUTE


def test_the_narrowest_closing_mode_wins_not_the_one_with_the_best_margin() -> None:
    """`wide-400` needs 5.5 dB less OSNR than `narrow-400` and therefore always
    shows the better margin. It also occupies twice the spectrum, and the
    selector treats that as a cost."""
    result = choose_route([ROUTE_SHORT], SECTIONS, MODES, {}, rate_gbps=400)
    assert result.selection is not None
    assert result.selection.mode.name == "narrow-400"

    wide_only = choose_route([ROUTE_SHORT], SECTIONS, [WIDE], {}, rate_gbps=400)
    assert wide_only.selection is not None
    assert wide_only.selection.budget.osnr_margin_mdb > result.selection.budget.osnr_margin_mdb


def test_the_lowest_free_channel_is_taken_and_it_is_not_the_lowest_free_anchor() -> None:
    """Channels 1, 2 and 4 are held by 64 GBd carriers, and the answer is 6.

    Under the channel-number model the answer was 3, because 3 is the lowest
    anchor nobody had claimed. It is not usable: a 79.6 GHz carrier on channel 3
    reaches into the spectrum the carriers on 2 and 4 already hold, and so does
    one on channel 5. The first anchor that clears them all is 6. That gap between
    "free anchor" and "usable anchor" is the whole of this feature.
    """
    occupancy = {"sec-a-b": _held(1, 2, 4, baud_mbaud=QAM_BAUD)}
    result = choose_route([ROUTE_SHORT], SECTIONS, MODES, occupancy, rate_gbps=400)
    assert result.selection is not None
    assert result.selection.mode.baud_mbaud == QAM_BAUD
    assert result.selection.channel == 6


def _saturated() -> tuple[CarrierInterval, ...]:
    """Ninety-six 64 GBd carriers on ninety-six anchors: not one megahertz left.

    They overlap each other, which the collision check would refuse. That is
    deliberate and it is what `demo/90_fra_mil_saturated.yml` engineers: the
    fixture exists to leave the route with no spectrum at all, and the cheapest
    way to say that is to fill every anchor.
    """
    return _held(*range(1, GRID_CHANNEL_COUNT + 1), baud_mbaud=QAM_BAUD)


def test_a_route_with_no_spectrum_at_all_is_rejected_for_capacity() -> None:
    result = choose_route([ROUTE_SHORT], SECTIONS, MODES, {"sec-a-b": _saturated()}, rate_gbps=400)
    assert result.selection is None
    assert result.reason == REASON_CAPACITY
    assert "no spectrum at all is free" in (result.detail or "")


def test_a_route_whose_free_spectrum_is_too_narrow_says_so_rather_than_full() -> None:
    """FR-024a, at the layer that decides it.

    Every anchor but 50 is held, which leaves one free block of 20,400 MHz
    between the carriers on 49 and 51. The route is not full: it has free
    spectrum, and no mode in the catalog fits in 20.4 GHz. Those are different
    answers, and reporting the first would send an operator looking for somebody's
    wavelength to turn down when the answer is a narrower transponder.
    """
    held = _held(*(channel for channel in range(1, GRID_CHANNEL_COUNT + 1) if channel != 50), baud_mbaud=QAM_BAUD)
    occupancy = {"sec-a-b": held}

    strict = choose_route([ROUTE_SHORT], SECTIONS, MODES, occupancy, rate_gbps=400)
    assert strict.selection is None
    assert strict.reason == REASON_CAPACITY
    detail = strict.detail or ""
    assert "no anchor puts a narrow-400 carrier, which occupies 79,600 MHz, inside spectrum free" in detail
    assert "the widest free block is 20,400 MHz" in detail

    groomable = choose_route([ROUTE_SHORT], SECTIONS, MODES, occupancy, rate_gbps=400, require_free_channel=False)
    assert groomable.selection is not None
    assert groomable.selection.channel is None
    assert groomable.selection.channel_reason == CHANNEL_NO_BLOCK
    assert groomable.selection.widest_free_mhz == 20_400


def test_the_anchor_the_selector_hands_back_never_overlaps_what_is_already_lit() -> None:
    """FR-025, as the property rather than as one example.

    The carrier the generator writes at `selection.channel` has to pass
    `checks/channel_collision.py` on the branch that wrote it, and the check's
    rule is `plant.intervals_overlap`. So the assertion is the check's own
    predicate, run against every anchor the selector will offer over a sweep of
    occupancies. An allocator tested against a remembered channel number would
    still hand out a colliding anchor the first time a mode changed width.
    """
    for held in (1, 3, 7, 20, 44, 80):
        occupancy = {"sec-a-b": _held(*range(1, held + 1), baud_mbaud=QPSK_BAUD)}
        result = choose_route([ROUTE_SHORT], SECTIONS, MODES, occupancy, rate_gbps=400, require_free_channel=False)
        assert result.selection is not None
        selection = result.selection
        if selection.channel is None:
            continue
        center = channel_to_frequency_mhz(selection.channel)
        lower, upper = carrier_interval_mhz(center, selection.mode.baud_mbaud)
        allocated = CarrierInterval(
            carrier="oc-under-test",
            channel=selection.channel,
            center_mhz=center,
            lower_mhz=lower,
            upper_mhz=upper,
            mode=selection.mode.name,
        )
        assert CBAND_LOWER_EDGE_MHZ <= lower and upper <= CBAND_UPPER_EDGE_MHZ, f"{held} held: past the band edge"
        clashing = [lit.channel for lit in occupancy["sec-a-b"] if intervals_overlap(allocated, lit)]
        assert not clashing, f"{held} held: channel {selection.channel} overlaps the carriers on {clashing}"


def test_a_route_with_no_free_channel_is_a_candidate_when_no_channel_is_required() -> None:
    """The ordering fix. A full section still carries a service that grooms.

    A free channel is what lighting a wavelength needs. Nesting a client under a
    line container somebody else already lit needs none, so a section holding 96
    of 96 is not a refusal for a service that will groom, which is the state
    `demo/90_fra_mil_saturated.yml` puts `oms-fra-mil` in. The route comes back as
    a candidate carrying `channel=None` and the reason for it, and the caller owns
    the requirement from there: `generators/optical_service.py` grooms into it or
    refuses.

    Asserted against the default in the same test, because the two readings are a
    pair and the useful claim is that they differ.
    """
    occupancy = {"sec-a-b": _saturated()}
    groomable = choose_route([ROUTE_SHORT], SECTIONS, MODES, occupancy, rate_gbps=400, require_free_channel=False)
    assert groomable.selection is not None
    assert groomable.selection.route.key == ROUTE_SHORT.key
    assert groomable.selection.channel is None
    assert groomable.selection.channel_reason == CHANNEL_NO_SPECTRUM
    assert groomable.selection.widest_free_mhz == 0
    assert groomable.rejections == ()

    strict = choose_route([ROUTE_SHORT], SECTIONS, MODES, occupancy, rate_gbps=400)
    assert strict.selection is None
    assert strict.reason == REASON_CAPACITY


def test_a_route_with_spectrum_outranks_an_equal_one_without() -> None:
    """The third ranking term, and it is a tie break rather than a gate.

    Two one-section routes over identical plant, so hop count and margin cannot
    separate them. The one that can still light a wavelength wins, because it
    carries the service whether grooming works or not. The route with no spectrum
    is not discarded: it is second, and `_plan` in the generator falls back to it.

    The full route is deliberately named `sec-0-0`, which sorts before `sec-a-b`.
    The route key is the last ranking term, so a twin named later in the alphabet
    would pass this test whether the spectrum term did anything or not.
    """
    twin = _section("sec-0-0", "roadm-x", "roadm-y", (80, 70, 90))
    sections = {**SECTIONS, twin.name: twin}
    full = RouteCandidate(key="sec-0-0", section_names=("sec-0-0",), start_node="roadm-x")
    occupancy = {"sec-0-0": _saturated()}
    result = choose_route([full, ROUTE_SHORT], sections, MODES, occupancy, rate_gbps=400, require_free_channel=False)
    assert [candidate.route.key for candidate in result.candidates] == [ROUTE_SHORT.key, full.key]
    assert result.candidates[0].channel == 2, "channel 1 is a band-edge refusal at 64 GBd, not an occupancy one"
    assert result.candidates[1].channel is None


def test_a_route_that_closes_on_nothing_is_rejected_for_budget() -> None:
    """A mode nothing can satisfy, so the rejection is the plant's fault and the
    detail carries the best margin any mode managed."""
    impossible = _mode("impossible-400", "transponder", 400, 64_000, 90_000)
    result = choose_route([ROUTE_SHORT], SECTIONS, [impossible], {}, rate_gbps=400)
    assert result.selection is None
    assert result.reason == REASON_BUDGET
    assert "best margin" in (result.detail or "")


def test_a_route_that_closes_and_is_too_slow_is_rejected_for_latency() -> None:
    result = choose_route([ROUTE_SHORT], SECTIONS, MODES, {}, rate_gbps=400, max_latency_ns=1_000)
    assert result.selection is None
    assert result.reason == REASON_LATENCY
    assert "misses by" in (result.detail or "")


def test_no_latency_budget_means_no_latency_gate() -> None:
    """A service profile that leaves `max_latency_ns` null must not be gated on
    an invented default."""
    result = choose_route([ROUTE_SHORT], SECTIONS, MODES, {}, rate_gbps=400, max_latency_ns=None)
    assert result.selection is not None


def test_latency_outranks_capacity_when_both_happened() -> None:
    """A route rejected for latency had already closed optically and
    already had spectrum, so it is strictly more informative than "something was
    full", and reporting capacity here would be a true sentence about the wrong
    route."""
    lossy = _section("sec-x-y", "roadm-x", "roadm-y", (60, 60))
    sections = {**SECTIONS, lossy.name: lossy}
    full = RouteCandidate(key="sec-x-y", section_names=("sec-x-y",), start_node="roadm-x")
    occupancy = {"sec-x-y": _saturated()}
    result = choose_route([ROUTE_SHORT, full], sections, MODES, occupancy, rate_gbps=400, max_latency_ns=1_000)
    reasons = {rejection.reason for rejection in result.rejections}
    assert reasons == {REASON_CAPACITY, REASON_LATENCY}
    assert result.reason == REASON_LATENCY
    assert (result.detail or "").startswith(ROUTE_SHORT.key)


def test_the_refusal_names_the_nearest_miss_not_the_first_one() -> None:
    """Reporting whichever rejection came first would name an 8.7 ms overshoot
    on Frankfurt to Milan instead of the 854 us one."""
    result = choose_route([ROUTE_LONG, ROUTE_SHORT], SECTIONS, MODES, {}, rate_gbps=400, max_latency_ns=1_000)
    assert result.reason == REASON_LATENCY
    assert (result.detail or "").startswith(ROUTE_SHORT.key)


def test_fewer_sections_beats_a_better_margin() -> None:
    """The first ranking term, and the one that decides Berlin to Amsterdam.

    The long route is strictly worse on every axis here, so this is asserted the
    other way round too: give the long route the better margin by handing it a
    mode the short route cannot use, and the short route must still win.
    """
    result = choose_route([ROUTE_LONG, ROUTE_SHORT], SECTIONS, MODES, {}, rate_gbps=400)
    assert result.selection is not None
    assert result.selection.route.key == ROUTE_SHORT.key
    assert result.selection.route.hop_count == 1


def test_a_tie_on_sections_is_broken_by_margin() -> None:
    """Two one-section routes over different plant. The lossier one loses."""
    lossy = _section("sec-x-y", "roadm-x", "roadm-y", (100, 100, 100, 100, 100))
    sections = {**SECTIONS, lossy.name: lossy}
    alt = RouteCandidate(key="sec-x-y", section_names=("sec-x-y",), start_node="roadm-x")
    result = choose_route([alt, ROUTE_SHORT], sections, MODES, {}, rate_gbps=400)
    assert result.selection is not None
    assert result.selection.route.key == ROUTE_SHORT.key


def test_the_ranking_survives_a_shuffled_candidate_list() -> None:
    """A generator has to return the same answer every run, so the ordering has
    to be total rather than usually decisive. Ten permutations, one answer."""
    import itertools

    lossy = _section("sec-x-y", "roadm-x", "roadm-y", (100, 100, 100, 100, 100))
    sections = {**SECTIONS, lossy.name: lossy}
    alt = RouteCandidate(key="sec-x-y", section_names=("sec-x-y",), start_node="roadm-x")
    candidates = [ROUTE_SHORT, ROUTE_LONG, alt]

    outcomes = set()
    orders = set()
    for permutation in itertools.permutations(candidates):
        result = choose_route(list(permutation), sections, MODES, {}, rate_gbps=400)
        assert result.selection is not None
        outcomes.add((result.selection.route.key, result.selection.mode.name, result.selection.channel))
        orders.add(tuple(rejection.route_key for rejection in result.rejections))
    assert len(outcomes) == 1, f"the winner is not stable under permutation: {outcomes}"
    assert len(orders) == 1, f"the rejection order is not stable under permutation: {orders}"


def test_every_rejection_carries_a_reason_the_module_declares() -> None:
    occupancy = {"sec-b-c": _saturated()}
    result = choose_route([ROUTE_SHORT, ROUTE_LONG], SECTIONS, MODES, occupancy, rate_gbps=400, max_latency_ns=1_000)
    assert all(rejection.reason in REASON_PRECEDENCE for rejection in result.rejections)
    assert all(rejection.detail for rejection in result.rejections)


def test_every_viable_route_comes_back_as_a_ranked_candidate() -> None:
    """ "Rejected" and "not chosen" are different answers.

    On Berlin to Amsterdam nothing is rejected at all: four routes are viable
    and three lose the ranking. A result that only carried rejections would let
    the generator log one line and explain nothing.
    """
    result = choose_route([ROUTE_LONG, ROUTE_SHORT], SECTIONS, MODES, {}, rate_gbps=400)
    assert len(result.candidates) == 2
    assert result.rejections == ()
    assert result.selection is result.candidates[0]
    assert [candidate.route.key for candidate in result.candidates] == [ROUTE_SHORT.key, ROUTE_LONG.key]


def test_a_refusal_carries_no_candidates() -> None:
    occupancy = {"sec-a-b": _saturated()}
    result = choose_route([ROUTE_SHORT], SECTIONS, MODES, occupancy, rate_gbps=400)
    assert result.selection is None
    assert result.candidates == ()


def test_the_nearest_miss_is_the_smallest_overshoot_not_the_shortest_route() -> None:
    """The second defect the live runs found.

    Two routes of equal section count can miss a latency budget by very
    different margins. Ordering by section count and then by name reported
    whichever sorted first alphabetically, which on Berlin to Amsterdam meant
    naming the Frankfurt route missing by 3951 us instead of the Hamburg route
    missing by 2923 us. Distance is the thing being measured, so distance is
    what is compared.
    """
    near = _section("sec-near", "roadm-a", "roadm-n", (40, 40))
    far = _section("sec-far", "roadm-a", "roadm-f", (90, 90, 90))
    sections = {near.name: near, far.name: far}
    # "sec-far" sorts before "sec-near", so a name tie-break would pick the far
    # one. Both cross one section, so a section-count tie-break would too.
    routes = [
        RouteCandidate(key="sec-far", section_names=("sec-far",), start_node="roadm-a"),
        RouteCandidate(key="sec-near", section_names=("sec-near",), start_node="roadm-a"),
    ]
    result = choose_route(routes, sections, MODES, {}, rate_gbps=400, max_latency_ns=1_000)
    assert result.reason == REASON_LATENCY
    assert (result.detail or "").startswith("sec-near")
    by_key = {rejection.route_key: rejection for rejection in result.rejections}
    assert by_key["sec-near"].shortfall < by_key["sec-far"].shortfall


def test_a_capacity_rejection_has_no_distance_to_report() -> None:
    """There is no partial channel. Section count breaks the tie instead."""
    occupancy = {"sec-a-b": _saturated()}
    result = choose_route([ROUTE_SHORT], SECTIONS, MODES, occupancy, rate_gbps=400)
    assert result.rejections[0].reason == REASON_CAPACITY
    assert result.rejections[0].shortfall == 0
