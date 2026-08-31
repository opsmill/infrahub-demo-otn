"""Hand-computed reference values for the link budget engine.

Every expected number in this module was worked out from the formulae in design
section 4 and written down before the function that produces it existed. None
of them was produced by running the code under test. Where the arithmetic is
longer than one line it is written out in the docstring, digit by digit, so a
reader can check the test without running it.

**The fixtures use unequal spans and unequal node losses on purpose.** The
shipped dataset gives every span inside a section the same length and all
fourteen ROADMs the same 7 dB insertion loss. Against data like that, a
reversed section, a mis-ordered chain and a double-charged shared node all
produce exactly the right totals. `test_budget_claims.py` is where the shipped
numbers are asserted; this module is where the engine is actually tested.
"""

import math
from dataclasses import replace

import pytest

from infrahub_demo_otn.budget import (
    AMPLIFIER,
    LAUNCH_POWER_PER_CHANNEL_MDBM,
    NODE,
    OSNR_REFERENCE_MDB,
    ROADM_FILTERING_PENALTY_MDB,
    ROADM_LATENCY_NS,
    SPAN,
    SYSTEM_MARGIN_MDB,
    AmplifierInput,
    ModeInput,
    NodeInput,
    RegeneratorInput,
    RouteBudget,
    SectionInput,
    SegmentBudget,
    SegmentInput,
    SpanInput,
    cascade_osnr_mdb,
    dispersion_margin_fs_per_nm,
    evaluate_path,
    evaluate_route,
    flatten_path,
    order_sections,
    osnr_margin_mdb,
    osnr_stage_mdb,
    span_delay_ns,
    span_dispersion_fs_per_nm,
    span_fiber_loss_mdb,
    span_loss_mdb,
)
from infrahub_demo_otn.units import db_to_mdb, propagation_delay_ns

G652_ATTENUATION_MDB_PER_KM = 200
G652_DISPERSION_FS_PER_NM_KM = 17_000

BOOSTER_NF_MDB = 4_500
LINE_NF_MDB = 4_000
BOOSTER_GAIN_MDB = 17_000
LINE_GAIN_MDB = 22_000


def make_span(name: str, km: int, splices: int) -> SpanInput:
    """A G.652.D span with two mated connector pairs and 1.5 dB of ageing."""
    return SpanInput(
        name=name,
        length_m=km * 1_000,
        attenuation_mdb_per_km=G652_ATTENUATION_MDB_PER_KM,
        dispersion_fs_per_nm_km=G652_DISPERSION_FS_PER_NM_KM,
        splice_count=splices,
        splice_loss_mdb=50,
        connector_count=2,
        connector_loss_mdb=300,
        aging_margin_mdb=1_500,
    )


def make_amplifiers(count: int, prefix: str) -> tuple[AmplifierInput, ...]:
    """One booster then `count - 1` line amplifiers, matching the seed data."""
    booster = AmplifierInput(f"{prefix}-bst", BOOSTER_NF_MDB, BOOSTER_GAIN_MDB)
    rest = tuple(AmplifierInput(f"{prefix}-a{index:02d}", LINE_NF_MDB, LINE_GAIN_MDB) for index in range(1, count))
    return (booster, *rest)


def mirror_amplifiers(count: int, prefix: str) -> tuple[AmplifierInput, ...]:
    """The `b_to_a` chain of a section whose two directions carry the same units.

    Literally `make_amplifiers` reversed, and that is the point. The
    single-chain model asserted that a flipped section met its one chain
    backwards, so a fixture built this way budgets to exactly the number the
    single-chain model produced. Every hand-computed figure in this module is
    therefore unchanged by the split into two chains, and the split is exercised
    by the dedicated tests below, which give the two directions genuinely
    different chains.
    """
    return tuple(reversed(make_amplifiers(count, prefix)))


def uneven_section() -> SectionInput:
    """Three spans of 50, 70 and 90 km, between a 7.0 dB and a 4.0 dB node.

    Both asymmetries are the point. Equal spans hide an ordering defect and
    equal nodes hide an orientation defect.

    Fiber loss per span, ageing excluded:
        50 km: 50 x 200 + 12 x 50 + 2 x 300 = 10000 + 600 + 600 = 11200 mdB
        70 km: 70 x 200 + 17 x 50 + 2 x 300 = 14000 +  850 + 600 = 15450 mdB
        90 km: 90 x 200 + 22 x 50 + 2 x 300 = 18000 + 1100 + 600 = 19700 mdB
    """
    return SectionInput(
        name="oms-test-uneven",
        head_node=NodeInput("roadm-head", 7_000),
        tail_node=NodeInput("roadm-tail", 4_000),
        spans=(make_span("span-50", 50, 12), make_span("span-70", 70, 17), make_span("span-90", 90, 22)),
        amplifiers_a2b=make_amplifiers(4, "amp-uneven"),
        amplifiers_b2a=mirror_amplifiers(4, "amp-uneven"),
    )


def second_section() -> SectionInput:
    """Two 60 km spans hanging off `roadm-tail`, for the two-section tests."""
    return SectionInput(
        name="oms-test-second",
        head_node=NodeInput("roadm-tail", 4_000),
        tail_node=NodeInput("roadm-far", 6_000),
        spans=(make_span("span-60a", 60, 15), make_span("span-60b", 60, 15)),
        amplifiers_a2b=make_amplifiers(3, "amp-second"),
        amplifiers_b2a=mirror_amplifiers(3, "amp-second"),
    )


MODE_400G_16QAM = ModeInput(
    name="DP-16QAM 64GBd 400G",
    required_osnr_mdb=24_500,
    cd_tolerance_fs_per_nm=50_000_000,
    fec_latency_ns=4_000,
)


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------


def test_the_named_constants_carry_the_values_the_design_states() -> None:
    """The four constants the whole budget is calibrated on.

    Asserted here so a silent retune fails a test rather than changing an
    answer."""
    assert OSNR_REFERENCE_MDB == 58_000
    assert LAUNCH_POWER_PER_CHANNEL_MDBM == 3_000
    assert ROADM_FILTERING_PENALTY_MDB == 500
    assert SYSTEM_MARGIN_MDB == 1_000


# --------------------------------------------------------------------------
# Per-span quantities
# --------------------------------------------------------------------------


def test_span_fiber_loss_is_attenuation_plus_splices_plus_connectors() -> None:
    """90 x 200 = 18000, 22 x 50 = 1100, 2 x 300 = 600. Total 19700 mdB."""
    assert span_fiber_loss_mdb(make_span("s", 90, 22)) == 19_700


def test_span_loss_adds_the_ageing_allowance_and_nothing_else() -> None:
    """19700 + 1500 = 21200 mdB. The two functions differ by exactly the
    allowance, which is what makes the power-budget and OSNR split checkable."""
    span = make_span("s", 90, 22)
    assert span_loss_mdb(span) == 21_200
    assert span_loss_mdb(span) - span_fiber_loss_mdb(span) == span.aging_margin_mdb


