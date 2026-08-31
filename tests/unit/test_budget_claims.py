"""The documented numeric claims, recomputed from `objects/` through `budget.py`.

`test_geant_dataset.py` confirms route *lengths* from the plant. Whether a route
closes is a different question: it needs the OSNR cascade and the amplifier
noise figures, so those verdicts are computed here.

Every assertion reads the committed dataset and runs it through the same engine
the check runs. Nothing is quoted from prose. A seed change that moves a span
fails a test rather than quietly making a published claim false.

Margins are asserted with a bracket rather than an exact value. The interesting
routes pass by half a decibel and fail by a quarter, so an exact assertion would
break on a rounding change while a sign-only assertion would miss a route
drifting from comfortable to marginal.
"""

from typing import Any

import pytest

from infrahub_demo_otn.budget import (
    ROADM_LATENCY_NS,
    ModeInput,
    RegeneratorInput,
    RouteBudget,
    SectionInput,
    SegmentInput,
    evaluate_path,
    evaluate_route,
)
from infrahub_demo_otn.plant import build_mode, build_section
from tests.unit.conftest import objects_of_kind

QAM16_400G = "DP-16QAM 64GBd 400G"
QPSK_400G = "DP-QPSK 128GBd 400G"
QPSK_100G = "DP-QPSK 32GBd 100G"

BERLIN_AMSTERDAM_ROUTES: dict[str, list[str]] = {
    "BER-HAM-AMS": ["oms-ham-ber", "oms-ams-ham"],
    "BER-FRA-AMS": ["oms-ber-fra", "oms-ams-fra"],
    "BER-PRG-FRA-AMS": ["oms-ber-prg", "oms-prg-fra", "oms-ams-fra"],
    "BER-CPH-HAM-AMS": ["oms-ber-cph", "oms-ham-cph", "oms-ams-ham"],
}

MADRID_WARSAW = ["oms-par-mad", "oms-par-fra", "oms-prg-fra", "oms-prg-waw"]


def _by_name(kind: str) -> dict[str, dict[str, Any]]:
    return {str(record["name"]): dict(record) for record in objects_of_kind(kind)}


def _spans_with_pumps() -> dict[str, dict[str, Any]]:
    """Every span, carrying the pumps that point at it.

    The object files write the edge on the pump, because `OtnRamanPump.span` is
    the mandatory side and `OtnFiberSpan.raman_pumps` is the inverse. A live
    query traverses that inverse; here the traversal is done by hand, in the
    shape `plant.peers` walks, so the committed Raman data reaches the engine
    the same way it will on a server.

    Without this the pumps would be inert to every offline assertion below.
    Every pumped section would budget as if unpumped, every claim would pass,
    and nothing would say the gain was never read. That is the failure this
    feature exists to remove, so it is not one to reproduce in the tests.
    """
    spans = {name: dict(record) for name, record in _by_name("OtnFiberSpan").items()}
    for pump in objects_of_kind("OtnRamanPump"):
        span = spans[str(pump["span"])]
        span.setdefault("raman_pumps", {"edges": []})["edges"].append({"node": dict(pump)})
    return spans


def _sections() -> dict[str, SectionInput]:
    """Build every section from the committed object YAML.

    The object files hold flat scalars rather than GraphQL wrappers, so this
    calls `plant.build_section` directly. That is the same function the check
    reaches through `sections_from_graphql`, which is what keeps the test and
    the check measuring the same thing.
    """
    fibers = _by_name("OtnFiberType")
    spans = _spans_with_pumps()
    amplifiers = _by_name("OtnAmplifier")
    roadms = _by_name("OtnRoadm")
    built: dict[str, SectionInput] = {}
    for name, record in _by_name("OtnOpticalMultiplexSection").items():
        built[name] = build_section(
            name=name,
            head=roadms[str(record["roadm_a"])],
            tail=roadms[str(record["roadm_b"])],
            spans=[(spans[str(span)], fibers[str(spans[str(span)]["fiber_type"])]) for span in record["spans"]],
            amplifiers_a2b=[amplifiers[str(amplifier)] for amplifier in record["amplifiers_a2b"]],
            amplifiers_b2a=[amplifiers[str(amplifier)] for amplifier in record["amplifiers_b2a"]],
        )
    return built


