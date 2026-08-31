"""`impact.py`, against hand-built payloads. No server, no dataset.

Every payload here is written out in full rather than loaded from `objects/`,
for the same reason `test_budget.py` does: a test whose input is the
shipped data can only ever assert what the shipped data happens to contain, and
the shipped data has no collided channel, no mixed fiber type and no service
outside a conduit. Those are the cases that break a report.

`test_impact_claims.py` is the other half. It asserts the shipped numbers.
"""

import pytest

from infrahub_demo_otn.impact import (
    AI_PROFILES,
    DiversityFinding,
    circuit_segments,
    conduit_groups,
    is_ai_profile,
    latency_rows,
    non_diverse_pairs,
    path_propagation_ns,
    reach_table,
    section_lengths_m,
    service_exposures,
)
from infrahub_demo_otn.plant import nodes_of
from infrahub_demo_otn.units import channel_to_frequency_mhz, propagation_delay_ns


def attribute(value: object) -> dict[str, object]:
    return {"value": value}


def edges(*nodes: dict[str, object]) -> dict[str, object]:
    return {"edges": [{"node": node} for node in nodes]}


def one(node: dict[str, object] | None) -> dict[str, object]:
    return {"node": node}


def span(name: str, length_m: int, conduit: str | None = None, group_index: int | None = 1468) -> dict[str, object]:
    return {
        "__typename": "OtnFiberSpan",
        "name": attribute(name),
        "length_m": attribute(length_m),
        "conduit": one({"name": attribute(conduit)} if conduit else None),
        "fiber_type": one({"group_index_milli": attribute(group_index)} if group_index else None),
    }


def section(name: str, *spans: dict[str, object]) -> dict[str, object]:
    return {"id": name, "__typename": "OtnOpticalMultiplexSection", "name": attribute(name), "spans": edges(*spans)}


def carrier(name: str, channel: int, *sections: str, baud: int = 64000) -> dict[str, object]:
    """One carrier shaped like `queries/channel_occupancy.gql` returns it.

    The anchor's centre frequency and the mode's symbol rate are here because
    occupancy is spectrum now, and `plant.occupancy_from_graphql` fails closed
    without either. The occupancy table still counts anchors, so `baud` never
    changes an assertion in this file; it changes whether the payload is legal.
    """
    return {
        "id": name,
        "__typename": "OtnOpticalCarrier",
        "name": attribute(name),
        "channel": one(
            {
                "channel_number": attribute(channel),
                "center_frequency_mhz": attribute(channel_to_frequency_mhz(channel)),
            }
        ),
        "optical_mode": one({"name": attribute("DP-16QAM 64GBd 400G"), "baud_mbaud": attribute(baud)}),
        "sections": edges(*(named(item) for item in sections)),
    }


def named(name: str) -> dict[str, object]:
    """A relationship peer that carries only its name."""
    return {"name": attribute(name)}


def hop(sequence: int, element: dict[str, object]) -> dict[str, object]:
    return {"sequence": attribute(sequence), "element": one(element)}


def roadm(name: str) -> dict[str, object]:
    return {"__typename": "OtnRoadm", "name": attribute(name)}