def test_span_loss_rounds_a_fractional_kilometre_rather_than_truncating() -> None:
    """A 73,334 m span at 200 mdB/km is 14,666.8 mdB, which rounds to 14667.

    Truncation would lose 0.8 mdB here and on every one of the 37 spans of the
    longest route. 132 spans in the shipped plant are not whole kilometres.
    """
    span = SpanInput(
        name="s",
        length_m=73_334,
        attenuation_mdb_per_km=200,
        dispersion_fs_per_nm_km=17_000,
    )
    assert span_fiber_loss_mdb(span) == 14_667


def test_span_dispersion_is_length_times_coefficient() -> None:
    """90 km x 17000 fs/nm/km = 1,530,000 fs/nm, which is 1530 ps/nm."""
    assert span_dispersion_fs_per_nm(make_span("s", 90, 22)) == 1_530_000


def test_span_delay_delegates_to_units_rather_than_reimplementing_it() -> None:
    """The propagation constant lives in one place. This asserts the engine
    reads it rather than carrying a second copy that can drift."""
    span = make_span("s", 90, 22)
    assert span_delay_ns(span) == propagation_delay_ns(90_000, span.group_index_milli)
    assert span_delay_ns(span) == 440_705


# --------------------------------------------------------------------------
# OSNR arithmetic
# --------------------------------------------------------------------------


def test_one_stage_is_input_power_minus_noise_figure_plus_the_reference() -> None:
    """P_in = +3.0 dBm launch minus 19.7 dB of fiber = -16.7 dBm.
    Stage = -16700 - 4000 + 58000 = 37300 mdB, which is 37.300 dB."""
    input_power = LAUNCH_POWER_PER_CHANNEL_MDBM - 19_700
    assert input_power == -16_700
    assert osnr_stage_mdb(input_power, LINE_NF_MDB) == 37_300


def test_two_equal_stages_cascade_to_three_point_zero_one_below_either() -> None:
    """1/OSNR = 2 x 10^-3.5, so OSNR = 35.0 - 10*log10(2) = 31.9897 dB.

    Rounded half up to millidecibels that is 31990, which is 3010 below 35000.
    The same offset appears for any pair of equal stages, which is why the
    30.0 dB case is asserted too.
    """
    assert cascade_osnr_mdb([35_000, 35_000]) == 31_990
    assert cascade_osnr_mdb([30_000, 30_000]) == 26_990
    assert 35_000 - cascade_osnr_mdb([35_000, 35_000]) == db_to_mdb(10 * math.log10(2))


def test_a_single_stage_cascades_to_itself() -> None:
    assert cascade_osnr_mdb([37_300]) == 37_300


def test_a_much_better_stage_barely_moves_the_total() -> None:
    """49.5 dB against 37.3 dB is 12.2 dB of headroom.

        10^(-4.9500) = 1.12201845e-5
        10^(-3.7300) = 1.86208714e-4
        sum          = 1.97428898e-4
        -10 log10(1.97428898e-4) = 37.04606 dB -> 37046 mdB

    The booster contributes 5.7 percent of the noise and costs 0.254 dB. That
    is why a booster's noise figure hardly matters and a line amplifier's does.
    """
    assert cascade_osnr_mdb([49_500, 37_300]) == 37_046


def test_cascading_nothing_raises_rather_than_evaluating_log_of_zero() -> None:
    with pytest.raises(ValueError, match="empty stage sequence"):
        cascade_osnr_mdb([])


def test_the_margin_subtracts_both_the_requirement_and_the_system_margin() -> None:
    """29258 - 24500 - 1000 = 3758 mdB."""
    assert osnr_margin_mdb(29_258, 24_500) == 3_758
    assert osnr_margin_mdb(24_500, 24_500) == -1_000
    assert osnr_margin_mdb(25_500, 24_500) == 0


def test_the_dispersion_gate_is_independent_of_the_osnr_gate() -> None:
    """50,490,000 fs/nm against a 50,000,000 tolerance is -490,000."""
    assert dispersion_margin_fs_per_nm(50_490_000, 50_000_000) == -490_000
    assert dispersion_margin_fs_per_nm(13_260_000, 50_000_000) == 36_740_000


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def test_a_single_section_orders_to_itself() -> None:
    ordered, start = order_sections([uneven_section()])
    assert [section.name for section in ordered] == ["oms-test-uneven"]
    assert start == "roadm-head"


def test_two_sections_order_the_same_way_whichever_order_they_arrive_in() -> None:
    """The set is unordered, so both input orders must give one answer."""
    forward, start_a = order_sections([uneven_section(), second_section()])
    backward, start_b = order_sections([second_section(), uneven_section()])
    assert [section.name for section in forward] == [section.name for section in backward]
    assert start_a == start_b == "roadm-far"


def test_an_explicit_start_node_picks_the_direction() -> None:
    ordered, start = order_sections([uneven_section(), second_section()], start_node="roadm-head")
    assert start == "roadm-head"
    assert [section.name for section in ordered] == ["oms-test-uneven", "oms-test-second"]


def test_a_start_node_that_is_not_an_endpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="roadm-tail is not an endpoint"):
        order_sections([uneven_section(), second_section()], start_node="roadm-tail")


def test_a_branch_is_rejected_and_the_message_names_the_junction() -> None:
    """Three sections meeting at one ROADM is a tree, not a path. Summing them
    gives a number for a route that does not exist."""
    third = SectionInput(
        name="oms-test-third",
        head_node=NodeInput("roadm-tail", 4_000),
        tail_node=NodeInput("roadm-other", 5_000),
        spans=(make_span("span-40", 40, 10),),
        amplifiers_a2b=make_amplifiers(2, "amp-third"),
        amplifiers_b2a=mirror_amplifiers(2, "amp-third"),
    )
    with pytest.raises(ValueError, match="branch at"):
        order_sections([uneven_section(), second_section(), third])


def test_a_disconnected_pair_is_rejected() -> None:
    detached = SectionInput(
        name="oms-test-detached",
        head_node=NodeInput("roadm-alpha", 7_000),
        tail_node=NodeInput("roadm-beta", 7_000),
        spans=(make_span("span-30", 30, 8),),
        amplifiers_a2b=make_amplifiers(2, "amp-detached"),
        amplifiers_b2a=mirror_amplifiers(2, "amp-detached"),
    )
    with pytest.raises(ValueError, match="do not form a simple chain"):
        order_sections([uneven_section(), detached])


def test_a_cycle_is_rejected() -> None:
    """A closed ring has no endpoint of degree one at all."""
    closing = SectionInput(
        name="oms-test-closing",
        head_node=NodeInput("roadm-far", 6_000),
        tail_node=NodeInput("roadm-head", 7_000),
        spans=(make_span("span-30", 30, 8),),
        amplifiers_a2b=make_amplifiers(2, "amp-closing"),
        amplifiers_b2a=mirror_amplifiers(2, "amp-closing"),
    )
    with pytest.raises(ValueError, match="do not form a simple chain"):
        order_sections([uneven_section(), second_section(), closing])


