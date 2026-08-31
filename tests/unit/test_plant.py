"""The adapter between an Infrahub payload and the budget engine.

The check and the transform both read a GraphQL response and both turn it into
`SectionInput`. This module is the one place that conversion happens, and this
is where it is tested, offline, against a payload shaped exactly like the one
`queries/osnr_margin.gql` returns.

The interesting assertions are the ones that would otherwise only fail against a
running server: that a section's spans and amplifiers are sorted by
`oms_sequence` and not by the order the query happened to return them, that a
missing sequence is an error rather than a position of zero, that the section's
two amplifier relationships arrive as two chains and are sorted independently,
that a query selecting only one of them is refused by name rather than budgeted
short, and that the Raman pumps on a span sum to the three integers the engine
reads.
"""

from typing import Any

import pytest

from infrahub_demo_otn import plant
from infrahub_demo_otn.plant import (
    build_span,
    carriers_from_graphql,
    nodes_of,
    peer,
    peers,
    sections_from_graphql,
    unwrap,
)
from infrahub_demo_otn.units import (
    CBAND_EXTENT_MHZ,
    CBAND_LOWER_EDGE_MHZ,
    CBAND_UPPER_EDGE_MHZ,
    carrier_interval_mhz,
    channel_to_frequency_mhz,
)

FIBER = {
    "name": {"value": "G.652.D"},
    "attenuation_mdb_per_km": {"value": 200},
    "dispersion_fs_per_nm_km": {"value": 17_000},
    "group_index_milli": {"value": 1468},
}


def span_node(name: str, sequence: int, length_m: int) -> dict[str, Any]:
    return {
        "name": {"value": name},
        "oms_sequence": {"value": sequence},
        "length_m": {"value": length_m},
        "splice_count": {"value": 18},
        "splice_loss_mdb": {"value": 50},
        "connector_count": {"value": 2},
        "connector_loss_mdb": {"value": 300},
        "aging_margin_mdb": {"value": 1_500},
        "fiber_type": {"node": FIBER},
    }


def amplifier_node(name: str, sequence: int) -> dict[str, Any]:
    """One amplifier record. No direction on it: which chain it is in is which
    of the section's two relationships holds it."""
    return {
        "name": {"value": name},
        "oms_sequence": {"value": sequence},
        "noise_figure_mdb": {"value": 4_000},
        "gain_mdb": {"value": 22_000},
    }


def roadm_node(name: str) -> dict[str, Any]:
    return {"name": {"value": name}, "insertion_loss_mdb": {"value": 7_000}}


def payload(shuffle: bool = False) -> dict[str, Any]:
    """One carrier over one two-span section.

    With `shuffle`, the spans and amplifiers come back in reverse sequence
    order, which is what an unordered relationship is entitled to do.
    """
    spans = [span_node("span-b", 2, 80_000), span_node("span-a", 1, 70_000)]
    forward = [amplifier_node("amp-c", 3), amplifier_node("amp-a", 1), amplifier_node("amp-b", 2)]
    reverse = [amplifier_node("amp-z", 3), amplifier_node("amp-x", 1), amplifier_node("amp-y", 2)]
    if not shuffle:
        spans = sorted(spans, key=lambda node: node["oms_sequence"]["value"])
        forward = sorted(forward, key=lambda node: node["oms_sequence"]["value"])
        reverse = sorted(reverse, key=lambda node: node["oms_sequence"]["value"])
    return {
        "OtnOpticalCarrier": {
            "edges": [
                {
                    "node": {
                        "id": "abc",
                        "__typename": "OtnOpticalCarrier",
                        "name": {"value": "oc-ch001-test"},
                        "optical_mode": {
                            "node": {
                                "name": {"value": "DP-QPSK 128GBd 400G"},
                                "required_osnr_mdb": {"value": 19_000},
                                "cd_tolerance_fs_per_nm": {"value": 50_000_000},
                                "fec_latency_ns": {"value": 4_000},
                            }
                        },
                        "sections": {"edges": [{"node": {"name": {"value": "oms-test"}}}]},
                    }
                }
            ]
        },
        "OtnOpticalMultiplexSection": {
            "edges": [
                {
                    "node": {
                        "id": "def",
                        "__typename": "OtnOpticalMultiplexSection",
                        "name": {"value": "oms-test"},
                        "roadm_a": {"node": roadm_node("roadm-a")},
                        "roadm_b": {"node": roadm_node("roadm-b")},
                        "spans": {"edges": [{"node": node} for node in spans]},
                        "amplifiers_a2b": {"edges": [{"node": node} for node in forward]},
                        "amplifiers_b2a": {"edges": [{"node": node} for node in reverse]},
                    }
                }
            ]
        },
    }