def service(
    name: str,
    profile: str = "ip-transit",
    *,
    customer: str = "NREN-XX",
    budget: int | None = None,
    path: dict[str, object] | None = None,
    paths: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    return {
        "id": name,
        "__typename": "OtnService",
        "name": attribute(name),
        "customer": attribute(customer),
        "service_profile": attribute(profile),
        "max_latency_ns": attribute(budget),
        # A collection, because `OtnService.optical_path` is cardinality many:
        # a circuit regenerated at an intermediate site is one path per
        # wavelength. `path` writes the one segment a circuit spanning one
        # wavelength has; `paths` writes a chain, and writes it in whatever order
        # the caller passes, because an Infrahub relationship hands back a set.
        "optical_path": edges(*(paths or ((path,) if path else ()))),
    }


def switch(name: str, site: str, framing_ns: int = 1200) -> dict[str, object]:
    """One `OtnOduSwitch` as it arrives off `OtnOpticalCarrier.odu_switches`."""
    return {
        "name": attribute(name),
        "switching_mode": attribute("regenerator"),
        "framing_latency_ns": attribute(framing_ns),
        "site": one(named(site)),
    }


def path(
    *hops: dict[str, object],
    total_length_m: int = 0,
    latency_ns: int = 0,
    sections: tuple[str, ...] = (),
    name: str = "a-path",
    sequence: int = 1,
    carrier: str = "a-carrier",
    switches: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    return {
        "name": attribute(name),
        "segment_sequence": attribute(sequence),
        "total_length_m": attribute(total_length_m),
        "latency_ns": attribute(latency_ns),
        "hops": edges(*hops),
        "carrier": one(
            {
                "name": attribute(carrier),
                "sections": edges(*(named(item) for item in sections)),
                "odu_switches": edges(*switches),
            }
        ),
    }


# ---------------------------------------------------------------------------
# Reach
# ---------------------------------------------------------------------------


def _reach_payload() -> dict[str, object]:
    return {
        "OtnOpticalMultiplexSection": edges(
            section("oms-short", span("s1", 120_000)),
            section("oms-long", span("s2", 300_000), span("s3", 200_000)),
        ),
        "OtnOpticalMode": edges(
            {
                "id": "zr",
                "name": attribute("400ZR"),
                "mode_class": attribute("pluggable"),
                "line_rate_gbps": attribute(400),
                "nominal_reach_m": attribute(120_000),
                "required_osnr_mdb": attribute(26_000),
            },
            {
                "id": "lh",
                "name": attribute("DP-QPSK 32GBd 100G"),
                "mode_class": attribute("transponder"),
                "line_rate_gbps": attribute(100),
                "nominal_reach_m": attribute(3_000_000),
                "required_osnr_mdb": attribute(14_000),
            },
        ),
    }


def test_section_length_is_the_sum_of_its_spans() -> None:
    assert section_lengths_m(_reach_payload()) == {"oms-short": 120_000, "oms-long": 500_000}


def test_reach_equal_to_length_is_in_reach() -> None:
    """The boundary, pinned deliberately.

    A 120 km part over a 120 km section reaches it. Off-by-one here turns the
    published ZR finding from "zero of twenty-one" into an argument about strict
    inequality.
    """
    modes = {mode.name: mode for mode in reach_table(_reach_payload())}
    assert modes["400ZR"].in_reach == ("oms-short",)
    assert modes["400ZR"].out_of_reach == ("oms-long",)
    assert modes["400ZR"].shortfall_to_shortest_m == 0
    assert not modes["400ZR"].reaches_nothing


def test_a_mode_shorter_than_every_section_reaches_nothing() -> None:
    payload = _reach_payload()
    payload["OtnOpticalMultiplexSection"] = edges(section("oms-short", span("s1", 220_000)))
    mode = next(item for item in reach_table(payload) if item.name == "400ZR")
    assert mode.reaches_nothing
    assert mode.in_reach == ()
    assert mode.shortfall_to_shortest_m == 100_000


def test_a_long_reach_mode_covers_everything() -> None:
    mode = next(item for item in reach_table(_reach_payload()) if item.line_rate_gbps == 100)
    assert mode.reaches_everything
    assert mode.section_count == 2
    assert mode.shortfall_to_shortest_m < 0


def test_reach_over_a_plant_with_no_sections_raises() -> None:
    with pytest.raises(ValueError, match="no optical multiplex section"):
        reach_table({"OtnOpticalMode": edges(), "OtnOpticalMultiplexSection": edges()})


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------


def test_propagation_is_summed_per_span_at_each_span_group_index() -> None:
    """FR-013a. Mixed fiber is why this is a sum and not one division.

    Two 100 km spans, one G.652 at 1468 and one G.654 at 1467. Computing once
    over 200 km at 1468 overstates the delay; summing per span does not.
    """
    mixed = path(
        hop(0, roadm("roadm-a")),
        hop(1, span("s1", 100_000, group_index=1468)),
        hop(2, span("s2", 100_000, group_index=1467)),
    )
    expected = propagation_delay_ns(100_000, 1468) + propagation_delay_ns(100_000, 1467)
    assert path_propagation_ns(mixed) == expected
    assert expected != propagation_delay_ns(200_000, 1468)


def test_a_span_without_a_fiber_type_falls_back_to_the_published_default() -> None:
    fallback = path(hop(1, span("s1", 100_000, group_index=None)))
    assert path_propagation_ns(fallback) == propagation_delay_ns(100_000, 1468)


def test_only_span_hops_contribute_propagation() -> None:
    assert path_propagation_ns(path(hop(0, roadm("roadm-a")), hop(1, roadm("roadm-b")))) == 0


def test_hops_are_summed_in_sequence_order_whatever_order_they_arrive_in() -> None:
    forwards = path(hop(1, span("s1", 100_000)), hop(2, span("s2", 300_000)))
    backwards = path(hop(2, span("s2", 300_000)), hop(1, span("s1", 100_000)))
    assert path_propagation_ns(forwards) == path_propagation_ns(backwards)


def test_a_service_with_a_budget_gets_a_signed_margin() -> None:
    payload = {
        "OtnService": edges(
            service(
                "svc-ai",
                "ai-training-dci",
                budget=4_000_000,
                path=path(
                    hop(1, span("s1", 780_000)), latency_ns=3_824_741, total_length_m=780_000, sections=("oms-x",)
                ),
            )
        )
    }
    row = latency_rows(payload)[0]
    assert row.margin_ns == 175_259
    assert row.ok is True
    assert row.sections == ("oms-x",)
    assert row.propagation_ns == propagation_delay_ns(780_000, 1468)
    assert row.overhead_ns == row.latency_ns - row.propagation_ns
    assert row.overhead_share_percent < 1.0


def test_a_service_over_budget_fails_and_the_margin_is_negative() -> None:
    payload = {
        "OtnService": edges(service("svc-slow", "ai-training-dci", budget=4_000_000, path=path(latency_ns=4_853_605)))
    }
    row = latency_rows(payload)[0]
    assert row.margin_ns == -853_605
    assert row.ok is False


def test_a_service_with_no_budget_is_neither_a_pass_nor_a_fail() -> None:
    payload = {"OtnService": edges(service("svc-transit", path=path(latency_ns=3_923_026)))}
    row = latency_rows(payload)[0]
    assert row.budget_ns is None
    assert row.margin_ns is None
    assert row.ok is None


def test_an_unprovisioned_service_produces_no_row() -> None:
    assert latency_rows({"OtnService": edges(service("svc-rejected"))}) == []


def test_the_tightest_margin_sorts_first() -> None:
    payload = {
        "OtnService": edges(
            service("svc-loose", "hpc-research", budget=9_000_000, path=path(latency_ns=1_000_000)),
            service("svc-tight", "ai-training-dci", budget=4_000_000, path=path(latency_ns=3_900_000)),
            service("svc-none", path=path(latency_ns=2_000_000)),
        )
    }
    assert [row.service for row in latency_rows(payload)] == ["svc-tight", "svc-loose", "svc-none"]


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def test_the_three_latency_bound_profiles_and_nothing_else() -> None:
    assert AI_PROFILES == {"ai-training-dci", "ai-inference", "hpc-research"}
    assert is_ai_profile("hpc-research")
    assert not is_ai_profile("ip-transit")
    assert not is_ai_profile("legacy-sdh")
    assert not is_ai_profile(None)


# ---------------------------------------------------------------------------
# Shared risk
# ---------------------------------------------------------------------------


def _exposure_payload() -> dict[str, object]:
    return {
        "OtnService": edges(
            service(
                "svc-a",
                "ai-training-dci",
                path=path(hop(0, roadm("r1")), hop(1, span("s1", 100_000, "cd-north")), hop(2, span("s2", 100_000))),
            ),
            service(
                "svc-b",
                "hpc-research",
                path=path(hop(1, span("s3", 100_000, "cd-north")), hop(2, span("s4", 100_000, "cd-south"))),
            ),
            service("svc-c", "ip-transit", path=path(hop(1, span("s5", 100_000, "cd-south")))),
            service("svc-d", "ip-transit", path=path(hop(1, span("s6", 100_000)))),
            service("svc-unprovisioned", "ai-inference"),
        )
    }


def test_exposure_is_read_off_the_spans_and_unducted_spans_are_counted() -> None:
    exposures = {item.service: item for item in service_exposures(_exposure_payload())}
    assert exposures["svc-a"].conduits == ("cd-north",)
    assert exposures["svc-a"].span_count == 2
    assert exposures["svc-a"].unducted_span_count == 1
    assert exposures["svc-a"].is_ai
    assert exposures["svc-d"].conduits == ()
    assert "svc-unprovisioned" not in exposures


def test_conduit_groups_name_who_else_is_in_the_duct() -> None:
    groups = conduit_groups(service_exposures(_exposure_payload()))
    assert groups == {"cd-north": ("svc-a", "svc-b"), "cd-south": ("svc-b", "svc-c")}


def test_pairs_come_out_of_the_groups_with_the_right_severity() -> None:
    findings = non_diverse_pairs(service_exposures(_exposure_payload()))
    assert findings == [
        DiversityFinding("svc-a", "svc-b", ("cd-north",), True),
        DiversityFinding("svc-b", "svc-c", ("cd-south",), False),
    ]
    assert [finding.severity for finding in findings] == ["high", "note"]


def test_a_service_in_no_conduit_is_in_no_pair() -> None:
    findings = non_diverse_pairs(service_exposures(_exposure_payload()))
    assert not [finding for finding in findings if "svc-d" in (finding.service_a, finding.service_b)]


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------


def _chain_payload(*, junction: bool = True) -> dict[str, object]:
    """One two-segment circuit, and one single-segment circuit sharing a duct.

    The chain's segments are written **second first**, and its first segment is
    alone in `cd-iberia` while its second shares `cd-north` with `svc-plain`.
    Both are deliberate: the order is what a set-valued relationship hands back,
    and the asymmetry is what makes a first-segment-only walk answer that the two
    services are diverse.
    """
    joint = switch("oeo-fra", "fra")
    return {
        "OtnService": edges(
            service(
                "svc-chain",
                "ai-training-dci",
                paths=(
                    path(
                        hop(1, span("s-poland", 100_000, "cd-north")),
                        name="path-chain-2",
                        sequence=2,
                        carrier="oc-fra-waw",
                        switches=(joint,) if junction else (switch("oeo-waw", "waw"),),
                    ),
                    path(
                        hop(1, span("s-iberia", 100_000, "cd-iberia")),
                        name="path-chain-1",
                        sequence=1,
                        carrier="oc-mad-fra",
                        switches=(joint,) if junction else (),
                    ),
                ),
            ),
            service(
                "svc-plain",
                "hpc-research",
                path=path(hop(1, span("s-plain", 100_000, "cd-north")), carrier="oc-plain"),
            ),
        )
    }


def _circuit(payload: dict[str, object], name: str = "svc-chain") -> dict[str, object]:
    return next(node for node in nodes_of(payload, "OtnService") if node["name"] == name)


def test_circuit_segments_order_the_chain_and_name_the_device_that_joins_it() -> None:
    """The one walk all three reports follow.

    The junction is derived from the two carriers' `odu_switches` and is stored
    nowhere: `OtnOpticalPath` has no relationship to the device, and the fact
    that makes a device a junction is that it terminates the wavelength arriving
    and the wavelength leaving. `chains.joins` reads the same relationship when
    it picks a cover.
    """
    segments = circuit_segments(_circuit(_chain_payload()))
    assert [item.sequence for item in segments] == [1, 2], "sorted on segment_sequence, not payload order"
    assert [item.carrier_name for item in segments] == ["oc-mad-fra", "oc-fra-waw"]
    assert segments[0].junction_device == "oeo-fra"
    assert segments[0].junction_site == "fra"
    assert segments[1].junction is None, "nothing regenerates light nobody carries on"


def test_two_carriers_with_no_device_in_common_are_joined_by_nothing() -> None:
    """R-008's phantom junction. A boundary is not a junction.

    Two carriers meeting where no device terminates both is the shape the
    traversal returned 48 times, and it is a data defect rather than a junction
    the walk should guess at. `None` is what the reports label.
    """
    assert circuit_segments(_circuit(_chain_payload(junction=False)))[0].junction is None


def test_exposure_unions_the_conduits_of_every_segment() -> None:
    """FR-019. The narrow answer this replaces looked like a clean bill of health.

    Reading the lowest `segment_sequence` gave the ducts of the chain's first
    half and none of its second, so the pair sharing `cd-north` was absent from
    the report rather than wrong in it.
    """
    exposures = {item.service: item for item in service_exposures(_chain_payload())}
    assert exposures["svc-chain"].conduits == ("cd-iberia", "cd-north")
    assert exposures["svc-chain"].segment_count == 2
    assert exposures["svc-chain"].is_regenerated
    assert exposures["svc-chain"].span_count == 2, "one span per segment, counted over both"
    assert not exposures["svc-plain"].is_regenerated
    assert non_diverse_pairs(service_exposures(_chain_payload())) == [
        DiversityFinding("svc-chain", "svc-plain", ("cd-north",), True)
    ]


def test_a_pair_sharing_two_conduits_sorts_above_a_pair_sharing_one() -> None:
    payload = {
        "OtnService": edges(
            service("svc-1", "ip-transit", path=path(hop(1, span("s1", 1, "cd-a")), hop(2, span("s2", 1, "cd-b")))),
            service("svc-2", "ip-transit", path=path(hop(1, span("s3", 1, "cd-a")), hop(2, span("s4", 1, "cd-b")))),
            service("svc-3", "ip-transit", path=path(hop(1, span("s5", 1, "cd-a")))),
        )
    }
    findings = non_diverse_pairs(service_exposures(payload))
    assert findings[0].shared == ("cd-a", "cd-b")
    assert findings[0].service_a == "svc-1"
    assert len(findings) == 3