def test_a_section_with_the_wrong_amplifier_count_is_rejected() -> None:
    """N spans need N+1 amplifiers. Anything else means the chain has a hole."""
    broken = SectionInput(
        name="oms-test-broken",
        head_node=NodeInput("roadm-head", 7_000),
        tail_node=NodeInput("roadm-tail", 4_000),
        spans=(make_span("span-50", 50, 12), make_span("span-70", 70, 17)),
        amplifiers_a2b=make_amplifiers(2, "amp-broken"),
        amplifiers_b2a=mirror_amplifiers(2, "amp-broken"),
    )
    with pytest.raises(ValueError, match="2 amplifiers for 2 spans, expected 3"):
        order_sections([broken])


# --------------------------------------------------------------------------
# Flattening
# --------------------------------------------------------------------------


def test_flattening_one_section_alternates_node_amp_span_and_ends_on_a_node() -> None:
    elements = flatten_path([uneven_section()], "roadm-head")
    assert [element.kind for element in elements] == [
        NODE,
        AMPLIFIER,
        SPAN,
        AMPLIFIER,
        SPAN,
        AMPLIFIER,
        SPAN,
        AMPLIFIER,
        NODE,
    ]
    assert elements[0].name == "roadm-head"
    assert elements[-1].name == "roadm-tail"


def test_two_sections_produce_three_nodes_and_the_shared_one_appears_once() -> None:
    """S sections give S+1 nodes. A per-section walk would emit `roadm-tail`
    twice and charge its 4 dB twice, which is larger than either margin the
    demo's story turns on."""
    elements = flatten_path([uneven_section(), second_section()], "roadm-head")
    nodes = [element.name for element in elements if element.kind == NODE]
    assert nodes == ["roadm-head", "roadm-tail", "roadm-far"]


def test_a_section_traversed_backwards_is_flipped_before_it_is_walked() -> None:
    """Starting at `roadm-far` means `oms-test-uneven` is walked tail to head,
    so its spans come out 90, 70, 50 rather than 50, 70, 90."""
    elements = flatten_path([second_section(), uneven_section()], "roadm-far")
    spans = [element.name for element in elements if element.kind == SPAN]
    assert spans == ["span-60b", "span-60a", "span-90", "span-70", "span-50"]


def test_flattening_from_a_node_the_first_section_does_not_touch_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not touch"):
        flatten_path([uneven_section()], "roadm-far")


# --------------------------------------------------------------------------
# The hand-verified reference case
# --------------------------------------------------------------------------


def five_span_450_km_section() -> SectionInput:
    """Five 90 km spans between two 7.0 dB ROADMs, six amplifiers."""
    return SectionInput(
        name="oms-reference-450",
        head_node=NodeInput("roadm-a", 7_000),
        tail_node=NodeInput("roadm-b", 7_000),
        spans=tuple(make_span(f"span-{index}", 90, 22) for index in range(1, 6)),
        amplifiers_a2b=make_amplifiers(6, "amp-reference"),
        amplifiers_b2a=mirror_amplifiers(6, "amp-reference"),
    )


def test_the_known_good_450_km_five_span_case() -> None:
    """The reference case, computed by hand.

    Five identical 90 km G.652.D spans, 22 splices each, two mated connector
    pairs, 1.5 dB ageing, between two 7.0 dB ROADMs. Six amplifiers: a 4.5 dB
    booster then five 4.0 dB line amplifiers.

    **Per-span loss**

        fiber = 90 x 200 + 22 x 50 + 2 x 300 = 18000 + 1100 + 600 = 19700 mdB
        full  = 19700 + 1500                                      = 21200 mdB

    **Total loss**, ageing included, both ROADMs charged once each

        7000 + 5 x 21200 + 7000 = 7000 + 106000 + 7000 = 120000 mdB = 120.0 dB

    **Stages.** Launch is +3.0 dBm per channel. The loss ahead of an amplifier
    is the loss of whatever sits immediately before it on the flat chain.

        booster: P_in = 3000 - 7000  = -4000  -> -4000  - 4500 + 58000 = 49500
        five line amps: P_in = 3000 - 19700 = -16700 -> -16700 - 4000 + 58000 = 37300

    **Cascade**, in the linear domain

        10^(-4.9500) = 1.12201845e-5
        10^(-3.7300) = 1.86208714e-4, and five of them = 9.31043569e-4
        sum          = 9.42263753e-4
        -10 log10(9.42263753e-4) = 30.25827515 dB -> 30258 mdB

    **Node penalty**: two ROADMs at 0.5 dB each.

        30258 - 1000 = 29258 mdB = 29.258 dB

    **Margin** against DP-16QAM 64GBd 400G, 24.5 dB required, 1.0 dB system

        29258 - 24500 - 1000 = 3758 mdB = +3.758 dB

    **Dispersion**: 450 km x 17000 fs/nm/km = 7,650,000 fs/nm, which is
    7650 ps/nm against a 50,000 ps/nm tolerance.

    **Latency**: five spans at 440,705 ns, two nodes at 150, six amplifiers at
    100, plus 4000 ns of FEC.

        5 x 440705 + 300 + 600 + 4000 = 2203525 + 4900 = 2208425 ns
    """
    budget = evaluate_path([five_span_450_km_section()], MODE_400G_16QAM)

    assert budget.span_count == 5
    assert budget.amplifier_count == 6
    assert budget.node_count == 2
    assert budget.total_length_m == 450_000
    assert budget.total_loss_mdb == 120_000

    stages = [hop.osnr_stage_mdb for hop in budget.hops if hop.kind == AMPLIFIER]
    assert stages == [49_500, 37_300, 37_300, 37_300, 37_300, 37_300]

    assert budget.osnr_total_mdb == 29_258
    assert budget.osnr_margin_mdb == 3_758
    assert budget.osnr_ok

    assert budget.cd_total_fs_per_nm == 7_650_000
    assert budget.cd_margin_fs_per_nm == 42_350_000
    assert budget.cd_ok

    assert budget.latency_ns == 2_208_425
    assert budget.gain_shortfalls == ()
    assert budget.ok


# --------------------------------------------------------------------------
# evaluate_path
# --------------------------------------------------------------------------


def test_the_uneven_section_budgets_to_its_hand_computed_totals() -> None:
    """Three spans of 50, 70 and 90 km, a 7.0 dB head and a 4.0 dB tail.

    fiber losses: 11200, 15450, 19700 mdB
    full  losses: 12700, 16950, 21200 mdB
    total loss  : 7000 + 12700 + 16950 + 21200 + 4000 = 61850 mdB

    stages: booster  3000 -  7000 -  4500 + 58000 = 49500
            after 50 3000 - 11200 - 4000 + 58000 = 45800
            after 70 3000 - 15450 - 4000 + 58000 = 41550
            after 90 3000 - 19700 - 4000 + 58000 = 37300

    cascade = 35.32072724 dB -> 35321 mdB
    two nodes at 0.5 dB      -> 34321 mdB

    dispersion: 210 km x 17000 = 3,570,000 fs/nm
    delay: 244836 + 342770 + 440705 = 1028311 ns
    latency: 1028311 + 2 x 150 + 4 x 100 + 4000 = 1033011 ns
    """
    budget = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-head")

    assert [hop.osnr_stage_mdb for hop in budget.hops if hop.kind == AMPLIFIER] == [49_500, 45_800, 41_550, 37_300]
    assert budget.total_loss_mdb == 61_850
    assert budget.osnr_total_mdb == 34_321
    assert budget.osnr_margin_mdb == 34_321 - 24_500 - 1_000
    assert budget.cd_total_fs_per_nm == 3_570_000
    assert budget.latency_ns == 1_033_011


