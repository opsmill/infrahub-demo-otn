"""The OSNR, capacity, diversity, collision and monitor checks, executed offline.

These are the pieces of code that decide whether a proposed change can merge.

The OSNR half runs against the committed dataset, because the claim it makes is
about the shipped plant. The capacity half runs against hand-built container
trees, because the shipped dataset holds no overfilled parent and a check with no
failing case in its tests is a check nobody has seen fail. The diversity half is
hand-built for the same reason and one more: no shipped object declares a
diversity group, so a payload assembled from `objects/` would exercise only the
branch where the check says nothing.

The diversity tests carry one assertion the other two do not need, which is that
the check stays **silent** about services that declared no group. That silence is
the requirement rather than an optimisation, so it is asserted from both
directions: a payload holding undeclared services the real query never selects,
and the shape of the query itself.

The carrier loop alone reaches five of the twenty-one sections, because only
five are crossed by a carrier, so Paris to Madrid would never be looked at and
the demo's headline route would be reported clean by a check that had not
budgeted it. The section sweep closes that, and the number this file exists to
hold is the one that says so: **42 evaluations, twenty-one sections in two
directions, every run.**

The payload is built from `objects/` in the shape `queries/osnr_margin.gql`
returns, so the sections here are the sections that ship. A synthetic payload
would assert that the sweep multiplies by two, which is arithmetic. This asserts
that the shipped plant fails where the story says it fails.

Two failure shapes are pinned deliberately, because both produce a green check
over an unbudgeted network:

- a branch with no carriers must still be swept, and
- the reference mode must arrive whether or not a carrier uses it.

The collision half is hand-built too, and for a reason the shipped dataset makes
plain: it holds carriers that overlap, so a payload assembled from `objects/`
would exercise the failing branch and nothing else. The five cases here are the
five the rule turns on, which are overlap on a shared section, overlap with no
shared section, a shared edge, a carrier past the band edge and a carrier that
reserves nothing anywhere.

The two monitor halves are hand-built for the strongest version of that reason:
the shipped dataset passes both of them, and it was repaired so that it would.
The 71-against-40 case below is the observation taken before that repair, kept
here because a check whose only evidence is a green run is a check nobody has
seen fail. The completeness half is the same shape in reverse: every device that
should carry a monitor carries one on `main`, so the five absences and the orphan
exist only in these payloads.
"""

import importlib.util
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from infrahub_sdk.ctl.repository import get_repository_config

from infrahub_demo_otn.containers import free_slots
from infrahub_demo_otn.impact import non_diverse_pairs, service_exposures
from infrahub_demo_otn.plant import nodes_of, peers, sections_from_graphql
from infrahub_demo_otn.units import CBAND_LOWER_EDGE_MHZ, channel_to_frequency_mhz, occupied_width_mhz
from tests.unit.conftest import REPO_ROOT, objects_of_kind
from tests.unit.test_budget_claims import QAM16_400G, _by_name, _spans_with_pumps

CONFIG = get_repository_config(REPO_ROOT / ".infrahub.yml")
BRANCH = "probe-under-test"

SHIPPED_SECTIONS = 21
"""Sections in `objects/16_geant_sections.yml`. Asserted, not assumed."""

SHIPPED_EVALUATIONS = SHIPPED_SECTIONS * 2
"""SC-001's forty-two. Two directions per section, and nothing else in it."""

FAILING_SECTION = "oms-par-mad"
"""The route the demo's story says cannot be lit, and now the check agrees."""


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def _attribute(value: Any) -> dict[str, Any]:
    return {"value": value}


def _node(record: dict[str, Any], *fields: str) -> dict[str, Any]:
    """One GraphQL node holding the named attributes and nothing else.

    Selecting exactly the fields the query selects is the point. A payload
    richer than the query lets the check read a field no server would ever send
    it: drop `direction` from the query and the marshaller has to guess.
    """
    return {field: _attribute(record.get(field)) for field in fields}


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _one(node: dict[str, Any]) -> dict[str, Any]:
    return {"node": node}


def _mode_nodes() -> list[dict[str, Any]]:
    return [
        {
            "id": name,
            "__typename": "OtnOpticalMode",
            **_node(record, "name", "required_osnr_mdb", "cd_tolerance_fs_per_nm", "fec_latency_ns"),
        }
        for name, record in sorted(_by_name("OtnOpticalMode").items())
    ]


