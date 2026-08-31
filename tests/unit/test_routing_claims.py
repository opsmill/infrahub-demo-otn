"""The two demo scenarios, recomputed from `objects/` through the selector.

Same shape as `test_budget_claims.py` and for the same reason: every number the
demo guide prints is derived here from the committed dataset, so a seed change
fails a test rather than quietly making a published claim false.

Routes are enumerated here rather than fetched, because `client.traverse_paths`
needs a server and these tests do not have one. The enumerator is a plain
depth-limited walk over the twenty-one sections and it is *not* what ships: the
generator uses the server's path traversal. What this module asserts is the
selection, and the selection takes a list of routes from wherever.
`test_geant_dataset.py` already asserts that the section graph is what we think
it is.
"""

from typing import Any

import pytest

from infrahub_demo_otn.budget import SectionInput
from infrahub_demo_otn.plant import CarrierInterval, build_mode, build_section, free_blocks
from infrahub_demo_otn.routing import (
    CHANNEL_NO_SPECTRUM,
    REASON_LATENCY,
    ModeCandidate,
    RouteCandidate,
    choose_route,
)
from infrahub_demo_otn.units import (
    CBAND_EXTENT_MHZ,
    CBAND_LOWER_EDGE_MHZ,
    CBAND_UPPER_EDGE_MHZ,
    GRID_CHANNEL_COUNT,
    carrier_interval_mhz,
    channel_to_frequency_mhz,
    m_to_km,
    mdb_to_db,
    ns_to_us,
    propagation_delay_ns,
)
from tests.unit.conftest import objects_of_kind

QAM16_400G = "DP-16QAM 64GBd 400G"
QPSK_400G = "DP-QPSK 128GBd 400G"

BERLIN = "roadm-ber-01"
AMSTERDAM = "roadm-ams-01"
FRANKFURT = "roadm-fra-01"
MILAN = "roadm-mil-01"

FOUR_MS_NS = 4_000_000
FIVE_MS_NS = 5_000_000


def _by_name(kind: str) -> dict[str, dict[str, Any]]:
    return {str(record["name"]): dict(record) for record in objects_of_kind(kind)}


def _sections() -> dict[str, SectionInput]:
    fibers = _by_name("OtnFiberType")
    spans = _by_name("OtnFiberSpan")
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


def _modes() -> list[ModeCandidate]:
    return [
        ModeCandidate(
            name=name,
            mode_class=str(record["mode_class"]),
            line_rate_gbps=int(record["line_rate_gbps"]),
            baud_mbaud=int(record["baud_mbaud"]),
            budget_input=build_mode(record),
        )
        for name, record in _by_name("OtnOpticalMode").items()
    ]


def _occupancy() -> dict[str, tuple[CarrierInterval, ...]]:
    """The spectrum the shipped carriers hold, per section.

    The same derivation `plant.occupancy_from_graphql` performs on a GraphQL
    payload, rebuilt here from the object files because these tests have no
    server. Each carrier's anchor gives the centre and its mode gives the width,
    which is the one relationship hop this feature is built on.
    """
    modes = _by_name("OtnOpticalMode")
    used: dict[str, list[CarrierInterval]] = {}
    for record in objects_of_kind("OtnOpticalCarrier"):
        channel = int(record["channel"])
        center = channel_to_frequency_mhz(channel)
        mode = str(record["optical_mode"])
        lower, upper = carrier_interval_mhz(center, int(modes[mode]["baud_mbaud"]))
        interval = CarrierInterval(
            carrier=str(record["name"]),
            channel=channel,
            center_mhz=center,
            lower_mhz=lower,
            upper_mhz=upper,
            mode=mode,
        )
        for section in record.get("sections") or []:
            used.setdefault(str(section), []).append(interval)
    return {
        name: tuple(sorted(intervals, key=lambda item: (item.lower_mhz, item.upper_mhz, item.carrier)))
        for name, intervals in used.items()
    }