def test_walking_the_uneven_section_backwards_changes_the_osnr_but_not_the_loss() -> None:
    """Direction is observable, and the shipped data cannot show it.

    Flipping reverses both ordered tuples, so the amplifier that was the
    booster becomes the pre-amplifier and the chain meets the spans as 90, 70,
    50. Every stage changes:

        a03  (4.0 dB) behind the 4.0 dB tail node: 3000 -  4000 - 4000 + 58000 = 53000
        a02  (4.0 dB) behind span-90, 19700 mdB  : 3000 - 19700 - 4000 + 58000 = 37300
        a01  (4.0 dB) behind span-70, 15450 mdB  : 3000 - 15450 - 4000 + 58000 = 41550
        bst  (4.5 dB) behind span-50, 11200 mdB  : 3000 - 11200 - 4500 + 58000 = 45300

        cascade = 35365 mdB, less the same 1000 of node penalty = 34365 mdB

    Forward it is 34321, so the two directions differ by 0.044 dB. Loss,
    dispersion and delay are sums over the same elements and do not move. An
    implementation that always read `roadm_a` would pass every other assertion
    in this module.
    """
    forward = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-head")
    backward = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-tail")

    assert backward.total_loss_mdb == forward.total_loss_mdb
    assert backward.cd_total_fs_per_nm == forward.cd_total_fs_per_nm
    assert backward.latency_ns == forward.latency_ns
    assert backward.osnr_total_mdb == 34_365
    assert backward.osnr_total_mdb != forward.osnr_total_mdb
    assert [hop.name for hop in backward.hops if hop.kind == SPAN] == ["span-90", "span-70", "span-50"]


def test_a_two_section_path_charges_the_shared_node_exactly_once() -> None:
    """The critique's E2, asserted rather than reasoned about.

    One section is 61850 mdB including both its nodes. Adding the second
    section must add its two 60 km spans and its far node at 6000, and nothing
    else. `roadm-tail` is already inside the first total.

        60 km span: 60 x 200 + 15 x 50 + 2 x 300 + 1500 = 12000 + 750 + 600 + 1500 = 14850
        61850 + 14850 + 14850 + 6000 = 97550 mdB
    """
    one = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-head")
    two = evaluate_path([uneven_section(), second_section()], MODE_400G_16QAM, start_node="roadm-head")

    span_60_full = 60 * 200 + 15 * 50 + 2 * 300 + 1_500
    assert span_60_full == 14_850
    assert two.total_loss_mdb == one.total_loss_mdb + 2 * span_60_full + 6_000
    assert two.total_loss_mdb == 97_550
    assert two.node_count == 3
    assert two.span_count == 5
    assert two.amplifier_count == 7


def test_a_longer_path_never_has_a_better_osnr_when_the_spans_are_equal() -> None:
    """Adding stages can only add noise. This is the sanity check that the
    cascade runs over the whole path rather than per section."""
    one = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-head")
    two = evaluate_path([uneven_section(), second_section()], MODE_400G_16QAM, start_node="roadm-head")
    assert two.osnr_total_mdb < one.osnr_total_mdb


def test_a_mode_that_needs_more_than_the_path_delivers_fails_with_a_signed_margin() -> None:
    demanding = ModeInput("DP-64QAM 64GBd 600G", required_osnr_mdb=29_000, cd_tolerance_fs_per_nm=30_000_000)
    budget = evaluate_path([uneven_section()], demanding, start_node="roadm-head")
    assert budget.osnr_margin_mdb == 34_321 - 29_000 - 1_000
    assert budget.osnr_ok


def test_the_dispersion_gate_can_fail_while_the_osnr_gate_passes() -> None:
    """Two independent constraints. 400ZR tolerates 2,400,000 fs/nm and this
    path accumulates 3,570,000."""
    zr = ModeInput("400ZR", required_osnr_mdb=26_000, cd_tolerance_fs_per_nm=2_400_000)
    budget = evaluate_path([uneven_section()], zr, start_node="roadm-head")
    assert budget.osnr_ok
    assert not budget.cd_ok
    assert budget.cd_margin_fs_per_nm == 2_400_000 - 3_570_000
    assert not budget.ok


def test_an_underpowered_amplifier_is_named_even_when_the_osnr_passes() -> None:
    """The gain gate is a third, independent verdict. A 12 dB line amplifier
    cannot recover a 21.2 dB span, and the OSNR arithmetic says nothing about
    it because OSNR assumes the power is restored."""
    weak = AmplifierInput("amp-weak", LINE_NF_MDB, 12_000)
    section = SectionInput(
        name="oms-test-weak",
        head_node=NodeInput("roadm-head", 7_000),
        tail_node=NodeInput("roadm-tail", 4_000),
        spans=(make_span("span-90", 90, 22),),
        amplifiers_a2b=(AmplifierInput("amp-bst", BOOSTER_NF_MDB, BOOSTER_GAIN_MDB), weak),
        amplifiers_b2a=(AmplifierInput("amp-bst", BOOSTER_NF_MDB, BOOSTER_GAIN_MDB), weak),
    )
    budget = evaluate_path([section], MODE_400G_16QAM, start_node="roadm-head")
    assert budget.osnr_ok
    assert budget.gain_shortfalls == ("amp-weak",)
    assert not budget.gain_ok
    assert not budget.ok


def test_the_gain_gate_uses_the_loss_including_ageing() -> None:
    """The amplifier has to still recover the span at end of life, so the gate
    reads `span_loss_mdb` and not `span_fiber_loss_mdb`. A 20 dB amplifier
    covers 19.7 dB of fiber and not the 21.2 dB the span will cost."""
    borderline = AmplifierInput("amp-borderline", LINE_NF_MDB, 20_000)
    section = SectionInput(
        name="oms-test-borderline",
        head_node=NodeInput("roadm-head", 7_000),
        tail_node=NodeInput("roadm-tail", 4_000),
        spans=(make_span("span-90", 90, 22),),
        amplifiers_a2b=(AmplifierInput("amp-bst", BOOSTER_NF_MDB, BOOSTER_GAIN_MDB), borderline),
        amplifiers_b2a=(AmplifierInput("amp-bst", BOOSTER_NF_MDB, BOOSTER_GAIN_MDB), borderline),
    )
    budget = evaluate_path([section], MODE_400G_16QAM, start_node="roadm-head")
    assert budget.gain_shortfalls == ("amp-borderline",)