def _modes() -> dict[str, ModeInput]:
    return {name: build_mode(record) for name, record in _by_name("OtnOpticalMode").items()}


def _budget(section_names: list[str], mode_name: str) -> Any:
    sections = _sections()
    return evaluate_path([sections[name] for name in section_names], _modes()[mode_name])


# --------------------------------------------------------------------------
# The three Berlin to Amsterdam outcomes
# --------------------------------------------------------------------------


def test_the_shortest_berlin_route_wins_at_400g_16qam() -> None:
    """BER-HAM-AMS wins, by 2.284 dB.

    800 km, ten spans, twelve amplifiers, three ROADMs, OSNR 27.784 dB against
    a 24.5 dB requirement and a 1.0 dB system margin.
    """
    budget = _budget(BERLIN_AMSTERDAM_ROUTES["BER-HAM-AMS"], QAM16_400G)
    assert budget.total_length_m == 800_000
    assert budget.amplifier_count == 12
    assert budget.node_count == 3
    assert budget.span_count == 10
    assert budget.osnr_ok
    assert 2_000 < budget.osnr_margin_mdb < 2_600, budget.osnr_margin_mdb
    assert budget.ok


def test_the_middle_berlin_route_is_viable_at_400g_16qam_and_only_just() -> None:
    """BER-FRA-AMS is viable at 400G 16QAM, and closes at +0.507 dB.

    The route runs one percent past the mode's nominal reach, so only an OSNR
    budget can settle it. This is that call.
    """
    budget = _budget(BERLIN_AMSTERDAM_ROUTES["BER-FRA-AMS"], QAM16_400G)
    assert budget.total_length_m == 1_010_000
    assert budget.osnr_ok
    assert 200 < budget.osnr_margin_mdb < 900, budget.osnr_margin_mdb
    assert budget.ok


def test_the_longest_berlin_route_fails_at_400g_16qam() -> None:
    """BER-PRG-FRA-AMS fails at 16QAM, by 0.259 dB.

    It carries 400G on the lower-order modulation instead, which is what makes
    the finding a modulation choice rather than a dead end.
    """
    budget = _budget(BERLIN_AMSTERDAM_ROUTES["BER-PRG-FRA-AMS"], QAM16_400G)
    assert budget.total_length_m == 1_220_000
    assert not budget.osnr_ok
    assert -700 < budget.osnr_margin_mdb < -100, budget.osnr_margin_mdb
    assert _budget(BERLIN_AMSTERDAM_ROUTES["BER-PRG-FRA-AMS"], QPSK_400G).ok


def test_the_three_berlin_verdicts_are_ordered_pass_pass_fail() -> None:
    """The design's whole story in one assertion. If a seed change reorders
    these, the story in section 7 stops being true and this fails first."""
    margins = [_budget(route, QAM16_400G).osnr_margin_mdb for route in list(BERLIN_AMSTERDAM_ROUTES.values())[:3]]
    assert margins == sorted(margins, reverse=True)
    assert margins[0] > 0 and margins[1] > 0 and margins[2] < 0


def test_the_copenhagen_route_is_longer_than_the_prague_route_and_better() -> None:
    """A finding the design does not contain.

    BER-CPH-HAM-AMS is 1330 km, 110 km longer than BER-PRG-FRA-AMS, and has
    0.137 dB more margin. Its sections are built from shorter spans, and span
    loss enters the cascade exponentially while route length enters it linearly.

    Route length does not order OSNR. That is not visible on a map, and it is
    only visible here because spans are modelled individually rather than as a
    section total.
    """
    prague = _budget(BERLIN_AMSTERDAM_ROUTES["BER-PRG-FRA-AMS"], QAM16_400G)
    copenhagen = _budget(BERLIN_AMSTERDAM_ROUTES["BER-CPH-HAM-AMS"], QAM16_400G)
    assert copenhagen.total_length_m > prague.total_length_m
    assert copenhagen.osnr_total_mdb > prague.osnr_total_mdb
    assert not copenhagen.osnr_ok