def test_unwrap_flattens_attributes_and_leaves_relationships_alone() -> None:
    flat = unwrap({"name": {"value": "x"}, "roadm_a": {"node": {"name": {"value": "y"}}}})
    assert flat["name"] == "x"
    assert flat["roadm_a"] == {"node": {"name": {"value": "y"}}}


def test_peer_and_peers_walk_the_two_relationship_shapes() -> None:
    node = unwrap(payload()["OtnOpticalMultiplexSection"]["edges"][0]["node"])
    assert peer(node, "roadm_a")["name"] == "roadm-a"
    assert [span["name"] for span in peers(node, "spans")] == ["span-a", "span-b"]


def test_peer_raises_when_a_cardinality_one_relationship_is_empty() -> None:
    with pytest.raises(ValueError, match="has no peer"):
        peer({"roadm_a": {"node": None}}, "roadm_a")


def test_nodes_of_returns_nothing_for_a_kind_the_query_did_not_select() -> None:
    assert list(nodes_of(payload(), "OtnFiberSpan")) == []


def test_a_section_is_ordered_by_sequence_and_not_by_arrival() -> None:
    """The whole reason `oms_sequence` was added. Infrahub relationships carry
    no order, so the query is entitled to return the chain backwards, and the
    budget is meaningless if the adapter trusts it."""
    arrived = sections_from_graphql(payload(shuffle=True))["oms-test"]
    assert [span.name for span in arrived.spans] == ["span-a", "span-b"]
    assert [amplifier.name for amplifier in arrived.amplifiers_a2b] == ["amp-a", "amp-b", "amp-c"]
    assert [amplifier.name for amplifier in arrived.amplifiers_b2a] == ["amp-x", "amp-y", "amp-z"]
    assert arrived.head_node.name == "roadm-a"
    assert arrived.tail_node.name == "roadm-b"


def test_a_span_without_a_sequence_is_an_error_rather_than_position_zero() -> None:
    broken = payload()
    del broken["OtnOpticalMultiplexSection"]["edges"][0]["node"]["spans"]["edges"][0]["node"]["oms_sequence"]
    with pytest.raises(ValueError, match="has no oms_sequence"):
        sections_from_graphql(broken)


def test_an_amplifier_without_a_sequence_is_an_error_too() -> None:
    broken = payload()
    del broken["OtnOpticalMultiplexSection"]["edges"][0]["node"]["amplifiers_a2b"]["edges"][0]["node"]["oms_sequence"]
    with pytest.raises(ValueError, match="has no oms_sequence"):
        sections_from_graphql(broken)


# --------------------------------------------------------------------------
# Two amplifier chains, one per relationship
# --------------------------------------------------------------------------


def directional_payload(
    forward: list[dict[str, Any]],
    reverse: list[dict[str, Any]],
    select: tuple[str, ...] = ("amplifiers_a2b", "amplifiers_b2a"),
) -> dict[str, Any]:
    """One two-span section holding whatever amplifier records are handed in.

    `select` names the relationships the query asked for. Dropping one models a
    query that forgot it, which is the regression the split creates.
    """
    base = payload()
    node = base["OtnOpticalMultiplexSection"]["edges"][0]["node"]
    for key in ("amplifiers_a2b", "amplifiers_b2a"):
        node.pop(key, None)
    for key, records in (("amplifiers_a2b", forward), ("amplifiers_b2a", reverse)):
        if key in select:
            node[key] = {"edges": [{"node": record} for record in records]}
    return base


def test_amplifiers_arrive_as_two_chains_that_are_sorted_independently() -> None:
    """Which chain an amplifier is in is which relationship holds it.

    Both chains arrive out of order, which is what an unordered relationship is
    entitled to return. Each has to come back sorted on its own `oms_sequence`,
    not on a single sequence shared across the section. Nothing reads a stored
    direction, because there is none to read.
    """
    section = sections_from_graphql(
        directional_payload(
            forward=[
                amplifier_node("amp-t-05", 3),
                amplifier_node("amp-t-01", 1),
                amplifier_node("amp-t-03", 2),
            ],
            reverse=[
                amplifier_node("amp-t-04", 2),
                amplifier_node("amp-t-02", 3),
                amplifier_node("amp-t-06", 1),
            ],
        )
    )["oms-test"]
    assert [amplifier.name for amplifier in section.amplifiers_a2b] == ["amp-t-01", "amp-t-03", "amp-t-05"]
    assert [amplifier.name for amplifier in section.amplifiers_b2a] == ["amp-t-06", "amp-t-04", "amp-t-02"]
    section.validate()