def test_every_hop_carries_running_totals_that_end_at_the_path_totals() -> None:
    """The transform renders these columns directly, so the last row has to
    agree with the summary block or the table contradicts its own footer."""
    budget = evaluate_path([uneven_section(), second_section()], MODE_400G_16QAM, start_node="roadm-head")
    last = budget.hops[-1]
    assert last.cumulative_length_m == budget.total_length_m
    assert last.cumulative_loss_mdb == budget.total_loss_mdb
    assert last.cumulative_osnr_mdb == budget.osnr_total_mdb
    assert last.cumulative_delay_ns + MODE_400G_16QAM.fec_latency_ns == budget.latency_ns


def test_the_running_osnr_is_undefined_before_the_first_amplifier() -> None:
    """The head ROADM has no OSNR yet: no stage has been contributed. `None`
    says that; zero would read as a total failure."""
    budget = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-head")
    assert budget.hops[0].kind == NODE
    assert budget.hops[0].cumulative_osnr_mdb is None
    assert budget.hops[1].cumulative_osnr_mdb is not None


def test_the_running_osnr_only_ever_gets_worse() -> None:
    budget = evaluate_path([uneven_section(), second_section()], MODE_400G_16QAM, start_node="roadm-head")
    running = [hop.cumulative_osnr_mdb for hop in budget.hops if hop.cumulative_osnr_mdb is not None]
    assert running == sorted(running, reverse=True)


def test_evaluating_no_sections_at_all_raises() -> None:
    with pytest.raises(ValueError, match="empty section list"):
        evaluate_path([], MODE_400G_16QAM)


# --------------------------------------------------------------------------
# Two amplifier chains, one per direction
# --------------------------------------------------------------------------

NOISY_NF_MDB = 6_000
"""A 6.0 dB line amplifier, placed on purpose where it can be seen."""


def asymmetric_section() -> SectionInput:
    """Two chains that differ, which the shipped dataset cannot demonstrate.

    Three spans of 50, 70 and 90 km. The `a_to_b` chain is four identical 4.0 dB
    line amplifiers. The `b_to_a` chain is the same except at **position 2**,
    which carries a 6.0 dB unit.

    Position 2 of a `b_to_a` chain is the amplifier that follows the second span
    counting from `roadm_b`, which is the 90 km span. That is the whole of the
    numbering convention, and no assertion over the shipped dataset can reach
    it, because all 306 shipped amplifiers are identical.
    """
    quiet = [AmplifierInput(f"amp-asym-f{position}", LINE_NF_MDB, LINE_GAIN_MDB) for position in range(1, 5)]
    loud = [AmplifierInput(f"amp-asym-r{position}", LINE_NF_MDB, LINE_GAIN_MDB) for position in range(1, 5)]
    loud[1] = AmplifierInput("amp-asym-r2", NOISY_NF_MDB, LINE_GAIN_MDB)
    return SectionInput(
        name="oms-test-asym",
        head_node=NodeInput("roadm-head", 7_000),
        tail_node=NodeInput("roadm-tail", 4_000),
        spans=(make_span("span-50", 50, 12), make_span("span-70", 70, 17), make_span("span-90", 90, 22)),
        amplifiers_a2b=tuple(quiet),
        amplifiers_b2a=tuple(loud),
    )


def test_a_flip_swaps_the_two_chains_and_reverses_neither() -> None:
    """The correctness fix, asserted directly.

    Reversing one chain modelled the same erbium amplifiers running backwards.
    Swapping two chains models the hardware that is actually there. The spans
    still reverse, because a walk from `roadm_b` meets the last span first, and
    flipping twice has to return the section unchanged.
    """
    section = asymmetric_section()
    flipped = section.flipped()

    assert flipped.amplifiers_a2b == section.amplifiers_b2a
    assert flipped.amplifiers_b2a == section.amplifiers_a2b
    assert flipped.active_amplifiers == section.amplifiers_b2a
    assert [amplifier.name for amplifier in flipped.active_amplifiers] == [
        "amp-asym-r1",
        "amp-asym-r2",
        "amp-asym-r3",
        "amp-asym-r4",
    ]
    assert [span.name for span in flipped.spans] == ["span-90", "span-70", "span-50"]
    assert flipped.endpoints == ("roadm-tail", "roadm-head")
    assert flipped.flipped() == section


def test_one_chain_short_by_one_fails_on_its_own_and_the_message_names_it() -> None:
    """N spans need N+1 amplifiers per direction. A hole in one chain is not a
    hole in the other, and an operator reading the failure needs to know which
    half of the section to go and look at."""
    whole = asymmetric_section()
    whole.validate()

    short = replace(whole, amplifiers_b2a=whole.amplifiers_b2a[:-1])
    with pytest.raises(ValueError, match=r"oms-test-asym b_to_a chain has 3 amplifiers for 3 spans, expected 4"):
        short.validate()

    # The other direction is untouched by its neighbour's hole.
    assert len(short.amplifiers_a2b) == len(short.spans) + 1


def test_the_two_directions_of_the_asymmetric_section_budget_differently() -> None:
    """Where the noisy amplifier sits decides the answer, so the position is
    asserted rather than the mere fact that the two directions differ.

    **a_to_b**, from `roadm-head`, four identical 4.0 dB units:

        f1 behind the 7.0 dB head node : 3000 -  7000 - 4000 + 58000 = 50000
        f2 behind span-50, 11200 mdB   : 3000 - 11200 - 4000 + 58000 = 45800
        f3 behind span-70, 15450 mdB   : 3000 - 15450 - 4000 + 58000 = 41550
        f4 behind span-90, 19700 mdB   : 3000 - 19700 - 4000 + 58000 = 37300

        cascade = 35.338807 dB -> 35339 mdB, less 2 x 500 of node penalty
                = 34339 mdB

    **b_to_a**, from `roadm-tail`. The chain is not reversed, so r2 is still
    second, and second in this direction means behind the 90 km span:

        r1 behind the 4.0 dB tail node : 3000 -  4000 - 4000 + 58000 = 53000
        r2 behind span-90, 6.0 dB unit : 3000 - 19700 - 6000 + 58000 = 35300
        r3 behind span-70              : 3000 - 15450 - 4000 + 58000 = 41550
        r4 behind span-50              : 3000 - 11200 - 4000 + 58000 = 45800

        cascade = 34.018448 dB -> 34018 mdB, less the same 1000
                = 33018 mdB

    Number that chain from `roadm_a` instead and the 6.0 dB unit lands behind
    the 70 km span, giving 39550 in place of 35300 and a total of 34835 mdB.
    Asserting only that the two directions differ would pass under either
    numbering, and so would assert nothing about the one thing that can go
    wrong here.
    """
    forward = evaluate_path([asymmetric_section()], MODE_400G_16QAM, start_node="roadm-head")
    backward = evaluate_path([asymmetric_section()], MODE_400G_16QAM, start_node="roadm-tail")

    assert [hop.osnr_stage_mdb for hop in forward.hops if hop.kind == AMPLIFIER] == [50_000, 45_800, 41_550, 37_300]
    assert [hop.osnr_stage_mdb for hop in backward.hops if hop.kind == AMPLIFIER] == [53_000, 35_300, 41_550, 45_800]

    assert forward.osnr_total_mdb == 34_339
    assert backward.osnr_total_mdb == 33_018
    assert forward.osnr_margin_mdb == 34_339 - 24_500 - 1_000
    assert backward.osnr_margin_mdb == 33_018 - 24_500 - 1_000
    assert backward.total_loss_mdb == forward.total_loss_mdb