def test_all_four_berlin_routes_carry_400g_at_qpsk() -> None:
    """The 16QAM split is a modulation choice. At DP-QPSK 128GBd every route
    closes, so nothing about this corridor is unreachable at 400G."""
    for name, route in BERLIN_AMSTERDAM_ROUTES.items():
        budget = _budget(route, QPSK_400G)
        assert budget.ok, f"{name} at {QPSK_400G}: {budget.osnr_margin_mdb} mdB"


def test_the_budget_does_not_depend_on_which_end_the_path_starts_from() -> None:
    """A one-way model whose answer changes with the storage order of a section
    is reporting a property of the data model. Every ROADM carries the same
    insertion loss and every amplifier the same noise figure, so it must not.
    Giving the booster a gain or a noise figure of its own breaks exactly this.
    """
    sections = _sections()
    route = [sections[name] for name in BERLIN_AMSTERDAM_ROUTES["BER-PRG-FRA-AMS"]]
    forward = evaluate_path(route, _modes()[QAM16_400G], start_node="roadm-ber-01")
    backward = evaluate_path(route, _modes()[QAM16_400G], start_node="roadm-ams-01")
    assert forward.osnr_total_mdb == backward.osnr_total_mdb
    assert forward.total_loss_mdb == backward.total_loss_mdb
    assert forward.latency_ns == backward.latency_ns


# --------------------------------------------------------------------------
# The dispersion gate
# --------------------------------------------------------------------------


def test_madrid_to_warsaw_trips_the_dispersion_gate_at_400g() -> None:
    """The one site pair the dispersion gate refuses, through the engine.

    2970 km of G.652.D at 17 fs/nm/km accumulates 50,490,000 fs/nm against a
    50,000,000 tolerance. One site pair, at 400G only, by one percent.
    """
    for mode in (QAM16_400G, QPSK_400G):
        budget = _budget(MADRID_WARSAW, mode)
        assert budget.total_length_m == 2_970_000
        assert budget.cd_total_fs_per_nm == 50_490_000
        assert not budget.cd_ok
        assert budget.cd_margin_fs_per_nm == -490_000


def test_madrid_to_warsaw_passes_both_gates_at_100g() -> None:
    budget = _budget(MADRID_WARSAW, QPSK_100G)
    assert budget.cd_ok
    assert budget.osnr_ok
    assert budget.ok


def test_madrid_to_warsaw_fails_both_gates_at_400g_qpsk_and_the_osnr_one_barely() -> None:
    """A length-and-dispersion check alone cannot reach this result; the OSNR
    cascade is what adds it.

    At DP-QPSK 128GBd the dispersion gate fails by one percent and the OSNR gate
    fails by 0.021 dB. Two independent constraints landing within a quarter of a
    decibel of each other on the same site pair is a coincidence, and it is
    stated as one rather than presented as design.
    """
    budget = _budget(MADRID_WARSAW, QPSK_400G)
    assert not budget.cd_ok
    assert not budget.osnr_ok
    assert -200 < budget.osnr_margin_mdb < 0, budget.osnr_margin_mdb


def test_no_other_site_pair_route_in_the_dataset_trips_the_dispersion_gate() -> None:
    """The claim is that it fires once. Every section taken singly is well inside
    tolerance, so the gate is a route-length property and not a section one."""
    sections = _sections()
    mode = _modes()[QAM16_400G]
    for name, section in sections.items():
        budget = evaluate_path([section], mode)
        assert budget.cd_ok, f"{name} accumulates {budget.cd_total_fs_per_nm} fs/nm"


# --------------------------------------------------------------------------
# The shipped network closes its own gates
# --------------------------------------------------------------------------