def test_a_query_that_selects_only_one_chain_is_refused_by_name() -> None:
    """The one place this feature makes a failure quieter, and the guard for it.

    A stored `direction` used to make a forgetful query raise with the
    amplifier's name in the message. After the split there is no attribute to be
    missing, only an empty list, because `peers` hands back an empty list for a
    key that is not there. Without `validate()` the section would budget as
    healthy on half its amplifiers, which is a plausible wrong number in place of
    a crash.
    """
    section = sections_from_graphql(
        directional_payload(
            forward=[amplifier_node("amp-t-01", 1), amplifier_node("amp-t-03", 2), amplifier_node("amp-t-05", 3)],
            reverse=[],
            select=("amplifiers_a2b",),
        )
    )["oms-test"]
    assert section.amplifiers_b2a == ()
    with pytest.raises(ValueError, match="oms-test"):
        section.validate()


def test_a_chain_short_by_one_fails_that_direction_and_leaves_the_other_alone() -> None:
    """Both keys present and one chain a member short. The N+1 rule names the
    direction, so the message says which half of the section is the hole."""
    section = sections_from_graphql(
        directional_payload(
            forward=[amplifier_node("amp-t-01", 1), amplifier_node("amp-t-03", 2), amplifier_node("amp-t-05", 3)],
            reverse=[amplifier_node("amp-t-06", 1), amplifier_node("amp-t-04", 2)],
        )
    )["oms-test"]
    assert len(section.amplifiers_a2b) == 3
    assert len(section.amplifiers_b2a) == 2
    with pytest.raises(ValueError, match="oms-test"):
        section.validate()


def test_two_full_chains_validate() -> None:
    """The control. Watched passing so the two tests above are known to fail on
    the chain and not on something else in the payload."""
    section = sections_from_graphql(
        directional_payload(
            forward=[amplifier_node("amp-t-01", 1), amplifier_node("amp-t-03", 2), amplifier_node("amp-t-05", 3)],
            reverse=[amplifier_node("amp-t-06", 1), amplifier_node("amp-t-04", 2), amplifier_node("amp-t-02", 3)],
        )
    )["oms-test"]
    section.validate()


# --------------------------------------------------------------------------
# Raman pumps, summed here so the engine reads no pump object
# --------------------------------------------------------------------------


def pump_node(
    name: str,
    gain_mdb: int,
    insertion_loss_mdb: int,
    injection_end: str = "site_b",
    propagation: str = "counter",
) -> dict[str, Any]:
    """One pump record. It stores where it sits and which way it fires, and no
    direction: the direction it amplifies follows from those two."""
    return {
        "name": {"value": name},
        "on_off_gain_mdb": {"value": gain_mdb},
        "injection_end": {"value": injection_end},
        "propagation": {"value": propagation},
        "insertion_loss_mdb": {"value": insertion_loss_mdb},
    }


def pumped_span(*pumps: dict[str, Any]) -> dict[str, Any]:
    node = span_node("span-pumped", 1, 80_000)
    node["raman_pumps"] = {"edges": [{"node": pump} for pump in pumps]}
    return node


def test_pumps_sum_per_direction_and_the_combiner_loss_sums_over_all_of_them() -> None:
    """Two pumps one way, one the other, and three combiners in line.

        a_to_b gain: 12000 + 9000 = 21000 mdB
        b_to_a gain:               = 10000 mdB
        combiner   : 700 + 700 + 800 = 2200 mdB

    Two in one direction is the case that matters: they sum here, so the engine
    sees one integer per direction and needs no co-propagating case of its own.
    """
    span = build_span(
        unwrap(
            pumped_span(
                pump_node("pump-fwd-1", 12_000, 700, injection_end="site_b"),
                pump_node("pump-rev-1", 10_000, 700, injection_end="site_a"),
                pump_node("pump-fwd-2", 9_000, 800, injection_end="site_b"),
            )
        ),
        unwrap(FIBER),
    )
    assert span.raman_gain_mdb == 21_000
    assert span.raman_gain_reverse_mdb == 10_000
    assert span.pump_loss_mdb == 2_200


def test_a_span_with_no_pump_data_yields_zeros_rather_than_a_key_error() -> None:
    """Every one of the 132 shipped spans is this case today, and 123 of them
    stay this case after the branch lands."""
    span = build_span(unwrap(span_node("span-a", 1, 70_000)), unwrap(FIBER))
    assert (span.raman_gain_mdb, span.raman_gain_reverse_mdb, span.pump_loss_mdb) == (0, 0, 0)