# --------------------------------------------------------------------------
# Raman
# --------------------------------------------------------------------------

PUMP_GAIN_MDB = 10_000
"""10.0 dB of on-off gain, under the 15.0 dB ceiling the schema enforces."""

COMBINER_LOSS_MDB = 700
"""0.7 dB, the pump combiner sitting in line on the fibre."""


def raman_section(raman_gain_mdb: int = 0, pump_loss_mdb: int = 0) -> SectionInput:
    """One 90 km span between two identical 7.0 dB ROADMs, two identical amps.

    Everything except the Raman terms is symmetric, in both the nodes and the
    two chains, so any difference between the two directions is the pump and
    nothing else.
    """
    chain = (
        AmplifierInput("amp-raman-1", LINE_NF_MDB, LINE_GAIN_MDB),
        AmplifierInput("amp-raman-2", LINE_NF_MDB, LINE_GAIN_MDB),
    )
    span = replace(
        make_span("span-90", 90, 22),
        raman_gain_mdb=raman_gain_mdb,
        pump_loss_mdb=pump_loss_mdb,
    )
    return SectionInput(
        name="oms-test-raman",
        head_node=NodeInput("roadm-head", 7_000),
        tail_node=NodeInput("roadm-tail", 7_000),
        spans=(span,),
        amplifiers_a2b=chain,
        amplifiers_b2a=chain,
    )


def test_the_combiner_is_charged_both_ways_while_the_gain_is_credited_one_way() -> None:
    """A 90 km span at 19700 mdB, a 10.0 dB pump and a 0.7 dB combiner.

        pumped direction  : 19700 + 700 - 10000 = 10400 mdB
        unpumped direction: 19700 + 700 -     0 = 20400 mdB

    So the pumped direction improves by the on-off gain less the combiner, 9300
    mdB, and the unpumped direction is worse by the combiner alone, 700 mdB. A
    treatment that improved one direction and cost the other nothing would be a
    number chosen to look good.

    `span_loss_mdb` inherits the reduction with no edit of its own, which is
    what puts the power budget and the OSNR path on the same figure.
    """
    span = replace(make_span("span-90", 90, 22), raman_gain_mdb=PUMP_GAIN_MDB, pump_loss_mdb=COMBINER_LOSS_MDB)

    assert span_fiber_loss_mdb(span) == 10_400
    assert span_fiber_loss_mdb(span) == 19_700 - (PUMP_GAIN_MDB - COMBINER_LOSS_MDB)
    assert span_fiber_loss_mdb(span.flipped()) == 20_400
    assert span_fiber_loss_mdb(span.flipped()) == 19_700 + COMBINER_LOSS_MDB
    assert span_loss_mdb(span) == 10_400 + 1_500


def test_a_pumped_section_improves_one_direction_and_costs_the_other_the_combiner() -> None:
    """The same claim one level up, where a report reads it.

    Both ROADMs are 7.0 dB and both chains are identical, so the pre-amplifier
    stage is the only thing that can move.

        unpumped        : 3000 - 19700 - 4000 + 58000 = 37300
        pumped, forward : 3000 - 10400 - 4000 + 58000 = 46600
        pumped, reverse : 3000 - 20400 - 4000 + 58000 = 36600

    46600 - 37300 = 9300, the on-off gain less the combiner.
    37300 - 36600 =  700, the combiner on its own.
    """
    plain = raman_section()
    pumped = raman_section(raman_gain_mdb=PUMP_GAIN_MDB, pump_loss_mdb=COMBINER_LOSS_MDB)

    def stages(section: SectionInput, start: str) -> list[int | None]:
        budget = evaluate_path([section], MODE_400G_16QAM, start_node=start)
        return [hop.osnr_stage_mdb for hop in budget.hops if hop.kind == AMPLIFIER]

    assert stages(plain, "roadm-head") == [50_000, 37_300]
    assert stages(plain, "roadm-tail") == [50_000, 37_300]
    assert stages(pumped, "roadm-head") == [50_000, 46_600]
    assert stages(pumped, "roadm-tail") == [50_000, 36_600]

    forward = evaluate_path([pumped], MODE_400G_16QAM, start_node="roadm-head")
    backward = evaluate_path([pumped], MODE_400G_16QAM, start_node="roadm-tail")
    unpumped = evaluate_path([plain], MODE_400G_16QAM, start_node="roadm-head")
    assert forward.osnr_margin_mdb > unpumped.osnr_margin_mdb > backward.osnr_margin_mdb


def over_pumped_span() -> SpanInput:
    """A short span carrying two pumps in the same direction.

    80 km at 200 mdB/km plus four 50 mdB splices is 16200 mdB, a little above
    the 16167 mdB shortest span in the shipped plant. The schema caps one pump
    at 15000 mdB, so one pump cannot reach the floor and two can:

        one pump : 16200 +  700 - 15000 =   1900 mdB
        two pumps: 16200 + 1400 - 30000 = -12400 mdB
    """
    return SpanInput(
        name="span-over-pumped",
        length_m=80_000,
        attenuation_mdb_per_km=G652_ATTENUATION_MDB_PER_KM,
        dispersion_fs_per_nm_km=G652_DISPERSION_FS_PER_NM_KM,
        splice_count=4,
        splice_loss_mdb=50,
        raman_gain_mdb=30_000,
        pump_loss_mdb=1_400,
    )


def test_one_pump_cannot_reach_the_floor_on_the_shortest_span_in_the_plant() -> None:
    """Worth asserting, because a zero-floor test written with a single pump
    would clamp nothing and pass anyway."""
    single = replace(over_pumped_span(), raman_gain_mdb=15_000, pump_loss_mdb=700)
    assert span_fiber_loss_mdb(single) == 1_900


def test_effective_span_loss_is_floored_at_zero_and_the_clamp_does_not_raise() -> None:
    """Two 15.0 dB pumps against 16200 mdB of fibre gives -12400 mdB, and a
    negative loss would read through the rest of the arithmetic as gain.

    The floor is a clamp rather than a raise because
    `PathElement.fiber_loss_mdb` reaches this function once per hop while the
    budget report renders, and raising there turns a data error into a failed
    render.
    """
    span = over_pumped_span()
    assert span_fiber_loss_mdb(span) == 0
    assert span_loss_mdb(span) == 0