def test_every_shipped_carrier_passes_every_gate() -> None:
    """A pre-provisioned network that fails its own check teaches the wrong
    lesson on first run, and this is what stops one shipping."""
    sections = _sections()
    modes = _modes()
    for carrier in objects_of_kind("OtnOpticalCarrier"):
        route = [sections[str(name)] for name in carrier["sections"]]
        budget = evaluate_path(route, modes[str(carrier["optical_mode"])])
        assert budget.osnr_ok, f"{carrier['name']}: OSNR margin {budget.osnr_margin_mdb} mdB"
        assert budget.cd_ok, f"{carrier['name']}: dispersion margin {budget.cd_margin_fs_per_nm} fs/nm"
        assert budget.gain_ok, f"{carrier['name']}: {budget.gain_shortfalls} cannot recover the loss ahead"


def test_the_worst_shipped_margin_is_the_congested_corridor_at_16qam() -> None:
    """The margin as a number rather than as a pass or fail.

    The forty Frankfurt to Milan wavelengths run 780 km at DP-16QAM 64GBd 400G
    and close by 1.894 dB. That is the whole network's headroom, and a change
    that halves it without crossing zero is visible here.
    """
    sections = _sections()
    modes = _modes()
    margins = {
        str(carrier["name"]): (
            evaluate_path(
                [sections[str(name)] for name in carrier["sections"]],
                modes[str(carrier["optical_mode"])],
            ).osnr_margin_mdb,
            [str(name) for name in carrier["sections"]],
        )
        for carrier in objects_of_kind("OtnOpticalCarrier")
    }
    worst = min(margins.items(), key=lambda item: item[1][0])
    margin, crossed = worst[1]
    # Read off the carrier's own sections rather than out of its name. A name is
    # an identifier and the relationship is the fact.
    assert crossed == ["oms-fra-mil"], worst
    assert 1_600 < margin < 2_200, worst


def test_every_amplifier_can_recover_the_loss_ahead_of_it() -> None:
    """The gain gate, over every section taken singly. 22.0 dB against a
    largest span loss of 21.2 dB is 0.8 dB of headroom, so this fires the moment
    somebody stretches a span past roughly 93 km."""
    mode = _modes()[QPSK_100G]
    for name, section in _sections().items():
        budget = evaluate_path([section], mode)
        assert budget.gain_ok, f"{name}: {budget.gain_shortfalls}"


# --------------------------------------------------------------------------
# The reach story
# --------------------------------------------------------------------------


def test_the_worked_example_on_the_link_budget_page_is_what_the_engine_returns() -> None:
    """`link-budget.mdx` walks Amsterdam to Brussels hop by hop and prints a
    verdict table. It is the page's centrepiece and the one place a reader can
    follow the arithmetic all the way through, so every figure in it is checked
    here: the nine hops, the two stage OSNRs, the cumulative OSNR at each
    amplifier, and the six rows of the verdict.
    """
    budget = evaluate_path([_sections()["oms-ams-bru"]], _modes()[QAM16_400G])

    assert len(budget.hops) == 9, "two ROADMs, four amplifiers and three spans"
    assert [hop.osnr_stage_mdb for hop in budget.hops if hop.osnr_stage_mdb] == [50_000, 40_833, 40_833, 40_833]
    assert [hop.cumulative_osnr_mdb for hop in budget.hops[1:]] == [
        49_500,
        49_500,
        39_836,
        39_836,
        37_067,
        37_067,
        35_390,
        34_890,
    ]

    assert [round(hop.cumulative_delay_ns / 1_000, 1) for hop in budget.hops] == [
        0.1,
        0.2,
        359.3,
        359.4,
        718.5,
        718.6,
        1077.7,
        1077.8,
        1078.0,
    ]

    assert budget.total_length_m == 220_000
    assert budget.total_loss_mdb == 67_001
    assert budget.osnr_total_mdb == 34_890
    assert budget.required_osnr_mdb == 24_500
    assert budget.system_margin_mdb == 1_000
    assert budget.osnr_margin_mdb == 9_390
    assert budget.cd_total_fs_per_nm == 3_740_000
    assert budget.cd_tolerance_fs_per_nm == 50_000_000
    assert budget.cd_margin_fs_per_nm == 46_260_000
    assert round(budget.latency_ns / 1_000, 1) == 1082.0