@pytest.mark.parametrize(
    ("injection_end", "propagation", "expected"),
    [
        ("site_b", "counter", "forward"),
        ("site_a", "co", "forward"),
        ("site_a", "counter", "reverse"),
        ("site_b", "co", "reverse"),
    ],
)
def test_a_pump_credits_the_direction_its_placement_implies(
    injection_end: str, propagation: str, expected: str
) -> None:
    """All four combinations, because a reader who sees only the two shipped
    cases will assume counter-propagating is the only kind there is.

    A counter-propagating pump fires back up the fibre from the far end, so one
    at the B end amplifies the A to B signal. A co-propagating pump fires along
    with the signal from the near end, so one at the A end amplifies A to B as
    well. The two cases are two ways of reaching the same answer.
    """
    span = build_span(
        unwrap(pumped_span(pump_node("pump-x", 10_000, 700, injection_end=injection_end, propagation=propagation))),
        unwrap(FIBER),
    )
    credited = span.raman_gain_mdb if expected == "forward" else span.raman_gain_reverse_mdb
    other = span.raman_gain_reverse_mdb if expected == "forward" else span.raman_gain_mdb
    assert credited == 10_000
    assert other == 0


@pytest.mark.parametrize(
    ("drop", "replace", "message"),
    [
        ("injection_end", None, "pump-silent has no injection_end"),
        ("propagation", None, "pump-silent has no propagation"),
        (None, ("injection_end", "middle"), "pump-silent has injection_end 'middle'"),
        (None, ("propagation", "sideways"), "pump-silent has propagation 'sideways'"),
    ],
)
def test_a_pump_whose_placement_cannot_be_read_is_an_error(
    drop: str | None, replace: tuple[str, str] | None, message: str
) -> None:
    """Summing it into one direction would credit gain to a walk the pump does
    not touch, and nothing downstream could tell.

    The derivation needs both fields, so this is a query contract of two rather
    than the one a stored direction needed, and both halves raise.
    """
    silent = pump_node("pump-silent", 10_000, 700)
    if drop is not None:
        del silent[drop]
    if replace is not None:
        silent[replace[0]] = {"value": replace[1]}
    with pytest.raises(ValueError, match=message):
        build_span(unwrap(pumped_span(silent)), unwrap(FIBER))


def test_a_pump_carrying_neither_placement_field_is_an_error() -> None:
    """The fifth case: a query that selected the relationship and none of the
    fields the derivation needs."""
    silent = pump_node("pump-silent", 10_000, 700)
    del silent["injection_end"]
    del silent["propagation"]
    with pytest.raises(ValueError, match="pump-silent has no injection_end"):
        build_span(unwrap(pumped_span(silent)), unwrap(FIBER))


def test_a_span_takes_its_coefficients_from_its_fiber_type() -> None:
    span = build_span(unwrap(span_node("span-a", 1, 70_000)), unwrap(FIBER))
    assert span.attenuation_mdb_per_km == 200
    assert span.dispersion_fs_per_nm_km == 17_000
    assert span.group_index_milli == 1468


def test_a_carrier_carries_its_mode_and_its_section_names() -> None:
    carriers = carriers_from_graphql(payload())
    assert len(carriers) == 1
    assert carriers[0]["name"] == "oc-ch001-test"
    assert carriers[0]["section_names"] == ["oms-test"]
    assert carriers[0]["mode"] is not None
    assert carriers[0]["mode"].required_osnr_mdb == 19_000


def test_a_carrier_with_no_mode_reports_none_rather_than_being_dropped() -> None:
    """Silently dropping it would make the check report green for a carrier it
    never measured. The caller decides; the adapter does not hide it."""
    without = payload()
    without["OtnOpticalCarrier"]["edges"][0]["node"]["optical_mode"] = {"node": None}
    carriers = carriers_from_graphql(without)
    assert len(carriers) == 1
    assert carriers[0]["mode"] is None


# --------------------------------------------------------------------------
# The three readers the service generator needs
# --------------------------------------------------------------------------


def _attr(value: object) -> dict[str, object]:
    return {"value": value}


def _mode_payload(name: str, klass: str, rate: int, baud: int) -> dict[str, object]:
    return {
        "node": {
            "name": _attr(name),
            "mode_class": _attr(klass),
            "line_rate_gbps": _attr(rate),
            "baud_mbaud": _attr(baud),
            "required_osnr_mdb": _attr(24500),
            "cd_tolerance_fs_per_nm": _attr(50000000),
            "fec_latency_ns": _attr(4000),
        }
    }


def _carrier_payload(
    name: str,
    channel: int | None,
    sections: list[str],
    baud: int | None = 64000,
    mode_name: str = "DP-16QAM 64GBd 400G",
    with_mode: bool = True,
) -> dict[str, object]:
    """One carrier shaped like `queries/channel_collision.gql` returns it.

    The anchor carries its centre frequency and the mode carries its symbol rate,
    because those two are what the occupied interval is built from. `with_mode`
    and a `baud` of `None` exist to exercise the two fail-closed paths that the
    query contract is supposed to make impossible.
    """
    node: dict[str, object] = {
        "name": _attr(name),
        "sections": {"edges": [{"node": {"name": _attr(section)}} for section in sections]},
    }
    if channel is None:
        node["channel"] = None
    else:
        node["channel"] = {
            "node": {
                "channel_number": _attr(channel),
                "center_frequency_mhz": _attr(channel_to_frequency_mhz(channel)),
            }
        }
    if with_mode:
        node["optical_mode"] = {"node": {"name": _attr(mode_name), "baud_mbaud": _attr(baud)}}
    else:
        node["optical_mode"] = None
    return {"node": node}