def test_validate_is_where_an_over_pumped_span_is_reported() -> None:
    """The clamp keeps the render alive; this is what tells a person. The OSNR
    check already wraps `validate()` in a `ValueError` boundary and turns it
    into a named check error, so this message is the one they read."""
    section = SectionInput(
        name="oms-test-over-pumped",
        head_node=NodeInput("roadm-head", 7_000),
        tail_node=NodeInput("roadm-tail", 7_000),
        spans=(over_pumped_span(),),
        amplifiers_a2b=make_amplifiers(2, "amp-over"),
        amplifiers_b2a=mirror_amplifiers(2, "amp-over"),
    )
    with pytest.raises(ValueError, match="credited 12400 mdB more a_to_b Raman gain"):
        section.validate()
    with pytest.raises(ValueError, match="credited 12400 mdB more"):
        evaluate_path([section], MODE_400G_16QAM, start_node="roadm-head")


# --------------------------------------------------------------------------
# The route: segments, the conjunction, and the margin that does not exist
# --------------------------------------------------------------------------

MODE_600G_64QAM_DEMANDING = ModeInput(
    name="DP-64QAM 64GBd 600G",
    required_osnr_mdb=35_000,
    cd_tolerance_fs_per_nm=50_000_000,
    fec_latency_ns=4_000,
)
"""A mode the uneven section cannot carry and the second section can.

34321 delivered against 35000 required leaves segment 1 at -1.679 dB; 39366
against the same requirement leaves segment 2 at +3.366 dB. One segment short
and one comfortable is the case the conjunction exists for, and a fixture where
both fail would not distinguish a conjunction from a disjunction.
"""

FRAMING_LATENCY_NS = 3_000
"""What the O-E-O device in these fixtures charges for framing.

Deliberately larger than the 150 ns ROADM term and the 100 ns amplifier term, so
a route latency that dropped it fails by a visible amount rather than by
rounding.
"""


def regenerated_route(mode: ModeInput = MODE_400G_16QAM) -> tuple[SegmentInput, SegmentInput]:
    """Two segments joined at an O-E-O device, on one mode either side.

    Segment 1 is the uneven section walked from `roadm-head`, segment 2 the
    second section walked from `roadm-tail`. They are contiguous, so this is the
    same geography as the two-section path above, cut at the shared ROADM. That
    makes the pair directly comparable with `evaluate_path` over both sections,
    which is what the tests below use to show what regeneration changes.
    """
    return (
        SegmentInput(
            sections=(uneven_section(),),
            mode=mode,
            start_node="roadm-head",
            regenerator=RegeneratorInput("oeo-tail-01", FRAMING_LATENCY_NS),
        ),
        SegmentInput(sections=(second_section(),), mode=mode, start_node="roadm-tail"),
    )


def test_a_single_segment_route_is_exactly_what_evaluate_path_returns() -> None:
    """The compatibility claim, asserted rather than assumed.

    Every existing figure comes from `evaluate_path`, and 99 per cent of the
    shipped dataset is one segment of one. A route wrapper that changed those
    numbers would move every published claim, so it must not.
    """
    direct = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-head")
    route = evaluate_route([SegmentInput(sections=(uneven_section(),), mode=MODE_400G_16QAM, start_node="roadm-head")])

    assert route.segment_count == 1
    assert not route.is_regenerated
    assert route.regenerators == ()
    assert route.sole_segment() == direct
    assert route.latency_ns == direct.latency_ns
    assert route.total_length_m == direct.total_length_m
    assert route.ok == direct.ok


def test_a_two_segment_route_closes_when_both_segments_close() -> None:
    """Both segments pass at 400G 16QAM, so the conjunction is true.

    Segment 1 delivers 34321 mdB against 24500 required plus 1000 of system
    margin, which is +8.821 dB. Segment 2 delivers 39366, which is +13.866 dB.
    Neither figure is the route's margin and the route has none.
    """
    route = evaluate_route(regenerated_route())

    assert route.segment_count == 2
    assert route.is_regenerated
    assert route.ok
    assert route.failing_segments == ()
    assert route.segment_margins_mdb == ((1, 8_821), (2, 13_866))
    assert [name for name, _ in ((d.name, d) for d in route.regenerators)] == ["oeo-tail-01"]


def test_the_cascade_restarts_at_the_device_so_each_segment_beats_the_whole_path() -> None:
    """FR-012, and the only reason regeneration is worth building.

    The same geography as one uninterrupted path delivers 32638 mdB, because the
    cascade runs over all seven amplifiers. Cut at the device, the two cascades
    are four stages and three stages, and both land above the joint figure:
    34321 and 39366. A route budget that carried one cascade across the device
    would charge the second segment for noise the device removed.
    """
    joint = evaluate_path([uneven_section(), second_section()], MODE_400G_16QAM, start_node="roadm-head")
    route = evaluate_route(regenerated_route())

    assert joint.osnr_total_mdb == 32_638
    assert [segment.budget.osnr_total_mdb for segment in route.segments] == [34_321, 39_366]
    for _, margin in route.segment_margins_mdb:
        assert margin > joint.osnr_margin_mdb


def test_each_segment_is_evaluated_against_its_own_mode() -> None:
    """FR-012's second half. The two halves of a regenerated route are
    independent optical paths, so the shorter one can run a mode the longer one
    cannot, and the budget has to read each segment's own requirement.

    Segment 1 keeps 400G 16QAM at 24500 required, so +8.821 dB. Segment 2 takes
    the 35000 requirement, so 39366 - 35000 - 1000 = +3.366 dB. A route budget
    that used one mode for both would report 13866 for segment 2.
    """
    first, second = regenerated_route()
    route = evaluate_route((first, replace(second, mode=MODE_600G_64QAM_DEMANDING)))

    assert route.segment_margins_mdb == ((1, 8_821), (2, 3_366))
    assert route.ok


def test_a_route_does_not_close_when_one_segment_fails_and_it_names_that_segment() -> None:
    """The conjunction, and a negative result as a first-class output.

    Segment 1 is 1.679 dB short at the demanding mode and segment 2 is 3.366 dB
    clear. A route that averaged, or that took the better half, or that took the
    first half, would all report something other than a refusal. What the route
    reports instead is `False` plus the number of the segment that is short.
    """
    route = evaluate_route(regenerated_route(MODE_600G_64QAM_DEMANDING))

    assert route.segment_margins_mdb == ((1, -1_679), (2, 3_366))
    assert not route.ok
    assert route.failing_segments == (1,)


def test_a_route_does_not_close_when_the_failing_segment_is_the_second_one() -> None:
    """The mirror of the test above, because a conjunction written as
    `segments[0].ok` would pass that one and fail this."""
    first, second = regenerated_route()
    route = evaluate_route(
        (
            first,
            replace(
                second,
                mode=ModeInput("impossible", required_osnr_mdb=60_000, cd_tolerance_fs_per_nm=50_000_000),
            ),
        )
    )

    assert route.ok is False
    assert route.failing_segments == (2,)


