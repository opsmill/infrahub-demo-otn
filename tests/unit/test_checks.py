"""The OSNR, capacity, diversity, collision and monitor checks, executed offline."""

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
    """One GraphQL node holding the named attributes and nothing else."""
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
    """Import a registered file by path, the way the server loads it."""
    path = REPO_ROOT / file_path
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(file_path: str, class_name: str) -> Any:
    return getattr(_module(file_path), class_name)


def _run(payload: dict[str, Any], check_name: str = "osnr_margin") -> Any:
    """Instantiate and run the check with no client and no server."""
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
    """The other half of the same failure."""
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
    """The forty-two does not move, and nothing coarse is behind it."""
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
    """One container with its children, selected exactly as the query selects."""
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
    """Each named parent against the verdict the check gave it."""
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
    """The documentation's own example, now a failing check rather than a caveat."""
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
    """The distinction the whole capacity module exists to hold."""
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
    """The other half, and the reason it is about the count and not the type."""
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
    """Drift between the schema enum and the slot table, reported per container."""
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
    """What the live run on `main` reports, asserted offline."""
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
"""One branch holding every verdict at once: overfilled, exactly full, roomy, a."""


def _agreement_payload() -> dict[str, Any]:
    return _capacity_payload(
        *(
            _parent(name, odu_type, capacity, *(_child(*child) for child in children))
            for name, odu_type, capacity, children in AGREEMENT_TREE
        )
    )


def _generator_module() -> Any:
    """The provisioning generator, loaded by path from its own registration."""
    entry = next(item for item in CONFIG.generator_definitions if item.name == "optical_service")
    return _module(str(entry.file_path))


def test_the_check_and_the_generator_reach_the_same_verdict_on_every_container() -> None:
    """SC-006, and the one criterion no other test in this repository covers."""
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
    """The same agreement, stated as the consequence that matters."""
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
    """One hop over a fiber span, selected exactly as `diversity.gql` selects it."""
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
    """One wavelength of a circuit, one hop per duct named."""
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
    """The payload in the shape `diversity.gql` returns it: groups, nothing else."""
    return {"OtnDiversityGroup": _edges(*groups), **extra}


def test_two_services_in_one_group_sharing_a_duct_fail_naming_all_three() -> None:
    """The failing case, and the one the live run reproduces."""
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
    """The pair is not an object, so the group is what the failure attaches to."""
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
    """The silence, asserted rather than assumed. This is the test that must not."""
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
    """The other half of the silence, and the half a payload cannot show."""
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
    """Absent is not the same as diverse."""
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
    """FR-020. A regenerated circuit crosses its own ducts by construction."""
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
    """FR-019, from the direction that catches the narrow answer."""
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
    """Two routes outside every recorded conduit share unrecorded risk, not a duct."""
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
    """FR-021, asserted rather than trusted."""
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
"""The symbol rate whose occupied width is exactly one 50 GHz channel."""


def _carrier(name: str, channel: int, baud: int, *sections: str) -> dict[str, Any]:
    """One carrier in the shape `channel_collision.gql` returns it."""
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
    """The failure the channel-number rule passed, and the reason for the feature."""
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
    """Spectrum is scarce within a section, not across the network."""
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
    """A shared edge is not shared spectrum."""
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
    """The fail-closed case the check has always had, and still has."""
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
    """One service in the shape `provisionable.gql` returns it."""
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
    """Row one of the state table. Not rejected, no code, not accepted."""
    check = _run(_provisionable_payload(_service("svc-ber-ams-400g", segments=1)), PROVISIONABLE)
    assert _messages(check, "ERROR") == []
    assert not any("svc-ber-ams-400g" in line for line in _messages(check, "INFO"))


def test_a_refused_and_unaccepted_service_fails_naming_the_service_the_code_and_the_detail() -> None:
    """Row two, and the feature. SC-002 is the three parts in one message."""
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
    """Row three, and the test that must not be deleted by a change widening this."""
    check = _run(_provisionable_payload(_refused_service(accepted=True)), PROVISIONABLE)
    assert _messages(check, "ERROR") == []
    assert not any(REFUSED_SERVICE in line for line in _messages(check, "INFO"))