def _saturated(section: str = "oms-fra-mil") -> dict[str, tuple[CarrierInterval, ...]]:
    """The shipped occupancy with one section holding the whole band.

    What `demo/90_fra_mil_saturated.yml` engineers, expressed as spectrum. One
    interval covering the band edge to edge says "nothing is free here" without
    depending on how many carriers it takes to say it.
    """
    whole = CarrierInterval(
        carrier=f"oc-saturating-{section}",
        channel=48,
        center_mhz=channel_to_frequency_mhz(48),
        lower_mhz=CBAND_LOWER_EDGE_MHZ,
        upper_mhz=CBAND_UPPER_EDGE_MHZ,
        mode="the whole C-band",
    )
    return {**_occupancy(), section: (whole,)}


def _ends() -> dict[str, tuple[str, str]]:
    return {
        name: (str(record["roadm_a"]), str(record["roadm_b"]))
        for name, record in _by_name("OtnOpticalMultiplexSection").items()
    }


def _routes(source: str, destination: str, max_sections: int) -> list[RouteCandidate]:
    """Every simple route of at most `max_sections` sections, source to destination.

    A stand-in for what `client.traverse_paths` returns on a running server, and
    only for the tests. The generator does not contain this function.
    """
    ends = _ends()
    found: list[RouteCandidate] = []

    def walk(node: str, used: list[str], visited: frozenset[str]) -> None:
        if len(used) >= max_sections:
            return
        for name, (side_a, side_b) in ends.items():
            if name in used:
                continue
            nxt = side_b if side_a == node else side_a if side_b == node else None
            if nxt is None or nxt in visited:
                continue
            chain = [*used, name]
            if nxt == destination:
                found.append(RouteCandidate(key="|".join(chain), section_names=tuple(chain), start_node=source))
                continue
            walk(nxt, chain, visited | {nxt})

    walk(source, [], frozenset({source}))
    return found


# --------------------------------------------------------------------------
# Berlin to Amsterdam: the headline demo
# --------------------------------------------------------------------------


def test_berlin_to_amsterdam_offers_four_routes_within_three_sections() -> None:
    """Four routes, and no more.

    Hamburg and Frankfurt at two sections, Prague and Copenhagen at three.
    `test_geant_dataset.py` confirms the lengths from the plant; this confirms
    the graph still produces exactly those four and nothing has grown a fifth.
    """
    keys = {route.key for route in _routes(BERLIN, AMSTERDAM, max_sections=3)}
    assert keys == {
        "oms-ham-ber|oms-ams-ham",
        "oms-ber-fra|oms-ams-fra",
        "oms-ber-prg|oms-prg-fra|oms-ams-fra",
        "oms-ber-cph|oms-ham-cph|oms-ams-ham",
    }


@pytest.mark.parametrize(
    ("route_key", "expected_mode", "expected_km"),
    [
        ("oms-ham-ber|oms-ams-ham", QAM16_400G, 800),
        ("oms-ber-fra|oms-ams-fra", QAM16_400G, 1010),
        ("oms-ber-prg|oms-prg-fra|oms-ams-fra", QPSK_400G, 1220),
        ("oms-ber-cph|oms-ham-cph|oms-ams-ham", QPSK_400G, 1330),
    ],
)
def test_each_berlin_route_picks_the_narrowest_mode_that_closes(
    route_key: str, expected_mode: str, expected_km: int
) -> None:
    """The mode the selector picks, one row per route.

    Prague and Copenhagen fail at DP-16QAM by 0.259 dB and 0.122 dB, and both
    close one modulation order down. All four routes survive the budget gate;
    the losers lose on section count, not on optics.
    """
    sections = _sections()
    route = next(r for r in _routes(BERLIN, AMSTERDAM, max_sections=3) if r.key == route_key)
    result = choose_route([route], sections, _modes(), _occupancy(), rate_gbps=400)
    assert result.selection is not None, f"{route_key} closed on nothing: {result.detail}"
    assert result.selection.mode.name == expected_mode
    assert round(m_to_km(result.selection.budget.total_length_m)) == expected_km