def test_16qam_400g_closes_on_twenty_of_the_twenty_one_sections() -> None:
    """The spectrally efficient 400G mode covers everything except the longest
    single section, Paris to Madrid at 1250 km, which it misses by 0.535 dB.

    It is the longest section in the network by length, and the budget says it
    is also the one thing 16QAM cannot fit down.
    """
    mode = _modes()[QAM16_400G]
    failing = {name: evaluate_path([section], mode) for name, section in _sections().items()}
    failing = {name: budget for name, budget in failing.items() if not budget.osnr_ok}
    assert list(failing) == ["oms-par-mad"]
    assert -900 < failing["oms-par-mad"].osnr_margin_mdb < -200


@pytest.mark.parametrize("mode_name", ["400ZR", "800ZR"])
def test_the_120_km_pluggables_still_reach_nothing_once_osnr_is_computed(mode_name: str) -> None:
    """Nominal reach already settles it: the shortest section is 220 km and both
    120 km pluggables reach nothing. The OSNR budget agrees for a different
    reason, which is the stronger form of the finding.

    400ZR fails on dispersion as well: it tolerates 2400 ps/nm and the shortest
    section accumulates 3740.
    """
    catalog = _by_name("OtnOpticalMode")[mode_name]
    assert int(catalog["nominal_reach_m"]) == 120_000, "this is the 120 km claim, so the catalog reach is the premise"

    mode = _modes()[mode_name]
    shortest = _sections()["oms-ams-bru"]
    budget = evaluate_path([shortest], mode)
    assert not budget.ok, f"{mode_name} on the shortest section: {budget.osnr_margin_mdb} mdB"


# --------------------------------------------------------------------------
# What a regeneration buys on the shipped plant, and what it does not
# --------------------------------------------------------------------------

MADRID_WARSAW_AT_FRANKFURT: tuple[list[str], list[str]] = (
    ["oms-par-mad", "oms-par-fra"],
    ["oms-prg-fra", "oms-prg-waw"],
)
"""Madrid to Warsaw cut at Frankfurt, the middle ROADM of the four-section route.

Frankfurt is the split the two tests below use because it is the only one of the
three interior sites that leaves neither half carrying `oms-par-mad` alone.
"""

REGENERATOR_FRAMING_NS = 3_000
"""The framing delay `oeo-fra-01` ships with, not a placeholder any more.

It was a stated figure while no `OtnOduSwitch` object existed. The device is now
in `objects/19_geant_odu_switches.yml` carrying exactly this value, and
`test_geant_dataset.py::test_the_three_odu_switches_are_two_hub_cross_connects_and_one_regenerator`
holds it there, so the 14,558,963 ns route total below is computed from the
shipped device rather than from a number this module invented.
"""


def _route(split: tuple[list[str], list[str]], mode_name: str, framing_ns: int = REGENERATOR_FRAMING_NS) -> RouteBudget:
    """Two segments over the committed plant, joined at one O-E-O device."""
    sections = _sections()
    mode = _modes()[mode_name]
    first, second = split
    return evaluate_route(
        (
            SegmentInput(
                sections=tuple(sections[name] for name in first),
                mode=mode,
                regenerator=RegeneratorInput("oeo-fra-01", framing_ns),
            ),
            SegmentInput(sections=tuple(sections[name] for name in second), mode=mode),
        )
    )


def test_madrid_to_warsaw_closes_at_400g_qpsk_once_it_is_regenerated_at_frankfurt() -> None:
    """The claim regeneration exists to make, on committed data.

    As one path the route fails both gates: 50,490,000 fs/nm against a
    50,000,000 tolerance and an OSNR margin of -0.021 dB. Cut at Frankfurt it
    closes twice over, at +2.745 dB and +5.740 dB.

    Both gates move, and for the same reason. An O-E-O device terminates the
    light and re-originates it, so accumulated dispersion restarts along with the
    noise cascade: 31,790,000 and 18,700,000 fs/nm, each comfortably inside the
    same tolerance the whole route missed.

    Neither `+2.745` nor `+5.740` is this route's margin, and the route has none.
    """
    joint = _budget(MADRID_WARSAW, QPSK_400G)
    route = _route(MADRID_WARSAW_AT_FRANKFURT, QPSK_400G)

    assert not joint.ok
    assert route.ok
    assert route.is_regenerated
    assert route.failing_segments == ()
    assert route.segment_margins_mdb == ((1, 2_745), (2, 5_740))
    assert [segment.budget.cd_total_fs_per_nm for segment in route.segments] == [31_790_000, 18_700_000]
    assert all(segment.budget.cd_ok for segment in route.segments)
    assert route.total_length_m == joint.total_length_m == 2_970_000