def test_a_service_marked_accepted_that_carries_no_refusal_is_an_error() -> None:
    """Row five. The flag is set on the wrong node, and that blocks."""
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
    """Row four, fail closed."""
    check = _run(_provisionable_payload(_service("svc-hand-edited", status="rejected")), PROVISIONABLE)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "svc-hand-edited" in errors[0]
    assert "no reason code" in errors[0]
    assert "not a skip" in errors[0]


def test_an_unreadable_refusal_is_not_cleared_by_the_acceptance_flag() -> None:
    """The `any` in row four, which is the half an ordering mistake would lose."""
    check = _run(
        _provisionable_payload(_service("svc-hand-edited", status="rejected", accepted=True)),
        PROVISIONABLE,
    )
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "no reason code" in errors[0]
    assert "does not clear it" in errors[0]


def test_a_branch_with_no_services_says_it_looked_rather_than_passing_silently() -> None:
    """Row six. FR-013, and the reason it is a requirement at all."""
    check = _run(_provisionable_payload(), PROVISIONABLE)
    assert _messages(check, "ERROR") == []
    lines = _messages(check, "INFO")
    assert len(lines) == 1, lines
    assert "No service on this branch" in lines[0]
    assert "not a network that was proved provisionable" in lines[0]


def test_a_payload_with_no_service_collection_is_an_error_and_not_an_empty_branch() -> None:
    """A query that did not run must not read as a branch with nothing on it."""
    unreadable: tuple[dict[str, Any], ...] = ({}, {"OtnService": None}, {"OtnService": {}})
    for payload in unreadable:
        check = _run(payload, PROVISIONABLE)
        errors = _messages(check, "ERROR")
        assert len(errors) == 1, (payload, errors)
        assert "carries no OtnService collection" in errors[0]
        assert "not a branch with nothing on it" in errors[0]
        assert _messages(check, "INFO") == [], "an unread branch must not also report that it looked"


def test_an_empty_service_collection_is_still_the_branch_that_looked_and_found_none() -> None:
    """The other side of the guard above, which is what stops it over-firing."""
    check = _run({"OtnService": {"edges": []}}, PROVISIONABLE)
    assert _messages(check, "ERROR") == []
    assert "No service on this branch" in _messages(check, "INFO")[0]


def test_a_service_whose_status_did_not_come_back_fails_rather_than_reading_as_provisionable() -> None:
    """`status` is the field the gate turns on, so it is read strictly."""
    nulled = _refused_service()
    nulled["status"] = _attribute(None)
    with pytest.raises(ValueError, match="null status"):
        _run(_provisionable_payload(nulled), PROVISIONABLE)

    absent = _refused_service()
    del absent["status"]
    with pytest.raises(KeyError):
        _run(_provisionable_payload(absent), PROVISIONABLE)


def test_a_stale_reason_code_on_a_provisioned_service_is_reported_and_does_not_block() -> None:
    """The one state the table does not have a row for, said out loud anyway."""
    check = _run(
        _provisionable_payload(_service("svc-fra-mil-ai-400g", code=BUDGET, segments=1)),
        PROVISIONABLE,
    )
    assert _messages(check, "ERROR") == []
    stale = [line for line in _messages(check, "INFO") if "svc-fra-mil-ai-400g" in line]
    assert len(stale) == 1, _messages(check, "INFO")
    assert "stale" in stale[0]


def test_a_leftover_detail_with_no_reason_code_is_reported_on_the_same_terms() -> None:
    """The other half of FR-006's pair, which nothing was watching."""
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
    """Separate numbers for the reason `diversity.py` keeps its numbers separate."""
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
    """`REJECTED` is a literal in the check, because no shared constant holds it."""
    module = _module(_entry(PROVISIONABLE)[0])
    document = yaml.safe_load((REPO_ROOT / "schemas" / "otn_service.yml").read_text())
    node = next(entry for entry in document["nodes"] if entry["name"] == "Service")
    status = next(item for item in node["attributes"] if item["name"] == "status")
    assert module.REJECTED in {str(choice["name"]) for choice in status["choices"]}