def test_modes_from_graphql_carries_the_two_fields_the_selector_ranks_on() -> None:
    payload = {
        "OtnOpticalMode": {
            "edges": [
                _mode_payload("DP-16QAM 64GBd 400G", "transponder", 400, 64000),
                _mode_payload("400ZR", "pluggable", 400, 59840),
            ]
        }
    }
    modes = plant.modes_from_graphql(payload)
    assert [(mode.name, mode.mode_class, mode.baud_mbaud) for mode in modes] == [
        ("DP-16QAM 64GBd 400G", "transponder", 64000),
        ("400ZR", "pluggable", 59840),
    ]
    assert modes[0].budget_input.required_osnr_mdb == 24500


def test_occupancy_counts_one_carrier_against_every_section_it_crosses() -> None:
    payload = {
        "OtnOpticalCarrier": {
            "edges": [
                _carrier_payload("oc-a", 7, ["oms-ams-fra", "oms-fra-mil"]),
                _carrier_payload("oc-b", 9, ["oms-fra-mil"]),
            ]
        }
    }
    occupancy = plant.occupancy_from_graphql(payload)
    assert sorted(occupancy) == ["oms-ams-fra", "oms-fra-mil"]
    assert [(item.carrier, item.channel) for item in occupancy["oms-fra-mil"]] == [("oc-a", 7), ("oc-b", 9)]
    assert [item.carrier for item in occupancy["oms-ams-fra"]] == ["oc-a"]


def test_occupancy_is_an_interval_and_not_a_channel_number() -> None:
    """The whole point of the model. A wavelength occupies a width, and two
    carriers on the same anchor with different modes occupy different widths."""
    payload = {
        "OtnOpticalCarrier": {
            "edges": [
                _carrier_payload("oc-narrow", 41, ["oms-fra-mil"], baud=32000, mode_name="DP-QPSK 32GBd 100G"),
                _carrier_payload("oc-wide", 41, ["oms-fra-mil"], baud=128000, mode_name="DP-QPSK 128GBd 400G"),
            ]
        }
    }
    narrow, wide = sorted(plant.occupancy_from_graphql(payload)["oms-fra-mil"], key=lambda item: item.width_mhz)
    assert (narrow.width_mhz, wide.width_mhz) == (44400, 150000)
    assert narrow.center_mhz == wide.center_mhz == channel_to_frequency_mhz(41)
    assert (narrow.lower_mhz, narrow.upper_mhz) == carrier_interval_mhz(narrow.center_mhz, 32000)


def test_occupancy_sorts_each_section_by_lower_edge() -> None:
    """Sorted on construction, so free_blocks and the overlap test are one pass."""
    payload = {
        "OtnOpticalCarrier": {
            "edges": [
                _carrier_payload("oc-high", 60, ["oms-fra-mil"]),
                _carrier_payload("oc-low", 3, ["oms-fra-mil"]),
                _carrier_payload("oc-mid", 40, ["oms-fra-mil"]),
            ]
        }
    }
    intervals = plant.occupancy_from_graphql(payload)["oms-fra-mil"]
    assert [item.carrier for item in intervals] == ["oc-low", "oc-mid", "oc-high"]
    assert [item.lower_mhz for item in intervals] == sorted(item.lower_mhz for item in intervals)


def test_a_carrier_without_a_channel_is_an_error_not_a_skip() -> None:
    """An allocator that ignores the carrier it could not read hands out a
    channel that is already taken."""
    payload = {"OtnOpticalCarrier": {"edges": [_carrier_payload("oc-a", None, ["oms-fra-mil"])]}}
    with pytest.raises(ValueError, match="oc-a has no channel"):
        plant.occupancy_from_graphql(payload)


def test_a_carrier_crossing_no_section_is_an_error_not_a_skip() -> None:
    payload = {"OtnOpticalCarrier": {"edges": [_carrier_payload("oc-a", 7, [])]}}
    with pytest.raises(ValueError, match="oc-a crosses no section"):
        plant.occupancy_from_graphql(payload)


def test_a_carrier_without_a_mode_is_an_error_not_a_skip() -> None:
    """No mode means no symbol rate means no width. Defaulting it to one channel
    would report green for spectrum nobody measured."""
    payload = {"OtnOpticalCarrier": {"edges": [_carrier_payload("oc-a", 7, ["oms-fra-mil"], with_mode=False)]}}
    with pytest.raises(ValueError, match="oc-a has no optical mode"):
        plant.occupancy_from_graphql(payload)