def test_the_route_latency_is_the_segment_sum_plus_every_framing_delay() -> None:
    """FR-013, digit by digit.

        segment 1: 1033011 ns, propagation plus two ROADMs, four amplifiers and FEC
        segment 2:  592206 ns, the same shape over two 60 km spans
        framing  :    3000 ns, charged once, at the one device
        route    : 1033011 + 592206 + 3000 = 1628217 ns

    The joint one-path figure over the same geography is 1621067 ns. The route is
    7150 ns longer, and every nanosecond of that is accounted for: 3000 of
    framing, 4000 of a second FEC because a regenerator re-encodes, and 150
    because the shared ROADM is crossed twice, once dropping into the device and
    once adding out of it.
    """
    route = evaluate_route(regenerated_route())
    joint = evaluate_path([uneven_section(), second_section()], MODE_400G_16QAM, start_node="roadm-head")

    assert [segment.budget.latency_ns for segment in route.segments] == [1_033_011, 592_206]
    assert route.latency_ns == 1_033_011 + 592_206 + FRAMING_LATENCY_NS
    assert route.latency_ns == 1_628_217
    assert joint.latency_ns == 1_621_067
    assert route.latency_ns - joint.latency_ns == FRAMING_LATENCY_NS + 4_000 + ROADM_LATENCY_NS


def test_a_device_that_adds_no_framing_delay_adds_nothing_to_the_route() -> None:
    """`framing_latency_ns` defaults to 0 on the schema, so a device nobody has
    characterised adds nothing rather than adding a guess. The route figure has
    to read the same way."""
    first, second = regenerated_route()
    route = evaluate_route((replace(first, regenerator=RegeneratorInput("oeo-uncharacterised")), second))
    assert route.latency_ns == 1_033_011 + 592_206


def test_the_framing_delay_of_every_device_is_charged_and_charged_once() -> None:
    """Three segments, two devices, and no double count.

    A device is attached to the segment it terminates, so the count of devices
    is the count of segments less one by construction. That is what makes a
    doubled framing delay unrepresentable rather than merely untested.
    """
    route = evaluate_route(
        (
            SegmentInput(
                (uneven_section(),), MODE_400G_16QAM, "roadm-head", RegeneratorInput("oeo-one", FRAMING_LATENCY_NS)
            ),
            SegmentInput((second_section(),), MODE_400G_16QAM, "roadm-tail", RegeneratorInput("oeo-two", 500)),
            SegmentInput((uneven_section(),), MODE_400G_16QAM, "roadm-head"),
        )
    )

    assert route.segment_count == 3
    assert [device.name for device in route.regenerators] == ["oeo-one", "oeo-two"]
    assert route.latency_ns == 1_033_011 + 592_206 + 1_033_011 + FRAMING_LATENCY_NS + 500
    assert route.total_length_m == 210_000 + 120_000 + 210_000


def test_no_api_returns_a_single_margin_for_a_multi_segment_route() -> None:
    """T020's fourth assertion, and it is about the interface rather than a value.

    A margin on a regenerated route is a claim the model never computed, so the
    API must not have a shape that offers one. This walks every public member of
    a two-segment `RouteBudget` and fails if any of them hands back a bare
    integer under a margin-shaped or OSNR-shaped name.

    Written this way rather than as a list of forbidden attribute names on
    purpose: adding `route_margin_mdb`, `osnr_margin_mdb`, `worst_margin_mdb` or
    `cd_margin_fs_per_nm` later fails this test without anybody having to
    remember to extend it.
    """
    route = evaluate_route(regenerated_route())
    forbidden = ("margin", "osnr", "dispersion", "cd_")
    offenders = []

    for name in dir(route):
        if name.startswith("_"):
            continue
        try:
            value = getattr(route, name)
        except ValueError:
            # `sole_segment` refusing is the behaviour under test, not a breach.
            continue
        if callable(value) or isinstance(value, bool) or not isinstance(value, int):
            continue
        if any(word in name for word in forbidden):
            offenders.append(name)

    assert offenders == [], f"RouteBudget exposes {offenders} as a route-level scalar"

    # The margins are reachable, and only with their segment number attached, so
    # a caller cannot read one out without saying which segment it describes.
    assert route.segment_margins_mdb == ((1, 8_821), (2, 13_866))
    assert all(isinstance(pair, tuple) and len(pair) == 2 for pair in route.segment_margins_mdb)


def test_asking_a_multi_segment_route_for_one_budget_raises_and_says_why() -> None:
    """The other half of the same interface claim.

    `sole_segment` is the only way to a bare `PathBudget`, and it refuses on a
    regenerated route rather than picking one. The message carries both margins,
    because a caller who wanted a number needs to be told which numbers exist.
    """
    route = evaluate_route(regenerated_route())
    with pytest.raises(ValueError, match="2 segments and therefore no single margin"):
        route.sole_segment()
    with pytest.raises(ValueError, match=r"segment 1 \+8\.821 dB, segment 2 \+13\.866 dB"):
        route.sole_segment()


def test_a_route_with_a_gap_in_its_segment_sequence_is_rejected() -> None:
    """The half of the ordering rule the schema cannot take.

    `OtnOpticalPath` carries a uniqueness constraint on `(service,
    segment_sequence)`, so the server refuses a duplicate segment number at write
    time. A constraint has no opinion about absence, so 1, 2, 4 loads happily and
    is a broken circuit. This is where it stops.
    """
    budget = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-head")
    with pytest.raises(ValueError, match=r"segment sequences are \[1, 3\], expected \[1, 2\]"):
        RouteBudget(
            segments=(
                SegmentBudget(1, budget, RegeneratorInput("oeo-one")),
                SegmentBudget(3, budget),
            )
        )


def test_a_route_whose_segments_are_joined_by_nothing_is_rejected() -> None:
    """A middle segment with no device means two segments meeting at no
    regenerator, which is not a route. The message says so rather than producing
    a latency total that looks right."""
    budget = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-head")
    with pytest.raises(ValueError, match="segment 1 of 2 ends at no device"):
        RouteBudget(segments=(SegmentBudget(1, budget), SegmentBudget(2, budget)))


def test_a_device_on_the_last_segment_is_rejected() -> None:
    """The other direction of the same rule. A device past the far end would
    charge a framing delay for a regeneration that never happens, and the
    latency sum would be wrong by exactly one device."""
    budget = evaluate_path([uneven_section()], MODE_400G_16QAM, start_node="roadm-head")
    with pytest.raises(ValueError, match="the last segment carries regenerator oeo-two"):
        RouteBudget(
            segments=(
                SegmentBudget(1, budget, RegeneratorInput("oeo-one")),
                SegmentBudget(2, budget, RegeneratorInput("oeo-two", FRAMING_LATENCY_NS)),
            )
        )


def test_a_route_with_no_segments_at_all_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot budget a route with no segments"):
        evaluate_route([])
    with pytest.raises(ValueError, match="a route has at least one segment"):
        RouteBudget(segments=())