def test_the_query_fetches_every_service_so_the_empty_field_states_are_visible() -> None:
    """The shape assertion, and it is the opposite of the diversity one."""
    document = (REPO_ROOT / "queries" / "provisionable.gql").read_text()
    assert "\n  OtnService {" in document, "provisionable.gql stopped fetching every service"
    for field in ("status", "rejection_code", "rejection_detail", "refusal_accepted"):
        assert f"{field} {{" in document
    assert "OtnService(" not in document, "a filter here hides the states the check exists to catch"


def test_the_check_is_global_so_a_change_touching_one_service_still_judges_the_others() -> None:
    """FR-014. A targeted check would bind to the objects the change touched."""
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
    """One port node in the shape an inline fragment returns it."""
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
    """A multiplexer lighting `lit` coarse wavelengths."""
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
    """The control, and the summary is half of what it asserts."""
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
    """The pre-repair observation, kept because the dataset no longer holds it."""
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
    """The provisioning branch, and it is why this check is asymmetric."""
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
    """One branch holding both faults at once, which is the only way to tell the."""
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
    """The monitor, not the section."""
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
    """The zero boundary, and the reason `channels_by_section` takes a section list."""
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
    """Channels, not carrier records. This is the test that must survive a widening."""
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
    """The one condition this check owns alone."""
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
    """`LocationGeneric.shortname` carries no case constraint, so the join folds."""
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
    """One condition, one owner. `monitor_completeness` reports this one."""
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
    """The silence about the fourteen, said out loud once."""
    check = _run(
        _count_payload(muxes=(_mux("mux-fra-01", 0, _port(MUX_MONITOR, "MON-mux-fra-01", 8)),)),
        CHANNEL_COUNT,
    )
    assert _messages(check, "ERROR") == []
    spoken = " ".join(_messages(check, "INFO"))
    assert "1 dense multiplexer monitor(s) were not compared" in spoken
    assert "Compared 0 channel monitor(s)" in spoken


def test_a_coarse_multiplexer_monitor_is_compared_against_the_wavelengths_it_lights() -> None:
    """The two of the sixteen that are comparable, in both directions."""
    agreeing = _mux("mux-ams-02", 4, _port(MUX_MONITOR, "MON-mux-ams-02", 4))
    disagreeing = _mux("mux-asp-01", 4, _port(MUX_MONITOR, "MON-mux-asp-01", 5))
    check = _run(_count_payload(muxes=(agreeing, disagreeing)), CHANNEL_COUNT)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "MON-mux-asp-01" in errors[0]
    assert "difference of 1" in errors[0]


def test_a_coarse_multiplexer_monitor_below_its_lit_channels_is_an_error_and_not_a_lag() -> None:
    """The asymmetry stops at the degree monitors, and this is where."""
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
    """The payload in the shape `monitor_completeness.gql` returns it."""
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
    """One device of each of the five kinds, every one of them carrying its monitor."""
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
    """The summary is the requirement, not decoration."""
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
    """Four of the five pairings, one device each, one absence each."""
    check = _run(_covered(**{slot: (_device(kind, name),)}), COMPLETENESS)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert name in errors[0]
    assert kind in errors[0]
    assert monitor_kind in errors[0]


def test_a_degree_port_with_no_monitor_fails_and_is_named_by_the_port() -> None:
    """The fifth pairing, and it is the one applied per port."""
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
    """The port name alone is not an identity, and the finding must not use it as one."""
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
    """Two gaps are two findings, and each one names its own device."""
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
    """Presence, the other direction, and this check owns both."""
    check = _run(_covered(roadms=(_device("OtnRoadm", FRA, _port(DEGREE_MONITOR, "MON-DEG-MIL")),)), COMPLETENESS)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "MON-DEG-MIL" in errors[0]
    assert "DEG-MIL" in errors[0]
    assert "1 monitor(s) watch a port that does not exist" in " ".join(_messages(check, "INFO"))


def test_a_router_is_not_judged_and_the_summary_names_the_kinds_that_are_not() -> None:
    """The silence, stated rather than left to be inferred."""
    router = _device("OtnRouter", "rtr-fra-01", _port("OtnRouterPort"))
    check = _run(_covered(OtnRouter=_edges(router)), COMPLETENESS)
    assert _messages(check, "ERROR") == []
    summary = " ".join(_messages(check, "INFO"))
    assert "rtr-fra-01" not in summary
    assert "Routers, patch panels and ODU switches carry no monitor and are not judged here" in summary
    assert "rtr-fra-01" not in summary
    assert "Routers, patch panels and ODU switches carry no monitor and are not judged here" in summary