def test_no_single_regeneration_closes_madrid_to_warsaw_at_400g_16qam() -> None:
    """The negative result, stated rather than dropped.

    Regeneration is not a way to make any route carry any mode. At
    DP-16QAM 64GBd the OSNR requirement is 24.5 dB and every one of the three
    interior ROADMs leaves one half short:

        cut at Paris     : -0.535 dB and -2.439 dB, both short
        cut at Frankfurt : -2.755 dB and +0.240 dB, the first short
        cut at Prague    : -4.004 dB and +2.782 dB, the first short

    The Paris figure is the one the link budget page already publishes for
    `oms-par-mad`, so a single section of this route fails 16QAM on its own and
    no cut can rescue it. Two regenerations would be needed, and the route
    verdict says `False` at one rather than reporting the half that passed.
    """
    splits = {
        "PAR": (["oms-par-mad"], ["oms-par-fra", "oms-prg-fra", "oms-prg-waw"]),
        "FRA": (["oms-par-mad", "oms-par-fra"], ["oms-prg-fra", "oms-prg-waw"]),
        "PRG": (["oms-par-mad", "oms-par-fra", "oms-prg-fra"], ["oms-prg-waw"]),
    }
    expected = {
        "PAR": ((1, -535), (2, -2_439)),
        "FRA": ((1, -2_755), (2, 240)),
        "PRG": ((1, -4_004), (2, 2_782)),
    }
    failing = {"PAR": (1, 2), "FRA": (1,), "PRG": (1,)}

    for site, split in splits.items():
        route = _route(split, QAM16_400G)
        assert not route.ok, site
        assert route.segment_margins_mdb == expected[site], site
        assert route.failing_segments == failing[site], site


def test_the_regenerated_route_latency_is_the_segment_sum_plus_the_framing_delay() -> None:
    """FR-013 on committed data, and the figure `demo-latency` will move by.

    As one path Madrid to Warsaw takes 14,551,813 ns. Regenerated at Frankfurt
    the two segments take 9,163,620 and 5,392,343 ns, which is 14,555,963, and
    the device adds 3,000 more for 14,558,963.

    The 4,150 ns between the joint figure and the segment sum is not slack: 4,000
    of it is a second FEC latency, because a regenerator re-encodes and each
    segment is charged its own mode's FEC, and 150 of it is the Frankfurt ROADM
    crossed a second time, once dropping into the device and once adding out of
    it. Both are real delay and both would be missing from a route figure that
    reused the joint walk.
    """
    joint = _budget(MADRID_WARSAW, QPSK_400G)
    route = _route(MADRID_WARSAW_AT_FRANKFURT, QPSK_400G)

    assert joint.latency_ns == 14_551_813
    assert [segment.budget.latency_ns for segment in route.segments] == [9_163_620, 5_392_343]
    assert route.latency_ns == 9_163_620 + 5_392_343 + REGENERATOR_FRAMING_NS
    assert route.latency_ns == 14_558_963
    assert route.latency_ns - joint.latency_ns == REGENERATOR_FRAMING_NS + 4_000 + ROADM_LATENCY_NS


def test_a_regenerated_shipped_route_offers_no_single_margin_either() -> None:
    """The interface claim, asserted against committed data as well as fixtures.

    `test_budget.py` walks the whole public surface; this is the shorter version
    on a real route, because the report that will quote these figures reads a
    `RouteBudget` built exactly this way.
    """
    route = _route(MADRID_WARSAW_AT_FRANKFURT, QPSK_400G)

    assert not hasattr(route, "osnr_margin_mdb")
    assert not hasattr(route, "osnr_total_mdb")
    with pytest.raises(ValueError, match="2 segments and therefore no single margin"):
        route.sole_segment()