def test_berlin_to_amsterdam_picks_hamburg_on_channel_two() -> None:
    """The headline routing result, computed offline before it is run live.

    **Channel two, not channel one, and occupancy has nothing to do with it.**
    All 71 shipped carriers sit on `oms-fra-mil` and neither Hamburg section
    carries anything, so every anchor on this route is unclaimed. Channel 1 is
    refused by the band edge: the mode is 64 GBd and 79.6 GHz wide, channel 1
    centres 25 GHz above the lower edge of the modelled C-band, and the carrier
    would reach 14.8 GHz past it. The whole route is free and the answer still
    moved, which is the clearest statement of what the width model changed.
    """
    result = choose_route(
        _routes(BERLIN, AMSTERDAM, max_sections=3), _sections(), _modes(), _occupancy(), rate_gbps=400
    )
    assert result.selection is not None
    assert result.selection.route.key == "oms-ham-ber|oms-ams-ham"
    assert result.selection.mode.name == QAM16_400G
    assert result.selection.channel == 2
    assert result.selection.widest_free_mhz == CBAND_EXTENT_MHZ, "nothing crosses either Hamburg section"
    assert result.selection.route.hop_count == 2
    assert 2.2 < mdb_to_db(result.selection.budget.osnr_margin_mdb) < 2.4


def test_hamburg_beats_frankfurt_on_margin_at_the_same_section_count() -> None:
    """The second ranking term, doing real work. Both routes cross two sections;
    +2.284 dB beats +0.507 dB."""
    sections, modes, occupancy = _sections(), _modes(), _occupancy()
    routes = {route.key: route for route in _routes(BERLIN, AMSTERDAM, max_sections=3)}
    hamburg = choose_route([routes["oms-ham-ber|oms-ams-ham"]], sections, modes, occupancy, rate_gbps=400)
    frankfurt = choose_route([routes["oms-ber-fra|oms-ams-fra"]], sections, modes, occupancy, rate_gbps=400)
    assert hamburg.selection is not None and frankfurt.selection is not None
    assert hamburg.selection.route.hop_count == frankfurt.selection.route.hop_count
    assert hamburg.selection.budget.osnr_margin_mdb > frankfurt.selection.budget.osnr_margin_mdb


def test_the_cap_on_route_length_does_not_choose_the_winner() -> None:
    """Widening the search from three sections to four adds two candidates and
    changes nothing. If it did, the cap would be the decision."""
    sections, modes, occupancy = _sections(), _modes(), _occupancy()
    three = choose_route(_routes(BERLIN, AMSTERDAM, 3), sections, modes, occupancy, rate_gbps=400)
    four = choose_route(_routes(BERLIN, AMSTERDAM, 4), sections, modes, occupancy, rate_gbps=400)
    assert len(_routes(BERLIN, AMSTERDAM, 4)) > len(_routes(BERLIN, AMSTERDAM, 3))
    assert three.selection is not None and four.selection is not None
    assert three.selection.route.key == four.selection.route.key
    assert three.selection.channel == four.selection.channel


def test_the_berlin_route_materialises_twenty_five_hops() -> None:
    """Three ROADMs, twelve spans, ten amplifiers. The number the branch diff
    shows, so it is asserted rather than counted by hand at demo time."""
    result = choose_route(_routes(BERLIN, AMSTERDAM, 3), _sections(), _modes(), _occupancy(), rate_gbps=400)
    assert result.selection is not None
    assert len(result.selection.budget.hops) == 25


# --------------------------------------------------------------------------
# Frankfurt to Milan: the AI latency refusal
# --------------------------------------------------------------------------