# ---------------------------------------------------------------------------
# Carrier termination
# ---------------------------------------------------------------------------

TERMINATION = "carrier_termination"

AMS_MIL = "oc-ch002-ams-mil"
AMSTERDAM = "Amsterdam"
FRANKFURT = "Frankfurt"
MILAN = "Milan"
"""One wavelength on the shipped route Amsterdam to Milan, which crosses
Frankfurt. Two sections and three sites is the smallest shape that makes the
endpoint derivation say something a single section would not: Frankfurt is
touched twice and is not an end, so a check that named every site on the route
would name the wrong one."""


def _line_port(name: str, device: str, site: str | None) -> dict[str, Any]:
    """One line port in the shape `carrier_termination.gql` returns it."""
    node: dict[str, Any] = {"name": _attribute(device), "site": _one({"name": _attribute(site)})}
    if site is None:
        node.pop("site")
    return {"name": _attribute(name), "device": _one(node)}


def _section(name: str, site_a: str, site_b: str) -> dict[str, Any]:
    """One section between two ROADMs, each named by its site and nothing else.

    The check reads no ROADM name, so the payload holds none. Building it richer
    than the query would let the check read a field no server would send it.
    """
    return {
        "name": _attribute(name),
        "roadm_a": _one({"site": _one({"name": _attribute(site_a)})}),
        "roadm_b": _one({"site": _one({"name": _attribute(site_b)})}),
    }


AMS_FRA = _section("oms-ams-fra", AMSTERDAM, FRANKFURT)
FRA_MIL = _section("oms-fra-mil", FRANKFURT, MILAN)


def _wavelength(
    name: str,
    channel: int,
    *,
    status: str = "active",
    ports: tuple[dict[str, Any], ...] = (),
    sections: tuple[dict[str, Any], ...] = (AMS_FRA, FRA_MIL),
) -> dict[str, Any]:
    return {
        "id": name,
        "__typename": "OtnOpticalCarrier",
        "name": _attribute(name),
        "status": _attribute(status),
        "channel": _one({"channel_number": _attribute(channel)}),
        "line_ports": _edges(*ports),
        "sections": _edges(*sections),
    }


def _termination_payload(*carriers: dict[str, Any]) -> dict[str, Any]:
    return {"OtnOpticalCarrier": _edges(*carriers)}


BOTH_ENDS = (
    _line_port("L1", "xpdr-ams-01", AMSTERDAM),
    _line_port("L1", "xpdr-mil-01", MILAN),
)
"""What every shipped wavelength holds: one line port at each end of its route.
Forty carriers and eighty bound ports on `main`, so the failing payloads below
exist only here."""


def test_a_wavelength_terminated_at_both_ends_passes_and_the_summary_counts_it() -> None:
    """The count is the requirement, not decoration."""
    check = _run(_termination_payload(_wavelength(AMS_MIL, 2, ports=BOTH_ENDS)), TERMINATION)
    assert _messages(check, "ERROR") == []
    summary = " ".join(_messages(check, "INFO"))
    assert "1 active carrier(s) examined, every one terminated at both ends" in summary, summary


def test_a_wavelength_left_with_one_end_fails_and_names_the_site_that_stopped_terminating_it() -> None:
    """The state a deleted transponder reaches, and the one this check exists for."""
    carrier = _wavelength(AMS_MIL, 2, ports=(_line_port("L1", "xpdr-mil-01", MILAN),))
    check = _run(_termination_payload(carrier), TERMINATION)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert AMS_MIL in errors[0]
    assert "channel 2" in errors[0]
    assert f"Nothing at {AMSTERDAM} terminates it" in errors[0], errors[0]
    assert f"only {MILAN} terminates it" in errors[0], errors[0]
    assert FRANKFURT not in errors[0], errors[0]
    failures = [log for log in check.logs if log["level"] == "ERROR"]
    assert [(log["object_id"], log["object_type"]) for log in failures] == [(AMS_MIL, "OtnOpticalCarrier")]
    assert "1 of them terminated at fewer than 2 ends" in " ".join(_messages(check, "INFO"))