def _span_node(record: dict[str, Any], fibers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fiber = fibers[str(record["fiber_type"])]
    pumps = [
        _node(pump, "name", "injection_end", "propagation", "on_off_gain_mdb", "insertion_loss_mdb")
        for pump in (edge["node"] for edge in (record.get("raman_pumps") or {}).get("edges") or [])
    ]
    return {
        **_node(
            record,
            "name",
            "oms_sequence",
            "length_m",
            "splice_count",
            "splice_loss_mdb",
            "connector_count",
            "connector_loss_mdb",
            "aging_margin_mdb",
        ),
        "fiber_type": _one(
            _node(fiber, "name", "attenuation_mdb_per_km", "dispersion_fs_per_nm_km", "group_index_milli")
        ),
        "raman_pumps": _edges(*pumps),
    }


def _section_nodes() -> list[dict[str, Any]]:
    fibers = _by_name("OtnFiberType")
    spans = _spans_with_pumps()
    amplifiers = _by_name("OtnAmplifier")
    roadms = _by_name("OtnRoadm")
    nodes = []
    for name, record in sorted(_by_name("OtnOpticalMultiplexSection").items()):
        nodes.append(
            {
                "id": name,
                "__typename": "OtnOpticalMultiplexSection",
                "name": _attribute(name),
                "roadm_a": _one(_node(roadms[str(record["roadm_a"])], "name", "insertion_loss_mdb")),
                "roadm_b": _one(_node(roadms[str(record["roadm_b"])], "name", "insertion_loss_mdb")),
                "spans": _edges(*(_span_node(spans[str(span)], fibers) for span in record["spans"])),
                **{
                    chain: _edges(
                        *(
                            _node(
                                amplifiers[str(amplifier)],
                                "name",
                                "oms_sequence",
                                "noise_figure_mdb",
                                "gain_mdb",
                            )
                            for amplifier in record[chain]
                        )
                    )
                    for chain in ("amplifiers_a2b", "amplifiers_b2a")
                },
            }
        )
    return nodes


def _carrier_nodes() -> list[dict[str, Any]]:
    modes = _by_name("OtnOpticalMode")
    nodes = []
    for record in objects_of_kind("OtnOpticalCarrier"):
        mode = modes[str(record["optical_mode"])]
        nodes.append(
            {
                "id": str(record["name"]),
                "__typename": "OtnOpticalCarrier",
                "name": _attribute(record["name"]),
                "optical_mode": _one(
                    _node(mode, "name", "required_osnr_mdb", "cd_tolerance_fs_per_nm", "fec_latency_ns")
                ),
                "sections": _edges(*({"name": _attribute(section)} for section in record["sections"])),
            }
        )
    return nodes


def _payload(*, carriers: bool = True, modes: bool = True) -> dict[str, Any]:
    """The shipped plant in the shape `osnr_margin.gql` returns it."""
    return {
        "OtnOpticalMode": _edges(*(_mode_nodes() if modes else ())),
        "OtnOpticalCarrier": _edges(*(_carrier_nodes() if carriers else ())),
        "OtnOpticalMultiplexSection": _edges(*_section_nodes()),
    }


# ---------------------------------------------------------------------------
# Running the check
# ---------------------------------------------------------------------------


def _entry(name: str) -> tuple[str, str]:
    entry = next(item for item in CONFIG.check_definitions if item.name == name)
    return str(entry.file_path), entry.class_name


@cache
def _module(file_path: str) -> Any:
    """Import a registered file by path, the way the server loads it.

    `checks/` is not a package, deliberately, for the same reason `transforms/`
    and `generators/` are not: Infrahub loads these files by path and the test
    should reach them the same way.

    Cached on the path, the way `tests/unit/conftest.py` caches its readers. The
    checks hold no module state between runs, `_run` builds a fresh instance for
    every payload, and every `_run` in this file otherwise re-reads and re-execs
    a file off disk.
    """
    path = REPO_ROOT / file_path
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(file_path: str, class_name: str) -> Any:
    return getattr(_module(file_path), class_name)


def _run(payload: dict[str, Any], check_name: str = "osnr_margin") -> Any:
    """Instantiate and run the check with no client and no server.

    `InfrahubCheck.__init__` takes a branch and builds nothing that needs a
    connection, so the real constructor is used rather than `__new__`. The
    return is the instance, because the logs are the thing under test.

    The check is named rather than hardcoded, and the name is looked up in
    `.infrahub.yml`, so a check registered under a class the file does not define
    fails here rather than at repository-sync time.
    """
    file_path, class_name = _entry(check_name)
    check = _load(file_path, class_name)(branch=BRANCH)
    check.validate(data=payload)
    return check


def _messages(check: Any, level: str) -> list[str]:
    return [str(log["message"]) for log in check.logs if log["level"] == level]


@pytest.fixture(scope="module")
def swept() -> Any:
    return _run(_payload())


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def test_the_shipped_plant_still_holds_twenty_one_sections() -> None:
    """The forty-two below is two times this. Asserting it separately means a
    dataset that grows a section fails here rather than silently redefining what
    the count in the check's log line is a count of."""
    assert len(_by_name("OtnOpticalMultiplexSection")) == SHIPPED_SECTIONS


def test_the_sweep_evaluates_every_section_in_both_directions(swept: Any) -> None:
    """SC-001's forty-two, asserted rather than described.

    The number was in the specification before anything computed it. This is
    where it acquires a definition.
    """
    lines = [line for line in _messages(swept, "INFO") if line.startswith("Swept ")]
    assert len(lines) == 1, _messages(swept, "INFO")
    assert f"Swept {SHIPPED_EVALUATIONS} section evaluations over {SHIPPED_SECTIONS} sections" in lines[0]


def test_the_sweep_fails_paris_to_madrid_in_both_directions(swept: Any) -> None:
    """The payoff of the whole feature, and the reason main's check is red.

    Both directions, because a single message would leave open whether the
    other direction was fine or was never looked at.
    """
    errors = _messages(swept, "ERROR")
    for direction in ("a_to_b", "b_to_a"):
        matching = [line for line in errors if line.startswith(f"{FAILING_SECTION} {direction} is short of OSNR")]
        assert len(matching) == 1, errors
        assert QAM16_400G in matching[0]
        assert "0.535 dB" in matching[0], matching[0]


def test_no_section_other_than_paris_to_madrid_fails_the_sweep(swept: Any) -> None:
    """A sweep that failed half the network would be noise rather than a gate,
    and the demonstration would have nothing to point at."""
    named = {line.split(" ", 1)[0] for line in _messages(swept, "ERROR")}
    assert named == {FAILING_SECTION}


def test_the_sweep_summary_names_the_worst_direction(swept: Any) -> None:
    """The worst standalone margin is Paris to Madrid, and the line says which
    way round it was walked."""
    line = next(item for item in _messages(swept, "INFO") if item.startswith("Swept "))
    assert f"on {FAILING_SECTION} a_to_b" in line
    assert "-0.535 dB" in line


def test_a_branch_with_no_carriers_is_still_swept() -> None:
    """No early return on an empty carrier list.

    An `if not carriers: return` ahead of the sweep would mean a branch that
    removed its last carrier reported nothing at all while looking like a pass.
    """
    check = _run(_payload(carriers=False))
    line = next(item for item in _messages(check, "INFO") if item.startswith("Swept "))
    assert f"Swept {SHIPPED_EVALUATIONS} section evaluations" in line
    assert any(line.startswith(f"{FAILING_SECTION} a_to_b") for line in _messages(check, "ERROR"))


def test_a_branch_whose_catalog_lacks_the_reference_mode_is_an_error_not_a_skip() -> None:
    """The other half of the same failure.

    The sweep budgets against one named mode. A branch that does not carry it
    has nothing to sweep against, and the only safe answer is to say so: a
    silent return here is a green check over a network nothing budgeted.
    """
    check = _run(_payload(modes=False))
    errors = _messages(check, "ERROR")
    assert any(QAM16_400G in line and "no mode at all" in line for line in errors), errors
    assert not any(line.startswith("Swept ") for line in _messages(check, "INFO"))


# ---------------------------------------------------------------------------
# The carrier loop, which the sweep does not replace
# ---------------------------------------------------------------------------


def test_the_carrier_loop_still_budgets_every_shipped_carrier(swept: Any) -> None:
    """The sweep budgets sections standalone and the carrier loop budgets
    traffic over the sections it crosses. A section that closes on its own can
    still be the one a four-hop wavelength runs out of margin on, so neither
    loop replaces the other."""
    carriers = len(objects_of_kind("OtnOpticalCarrier"))
    line = next(item for item in _messages(swept, "INFO") if item.startswith("Re-validated "))
    assert f"Re-validated {carriers} carriers" in line


def test_every_shipped_carrier_still_passes(swept: Any) -> None:
    """A pre-provisioned network whose own traffic fails teaches the wrong
    lesson. Only the standalone Paris to Madrid sweep is allowed to be red."""
    assert not [line for line in _messages(swept, "ERROR") if not line.startswith(FAILING_SECTION)]


# ---------------------------------------------------------------------------
# The coarse plant stays outside both loops
# ---------------------------------------------------------------------------


def test_no_coarse_object_reaches_either_loop(swept: Any) -> None:
    """The forty-two does not move, and nothing coarse is behind it.

    A count that held while a coarse span quietly joined a section would be a
    count that stopped meaning anything, so the count and the exclusion are
    asserted together. Both loops are covered: `check_carriers` and
    `sweep_sections` are handed the same `sections` mapping, so a span absent
    from that mapping is absent from both.

    The exclusion is structural rather than filtered. `sections_from_graphql`
    reads spans off sections, the tail span belongs to no section, and the
    coarse multiplexers and the coarse channel plan are kinds the query does not
    select at all. This is what says so out loud, because the boundary is one
    writable `oms` away from disappearing.
    """
    line = next(item for item in _messages(swept, "INFO") if item.startswith("Swept "))
    assert f"Swept {SHIPPED_EVALUATIONS} section evaluations over {SHIPPED_SECTIONS} sections" in line

    sections = sections_from_graphql(_payload())
    assert len(sections) == SHIPPED_SECTIONS
    reached = {span.name for section in sections.values() for span in section.spans}

    coarse_sites = {
        str(site["shortname"]) for site in objects_of_kind("OtnSite") if str(site["site_type"]) == "customer"
    }
    assert coarse_sites, "no customer site in the dataset, so this guard is vacuous"
    coarse_spans = {
        str(span["name"])
        for span in objects_of_kind("OtnFiberSpan")
        if str(span["site_a"]) in coarse_sites or str(span["site_b"]) in coarse_sites
    }
    assert coarse_spans, "no coarse span in the dataset, so this guard is vacuous"
    assert not reached & coarse_spans, f"coarse spans reached the loops: {sorted(reached & coarse_spans)}"

    coarse_names = coarse_spans | {
        str(device["name"]) for device in objects_of_kind("OtnMuxDemux") if device.get("cwdm_channels")
    }
    assert coarse_names > coarse_spans, "no coarse multiplexer in the dataset, so this guard is vacuous"
    reported = " ".join(str(log["message"]) for log in swept.logs)
    named = sorted(name for name in coarse_names if name in reported)
    assert not named, f"the check reported on coarse objects: {named}"

    # The channel plan is a kind the query never asks for, so it cannot arrive
    # in the payload at all. Asserted against the shipped plan rather than
    # against an empty set, so a plan that vanished would not pass this quietly.
    assert objects_of_kind("OtnCwdmChannel"), "no coarse channel plan in the dataset"
    assert "OtnCwdmChannel" not in _payload()


# ---------------------------------------------------------------------------
# The container capacity check
# ---------------------------------------------------------------------------

CAPACITY = "container_capacity"
"""The check under test below, registered in `.infrahub.yml` under that name."""

LINE = "odu-line-oc-test"
"""The parent in every tree here. Named the way both writers name a line
container, so nothing in these trees is a shape the model cannot hold."""

FULL_ODU4_SLOTS = 80
"""What an ODU4 offers, and what the documentation's twenty ODU2s overfill."""


def _child(name: str, odu_type: str, slots: int) -> dict[str, Any]:
    """One child container, in the shape `container_capacity.gql` returns it."""
    return {
        "name": _attribute(name),
        "odu_type": _attribute(odu_type),
        "tributary_slots": _attribute(slots),
    }


def _parent(name: str, odu_type: str, capacity: int, *children: dict[str, Any]) -> dict[str, Any]:
    """One container with its children, selected exactly as the query selects.

    `tributary_slots` is absent on the parent because the query does not select
    it there. A payload richer than the query would let the check read a figure no
    server sends it, and this is the field where that matters most: the parent's
    own occupancy is the number a reader is most likely to compare against the
    wrong capacity.
    """
    return {
        "id": name,
        "__typename": "OtnContainer",
        "name": _attribute(name),
        "odu_type": _attribute(odu_type),
        "tributary_slot_capacity": _attribute(capacity),
        "child_containers": _edges(*children),
    }


def _capacity_payload(*containers: dict[str, Any]) -> dict[str, Any]:
    return {"OtnContainer": _edges(*containers)}


def _shipped_containers() -> list[dict[str, Any]]:
    """Every container in `objects/`, which is 71 line containers and no child.

    The base dataset is where the live run on `main` gets its verdict, so the
    offline suite asserts the same thing the live run reports.
    """
    return [
        _parent(str(record["name"]), str(record["odu_type"]), int(record["tributary_slot_capacity"]))
        for record in objects_of_kind("OtnContainer")
    ]


def _verdicts(check: Any, names: tuple[str, ...]) -> dict[str, str]:
    """Each named parent against the verdict the check gave it.

    Read out of the messages, because the messages are the whole of what a
    proposed change shows a reader, and matched on the start of the line so a
    child named inside an overfill message cannot be mistaken for a parent.

    The caller supplies the names it expects to have been judged. A verdict
    recovered from an absence alone would read the same for a parent that fitted
    and a parent the check never looked at, which is the one confusion this
    helper must not introduce.
    """
    errors = _messages(check, "ERROR")
    infos = _messages(check, "INFO")
    verdicts = {}
    for name in names:
        overfilled = [line for line in errors if line.startswith(f"{name} is overfilled")]
        unknown = [line for line in infos if line.startswith(f"{name} has no known free-slot figure")]
        assert not (overfilled and unknown), f"{name} was reported twice: {overfilled + unknown}"
        verdicts[name] = "overfilled" if overfilled else "unknown" if unknown else "fits"
    return verdicts


def test_an_overfilled_parent_fails_naming_the_container_and_both_figures() -> None:
    """The documentation's own example, now a failing check rather than a caveat.

    Twenty ODU2s at eight slots each is 160 in a container that offers 80. Both
    figures are asserted because a message carrying one of them sends the reader
    to look up the other, and the point of the check is that a proposed change
    states the whole finding.
    """
    children = [_child(f"odu-fill-{index:02d}", "ODU2", 8) for index in range(20)]
    check = _run(_capacity_payload(_parent(LINE, "ODU4", FULL_ODU4_SLOTS, *children)), CAPACITY)

    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert errors[0].startswith(f"{LINE} is overfilled")
    assert "commit 160 tributary slots" in errors[0]
    assert f"it offers {FULL_ODU4_SLOTS}" in errors[0]
    assert "over by 80" in errors[0]
    assert "odu-fill-00" in errors[0]


def test_a_parent_within_its_capacity_passes() -> None:
    """Two ODU2e clients in an ODU4 is eighteen slots of eighty, not sixteen.

    Nine each, not eight, so this tree also pins that the check reads the stored
    occupancy rather than assuming an ODU2's figure for a 10G client.
    """
    children = (_child("odu-fill-a", "ODU2e", 9), _child("odu-fill-b", "ODU2e", 9))
    check = _run(_capacity_payload(_parent(LINE, "ODU4", FULL_ODU4_SLOTS, *children)), CAPACITY)

    assert _messages(check, "ERROR") == []
    summary = _messages(check, "INFO")[-1]
    assert "1 of which hold children" in summary
    assert "1 are within their capacity, 0 have no known figure and 0 are overfilled" in summary


def test_a_parent_of_an_unsized_type_is_reported_unknown_and_not_counted_as_fitting() -> None:
    """The distinction the whole capacity module exists to hold.

    A VC-4 has no tributary slot capacity G.709 defines, so what its child costs
    it cannot be worked out. Reporting it as fitting would be a pass over a
    container that might be overfull; reporting it as overfilled would fail a
    branch on a figure nobody has. Both are asserted against, and so is the
    summary, because an unknown counted among the containers that fit is the same
    silent pass one step further away from the reader.
    """
    check = _run(
        _capacity_payload(_parent(LINE, "VC-4", 0, _child("odu-fill-a", "ODU1", 2))),
        CAPACITY,
    )

    assert _messages(check, "ERROR") == []
    reported = [line for line in _messages(check, "INFO") if line.startswith(f"{LINE} has no known")]
    assert len(reported) == 1, _messages(check, "INFO")
    assert "its own type VC-4 has no tributary slot capacity" in reported[0]
    assert "0 are within their capacity, 1 have no known figure and 0 are overfilled" in _messages(check, "INFO")[-1]


def test_an_unsized_child_with_no_stored_count_makes_its_parent_unknown() -> None:
    """The other half, and the reason it is about the count and not the type.

    A provisioned ODUflex carries a real occupancy derived from its client's bit
    rate, floored at one, so a stored zero on one means nobody wrote a count. That
    child is unreadable and its parent's figure goes with it. The same child
    carrying its real 165 slots is counted like any other, which is what stops the
    rule from reading as "an ODUflex anywhere makes a wavelength unmeasurable".
    """
    unwritten = _run(
        _capacity_payload(_parent(LINE, "ODUC4", 320, _child("odu-fill-flex", "ODUflex", 0))),
        CAPACITY,
    )
    reported = [line for line in _messages(unwritten, "INFO") if line.startswith(f"{LINE} has no known")]
    assert len(reported) == 1, _messages(unwritten, "INFO")
    assert "the occupancy of odu-fill-flex cannot be read" in reported[0]
    assert _messages(unwritten, "ERROR") == []

    counted = _run(
        _capacity_payload(_parent(LINE, "ODUC4", 320, _child("odu-fill-flex", "ODUflex", 165))),
        CAPACITY,
    )
    assert _messages(counted, "ERROR") == []
    assert "1 are within their capacity, 0 have no known figure" in _messages(counted, "INFO")[-1]


def test_a_type_outside_the_table_is_an_error_and_does_not_hide_the_rest() -> None:
    """Drift between the schema enum and the slot table, reported per container.

    One unreadable container must not take the others down with it, which is why
    the error boundary is inside the loop. Without it a single bad `odu_type`
    would raise out of `validate` and the overfilled parent alongside it would
    never be reported at all.
    """
    check = _run(
        _capacity_payload(
            _parent("odu-line-oc-bogus", "ODU5", 80, _child("odu-fill-a", "ODU2", 8)),
            _parent(LINE, "ODU4", FULL_ODU4_SLOTS, *(_child(f"odu-fill-{i:02d}", "ODU2", 8) for i in range(11))),
        ),
        CAPACITY,
    )
    errors = _messages(check, "ERROR")
    assert any(line.startswith("odu-line-oc-bogus cannot be measured") for line in errors), errors
    assert any(line.startswith(f"{LINE} is overfilled") for line in errors), errors


def test_the_shipped_dataset_holds_no_parent_at_all_and_the_check_says_so() -> None:
    """What the live run on `main` reports, asserted offline.

    Every pre-provisioned wavelength arrives lit and empty, so the check has 40
    containers and no committed total to compare. That is a pass, and the message
    says which pass it is: a bare "no errors" over a branch the check never found
    a parent on would read as evidence the rule was enforced somewhere.
    """
    containers = _shipped_containers()
    assert len(containers) == 40, "the base dataset no longer holds 40 line containers"
    check = _run(_capacity_payload(*containers), CAPACITY)

    assert _messages(check, "ERROR") == []
    assert _messages(check, "INFO") == [
        "Checked 40 containers and none of them holds a child, so no committed total can exceed a capacity. "
        "Every wavelength here is lit and empty"
    ]


def test_a_branch_with_no_containers_is_reported_rather_than_passed_silently() -> None:
    check = _run(_capacity_payload(), CAPACITY)
    assert _messages(check, "ERROR") == []
    assert _messages(check, "INFO") == ["No containers on this branch, so no parent has children to overfill it"]


# ---------------------------------------------------------------------------
# SC-006: the check and the generator, over one tree
# ---------------------------------------------------------------------------

AGREEMENT_TREE = (
    ("odu-line-oc-overfilled", "ODU4", 80, (("odu-fill-x1", "ODU2", 8), ("odu-fill-x2", "ODU2", 80))),
    ("odu-line-oc-exact", "ODU4", 80, (("odu-fill-y1", "ODU2", 8), ("odu-fill-y2", "ODU2", 72))),
    ("odu-line-oc-roomy", "ODUC4", 320, (("odu-fill-z1", "ODU2e", 9),)),
    ("odu-line-oc-unsized", "STM-N", 0, (("odu-fill-w1", "VC-4", 4),)),
    ("odu-line-oc-uncounted", "ODU4", 80, (("odu-fill-v1", "ODUflex", 0),)),
)
"""One branch holding every verdict at once: overfilled, exactly full, roomy, a
parent whose type has no capacity, and a child whose count was never written.

All five in one tree rather than five trees, because SC-006 is a statement about
a branch and not about a container. A per-shape comparison would pass while the
two disagreed about which of the five they were looking at.
"""


def _agreement_payload() -> dict[str, Any]:
    return _capacity_payload(
        *(
            _parent(name, odu_type, capacity, *(_child(*child) for child in children))
            for name, odu_type, capacity, children in AGREEMENT_TREE
        )
    )


def _generator_module() -> Any:
    """The provisioning generator, loaded by path from its own registration.

    Registered rather than hardcoded, on the same terms as the checks: a
    generator moved to another file fails here rather than leaving this test
    comparing the check against nothing.
    """
    entry = next(item for item in CONFIG.generator_definitions if item.name == "optical_service")
    return _module(str(entry.file_path))


def test_the_check_and_the_generator_reach_the_same_verdict_on_every_container() -> None:
    """SC-006, and the one criterion no other test in this repository covers.

    Both sides are run here rather than described. The check's verdict comes out
    of its messages; the generator's comes out of `_offered`, `_child_occupancy`
    and `containers.free_slots`, which is the reading its grooming decision makes.
    The two marshal the stored figures in separate files, so this is the guard
    that keeps them one rule: if either side starts treating an unwritten count as
    zero, or clamps a negative free figure, the mapping below stops matching.

    The failure this prevents is asymmetric and worse than either side failing
    alone. A check that passes what the generator then refuses leaves a merged
    branch whose services cannot be provisioned, and a check that fails what the
    generator packs into leaves a branch nobody can merge while every service on
    it works. Both send the reader to the wrong file.
    """
    payload = _agreement_payload()
    names = tuple(name for name, _, _, _ in AGREEMENT_TREE)
    verdicts = _verdicts(_run(payload, CAPACITY), names)
    assert sorted(set(verdicts.values())) == ["fits", "overfilled", "unknown"], verdicts

    generator = _generator_module().OpticalServiceGenerator
    for record in nodes_of(payload, "OtnContainer"):
        children = list(peers(record, "child_containers"))
        capacity = generator._offered(record)
        free = free_slots(capacity, [generator._child_occupancy(child) for child in children])
        expected = "unknown" if free is None else "overfilled" if free < 0 else "fits"
        assert verdicts[str(record["name"])] == expected, str(record["name"])


def test_the_generator_never_grooms_into_a_container_the_check_rejects() -> None:
    """The same agreement, stated as the consequence that matters.

    `_best_fit` picks the tightest line container that still takes the client, and
    SC-006 says no case exists where one side accepts what the other rejects. So
    whatever it picks has to be a container the check calls fine. The overfilled
    and the unmeasurable parents in the tree are both candidates it is handed and
    both are ones it must not return.
    """
    payload = _agreement_payload()
    names = tuple(name for name, _, _, _ in AGREEMENT_TREE)
    verdicts = _verdicts(_run(payload, CAPACITY), names)

    module = _generator_module()
    generator = module.OpticalServiceGenerator
    options = []
    for record in nodes_of(payload, "OtnContainer"):
        children = list(peers(record, "child_containers"))
        capacity = generator._offered(record)
        options.append(
            module.LineOption(
                name=str(record["name"]),
                odu_type=str(record["odu_type"]),
                capacity=capacity,
                free=free_slots(capacity, [generator._child_occupancy(child) for child in children]),
                carrier_name="oc-test",
                carrier_id="oc-test",
                own=False,
            )
        )

    for occupies in (1, 8, 9, 80):
        chosen = generator._best_fit(options, occupies)
        assert chosen is not None, f"nothing took a {occupies} slot client, so the assertion below is vacuous"
        assert verdicts[chosen.name] == "fits", f"{chosen.name} was groomed into and the check rejects it"


# ---------------------------------------------------------------------------
# The diversity check
# ---------------------------------------------------------------------------

DIVERSITY = "diversity"
"""The check under test below, registered in `.infrahub.yml` under that name."""

DUCT = "conduit-fra-mil-a1"
"""The duct two routes are put into deliberately, so the pair has to be found."""

OTHER_DUCT = "conduit-ams-lon-b2"
"""A duct on the far side of the network, so a diverse pair is really diverse."""


def _hop(sequence: int, span: str, conduit: str | None) -> dict[str, Any]:
    """One hop over a fiber span, selected exactly as `diversity.gql` selects it.

    A span outside every conduit arrives as `{"node": None}`, which is what the
    server sends and not the same shape as an absent key. Reading it as a conduit
    named null would invent the largest shared-risk group in the network out of
    missing data, and this is the payload that proves the walk does not.
    """
    return {
        "sequence": _attribute(sequence),
        "element": _one(
            {
                "__typename": "OtnFiberSpan",
                "name": _attribute(span),
                "conduit": _one({"name": _attribute(conduit)}) if conduit else {"node": None},
            }
        ),
    }


def _segment(sequence: int, *ducts: str | None) -> dict[str, Any]:
    """One wavelength of a circuit, one hop per duct named.

    The sequence is the stored `segment_sequence` rather than a list index,
    because that is what the walk sorts on and an Infrahub relationship hands
    back a set.
    """
    return {
        "segment_sequence": _attribute(sequence),
        "name": _attribute(f"path-seg-{sequence}"),
        "hops": _edges(*(_hop(index + 1, f"span-{sequence}-{index + 1}", duct) for index, duct in enumerate(ducts))),
    }


def _member(name: str, *segments: dict[str, Any]) -> dict[str, Any]:
    """One service in a group. No segment at all is a service with no route yet."""
    return {
        "id": name,
        "__typename": "OtnService",
        "name": _attribute(name),
        "customer": _attribute("EuroHPC"),
        "service_profile": _attribute("hpc-research"),
        "optical_path": _edges(*segments),
    }


def _group(name: str, *members: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": name,
        "__typename": "OtnDiversityGroup",
        "name": _attribute(name),
        "description": _attribute("The two routes sold as diverse."),
        "services": _edges(*members),
    }


def _diversity_payload(*groups: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """The payload in the shape `diversity.gql` returns it: groups, nothing else.

    `extra` exists for one test only, which puts a top-level `OtnService`
    collection into the payload that no server would send for this query. That is
    how the silence gets asserted rather than assumed.
    """
    return {"OtnDiversityGroup": _edges(*groups), **extra}


def test_two_services_in_one_group_sharing_a_duct_fail_naming_all_three() -> None:
    """The failing case, and the one the live run reproduces.

    The message has to carry the duct, both circuits and the group, because a
    proposed change shows the reader the message and nothing else. A failure that
    named only the group would send them looking through its members for the pair,
    and one that named only the pair would not say which promise was broken.
    """
    check = _run(
        _diversity_payload(
            _group(
                "diverse-hpc-pair",
                _member("svc-fra-mil-a", _segment(1, DUCT)),
                _member("svc-fra-mil-b", _segment(1, DUCT, OTHER_DUCT)),
            )
        ),
        DIVERSITY,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "svc-fra-mil-a" in errors[0]
    assert "svc-fra-mil-b" in errors[0]
    assert DUCT in errors[0]
    assert "diverse-hpc-pair" in errors[0]
    assert OTHER_DUCT not in errors[0], "a duct only one of the two crosses is not the finding"


def test_the_failure_is_logged_against_the_group_that_declared_the_requirement() -> None:
    """The pair is not an object, so the group is what the failure attaches to.

    Both services are within their own rights individually. The group is the thing
    that was declared and broken, and it is where a reader finds the requirement
    written next to the failure.
    """
    check = _run(
        _diversity_payload(
            _group(
                "diverse-hpc-pair",
                _member("svc-a", _segment(1, DUCT)),
                _member("svc-b", _segment(1, DUCT)),
            )
        ),
        DIVERSITY,
    )
    failures = [log for log in check.logs if log["level"] == "ERROR"]
    assert [(log["object_id"], log["object_type"]) for log in failures] == [("diverse-hpc-pair", "OtnDiversityGroup")]


def test_two_services_in_one_group_on_disjoint_ducts_pass() -> None:
    """The control. A declaration that holds must not report anything against it."""
    check = _run(
        _diversity_payload(
            _group(
                "diverse-hpc-pair",
                _member("svc-a", _segment(1, DUCT)),
                _member("svc-b", _segment(1, OTHER_DUCT)),
            )
        ),
        DIVERSITY,
    )
    assert _messages(check, "ERROR") == []


def test_a_shared_duct_between_services_that_declared_no_group_is_not_flagged() -> None:
    """The silence, asserted rather than assumed. This is the test that must not
    be deleted by a later change that widens the check.

    `srlg_exposure.py` argues that a check flagging every shared conduit blocks
    merges on exposure an operator accepted deliberately. The check answers that
    by reading members through `OtnDiversityGroup.services` only, so the payload
    here holds a top-level `OtnService` collection that the real query never
    selects: two circuits in the same duct, in no group. A check widened to scan
    every service would fail them, and this fails instead.
    """
    check = _run(
        _diversity_payload(
            _group("diverse-hpc-pair", _member("svc-a", _segment(1, DUCT))),
            OtnService=_edges(
                _member("svc-undeclared-x", _segment(1, DUCT)),
                _member("svc-undeclared-y", _segment(1, DUCT)),
            ),
        ),
        DIVERSITY,
    )
    assert _messages(check, "ERROR") == []
    spoken = " ".join(_messages(check, "ERROR") + _messages(check, "INFO"))
    assert "svc-undeclared-x" not in spoken
    assert "svc-undeclared-y" not in spoken


def test_the_query_is_rooted_on_the_group_so_an_undeclared_service_is_never_fetched() -> None:
    """The other half of the silence, and the half a payload cannot show.

    The check cannot flag what it never receives. Rooting the query on
    `OtnService` would put every undeclared circuit one `if` away from a blocked
    merge, so the shape is asserted here: `srlg_exposure.gql` is the query that
    fetches every service, and it backs a report that blocks nothing.
    """
    document = (REPO_ROOT / "queries" / "diversity.gql").read_text()
    assert "OtnDiversityGroup {" in document
    assert "\n  OtnService {" not in document, "diversity.gql grew a service-rooted collection"


def test_a_group_of_one_says_nothing_at_all() -> None:
    """One member declares nothing that can be broken, so there is nothing to say.

    Not even that its route is unknown: an undetermined verdict is a statement
    about a pair, and a group of one has none.
    """
    check = _run(_diversity_payload(_group("solo", _member("svc-alone"))), DIVERSITY)
    assert _messages(check, "ERROR") == []
    spoken = " ".join(_messages(check, "INFO"))
    assert "svc-alone" not in spoken


def test_a_member_with_no_route_is_reported_undetermined_and_not_as_diverse() -> None:
    """Absent is not the same as diverse.

    Declaring the group before provisioning the circuits is the normal order of
    work, so this must not block the branch that does it. What it must not do is
    read as a pass, which is why the member is named out loud.
    """
    check = _run(
        _diversity_payload(
            _group(
                "diverse-hpc-pair",
                _member("svc-routed", _segment(1, DUCT)),
                _member("svc-not-yet"),
            )
        ),
        DIVERSITY,
    )
    assert _messages(check, "ERROR") == []
    undetermined = [line for line in _messages(check, "INFO") if line.startswith("svc-not-yet")]
    assert len(undetermined) == 1, _messages(check, "INFO")
    assert "undetermined" in undetermined[0]
    assert "1 member(s) have no route yet" in " ".join(_messages(check, "INFO"))


def test_two_segments_of_one_circuit_in_one_duct_are_not_a_violation() -> None:
    """FR-020. A regenerated circuit crosses its own ducts by construction.

    A pairwise comparison over segments reports that as a diversity failure on a
    circuit that is behaving normally. The comparison is between two services over
    the union of each one's segments, and this is the payload that fails if anyone
    reintroduces the segment-versus-segment form.
    """
    check = _run(
        _diversity_payload(
            _group(
                "diverse-hpc-pair",
                _member("svc-regenerated", _segment(1, DUCT), _segment(2, DUCT)),
                _member("svc-other", _segment(1, OTHER_DUCT)),
            )
        ),
        DIVERSITY,
    )
    assert _messages(check, "ERROR") == []


def test_a_chained_circuit_is_compared_over_the_union_of_its_segments() -> None:
    """FR-019, from the direction that catches the narrow answer.

    Reading the first segment only would find the Frankfurt duct and miss the duct
    under the second half of the circuit, so the pair would pass. That answer is
    narrow rather than wrong, which is the dangerous kind: the report comes back
    looking more diverse than the network is.
    """
    check = _run(
        _diversity_payload(
            _group(
                "diverse-hpc-pair",
                _member("svc-regenerated", _segment(1, DUCT), _segment(2, OTHER_DUCT)),
                _member("svc-single", _segment(1, OTHER_DUCT)),
            )
        ),
        DIVERSITY,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert OTHER_DUCT in errors[0]
    assert DUCT not in errors[0]


def test_an_unducted_span_is_not_a_shared_conduit() -> None:
    """Two routes outside every recorded conduit share unrecorded risk, not a duct.

    Grouping them under one missing key would make every unducted circuit a
    diversity failure against every other, which is a green-to-red flip produced
    entirely by absent data.
    """
    check = _run(
        _diversity_payload(
            _group(
                "diverse-hpc-pair",
                _member("svc-a", _segment(1, None)),
                _member("svc-b", _segment(1, None)),
            )
        ),
        DIVERSITY,
    )
    assert _messages(check, "ERROR") == []


def test_the_summary_says_what_was_judged_even_when_no_group_exists() -> None:
    """A green result on a branch with no declaration must not read as diversity.

    It is not that nothing shares a duct. It is that nobody asked for anything
    else, and the line says where the unconditional answer lives.
    """
    check = _run(_diversity_payload(), DIVERSITY)
    assert _messages(check, "ERROR") == []
    summary = _messages(check, "INFO")
    assert len(summary) == 1, summary
    assert "No diversity group on this branch" in summary[0]
    assert "srlg_exposure" in summary[0]


def test_the_check_and_the_report_cannot_disagree_about_who_shares_a_duct() -> None:
    """FR-021, asserted rather than trusted.

    The drift this prevents is asymmetric: a check that passes what the report
    flags sends the reader looking for a bug in whichever of the two they trust
    less, and the report is the one an operator reads first. So the same routes are
    run through both, `impact.service_exposures` for the report's service-rooted
    payload and the check for the group-rooted one, and the pairs have to match.

    The third circuit is in no group. It shares a duct with the first, so the
    report names that pair and the check must not, which is the one place the two
    are allowed to differ and the reason this compares within the group only.
    """
    declared = (
        _member("svc-a", _segment(1, DUCT)),
        _member("svc-b", _segment(1, DUCT, OTHER_DUCT)),
    )
    undeclared = _member("svc-c", _segment(1, DUCT))

    reported = non_diverse_pairs(service_exposures({"OtnService": _edges(*declared, undeclared)}))
    within_group = {
        (pair.service_a, pair.service_b): pair.shared
        for pair in reported
        if {pair.service_a, pair.service_b} <= {"svc-a", "svc-b"}
    }
    assert within_group == {("svc-a", "svc-b"): (DUCT,)}, reported
    assert any("svc-c" in (pair.service_a, pair.service_b) for pair in reported), "the report stopped pairing svc-c"

    errors = _messages(_run(_diversity_payload(_group("diverse-hpc-pair", *declared)), DIVERSITY), "ERROR")
    assert len(errors) == 1, errors
    for (first, second), shared in within_group.items():
        assert first in errors[0]
        assert second in errors[0]
        assert all(duct in errors[0] for duct in shared)
    assert "svc-c" not in errors[0]


# ---------------------------------------------------------------------------
# The channel collision check, which now refuses overlap rather than equality
# ---------------------------------------------------------------------------

COLLISION = "channel_collision"
"""The check under test below, registered in `.infrahub.yml` under that name."""

SHARED_SECTION = "oms-fra-mil"
"""The flagship section, and the one both carriers cross when they are meant to."""

OTHER_SECTION = "oms-ams-lon"
"""A section on the far side of the network, so a passing pair really is apart."""

QPSK_128_MBAUD = 128_000
"""The widest seeded mode, 150.0 GHz occupied. Three 50 GHz channels of spectrum."""

QPSK_32_MBAUD = 32_000
"""The narrowest seeded mode, 44.4 GHz occupied."""

QAM16_64_MBAUD = 64_000
"""79.6 GHz occupied, which is what puts channel 1 outside the band."""

GRID_FILLING_MBAUD = 37_091
"""The symbol rate whose occupied width is exactly one 50 GHz channel.

No catalogue mode runs at it. It exists so two carriers on adjacent anchors meet
at exactly one frequency and share nothing, which the three seeded modes cannot
produce: on a 50 GHz raster their edges either overlap or leave a gap.
`test_two_touching_carriers_on_a_shared_section_pass` asserts the width first, so
a change to the roll-off or the guard band reports that the case stopped touching
rather than quietly passing for the wrong reason.
"""


def _carrier(name: str, channel: int, baud: int, *sections: str) -> dict[str, Any]:
    """One carrier in the shape `channel_collision.gql` returns it.

    The anchor centre is derived from the channel number rather than passed in,
    because that is the one relationship the schema guarantees and a hand-picked
    centre would let a test assert an overlap no grid can produce.
    """
    return {
        "id": name,
        "__typename": "OtnOpticalCarrier",
        "name": _attribute(name),
        "status": _attribute("active"),
        "channel": _one(
            {
                "channel_number": _attribute(channel),
                "center_frequency_mhz": _attribute(channel_to_frequency_mhz(channel)),
            }
        ),
        "optical_mode": _one({"name": _attribute(f"mode-{baud}"), "baud_mbaud": _attribute(baud)}),
        "sections": _edges(*({"name": _attribute(section)} for section in sections)),
    }


def _collision_payload(*carriers: dict[str, Any]) -> dict[str, Any]:
    return {"OtnOpticalCarrier": _edges(*carriers)}


def test_two_overlapping_carriers_on_a_shared_section_fail_naming_the_range() -> None:
    """The failure the channel-number rule passed, and the reason for the feature.

    Channel 40 and channel 41 are different numbers, so the old rule saw two
    carriers with nothing in common. A 128 GBd carrier is 150 GHz wide and swallows
    its neighbour whole.

    The message carries the shared range, both carriers and the section, because a
    proposed change shows the reader the message and nothing else.
    """
    check = _run(
        _collision_payload(
            _carrier("oc-wide", 40, QPSK_128_MBAUD, SHARED_SECTION),
            _carrier("oc-narrow", 41, QPSK_32_MBAUD, SHARED_SECTION),
        ),
        COLLISION,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "oc-wide" in errors[0]
    assert "oc-narrow" in errors[0]
    assert SHARED_SECTION in errors[0]
    lower = channel_to_frequency_mhz(41) - occupied_width_mhz(QPSK_32_MBAUD) // 2
    upper = lower + occupied_width_mhz(QPSK_32_MBAUD)
    assert f"{lower:,}" in errors[0], "the message has to name the overlap, not only the pair"
    assert f"{upper:,}" in errors[0]
    assert {log["level"] for log in check.logs} <= {"ERROR", "INFO"}, "log_warning does not exist in the SDK"


def test_two_overlapping_carriers_that_share_no_section_pass() -> None:
    """Spectrum is scarce within a section, not across the network.

    The same pair as above, moved apart. Reporting this would mean the network had
    one wavelength plan in total rather than one per section, and every second
    carrier the demo provisions would be refused.
    """
    check = _run(
        _collision_payload(
            _carrier("oc-wide", 40, QPSK_128_MBAUD, SHARED_SECTION),
            _carrier("oc-narrow", 41, QPSK_32_MBAUD, OTHER_SECTION),
        ),
        COLLISION,
    )
    assert _messages(check, "ERROR") == []
    assert _messages(check, "INFO"), "a clean run has to say what it looked at"


def test_two_touching_carriers_on_a_shared_section_pass() -> None:
    """A shared edge is not shared spectrum.

    The intervals are half-open, so the upper edge is the first frequency the
    carrier does not hold. With closed intervals a densely packed plan would report
    a collision on every boundary it has, which is exactly where the check has to
    be trusted.
    """
    assert occupied_width_mhz(GRID_FILLING_MBAUD) == 50_000, "the case stopped being a touching one"
    check = _run(
        _collision_payload(
            _carrier("oc-left", 40, GRID_FILLING_MBAUD, SHARED_SECTION),
            _carrier("oc-right", 41, GRID_FILLING_MBAUD, SHARED_SECTION),
        ),
        COLLISION,
    )
    assert _messages(check, "ERROR") == []


def test_a_carrier_past_the_band_edge_is_reported_unprovisionable() -> None:
    """Channel 1 sits 25 GHz above the lower edge and a 64 GBd carrier is 79.6 GHz
    wide, so it reaches below the band before it reaches any neighbour. It collides
    with nothing and still cannot be lit, which is a failure the pairwise rule
    cannot see on its own."""
    check = _run(_collision_payload(_carrier("oc-low", 1, QAM16_64_MBAUD, SHARED_SECTION)), COLLISION)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "oc-low" in errors[0]
    assert "unprovisionable" in errors[0]
    assert f"{CBAND_LOWER_EDGE_MHZ:,}" in errors[0]


def test_a_carrier_crossing_no_section_is_an_error_and_not_a_skip() -> None:
    """The fail-closed case the check has always had, and still has.

    A carrier whose channel reserves nothing on any section is a claim on nothing.
    Passing over it would report green for spectrum nobody measured, and the next
    generator run would allocate on top of it.
    """
    check = _run(_collision_payload(_carrier("oc-nowhere", 40, QPSK_32_MBAUD)), COLLISION)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "oc-nowhere" in errors[0]
    assert "no section" in errors[0]
    assert _messages(check, "INFO") == [], "an incomplete occupancy map must not also report a clean verdict"


# ---------------------------------------------------------------------------
# The provisionable check
# ---------------------------------------------------------------------------

PROVISIONABLE = "provisionable"
"""The check under test below, registered in `.infrahub.yml` under that name."""

REFUSED_SERVICE = "svc-mad-waw-400g"
"""Madrid to Warsaw at 400G, the refusal the demo exists to show."""

BUDGET = "budget"
"""One of the six reason codes. The schema refuses a seventh, so no test here
asserts that a bad code is rejected: that assertion lives in
`tests/unit/test_schema_contract.py` against the Dropdown itself."""

BUDGET_DETAIL = "best margin -0.535 dB on oms-par-mad with 16qam-400g"
"""The prose half. Asserted in the message because a code alone does not tell an
operator whether to re-plan, to regenerate or to build."""


def _service(
    name: str,
    *,
    status: str = "active",
    code: str | None = None,
    detail: str | None = None,
    accepted: bool = False,
    segments: int = 0,
) -> dict[str, Any]:
    """One service in the shape `provisionable.gql` returns it.

    Every field the query selects is present on every service, including the
    empty ones, because two of the five judged states are states where a field is
    empty. A builder that omitted a null code would hide the fail-closed case
    behind a missing key rather than an empty value, which is not what a server
    sends.
    """
    return {
        "id": name,
        "__typename": "OtnService",
        "name": _attribute(name),
        "status": _attribute(status),
        "rejection_code": _attribute(code),
        "rejection_detail": _attribute(detail),
        "refusal_accepted": _attribute(accepted),
        "optical_path": _edges(*({"id": f"{name}-seg-{index + 1}"} for index in range(segments))),
    }


def _provisionable_payload(*services: dict[str, Any]) -> dict[str, Any]:
    return {"OtnService": _edges(*services)}


def _refused_service(**overrides: Any) -> dict[str, Any]:
    """The refusal the gate exists for, before any override moves it off that state."""
    fields: dict[str, Any] = {"status": "rejected", "code": BUDGET, "detail": BUDGET_DETAIL}
    fields.update(overrides)
    return _service(REFUSED_SERVICE, **fields)


def test_a_provisioned_service_is_not_spoken_about_at_all() -> None:
    """Row one of the state table. Not rejected, no code, not accepted.

    A check that annotated every healthy service would drown the one message the
    proposed change exists to show, so the silence is asserted by name rather
    than only by the absence of errors.
    """
    check = _run(_provisionable_payload(_service("svc-ber-ams-400g", segments=1)), PROVISIONABLE)
    assert _messages(check, "ERROR") == []
    assert not any("svc-ber-ams-400g" in line for line in _messages(check, "INFO"))


def test_a_refused_and_unaccepted_service_fails_naming_the_service_the_code_and_the_detail() -> None:
    """Row two, and the feature. SC-002 is the three parts in one message.

    A proposed change shows the reader this message and nothing else. Naming only
    the service would send them querying for the reason; naming only the code
    would not say which circuit it was about; leaving out the detail would make
    every budget refusal in the network read identically.
    """
    check = _run(_provisionable_payload(_refused_service()), PROVISIONABLE)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert REFUSED_SERVICE in errors[0]
    assert BUDGET in errors[0]
    assert BUDGET_DETAIL in errors[0]
    assert "refusal_accepted" in errors[0], "a blocked merge has to say where the escape hatch is"


def test_the_failure_is_logged_against_the_service_that_cannot_be_built() -> None:
    """One node is the whole subject here, unlike the diversity check where the
    judgement is about a pair and attaches to the group that declared it."""
    check = _run(_provisionable_payload(_refused_service()), PROVISIONABLE)
    failures = [log for log in check.logs if log["level"] == "ERROR"]
    assert [(log["object_id"], log["object_type"]) for log in failures] == [(REFUSED_SERVICE, "OtnService")]


def test_a_refused_service_marked_accepted_is_not_spoken_about_at_all() -> None:
    """Row three, and the test that must not be deleted by a change widening this
    check.

    Some refusals are the answer rather than a failure. Madrid to Warsaw at 400G
    is a demo scenario whose entire point is the recorded refusal, and no amount
    of building makes it provisionable. A check that also flagged accepted
    refusals would block merges on decisions somebody already made, which is the
    capability the flag exists to preserve.
    """
    check = _run(_provisionable_payload(_refused_service(accepted=True)), PROVISIONABLE)
    assert _messages(check, "ERROR") == []
    assert not any(REFUSED_SERVICE in line for line in _messages(check, "INFO"))


def test_a_service_marked_accepted_that_carries_no_refusal_is_an_error() -> None:
    """Row five. The flag is set on the wrong node, and that blocks.

    Annotating would be the softer answer and the wrong one: the operator who set
    it believes a refusal is signed for, and the refusal they meant is on some
    other node, still unaccepted and still blocking.
    """
    check = _run(
        _provisionable_payload(_service("svc-ams-mil-ai-400g", accepted=True, segments=2)),
        PROVISIONABLE,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "svc-ams-mil-ai-400g" in errors[0]
    assert "refusal_accepted" in errors[0]
    assert "2 path segment(s)" in errors[0], "the likelier cause is a service that was since provisioned"


def test_a_service_marked_rejected_with_no_reason_code_is_an_error_and_not_a_skip() -> None:
    """Row four, fail closed.

    The generator writes the code and the detail together, so this state means a
    hand edit or a write path nobody modelled. Skipping it would report green for
    a refusal nobody can read, which is worse than reporting the refusal.
    """
    check = _run(_provisionable_payload(_service("svc-hand-edited", status="rejected")), PROVISIONABLE)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "svc-hand-edited" in errors[0]
    assert "no reason code" in errors[0]
    assert "not a skip" in errors[0]


def test_an_unreadable_refusal_is_not_cleared_by_the_acceptance_flag() -> None:
    """The `any` in row four, which is the half an ordering mistake would lose.

    Accepting a refusal means having read it, and a refusal with no code cannot
    have been read. Testing the flag before the code would let the one state the
    generator cannot produce merge on a signature nobody could have given.
    """
    check = _run(
        _provisionable_payload(_service("svc-hand-edited", status="rejected", accepted=True)),
        PROVISIONABLE,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "no reason code" in errors[0]
    assert "does not clear it" in errors[0]


def test_a_branch_with_no_services_says_it_looked_rather_than_passing_silently() -> None:
    """Row six. FR-013, and the reason it is a requirement at all.

    A green check on an empty branch and a green check on a provisioned network
    are indistinguishable from the proposed change, and only one of them is
    evidence of anything.
    """
    check = _run(_provisionable_payload(), PROVISIONABLE)
    assert _messages(check, "ERROR") == []
    lines = _messages(check, "INFO")
    assert len(lines) == 1, lines
    assert "No service on this branch" in lines[0]
    assert "not a network that was proved provisionable" in lines[0]


def test_a_payload_with_no_service_collection_is_an_error_and_not_an_empty_branch() -> None:
    """A query that did not run must not read as a branch with nothing on it.

    `plant.nodes_of` reads `payload.get(kind) or {}`, so a renamed query, a
    partial GraphQL error and a permission filter all arrive as zero services.
    Without the guard the check reports that it looked and found none, which is a
    claim about a branch this run never saw, and the gate goes green over every
    service on it.

    The three shapes are asserted together because a guard that caught only the
    absent key would still pass a null collection through.
    """
    unreadable: tuple[dict[str, Any], ...] = ({}, {"OtnService": None}, {"OtnService": {}})
    for payload in unreadable:
        check = _run(payload, PROVISIONABLE)
        errors = _messages(check, "ERROR")
        assert len(errors) == 1, (payload, errors)
        assert "carries no OtnService collection" in errors[0]
        assert "not a branch with nothing on it" in errors[0]
        assert _messages(check, "INFO") == [], "an unread branch must not also report that it looked"


def test_an_empty_service_collection_is_still_the_branch_that_looked_and_found_none() -> None:
    """The other side of the guard above, which is what stops it over-firing.

    A collection that came back holding no services is a readable answer and the
    check has to keep saying so. A guard that treated the two the same would
    block every branch with no services on it.
    """
    check = _run({"OtnService": {"edges": []}}, PROVISIONABLE)
    assert _messages(check, "ERROR") == []
    assert "No service on this branch" in _messages(check, "INFO")[0]


def test_a_service_whose_status_did_not_come_back_fails_rather_than_reading_as_provisionable() -> None:
    """`status` is the field the gate turns on, so it is read strictly.

    `str(service.get("status") or "")` turned a status that never arrived into a
    value that is not `rejected`, which is the branch where the check says nothing
    and the merge goes through. Both shapes are asserted: a null value and an
    absent key. The reason code already failed closed on the same payload, so this
    is the looser of the two reads catching up with the stricter one.
    """
    nulled = _refused_service()
    nulled["status"] = _attribute(None)
    with pytest.raises(ValueError, match="null status"):
        _run(_provisionable_payload(nulled), PROVISIONABLE)

    absent = _refused_service()
    del absent["status"]
    with pytest.raises(KeyError):
        _run(_provisionable_payload(absent), PROVISIONABLE)


def test_a_stale_reason_code_on_a_provisioned_service_is_reported_and_does_not_block() -> None:
    """The one state the table does not have a row for, said out loud anyway.

    The gate turns on `status`, and this service is not refused, so blocking would
    stop a branch on which the network improved. Passing unremarked would be worse
    than either: a code nobody cleared makes every filter by reason code wrong.
    `diversity.py` reports a member with no route on the same terms.
    """
    check = _run(
        _provisionable_payload(_service("svc-fra-mil-ai-400g", code=BUDGET, segments=1)),
        PROVISIONABLE,
    )
    assert _messages(check, "ERROR") == []
    stale = [line for line in _messages(check, "INFO") if "svc-fra-mil-ai-400g" in line]
    assert len(stale) == 1, _messages(check, "INFO")
    assert "stale" in stale[0]


def test_a_leftover_detail_with_no_reason_code_is_reported_on_the_same_terms() -> None:
    """The other half of FR-006's pair, which nothing was watching.

    The generator writes the code and the detail together or writes neither, so a
    detail sitting alone is the same broken pair as a code sitting alone. The
    check tested only the code, so this one passed unremarked and no filter by
    reason code would ever have surfaced it either.

    An INFO and not an error, for the reason the code branch is an INFO: the gate
    reads the status, and this service is not refused.
    """
    check = _run(
        _provisionable_payload(_service("svc-fra-mil-ai-400g", detail=BUDGET_DETAIL, segments=1)),
        PROVISIONABLE,
    )
    assert _messages(check, "ERROR") == []
    stale = [line for line in _messages(check, "INFO") if "svc-fra-mil-ai-400g" in line]
    assert len(stale) == 1, _messages(check, "INFO")
    assert "stale" in stale[0]
    assert "no reason code" in stale[0], "the message has to name which half was left behind"


def test_the_summary_counts_the_five_states_separately() -> None:
    """Separate numbers for the reason `diversity.py` keeps its numbers separate.

    A single "judged N services" would let them hide inside each other, and a
    branch whose every refusal is signed for is not a branch with no refusals on
    it.
    """
    check = _run(
        _provisionable_payload(
            _refused_service(),
            _service("svc-signed", status="rejected", code=BUDGET, detail="x", accepted=True),
            _service("svc-unreadable", status="rejected"),
            _service("svc-misplaced", accepted=True),
            _service("svc-clean", segments=1),
        ),
        PROVISIONABLE,
    )
    line = next(item for item in _messages(check, "INFO") if item.startswith("Judged "))
    assert "Judged 5 service(s)" in line
    assert "1 refused and unaccepted" in line
    assert "1 refused with no readable code" in line
    assert "1 accepting a refusal that does not exist" in line
    assert "1 refused and signed for" in line
    assert "1 carrying an optical path" in line
    assert len(_messages(check, "ERROR")) == 3, _messages(check, "ERROR")
    assert {log["level"] for log in check.logs} <= {"ERROR", "INFO"}, "log_warning does not exist in the SDK"


def test_the_status_the_gate_turns_on_is_a_choice_the_schema_declares() -> None:
    """`REJECTED` is a literal in the check, because no shared constant holds it.

    The generator writes the same literal. This is what stops the pair drifting:
    a status renamed in the schema fails here rather than turning the check
    silently green, which is the failure shape a string comparison against a
    Dropdown always has.
    """
    module = _module(_entry(PROVISIONABLE)[0])
    document = yaml.safe_load((REPO_ROOT / "schemas" / "otn_service.yml").read_text())
    node = next(entry for entry in document["nodes"] if entry["name"] == "Service")
    status = next(item for item in node["attributes"] if item["name"] == "status")
    assert module.REJECTED in {str(choice["name"]) for choice in status["choices"]}


def test_the_query_fetches_every_service_so_the_empty_field_states_are_visible() -> None:
    """The shape assertion, and it is the opposite of the diversity one.

    That query roots on the group so an undeclared service is never fetched. This
    one has to see every service, because a refusal with no code and an acceptance
    flag with no refusal are both states where a field is empty. A filter on
    `status: rejected` would fetch neither, and the check would report green over
    both.
    """
    document = (REPO_ROOT / "queries" / "provisionable.gql").read_text()
    assert "\n  OtnService {" in document, "provisionable.gql stopped fetching every service"
    for field in ("status", "rejection_code", "rejection_detail", "refusal_accepted"):
        assert f"{field} {{" in document
    assert "OtnService(" not in document, "a filter here hides the states the check exists to catch"


def test_the_check_is_global_so_a_change_touching_one_service_still_judges_the_others() -> None:
    """FR-014. A targeted check would bind to the objects the change touched.

    Adding a span, retiring a mode or filling a corridor makes a service nobody
    edited unprovisionable on the generator's next run. A `targets:` key here
    would look at the edited objects, find nothing wrong with them, and report
    green over the service that can no longer be built.
    """
    entry = next(item for item in CONFIG.check_definitions if item.name == PROVISIONABLE)
    assert entry.targets is None
    assert _module(str(entry.file_path)).ProvisionableCheck.query == PROVISIONABLE


# ---------------------------------------------------------------------------
# Channel count consistency
# ---------------------------------------------------------------------------

CHANNEL_COUNT = "channel_count_consistency"
COMPLETENESS = "monitor_completeness"

DEGREE_PORT = "OtnRoadmDegreePort"
DEGREE_MONITOR = "OtnRoadmDegreeMonitor"
MUX_MONITOR = "OtnMuxDemuxMonitor"

FRA = "roadm-fra-01"
MIL = "roadm-mil-01"
SECTION = "oms-fra-mil"
"""One section between two ROADMs, which is the smallest topology the degree
join has anything to say about. A single ROADM has a degree facing nowhere, and
that is a different case with its own test below."""

FIRST_ANCHOR = 191_350_000
"""The lowest centre frequency the C-band grid defines, in MHz. Any hashable
value would do here, because `channels_by_section` only compares anchors for
equality, but using a real one keeps the payload readable as a plan."""


def _port(typename: str, name: str | None = None, channel_count: int | None = None) -> dict[str, Any]:
    """One port node in the shape an inline fragment returns it.

    A port outside the fragment comes back with `__typename` and nothing else,
    which is what the query really sends, so the default omits the name. Building
    it richer than the query would let a check read a field no server would give
    it.
    """
    port: dict[str, Any] = {"id": f"{typename}:{name or 'plain'}", "__typename": typename}
    if name is not None:
        port["name"] = _attribute(name)
    if channel_count is not None:
        port["channel_count"] = _attribute(channel_count)
    return port


def _device(kind: str, name: str, *ports: dict[str, Any]) -> dict[str, Any]:
    return {"id": name, "__typename": kind, "name": _attribute(name), "ports": _edges(*ports)}


def _roadm(name: str, site: str, *ports: dict[str, Any]) -> dict[str, Any]:
    node = _device("OtnRoadm", name, *ports)
    node["site"] = _one({"shortname": _attribute(site)})
    return node


def _mux(name: str, lit: int, *ports: dict[str, Any]) -> dict[str, Any]:
    """A multiplexer lighting `lit` coarse wavelengths.

    `lit` of zero is a dense AWG. It is the absence of the relationship that
    makes it one, not a flag, which is why the payload says it by holding no
    channels rather than by setting a field.
    """
    node = _device("OtnMuxDemux", name, *ports)
    node["cwdm_channels"] = _edges(*({"center_wavelength_nm": _attribute(1271 + 20 * n)} for n in range(lit)))
    return node


def _count_section(name: str, roadm_a: str, roadm_b: str) -> dict[str, Any]:
    return {
        "id": name,
        "__typename": "OtnOpticalMultiplexSection",
        "name": _attribute(name),
        "roadm_a": _one({"name": _attribute(roadm_a)}),
        "roadm_b": _one({"name": _attribute(roadm_b)}),
    }


def _count_carrier(name: str, anchor: int, *sections: str) -> dict[str, Any]:
    return {
        "name": _attribute(name),
        "channel": _one({"center_frequency_mhz": _attribute(anchor)}),
        "sections": _edges(*({"name": _attribute(section)} for section in sections)),
    }


def _count_payload(
    *,
    sections: tuple[dict[str, Any], ...] = (),
    carriers: tuple[dict[str, Any], ...] = (),
    roadms: tuple[dict[str, Any], ...] = (),
    muxes: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """The payload in the shape `channel_count_consistency.gql` returns it."""
    return {
        "OtnOpticalMultiplexSection": _edges(*sections),
        "OtnOpticalCarrier": _edges(*carriers),
        "OtnRoadm": _edges(*roadms),
        "OtnMuxDemux": _edges(*muxes),
    }


def _degree_ends(reported_at_fra: int, reported_at_mil: int) -> tuple[dict[str, Any], ...]:
    """The two ROADMs at the ends of `SECTION`, each with one degree and its monitor."""
    return (
        _roadm(FRA, "fra", _port(DEGREE_PORT, "DEG-MIL"), _port(DEGREE_MONITOR, "MON-DEG-MIL", reported_at_fra)),
        _roadm(MIL, "mil", _port(DEGREE_PORT, "DEG-FRA"), _port(DEGREE_MONITOR, "MON-DEG-FRA", reported_at_mil)),
    )


def _lit(count: int, *, anchor: int = FIRST_ANCHOR, spacing: int = 50_000) -> tuple[dict[str, Any], ...]:
    """`count` carriers on `SECTION`, each on an anchor of its own."""
    return tuple(_count_carrier(f"wave-{n:03d}", anchor + n * spacing, SECTION) for n in range(count))


def test_a_channel_count_payload_that_agrees_passes_and_the_summary_names_the_comparison() -> None:
    """The control, and the summary is half of what it asserts.

    A check that logs nothing on a clean branch tells a reader that it ran and
    not what it looked at. Both monitors here are compared against the same
    section from opposite ends, and the summary has to say so.
    """
    check = _run(
        _count_payload(
            sections=(_count_section(SECTION, FRA, MIL),),
            carriers=_lit(2),
            roadms=_degree_ends(2, 2),
        ),
        CHANNEL_COUNT,
    )
    assert _messages(check, "ERROR") == []
    summary = " ".join(_messages(check, "INFO"))
    assert "Compared 2 channel monitor(s)" in summary
    assert "2 agree, 0 disagree in a way a dated reading cannot explain" in summary
    assert "0 have not yet seen a wavelength" in summary


def test_a_monitor_reporting_seventy_one_against_a_section_holding_forty_fails_with_both_numbers() -> None:
    """The pre-repair observation, kept because the dataset no longer holds it.

    Every degree monitor in `objects/` reported 71 channels, which was the number
    of ODU containers the generator happened to have to hand. `oms-fra-mil`
    carries 40. The repair on this branch rewrote the counts, so the only place
    the failure now exists is here, and a check whose only evidence is a green
    run is a check nobody has seen fail.

    The message has to carry both figures and the difference. One that said only
    "disagrees" would send the reader to look up two numbers the check already
    held.
    """
    check = _run(
        _count_payload(
            sections=(_count_section(SECTION, FRA, MIL),),
            carriers=_lit(40),
            roadms=_degree_ends(71, 40),
        ),
        CHANNEL_COUNT,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "MON-DEG-MIL" in errors[0]
    assert FRA in errors[0]
    assert "71" in errors[0]
    assert "40" in errors[0]
    assert "difference of 31" in errors[0]
    assert SECTION in errors[0]
    assert "invent light" in errors[0], "the error has to say why over-reporting is the gated direction"


def test_a_monitor_that_has_not_yet_seen_a_new_wavelength_is_an_info_and_does_not_block() -> None:
    """The provisioning branch, and it is why this check is asymmetric.

    `generators/optical_service.py` writes a carrier onto a branch and touches no
    monitor, so every degree along the new route sits one channel behind the
    section it faces. Gating on that would put a red validator on every
    provisioning branch in this demo and refuse the merge the walkthrough is
    built around.

    A monitor reading is dated, so it can only ever under-report a design that is
    newer than it. The disagreement is still reported, because a reader wants to
    see which monitors have fallen behind, and the message has to say plainly
    that it is not a refusal.
    """
    check = _run(
        _count_payload(
            sections=(_count_section(SECTION, FRA, MIL),),
            carriers=_lit(1),
            roadms=_degree_ends(0, 0),
        ),
        CHANNEL_COUNT,
    )
    assert _messages(check, "ERROR") == []
    behind = [line for line in _messages(check, "INFO") if "MON-DEG-MIL" in line]
    assert len(behind) == 1, _messages(check, "INFO")
    assert "does not block" in behind[0]
    assert "can only under-report" in behind[0]
    summary = " ".join(_messages(check, "INFO"))
    assert "0 agree, 0 disagree in a way a dated reading cannot explain" in summary
    assert "2 have not yet seen a wavelength" in summary


def test_the_two_directions_are_counted_apart_in_the_summary() -> None:
    """One branch holding both faults at once, which is the only way to tell the
    two counts are not one count printed twice.

    Frankfurt reports the stale 71 and is refused. Milan reports 39 against the
    40 on the same fibre and is not. A summary that folded either into "agree"
    would report a network in a state nobody can see from here.
    """
    check = _run(
        _count_payload(
            sections=(_count_section(SECTION, FRA, MIL),),
            carriers=_lit(40),
            roadms=_degree_ends(71, 39),
        ),
        CHANNEL_COUNT,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "MON-DEG-MIL" in errors[0]
    summary = " ".join(_messages(check, "INFO"))
    assert "0 agree, 1 disagree in a way a dated reading cannot explain" in summary
    assert "1 have not yet seen a wavelength" in summary


def test_the_disagreement_is_logged_against_the_monitor_holding_the_wrong_number() -> None:
    """The monitor, not the section.

    The monitor is the object holding the figure a reader would go and correct,
    and one section has a monitor at each end that can be wrong by different
    amounts. Naming the section would put two findings on one page and neither on
    the object that carries the fault.
    """
    check = _run(
        _count_payload(
            sections=(_count_section(SECTION, FRA, MIL),),
            carriers=_lit(40),
            roadms=_degree_ends(71, 40),
        ),
        CHANNEL_COUNT,
    )
    failures = [log for log in check.logs if log["level"] == "ERROR"]
    assert [log["object_type"] for log in failures] == [DEGREE_MONITOR]
    assert [log["object_id"] for log in failures] == [f"{DEGREE_MONITOR}:MON-DEG-MIL"]


def test_a_section_with_no_carriers_and_a_monitor_reporting_zero_passes() -> None:
    """The zero boundary, and the reason `channels_by_section` takes a section list.

    An empty section has to report 0 rather than fall out of the occupancy
    mapping. A caller reading a missing key as "nothing to check" would skip both
    monitors here, and a skipped monitor is an unchecked monitor, not a passing
    one. So this asserts the pass and the summary that says two were compared.
    """
    check = _run(
        _count_payload(
            sections=(_count_section(SECTION, FRA, MIL),),
            roadms=_degree_ends(0, 0),
        ),
        CHANNEL_COUNT,
    )
    assert _messages(check, "ERROR") == []
    assert "Compared 2 channel monitor(s)" in " ".join(_messages(check, "INFO"))


def test_two_carriers_on_one_anchor_count_as_one_channel_and_raise_nothing() -> None:
    """Channels, not carrier records. This is the test that must survive a widening.

    An optical channel monitor counts light on a fibre, and two carriers on one
    anchor are one channel to it. They are also a collision, and
    `checks/channel_collision.py` already reports it. Counting records here would
    raise a second finding for a fault another check has named, against a channel
    count that is correct.
    """
    collided = (
        _count_carrier("wave-a", FIRST_ANCHOR, SECTION),
        _count_carrier("wave-b", FIRST_ANCHOR, SECTION),
    )
    check = _run(
        _count_payload(
            sections=(_count_section(SECTION, FRA, MIL),),
            carriers=collided,
            roadms=_degree_ends(1, 1),
        ),
        CHANNEL_COUNT,
    )
    assert _messages(check, "ERROR") == []
    spoken = " ".join(_messages(check, "ERROR") + _messages(check, "INFO"))
    assert "wave-a" not in spoken, "the collision belongs to channel_collision and is not named twice"
    assert "wave-b" not in spoken


def test_a_degree_whose_far_site_resolves_to_no_section_is_an_error() -> None:
    """The one condition this check owns alone.

    The port exists and its monitor exists, so `monitor_completeness` is silent
    about the pair. What is wrong is that the fibre the monitor reports about
    cannot be identified, and nothing else in the repository looks at that.
    """
    stranded = _roadm(
        FRA,
        "fra",
        _port(DEGREE_PORT, "DEG-XYZ"),
        _port(DEGREE_MONITOR, "MON-DEG-XYZ", 12),
    )
    check = _run(_count_payload(roadms=(stranded,)), CHANNEL_COUNT)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "MON-DEG-XYZ" in errors[0]
    assert "DEG-XYZ" in errors[0]
    assert "xyz" in errors[0]
    assert "1 facing a site with no section" in " ".join(_messages(check, "INFO"))


@pytest.mark.parametrize("shortname", ["FRA", "Fra"])
def test_a_site_whose_shortname_is_not_lower_case_still_resolves_through_the_degree_join(shortname: str) -> None:
    """`LocationGeneric.shortname` carries no case constraint, so the join folds.

    `degree_port_name` writes the shortname in upper case and
    `far_site_of_degree` reads it back folded down, so a site created as `FRA` or
    `Fra` produces a degree the section map could only match under the folded
    key. Before the fold was applied on both sides, every degree monitor facing
    such a site was refused as facing a site with no section, which is a blocked
    merge on data that is entirely correct.
    """
    roadms = (
        _roadm(FRA, shortname, _port(DEGREE_PORT, "DEG-MIL"), _port(DEGREE_MONITOR, "MON-DEG-MIL", 2)),
        _roadm(MIL, "mil", _port(DEGREE_PORT, "DEG-FRA"), _port(DEGREE_MONITOR, "MON-DEG-FRA", 2)),
    )
    check = _run(
        _count_payload(sections=(_count_section(SECTION, FRA, MIL),), carriers=_lit(2), roadms=roadms),
        CHANNEL_COUNT,
    )
    assert _messages(check, "ERROR") == []
    summary = " ".join(_messages(check, "INFO"))
    assert "Compared 2 channel monitor(s)" in summary
    assert "0 facing a site with no section" in summary


def test_a_monitor_with_no_degree_port_is_an_info_that_defers_and_not_an_error() -> None:
    """One condition, one owner. `monitor_completeness` reports this one.

    Raising here as well would put two findings on one fault, and a reader
    fixing the second would find the first already answered. There is no level
    between the two, so deferring means INFO.
    """
    orphaned = _roadm(FRA, "fra", _port(DEGREE_MONITOR, "MON-DEG-MIL", 40))
    check = _run(
        _count_payload(
            sections=(_count_section(SECTION, FRA, MIL),),
            carriers=_lit(40),
            roadms=(orphaned,),
        ),
        CHANNEL_COUNT,
    )
    assert _messages(check, "ERROR") == []
    deferred = [line for line in _messages(check, "INFO") if "MON-DEG-MIL" in line]
    assert len(deferred) == 1, _messages(check, "INFO")
    assert "DEG-MIL" in deferred[0]
    assert "monitor_completeness" in deferred[0]


def test_a_dense_multiplexer_monitor_is_reported_uncomparable_and_raises_nothing() -> None:
    """The silence about the fourteen, said out loud once.

    A dense AWG holds no relationship to the carriers passing through it, so
    there is nothing to compare its count against. The check says so rather than
    passing quietly, because a monitor nobody compared must not read as a monitor
    that agreed.
    """
    check = _run(
        _count_payload(muxes=(_mux("mux-fra-01", 0, _port(MUX_MONITOR, "MON-mux-fra-01", 8)),)),
        CHANNEL_COUNT,
    )
    assert _messages(check, "ERROR") == []
    spoken = " ".join(_messages(check, "INFO"))
    assert "1 dense multiplexer monitor(s) were not compared" in spoken
    assert "Compared 0 channel monitor(s)" in spoken


def test_a_coarse_multiplexer_monitor_is_compared_against_the_wavelengths_it_lights() -> None:
    """The two of the sixteen that are comparable, in both directions.

    A coarse multiplexer says which wavelengths it lights through
    `cwdm_channels`, so its monitor has something to be judged against. This is
    the pair that shows the dense silence is about the missing relationship and
    not about multiplexers.
    """
    agreeing = _mux("mux-ams-02", 4, _port(MUX_MONITOR, "MON-mux-ams-02", 4))
    disagreeing = _mux("mux-asp-01", 4, _port(MUX_MONITOR, "MON-mux-asp-01", 5))
    check = _run(_count_payload(muxes=(agreeing, disagreeing)), CHANNEL_COUNT)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "MON-mux-asp-01" in errors[0]
    assert "difference of 1" in errors[0]


def test_a_coarse_multiplexer_monitor_below_its_lit_channels_is_an_error_and_not_a_lag() -> None:
    """The asymmetry stops at the degree monitors, and this is where.

    A degree monitor is judged against a live design that a branch can add to
    while the reading stands still, so a reading below it is explained by the
    clock. `cwdm_channels` is the fixed set of wavelengths a filter passes.
    Nothing in this repository provisions one onto a branch, so there is no clock
    to explain a monitor sitting below it, and a disagreement in either direction
    is a record being wrong.
    """
    check = _run(
        _count_payload(muxes=(_mux("mux-asp-01", 4, _port(MUX_MONITOR, "MON-mux-asp-01", 3)),)),
        CHANNEL_COUNT,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "MON-mux-asp-01" in errors[0]
    assert "fixed property of its filter" in errors[0]
    assert "1 disagree in a way a dated reading cannot explain" in " ".join(_messages(check, "INFO"))


# ---------------------------------------------------------------------------
# Monitor completeness
# ---------------------------------------------------------------------------


def _completeness_payload(
    *,
    amplifiers: tuple[dict[str, Any], ...] = (),
    pumps: tuple[dict[str, Any], ...] = (),
    transponders: tuple[dict[str, Any], ...] = (),
    muxes: tuple[dict[str, Any], ...] = (),
    roadms: tuple[dict[str, Any], ...] = (),
    **extra: Any,
) -> dict[str, Any]:
    """The payload in the shape `monitor_completeness.gql` returns it.

    `extra` exists for the router test, which puts a collection into the payload
    that the real query never selects. That is how the silence about the kinds
    carrying no monitor gets asserted rather than assumed.
    """
    return {
        "OtnAmplifier": _edges(*amplifiers),
        "OtnRamanPump": _edges(*pumps),
        "OtnTransponder": _edges(*transponders),
        "OtnMuxDemux": _edges(*muxes),
        "OtnRoadm": _edges(*roadms),
        **extra,
    }


AMPLIFIER = "amp-fra-mil-04"
PUMP = "raman-fra-mil-01"
TRANSPONDER = "txp-fra-01"
MULTIPLEXER = "mux-ams-02"


def _covered(**swapped: Any) -> dict[str, Any]:
    """One device of each of the five kinds, every one of them carrying its monitor.

    Each test swaps one slot for a version with the monitor removed, so the
    payload it runs against differs from the passing one in exactly the thing
    under test.
    """
    parts: dict[str, Any] = {
        "amplifiers": (
            _device("OtnAmplifier", AMPLIFIER, _port("OtnAmplifierPort"), _port("OtnAmplifierMonitor", "MON-IN")),
        ),
        "pumps": (_device("OtnRamanPump", PUMP, _port("OtnRamanMonitor", "MON-PUMP")),),
        "transponders": (
            _device("OtnTransponder", TRANSPONDER, _port("OtnClientPort"), _port("OtnReceiverMonitor", "MON-RX")),
        ),
        "muxes": (_device("OtnMuxDemux", MULTIPLEXER, _port(MUX_MONITOR, "MON-MUX")),),
        "roadms": (_device("OtnRoadm", FRA, _port(DEGREE_PORT, "DEG-MIL"), _port(DEGREE_MONITOR, "MON-DEG-MIL")),),
    }
    parts.update(swapped)
    return _completeness_payload(**parts)


def test_full_coverage_passes_and_the_summary_states_it_per_kind() -> None:
    """The summary is the requirement, not decoration.

    A green result that says only "passed" is the failure mode the deleted check
    of this name was removed for. Per kind rather than as one total, because the
    real totals differ by two orders of magnitude and 306 covered amplifiers
    would hide nine uncovered Raman pumps inside a single percentage.
    """
    check = _run(_covered(), COMPLETENESS)
    assert _messages(check, "ERROR") == []
    summary = " ".join(_messages(check, "INFO"))
    assert "Monitor coverage complete" in summary
    for expected in ("1/1 amplifiers", "1/1 Raman pumps", "1/1 transponders", "1/1 multiplexers", "1/1 ROADM degrees"):
        assert expected in summary, summary


@pytest.mark.parametrize(
    ("slot", "kind", "name", "monitor_kind"),
    [
        ("amplifiers", "OtnAmplifier", AMPLIFIER, "OtnAmplifierMonitor"),
        ("pumps", "OtnRamanPump", PUMP, "OtnRamanMonitor"),
        ("transponders", "OtnTransponder", TRANSPONDER, "OtnReceiverMonitor"),
        ("muxes", "OtnMuxDemux", MULTIPLEXER, MUX_MONITOR),
    ],
)
def test_each_device_pairing_fails_when_its_monitor_is_absent(
    slot: str, kind: str, name: str, monitor_kind: str
) -> None:
    """Four of the five pairings, one device each, one absence each.

    Parametrized rather than written four times because the rule is one rule over
    a table: `monitors.MONITOR_BY_DEVICE_KIND` is what says these four kinds
    carry a monitor, and a fifth row added there and not here would leave the new
    pairing untested.
    """
    check = _run(_covered(**{slot: (_device(kind, name),)}), COMPLETENESS)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert name in errors[0]
    assert kind in errors[0]
    assert monitor_kind in errors[0]


def test_a_degree_port_with_no_monitor_fails_and_is_named_by_the_port() -> None:
    """The fifth pairing, and it is the one applied per port.

    A ROADM carries one monitor per degree rather than one per device, so the
    subject is the port. The finding names the port, and it is logged against the
    ROADM, which is the object with an `id` in this payload and the page an
    operator would open.
    """
    check = _run(_covered(roadms=(_device("OtnRoadm", FRA, _port(DEGREE_PORT, "DEG-MIL")),)), COMPLETENESS)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "DEG-MIL" in errors[0]
    assert DEGREE_MONITOR in errors[0]
    failures = [log for log in check.logs if log["level"] == "ERROR"]
    assert [(log["object_id"], log["object_type"]) for log in failures] == [(FRA, "OtnRoadm")]
    assert "0/1 ROADM degrees" in " ".join(_messages(check, "INFO"))


SHARED_DEGREE = "DEG-FRA"
AMS = "roadm-ams-01"
BER = "roadm-ber-01"
"""Two of the six ROADMs that carry a degree called `DEG-FRA` in the shipped
dataset. `(device, name)` is the uniqueness constraint on `OtnGenericPort`, so a
port name is unique on its device and nowhere else, and thirteen degree names
repeat across the fifteen sites."""


def test_a_degree_name_shared_by_two_roadms_is_reported_against_the_one_that_is_missing_it() -> None:
    """The port name alone is not an identity, and the finding must not use it as one.

    Six ROADMs ship a degree called `DEG-FRA`. Keyed by the port name, the owner
    of the last one iterated wins, and the finding sends an operator to a device
    that is fine while the one with the gap is never named. This never fires on
    the shipped dataset, where coverage is complete: it fires exactly when the
    check does its job.
    """
    covered_roadm = _device("OtnRoadm", BER, _port(DEGREE_PORT, SHARED_DEGREE), _port(DEGREE_MONITOR, "MON-DEG-FRA"))
    bare = _device("OtnRoadm", AMS, _port(DEGREE_PORT, SHARED_DEGREE))
    check = _run(_covered(roadms=(bare, covered_roadm)), COMPLETENESS)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert SHARED_DEGREE in errors[0]
    assert AMS in errors[0], errors[0]
    assert BER not in errors[0], errors[0]
    failures = [log for log in check.logs if log["level"] == "ERROR"]
    assert [(log["object_id"], log["object_type"]) for log in failures] == [(AMS, "OtnRoadm")]
    assert "1/2 ROADM degrees" in " ".join(_messages(check, "INFO"))


def test_two_roadms_missing_the_same_degree_monitor_get_one_finding_each() -> None:
    """Two gaps are two findings, and each one names its own device.

    Keyed by the port name, both findings carry the same text and the same
    `object_id`, so an operator reading them sees one device twice and cannot
    tell that a second is uncovered.
    """
    check = _run(
        _covered(
            roadms=(
                _device("OtnRoadm", AMS, _port(DEGREE_PORT, SHARED_DEGREE)),
                _device("OtnRoadm", BER, _port(DEGREE_PORT, SHARED_DEGREE)),
            )
        ),
        COMPLETENESS,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 2, errors
    assert len(set(errors)) == 2, errors
    assert any(AMS in error for error in errors), errors
    assert any(BER in error for error in errors), errors
    failures = [log for log in check.logs if log["level"] == "ERROR"]
    assert sorted(str(log["object_id"]) for log in failures) == [AMS, BER]
    assert "0/2 ROADM degrees" in " ".join(_messages(check, "INFO"))


def test_a_degree_monitor_with_no_degree_port_is_an_error_here() -> None:
    """Presence, the other direction, and this check owns both.

    An orphan monitor is as much a defect as an absent one. Its reading is
    mandatory, so it is always a number, and a number about a port nobody can
    find is quoted by every report downstream as though it were about something.
    `channel_count_consistency` meets the same monitor and defers with an INFO.
    """
    check = _run(_covered(roadms=(_device("OtnRoadm", FRA, _port(DEGREE_MONITOR, "MON-DEG-MIL")),)), COMPLETENESS)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "MON-DEG-MIL" in errors[0]
    assert "DEG-MIL" in errors[0]
    assert "1 monitor(s) watch a port that does not exist" in " ".join(_messages(check, "INFO"))


def test_a_router_is_not_judged_and_the_summary_names_the_kinds_that_are_not() -> None:
    """The silence, stated rather than left to be inferred.

    A router, a patch panel and an ODU switch carry no monitor and are not
    expected to. The payload here holds a router collection the real query never
    selects, so a check widened to sweep every device would fail it and this
    fails instead. The summary names the three, because a reader who cannot see
    the boundary of what was judged cannot tell a pass from an oversight.
    """
    router = _device("OtnRouter", "rtr-fra-01", _port("OtnRouterPort"))
    check = _run(_covered(OtnRouter=_edges(router)), COMPLETENESS)
    assert _messages(check, "ERROR") == []
    summary = " ".join(_messages(check, "INFO"))
    assert "rtr-fra-01" not in summary
    assert "Routers, patch panels and ODU switches carry no monitor and are not judged here" in summary