def test_the_frankfurt_corridor_ships_full_and_takes_exactly_one_more_400g() -> None:
    """The premise of the scenario, measured after the re-seed.

    Forty carriers cross `oms-fra-mil` and 56 channel numbers are unclaimed, and
    those 56 are not 56 places to put a wavelength. The carriers are 44.4, 79.6
    and 150.0 GHz wide on a 50 GHz grid, so the spectrum they leave behind is 26
    blocks and 25 of them are slivers 20,400 MHz or narrower. Counting channel
    numbers on this corridor overstates its free capacity by a factor of 56.

    **One anchor is left, and it is left on purpose.** `demo/90` and `demo/04`
    both saturate this corridor, and both need the spectrum to run out when they
    fill it rather than before they load. The refusal they demonstrate is a
    refusal on tributary slots, and a corridor already out of spectrum would
    answer on the wrong layer.

    The plan before the re-seed asked for 7,306,000 MHz of a 4,800,000 MHz band
    and `checks/channel_collision.py` reported 91 overlapping pairs against it.
    """
    used = _occupancy()["oms-fra-mil"]
    assert len(used) == 40
    assert GRID_CHANNEL_COUNT - len({interval.channel for interval in used}) == 56

    blocks = free_blocks(used)
    assert len(blocks) == 26
    assert max(block.width_mhz for block in blocks) == 152_800
    assert sum(block.width_mhz for block in blocks) == 665_600
    assert sum(interval.upper_mhz - interval.lower_mhz for interval in used) == CBAND_EXTENT_MHZ - 665_600

    result = choose_route(_routes(FRANKFURT, MILAN, 1), _sections(), _modes(), _occupancy(), rate_gbps=400)
    assert result.selection is not None, "one 400G anchor is left on the corridor"
    assert result.selection.mode.name == "DP-16QAM 64GBd 400G"
    assert result.selection.channel == 95, "the last block starts at 195,972,200 MHz and channel 95 is what fits it"


def test_the_geneva_detour_has_no_capacity_problem_at_all() -> None:
    """This is what makes "no capacity" the wrong answer for the AI service:
    the alternative route has every channel free."""
    occupancy = _occupancy()
    assert "oms-fra-gva" not in occupancy
    assert "oms-gva-mil" not in occupancy


def test_the_direct_frankfurt_to_milan_route_fits_inside_four_milliseconds() -> None:
    """3824.741 us end to end, against 3819 us of propagation alone.

    The difference is two ROADMs, ten amplifiers and the FEC, which is why the
    margin against a four millisecond budget is 175 us and not 181.

    `require_free_channel=False`, which is what `generators/optical_service.py`
    passes and what makes the corridor a candidate rather than a refusal. The
    figures asserted here are the route's length and its delay, and neither is a
    function of what the corridor already carries. The strict reading refuses this
    route outright before the re-seed, which the oversubscription test above
    measures.
    """
    routes = [r for r in _routes(FRANKFURT, MILAN, max_sections=1)]
    result = choose_route(
        routes,
        _sections(),
        _modes(),
        _occupancy(),
        rate_gbps=400,
        max_latency_ns=FOUR_MS_NS,
        require_free_channel=False,
    )
    assert result.selection is not None
    latency_us = ns_to_us(result.selection.budget.latency_ns)
    assert 3824.5 < latency_us < 3825.0
    assert 175.0 < ns_to_us(FOUR_MS_NS - result.selection.budget.latency_ns) < 175.5


AI_PAYLOAD_TABLE = (
    # source ROADM, destination ROADM, sections, km, propagation us, end to end us
    ("roadm-fra-01", "roadm-gva-01", 1, 590, 2889.065, 2894.165),
    (FRANKFURT, MILAN, 1, 780, 3819.441, 3824.741),
    ("roadm-vie-01", MILAN, 1, 800, 3917.380, 3922.680),
    (AMSTERDAM, MILAN, 2, 1250, 6120.901, 6127.051),
)
"""The four rows of the electronics-share table in `ai-payloads.mdx`, in the
order the page prints them."""


def _propagation_ns(section_names: tuple[str, ...]) -> int:
    """Fiber propagation over a route, summed span by span at that span's own
    group index. The page says the column is computed this way rather than once
    over the total length, so the test computes it that way too."""
    fibers = _by_name("OtnFiberType")
    spans = _by_name("OtnFiberSpan")
    total = 0
    for name in section_names:
        for span in _by_name("OtnOpticalMultiplexSection")[name]["spans"]:
            record = spans[str(span)]
            index = int(fibers[str(record["fiber_type"])]["group_index_milli"])
            total += propagation_delay_ns(int(record["length_m"]), index)
    return total