def test_two_line_ports_at_one_site_is_one_end_terminated_and_not_two() -> None:
    """The state an operator reaches by re-binding the wavelength to the wrong spare."""
    both_at_milan = (_line_port("L1", "xpdr-mil-01", MILAN), _line_port("L2", "xpdr-mil-01", MILAN))
    check = _run(_termination_payload(_wavelength(AMS_MIL, 2, ports=both_at_milan)), TERMINATION)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "only 2 line ports at Milan terminate it" in errors[0], errors[0]
    assert f"Nothing at {AMSTERDAM} terminates it" in errors[0], errors[0]
    assert "1 of them terminated at fewer than 2 ends" in " ".join(_messages(check, "INFO"))


def test_a_third_line_port_bound_to_a_two_ended_wavelength_fails() -> None:
    """The opposite fault, and the schema cannot refuse it."""
    three = (*BOTH_ENDS, _line_port("L2", "xpdr-mil-01", MILAN))
    check = _run(_termination_payload(_wavelength(AMS_MIL, 2, ports=three)), TERMINATION)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert AMS_MIL in errors[0]
    assert f"3 line ports bind it, at {AMSTERDAM} and {MILAN}, where a wavelength has 2 ends" in errors[0], errors[0]
    assert "terminates it" not in errors[0], errors[0]
    summary = " ".join(_messages(check, "INFO"))
    assert "1 bound to more line ports than a wavelength has ends" in summary, summary


def test_an_active_wavelength_with_no_line_port_at_all_fails() -> None:
    """The degenerate case of the same rule, not a second rule.

    Reachable by deleting both transponders, and it is the case FR-027 words the
    requirement around. Both ends are named, because both are gone.
    """
    check = _run(_termination_payload(_wavelength(AMS_MIL, 2)), TERMINATION)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "no line port terminates it" in errors[0], errors[0]
    assert f"Nothing at {AMSTERDAM} and {MILAN} terminates it" in errors[0], errors[0]


def test_a_planned_carrier_binding_no_line_port_is_not_a_failure_and_the_summary_says_so() -> None:
    """The scope, asserted from the side that would make this check unusable."""
    planned = _wavelength("oc-ch014-ber-ams", 14, status="planned")
    check = _run(_termination_payload(_wavelength(AMS_MIL, 2, ports=BOTH_ENDS), planned), TERMINATION)
    assert _messages(check, "ERROR") == []
    summary = " ".join(_messages(check, "INFO"))
    assert "1 active carrier(s) examined" in summary, summary
    assert "1 planned carrier(s) are outside the scope" in summary, summary


def test_a_decommissioned_carrier_is_skipped_and_counted_separately() -> None:
    """The other skipped status, and the summary keeps the two apart."""
    retired = _wavelength("oc-ch017-ams-mil", 17, status="decommissioned")
    check = _run(_termination_payload(retired), TERMINATION)
    assert _messages(check, "ERROR") == []
    summary = " ".join(_messages(check, "INFO"))
    assert "No active carrier is on this branch, so none can be unterminated" in summary, summary
    assert "1 decommissioned carrier(s) are outside the scope" in summary, summary


def test_a_route_that_is_not_a_two_ended_path_is_not_guessed_at() -> None:
    """A finding says what it knows. Where the route says nothing, neither does it."""
    carrier = _wavelength(AMS_MIL, 2, ports=(_line_port("L1", "xpdr-mil-01", MILAN),), sections=())
    check = _run(_termination_payload(carrier), TERMINATION)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "its route does not say where that end is" in errors[0], errors[0]
    assert AMSTERDAM not in errors[0], errors[0]


def test_a_line_port_on_a_device_with_no_site_is_named_by_the_device() -> None:
    """`OtnGenericDevice.site` is optional, and the schema says why."""
    carrier = _wavelength(AMS_MIL, 2, ports=(_line_port("L1", "xpdr-mil-01", None),))
    check = _run(_termination_payload(carrier), TERMINATION)
    errors = _messages(check, "ERROR")
    assert len(errors) == 1, errors
    assert "only xpdr-mil-01 terminates it" in errors[0], errors[0]