def test_a_mode_without_a_symbol_rate_is_an_error_not_a_skip() -> None:
    payload = {"OtnOpticalCarrier": {"edges": [_carrier_payload("oc-a", 7, ["oms-fra-mil"], baud=None)]}}
    with pytest.raises(ValueError, match="oc-a runs mode DP-16QAM 64GBd 400G with no symbol rate"):
        plant.occupancy_from_graphql(payload)


def test_an_anchor_without_a_centre_frequency_is_an_error_not_a_skip() -> None:
    """The query contract, stated as a failure. A query that selects the channel
    number and forgets the centre frequency cannot place the interval, and the
    derivation says so rather than guessing the centre off the channel number."""
    payload = {
        "OtnOpticalCarrier": {
            "edges": [
                {
                    "node": {
                        "name": _attr("oc-a"),
                        "channel": {"node": {"channel_number": _attr(7)}},
                        "optical_mode": {"node": {"name": _attr("m"), "baud_mbaud": _attr(64000)}},
                        "sections": {"edges": [{"node": {"name": _attr("oms-fra-mil")}}]},
                    }
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="whose centre frequency the query did not select"):
        plant.occupancy_from_graphql(payload)


# --------------------------------------------------------------------------
# Free blocks, bounded by the band edges and not by the outermost carrier
# --------------------------------------------------------------------------


def _interval(lower: int, upper: int, carrier: str = "oc") -> plant.CarrierInterval:
    return plant.CarrierInterval(
        carrier=carrier,
        channel=1,
        center_mhz=(lower + upper) // 2,
        lower_mhz=lower,
        upper_mhz=upper,
        mode="m",
    )


def test_an_empty_section_is_one_free_block_of_the_whole_band() -> None:
    assert plant.free_blocks(()) == (plant.FreeBlock(CBAND_LOWER_EDGE_MHZ, CBAND_UPPER_EDGE_MHZ),)
    assert plant.free_blocks(())[0].width_mhz == CBAND_EXTENT_MHZ


def test_one_carrier_mid_band_leaves_two_free_blocks_and_not_none() -> None:
    """The reason the band edges bound the derivation rather than the carriers.
    Bounding by the first and last carrier would report a section carrying one
    wavelength as having no free spectrum at all."""
    center = channel_to_frequency_mhz(48)
    lower, upper = carrier_interval_mhz(center, 64000)
    blocks = plant.free_blocks([_interval(lower, upper)])
    assert blocks == (
        plant.FreeBlock(CBAND_LOWER_EDGE_MHZ, lower),
        plant.FreeBlock(upper, CBAND_UPPER_EDGE_MHZ),
    )
    assert sum(block.width_mhz for block in blocks) == CBAND_EXTENT_MHZ - 79600


def test_carriers_touching_the_band_edges_leave_one_block_in_the_middle() -> None:
    """Channel 2 and channel 95 at 128 GBd sit flush against the edges, so the
    only free spectrum is between them and neither edge contributes a block."""
    low_lower, low_upper = carrier_interval_mhz(channel_to_frequency_mhz(2), 128000)
    high_lower, high_upper = carrier_interval_mhz(channel_to_frequency_mhz(95), 128000)
    assert (low_lower, high_upper) == (CBAND_LOWER_EDGE_MHZ, CBAND_UPPER_EDGE_MHZ)
    assert plant.free_blocks([_interval(low_lower, low_upper), _interval(high_lower, high_upper)]) == (
        plant.FreeBlock(low_upper, high_lower),
    )


def test_a_fully_occupied_section_reports_no_free_block() -> None:
    assert plant.free_blocks([_interval(CBAND_LOWER_EDGE_MHZ, CBAND_UPPER_EDGE_MHZ)]) == ()


def test_overlapping_carriers_merge_rather_than_producing_a_negative_block() -> None:
    """A branch that has not passed the collision check can hold two carriers on
    the same spectrum, and the free-block report is one of the things read while
    working out why."""
    first = _interval(CBAND_LOWER_EDGE_MHZ + 100_000, CBAND_LOWER_EDGE_MHZ + 300_000, "oc-a")
    second = _interval(CBAND_LOWER_EDGE_MHZ + 200_000, CBAND_LOWER_EDGE_MHZ + 400_000, "oc-b")
    assert plant.free_blocks([first, second]) == (
        plant.FreeBlock(CBAND_LOWER_EDGE_MHZ, CBAND_LOWER_EDGE_MHZ + 100_000),
        plant.FreeBlock(CBAND_LOWER_EDGE_MHZ + 400_000, CBAND_UPPER_EDGE_MHZ),
    )


def test_a_carrier_running_past_a_band_edge_is_clipped_not_negated() -> None:
    """The carrier is unprovisionable and the collision check is what says so.
    Clipping only stops this function claiming free spectrum outside the band."""
    blocks = plant.free_blocks([_interval(CBAND_LOWER_EDGE_MHZ - 50_000, CBAND_LOWER_EDGE_MHZ + 50_000)])
    assert blocks == (plant.FreeBlock(CBAND_LOWER_EDGE_MHZ + 50_000, CBAND_UPPER_EDGE_MHZ),)


# --------------------------------------------------------------------------
# The overlap predicate, whose whole difficulty is the touching pair
# --------------------------------------------------------------------------


def test_two_disjoint_intervals_do_not_overlap() -> None:
    left = _interval(191_400_000, 191_444_400, "oc-a")
    right = _interval(191_500_000, 191_544_400, "oc-b")
    assert plant.intervals_overlap(left, right) is False
    assert plant.intervals_overlap(right, left) is False


def test_two_intervals_meeting_at_one_frequency_do_not_overlap() -> None:
    """The case the half-open convention exists for.

    The upper edge is the first frequency an interval does not hold, so the
    neighbour that starts there takes nothing from it. Closed intervals would
    report a collision on every boundary of a densely packed plan.
    """
    left = _interval(191_400_000, 191_444_400, "oc-a")
    right = _interval(191_444_400, 191_488_800, "oc-b")
    assert plant.intervals_overlap(left, right) is False
    assert plant.intervals_overlap(right, left) is False


def test_two_partially_overlapping_intervals_overlap() -> None:
    """One megahertz of shared spectrum is a collision.

    The right interval starts one below the left one's upper edge, so they hold
    191,444,399 MHz in common and nothing else.
    """
    left = _interval(191_400_000, 191_444_400, "oc-a")
    right = _interval(191_444_399, 191_488_799, "oc-b")
    assert plant.intervals_overlap(left, right) is True
    assert plant.intervals_overlap(right, left) is True


def test_an_interval_wholly_inside_another_overlaps() -> None:
    """A 32 GBd carrier sitting inside a 128 GBd one shares no edge with it, so
    an edge comparison alone would pass the pair that is most obviously lit."""
    wide = _interval(191_275_000, 191_425_000, "oc-128gbd")
    narrow = _interval(191_327_800, 191_372_200, "oc-32gbd")
    assert plant.intervals_overlap(wide, narrow) is True
    assert plant.intervals_overlap(narrow, wide) is True


def test_an_interval_overlaps_itself() -> None:
    """Two carriers on the same anchor and the same mode are the densest
    collision there is, and the predicate is not reflexive by accident."""
    same = _interval(191_400_000, 191_444_400)
    assert plant.intervals_overlap(same, same) is True


def test_two_adjacent_channels_at_128_gbd_overlap_by_a_hundred_gigahertz() -> None:
    """The failure the channel-number rule passed and this one refuses.

    Channel 40 and channel 41 are 50 GHz apart and each carrier is 150 GHz wide,
    so they share 100 GHz. Their channel numbers differ, so the old rule merged
    both and nothing reported the interference.
    """
    low_lower, low_upper = carrier_interval_mhz(channel_to_frequency_mhz(40), 128000)
    high_lower, high_upper = carrier_interval_mhz(channel_to_frequency_mhz(41), 128000)
    left = _interval(low_lower, low_upper, "oc-ch040")
    right = _interval(high_lower, high_upper, "oc-ch041")
    assert plant.intervals_overlap(left, right) is True
    assert min(low_upper, high_upper) - max(low_lower, high_lower) == 100_000


def test_the_predicate_reads_a_free_block_as_readily_as_a_carrier() -> None:
    """Both shapes are half-open intervals, so the capacity question "does this
    carrier fit that block" is the collision question with different nouns."""
    block = plant.FreeBlock(lower_mhz=191_400_000, upper_mhz=191_500_000)
    inside = _interval(191_420_000, 191_464_400)
    beyond = _interval(191_500_000, 191_544_400)
    assert plant.intervals_overlap(block, inside) is True
    assert plant.intervals_overlap(block, beyond) is False


def test_the_gap_test_and_the_overlap_test_agree_about_touching_intervals() -> None:
    """`free_blocks` sweeps a cursor rather than calling the predicate, so this
    is what stops the two drifting apart. Touching carriers do not overlap, and
    the sweep must report no block between them rather than one of zero width."""
    lower = CBAND_LOWER_EDGE_MHZ + 100_000
    middle = lower + 44_400
    upper = middle + 44_400
    left = _interval(lower, middle, "oc-a")
    right = _interval(middle, upper, "oc-b")
    assert plant.intervals_overlap(left, right) is False
    assert plant.free_blocks([left, right]) == (
        plant.FreeBlock(CBAND_LOWER_EDGE_MHZ, lower),
        plant.FreeBlock(upper, CBAND_UPPER_EDGE_MHZ),
    )


def _hop(kind: str, name: str) -> dict[str, str]:
    return {"kind": kind, "name": name}


HAMBURG_PATH = [
    _hop("OtnRoadm", "roadm-ber-01"),
    _hop("OtnOpticalMultiplexSection", "oms-ham-ber"),
    _hop("OtnRoadm", "roadm-ham-01"),
    _hop("OtnOpticalMultiplexSection", "oms-ams-ham"),
    _hop("OtnRoadm", "roadm-ams-01"),
]


def test_a_traversal_path_becomes_a_route_of_its_sections() -> None:
    routes = plant.routes_from_traversal([HAMBURG_PATH], "roadm-ber-01", "roadm-ams-01")
    assert len(routes) == 1
    assert routes[0].section_names == ("oms-ham-ber", "oms-ams-ham")
    assert routes[0].key == "oms-ham-ber|oms-ams-ham"
    assert routes[0].start_node == "roadm-ber-01"
    assert routes[0].hop_count == 2


def test_a_truncated_traversal_raises_rather_than_ranking_a_partial_set() -> None:
    """The server ran out of budget, so what came back is a prefix of the
    answer and the winner picked from it is the winner of a different question.
    """
    with pytest.raises(ValueError, match="ran out of budget at depth 6"):
        plant.routes_from_traversal([HAMBURG_PATH], "roadm-ber-01", "roadm-ams-01", truncated_at_depth=6)


def test_a_path_that_wanders_through_another_kind_is_refused() -> None:
    """A traversal filtered only on node kind returns exactly this: two
    unrelated spans joined through their shared fiber type. It budgets cleanly
    and it is not a route."""
    wandering = [
        _hop("OtnRoadm", "roadm-ber-01"),
        _hop("OtnOpticalMultiplexSection", "oms-ber-cph"),
        _hop("OtnFiberSpan", "span-ber-cph-01"),
        _hop("OtnFiberType", "G.652.D"),
        _hop("OtnRoadm", "roadm-ams-01"),
    ]
    with pytest.raises(ValueError, match="not an alternating ROADM and section walk"):
        plant.routes_from_traversal([wandering], "roadm-ber-01", "roadm-ams-01")


def test_a_path_with_an_even_hop_count_cannot_alternate() -> None:
    with pytest.raises(ValueError, match="which cannot alternate"):
        plant.routes_from_traversal([HAMBURG_PATH[:-1]], "roadm-ber-01", "roadm-ams-01")


def test_a_path_that_does_not_run_between_the_two_endpoints_is_refused() -> None:
    with pytest.raises(ValueError, match="not roadm-ber-01 to roadm-lon-01"):
        plant.routes_from_traversal([HAMBURG_PATH], "roadm-ber-01", "roadm-lon-01")


def test_a_path_that_revisits_a_roadm_is_refused() -> None:
    """A wavelength does not pass through a node twice."""
    looping = [
        _hop("OtnRoadm", "roadm-ber-01"),
        _hop("OtnOpticalMultiplexSection", "oms-ham-ber"),
        _hop("OtnRoadm", "roadm-ham-01"),
        _hop("OtnOpticalMultiplexSection", "oms-ham-cph"),
        _hop("OtnRoadm", "roadm-ber-01"),
    ]
    with pytest.raises(ValueError, match="revisits a ROADM"):
        plant.routes_from_traversal([looping], "roadm-ber-01", "roadm-ber-01")


def test_a_carrier_can_be_excluded_from_its_own_occupancy() -> None:
    """The defect the first live re-run found.

    A generator re-run reads a branch that already holds the carrier its
    previous run wrote. Counting that carrier makes the service's own channel
    look taken, so the allocation moves on by one: channel 1, then 2, then 3, a
    different answer every run. Determinism is the demo's requirement, so the
    generator excludes the carrier it is about to write.
    """
    payload = {
        "OtnOpticalCarrier": {
            "edges": [
                _carrier_payload("oc-svc-ber-ams-400g", 1, ["oms-ham-ber", "oms-ams-ham"]),
                _carrier_payload("oc-ch002-fra-mil", 2, ["oms-fra-mil"]),
            ]
        }
    }
    counted = plant.occupancy_from_graphql(payload)
    assert [item.carrier for item in counted["oms-ham-ber"]] == ["oc-svc-ber-ams-400g"]
    excluded = plant.occupancy_from_graphql(payload, exclude={"oc-svc-ber-ams-400g"})
    assert "oms-ham-ber" not in excluded
    assert [item.channel for item in excluded["oms-fra-mil"]] == [2]