@pytest.mark.parametrize(("source", "destination", "sections", "km", "propagation_us", "total_us"), AI_PAYLOAD_TABLE)
def test_the_electronics_share_table_is_what_the_engine_computes(
    source: str, destination: str, sections: int, km: int, propagation_us: float, total_us: float
) -> None:
    """`ai-payloads.mdx` prints four services with their propagation, their end
    to end delay and the share the electronics take, and uses the gap between
    the two columns to rule out a latency-against-reach trade-off. Only the
    Frankfurt to Milan row was asserted anywhere; the other three were prose.

    `require_free_channel=False` for the reason the test above gives: two of the
    four rows cross `oms-fra-mil`, which takes no new 400G until the re-seed, and
    a length and a delay are not functions of the spectrum already lit.
    """
    result = choose_route(
        _routes(source, destination, max_sections=sections),
        _sections(),
        _modes(),
        _occupancy(),
        rate_gbps=400,
        require_free_channel=False,
    )
    assert result.selection is not None, result.reason
    budget = result.selection.budget
    assert round(m_to_km(budget.total_length_m)) == km
    assert round(ns_to_us(_propagation_ns(result.selection.route.section_names)), 3) == propagation_us
    assert round(ns_to_us(budget.latency_ns), 3) == total_us


def test_a_saturated_corridor_refuses_the_ai_service_on_latency_not_capacity() -> None:
    """`choose_route` in its strict reading, which is no longer the scenario.

    Every call here leaves `require_free_channel` at its default of `True`, which
    is the reading a caller gets when it has not thought about grooming: a full
    section is a `capacity` rejection, every surviving route is too slow, and the
    reported reason is latency naming the nearest miss. That is still the
    contract, and it is what the flag defaults to for a reason.

    It is no longer what `demo/90_fra_mil_saturated.yml` produces.
    `generators/optical_service.py` passes `require_free_channel=False`, so the
    saturated corridor comes back as a candidate with `channel=None` and the
    service is refused on slots rather than on latency. That outcome is asserted
    in `tests/unit/test_generator.py`, where the packing decision lives. The
    three tests here assert the routing layer's own behaviour and the latency
    figures `docs/docs/demo-otn/ai-payloads.mdx` publishes, both of which are
    unchanged.
    """
    occupancy = _saturated()
    result = choose_route(
        _routes(FRANKFURT, MILAN, max_sections=4),
        _sections(),
        _modes(),
        occupancy,
        rate_gbps=400,
        max_latency_ns=FOUR_MS_NS,
    )
    assert result.selection is None
    assert result.reason == REASON_LATENCY
    detail = result.detail or ""
    assert detail.startswith("oms-fra-gva|oms-gva-mil")
    assert "990 km" in detail
    assert "4853.605 us" in detail
    assert "misses by 853.605 us" in detail


def test_the_detour_costs_one_thousand_and_twenty_eight_microseconds_end_to_end() -> None:
    """The published 1,028,312 ns is propagation only, and `test_units.py`
    asserts it exactly. End to end the detour costs 552 ns more, from two extra
    ROADMs and four extra amplifiers.

    `require_free_channel=False` on the direct corridor, which takes no new 400G
    before the re-seed. The figure being compared is a delay difference."""
    sections, modes, occupancy = _sections(), _modes(), _occupancy()
    direct = choose_route(
        _routes(FRANKFURT, MILAN, 1), sections, modes, occupancy, rate_gbps=400, require_free_channel=False
    )
    detour = next(r for r in _routes(FRANKFURT, MILAN, 2) if r.key == "oms-fra-gva|oms-gva-mil")
    via_geneva = choose_route([detour], sections, modes, occupancy, rate_gbps=400)
    assert direct.selection is not None and via_geneva.selection is not None
    penalty_ns = via_geneva.selection.budget.latency_ns - direct.selection.budget.latency_ns
    assert penalty_ns == 1_028_864
    assert penalty_ns - 1_028_312 == 552


def test_five_milliseconds_would_not_have_broken_it() -> None:
    """The control: the refusal is a property of the four millisecond budget.

    At a five millisecond budget the saturated corridor provisions on the Geneva
    detour instead of being refused."""
    occupancy = _saturated()
    result = choose_route(
        _routes(FRANKFURT, MILAN, max_sections=4),
        _sections(),
        _modes(),
        occupancy,
        rate_gbps=400,
        max_latency_ns=FIVE_MS_NS,
    )
    assert result.selection is not None
    assert result.selection.route.key == "oms-fra-gva|oms-gva-mil"
    assert result.selection.mode.name == QAM16_400G


@pytest.mark.parametrize(
    ("max_latency_ns", "expected"),
    [
        (FOUR_MS_NS, ["oms-fra-mil"]),
        (FIVE_MS_NS, ["oms-fra-mil", "oms-fra-gva|oms-gva-mil"]),
        (None, ["oms-fra-mil", "oms-fra-gva|oms-gva-mil", "oms-prg-fra|oms-prg-vie|oms-vie-mil"]),
    ],
)
def test_the_candidate_list_the_generator_sees_on_the_saturated_corridor(
    max_latency_ns: int | None, expected: list[str]
) -> None:
    """The controls table in `docs/docs/demo-otn/provisioning-scenarios.mdx`, as the
    generator reads it rather than as the strict default reads it.

    `require_free_channel=False`, which is what
    `generators/optical_service.py` passes, so the exhausted direct corridor is a
    candidate at every budget instead of a `capacity` rejection. At four
    milliseconds it is the only one, which is why the refusal message names it. At
    five milliseconds the Geneva detour joins it and loses, because hop count is
    the first ranking term and one section beats two. Removing the budget adds the
    longer routes below both.

    The four-section route is left off the last row on purpose: it closes, and it
    ranks last, and asserting a prefix of the order is what the doc claims.
    """
    occupancy = _saturated()
    result = choose_route(
        _routes(FRANKFURT, MILAN, max_sections=4),
        _sections(),
        _modes(),
        occupancy,
        rate_gbps=400,
        max_latency_ns=max_latency_ns,
        require_free_channel=False,
    )
    assert [candidate.route.key for candidate in result.candidates][: len(expected)] == expected
    assert result.candidates[0].channel is None, "the direct corridor is out of spectrum and still ranks first"
    assert result.candidates[0].channel_reason == CHANNEL_NO_SPECTRUM, "and it is out of spectrum entirely, not just"
    assert result.candidates[0].widest_free_mhz == 0


def test_without_a_latency_budget_the_saturated_corridor_provisions_the_detour() -> None:
    """The other control. Remove the constraint and the refusal disappears, so
    the refusal is the constraint's doing and not the plant's."""
    occupancy = _saturated()
    result = choose_route(_routes(FRANKFURT, MILAN, 4), _sections(), _modes(), occupancy, rate_gbps=400)
    assert result.selection is not None
    assert result.selection.route.key == "oms-fra-gva|oms-gva-mil"


# --------------------------------------------------------------------------
# The ZR catalog, which is eligible for nothing
# --------------------------------------------------------------------------


def test_no_zr_mode_is_ever_selected() -> None:
    """400ZR would survive the OSNR gate on the Hamburg route and die
    on dispersion, but the reason it is not provisionable is that a ZR
    wavelength originates in a router port and every router port here is grey.
    The selector states that as equipment rather than relying on the arithmetic
    reaching the right answer for the wrong reason.
    """
    for source, destination in ((BERLIN, AMSTERDAM), (FRANKFURT, MILAN)):
        result = choose_route(_routes(source, destination, 3), _sections(), _modes(), _occupancy(), rate_gbps=400)
        assert result.selection is not None
        assert result.selection.mode.mode_class == "transponder"
