"""Every scenario under `demo/`, against every check the pipeline registers."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from functools import cache
from typing import Any

import pytest
from infrahub_sdk.ctl.repository import get_repository_config

from tests.unit.conftest import REPO_ROOT, object_documents
from tests.unit.scenariopayloads import merged, payload, scenario_files, schema

CONFIG = get_repository_config(REPO_ROOT / ".infrahub.yml")

GENERATOR_RELATIONSHIPS = ("optical_path", "child_containers")
"""What only a generator writes.

A scenario file declares services, wavelengths, containers and hardware. It never
declares an optical path: that is 25 ordered hops the generator derives from a
route it chose, and no human writes one into YAML.

`child_containers` is the other one, and it is the softer of the two. A scenario
file can declare a container under a parent, and `demo/05_odu_mixed_fill.yml` and
`demo/90_fra_mil_saturated.yml` both do, so `container_capacity` is judged on
those. `demo/04_odu_ten_in_one.yml` is the case this covers: the ODU4 it declares
is filled by ten client containers the generator grooms into it, so the file that
exists to exercise that check declares nothing for it to read.

A check that reads one of these reads a field the merged view leaves empty and
the pipeline finds full, and that is the whole of the difference this module
cannot see.
"""


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Passes:
    """The check ran and logged no error."""


@dataclass(frozen=True)
class Fails:
    """The check ran and logged `count` errors, and the reason says why that is right."""

    count: int
    reason: str


@dataclass(frozen=True)
class NeedsGenerator:
    """The verdict here belongs to the branch after the generator, not before it."""

    count: int
    reason: str


Outcome = Passes | Fails | NeedsGenerator

PASSES = Passes()

NO_PATH_YET = NeedsGenerator(
    0,
    "`checks/diversity.py` compares the ducts under two services' optical paths, and a scenario file "
    "declares no optical path: that is 25 ordered hops derived from a route, and no human writes one into "
    "YAML. So this check reads an empty field in every view here and is silent whatever the scenario says. "
    "It is declared rather than recorded as a pass, because a pass would claim the sweep had judged "
    "something. `demo/09_diversity_fra_feeds.yml` shares `cd-fra-north` between two services and fails this "
    "check on a live branch; `tests/unit/test_demo_scenarios.py` is what holds both diversity scenarios to "
    "the ducts their headers promise",
)
"""The one check this module cannot judge at all, on any file.

Named once and used twelve times so the hole is a shape a reader can see, rather
than twelve cells that each look like a verdict.
"""

PAR_MAD = Fails(
    2,
    "Paris to Madrid is short of margin in both directions on the reference mode. It is the shipped "
    "finding the whole demo is built around, red on every branch that has not put the Raman pumps on",
)


EXPECTED: dict[tuple[str, str], Outcome] = {}


def _row(file_name: str, **cells: Outcome) -> None:
    for check, outcome in cells.items():
        EXPECTED[(file_name, check)] = outcome


def _quiet(file_name: str, **overrides: Outcome) -> None:
    """One scenario that changes nothing a check speaks about, other than the named cells."""
    cells: dict[str, Outcome] = dict(_DEFAULT)
    cells.update(overrides)
    _row(file_name, **cells)


_DEFAULT: dict[str, Outcome] = {
    "units_import": PASSES,
    "osnr_margin": PAR_MAD,
    "channel_collision": PASSES,
    "container_capacity": PASSES,
    "diversity": NO_PATH_YET,
    "provisionable": PASSES,
    "channel_count_consistency": PASSES,
    "monitor_completeness": PASSES,
    "carrier_termination": PASSES,
}
"""What a scenario that adds services and containers and nothing else looks like.

Only `osnr_margin` is red, and it is red on the default branch too.
"""

_quiet("00_services.yml")
_quiet("01_impact_services.yml")

_quiet(
    "02_par_mad_raman.yml",
    osnr_margin=PASSES,
)
"""The pumps go on and the shipped deficit closes. This is the one file in the
directory that turns a red check green, and it is the before-and-after the Raman
page is written around."""

_quiet("03_infiniband_service.yml")

_quiet(
    "04_odu_ten_in_one.yml",
    container_capacity=NeedsGenerator(
        0,
        "the ODU4 this file declares is filled by ten client containers the generator grooms into it, so "
        "before the generator runs the sweep sees a parent with no children and the check reports that no "
        "container on the branch holds one. That is a true reading of the loaded state and it judges nothing: "
        "this is the scenario built to exercise container_capacity, and offline it exercises none of it. "
        "`tests/unit/test_checks.py` is where the packing arithmetic is held",
    ),
    provisionable=NeedsGenerator(
        1,
        "`svc-lon-mil-sdh-11` carries `refusal_accepted` and, before the generator runs, carries no refusal "
        "to accept. The check reports an acceptance with nothing under it, which is correct about the loaded "
        "state and not about the branch: the generator runs first in the pipeline and refuses the service on "
        "`no-slots`, and the flag then has its refusal",
    ),
)
"""Ten SDH circuits in one ODU4 and an eleventh with nowhere to go. Three
wavelengths lit by hand on a corridor with one free line port at each end, which
is why this file racks a shelf at Frankfurt and one at Milan."""

_quiet("05_odu_mixed_fill.yml")

_quiet(
    "06_mad_waw_16qam.yml",
    osnr_margin=Fails(
        6,
        "Paris to Madrid in both directions, plus the four segment-level deficits the scenario exists to "
        "show: neither half of any of the three splits closes at DP-16QAM",
    ),
    channel_collision=Fails(
        8,
        "The three splits deliberately overlap. Channels 70, 71 and 72 sit 50,000 MHz apart and a "
        "DP-16QAM 64GBd carrier occupies 79,600, so each adjacent pair shares 29,600 MHz on every section "
        "both cross. `specs/022-provisionable-gate/refusal-baseline.md` measured the same failure",
    ),
)
"""The scenario 025 broke on. Six wavelengths, three regenerators, and
`carrier_termination` green: every segment now has a transponder at its outer end
and a regenerator line port at its inner one. Before this feature all six were
lit with nothing terminating either end."""

_quiet("07_mad_waw_qpsk.yml")
"""The same route at DP-QPSK on one regenerator, and it closes. Two wavelengths,
two regenerator line ports, and the two Madrid and Warsaw slots `06` left free."""

_quiet("08_diversity_mil_feeds.yml")
_quiet("09_diversity_fra_feeds.yml")
"""The two files the `diversity` cell exists for, and the two the sweep can say
least about. `NO_PATH_YET` above carries the reason."""

_quiet(
    "10_amplifier_without_monitor.yml",
    monitor_completeness=Fails(
        1,
        "`amp-ham-ber-11` arrives without its monitor, which is the entire content of the file. A green "
        "check here would mean the scenario had been repaired into nothing",
    ),
)

_quiet(
    "90_fra_mil_saturated.yml",
    provisionable=NeedsGenerator(
        1,
        "`svc-fra-mil-ai-400g` carries `refusal_accepted` before the generator has refused it, the same "
        "shape as `04`. On the branch the generator runs first, refuses it on `no-slots`, and the check "
        "reads a signed-for refusal and says nothing. That is what makes the walkthrough end green",
    ),
)


# ---------------------------------------------------------------------------
# Running a check
# ---------------------------------------------------------------------------


def _entry(name: str) -> Any:
    return next(item for item in CONFIG.check_definitions if item.name == name)


@cache
def _check_class(name: str) -> Any:
    """The class `.infrahub.yml` names, loaded from the file it names."""
    entry = _entry(name)
    path = REPO_ROOT / str(entry.file_path)
    spec = importlib.util.spec_from_file_location(f"scenario_check_{name}", path)
    assert spec is not None and spec.loader is not None, f"{entry.file_path} is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, entry.class_name)


@cache
def _errors(check_name: str, file_name: str | None) -> tuple[str, ...]:
    check = _check_class(check_name)(branch="scenario-sweep")
    check.validate(payload(check_name, file_name))
    return tuple(str(log["message"]) for log in check.logs if log["level"] == "ERROR")


CHECKS = tuple(entry.name for entry in CONFIG.check_definitions)
SCENARIOS = scenario_files()
CELLS = [(scenario, check) for scenario in SCENARIOS for check in CHECKS]


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------


def test_the_expectation_table_is_exactly_the_product_of_the_two_directories() -> None:
    """Every scenario against every check, no cell missing and none invented."""
    assert set(EXPECTED) == set(CELLS), (
        f"undeclared cells: {sorted(set(CELLS) - set(EXPECTED))}; "
        f"declared cells that do not exist: {sorted(set(EXPECTED) - set(CELLS))}"
    )


def test_the_sweep_covers_twelve_scenarios_and_nine_checks() -> None:
    """The two numbers this module's docstring publishes, read back from the tree."""
    assert len(SCENARIOS) == 12, f"demo/ holds {len(SCENARIOS)} scenarios: {SCENARIOS}"
    assert len(CHECKS) == 9, f".infrahub.yml registers {len(CHECKS)} checks: {CHECKS}"
    assert len(CELLS) == 108


@pytest.mark.parametrize("check_name", CHECKS)
def test_every_registered_check_can_be_swept(check_name: str) -> None:
    """A payload can be built for the check and the check runs against it."""
    view = merged(None)
    built = payload(check_name, None)
    assert built, f"{check_name} resolved to no collections at all"

    # Every record the view holds for a rooted kind has to come back. A count is
    # the assertion rather than "not empty", because several of these
    # collections are legitimately empty on the default branch and their
    # emptiness is a fact rather than a failure: no shipped object declares a
    # diversity group and no shipped object is a service. What would be a
    # failure is the resolver returning fewer than the view holds, which is how
    # a query it cannot read becomes a check swept over nothing and passing.
    for kind, collection in built.items():
        expected = len(view.get(kind, {}))
        assert len(collection["edges"]) == expected, (
            f"{check_name} resolved {len(collection['edges'])} {kind} where the branch holds {expected}"
        )
    _errors(check_name, "00_services.yml")


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("scenario", "check_name"), CELLS, ids=[f"{s}-{c}" for s, c in CELLS])
def test_each_scenario_against_each_check(scenario: str, check_name: str) -> None:
    """One cell. The declared outcome, against what the check actually logs."""
    expected = EXPECTED[(scenario, check_name)]
    errors = _errors(check_name, scenario)

    if isinstance(expected, Passes):
        assert not errors, f"{check_name} was expected to pass {scenario} and logged: {errors[0]}"
        return

    # A count of zero is a real declaration and not a pass in disguise. It says
    # the check is silent here and that its silence is not evidence, which is the
    # whole of what `NeedsGenerator` records about `diversity`. Asserting on the
    # count either way means a cell that starts reporting fails, which is what a
    # reader of that declaration would want.
    assert len(errors) == expected.count, (
        f"{check_name} reported {len(errors)} error(s) on {scenario}, not the {expected.count} declared. "
        f"The reason on record: {expected.reason}. "
        f"{'First: ' + errors[0] if errors else 'It reported nothing.'}"
    )


def test_a_needs_generator_cell_names_a_check_that_reads_a_generator_relationship() -> None:
    """`NeedsGenerator` is not an escape hatch, and this is what keeps it from becoming one."""
    for (scenario, check_name), outcome in sorted(EXPECTED.items()):
        if not isinstance(outcome, NeedsGenerator):
            continue
        query = (REPO_ROOT / "queries" / f"{check_name}.gql").read_text()
        reads = [name for name in GENERATOR_RELATIONSHIPS if name in query]
        assert reads, (
            f"{check_name} on {scenario} is declared NeedsGenerator, but {check_name}.gql selects none of "
            f"{GENERATOR_RELATIONSHIPS}, so a generator cannot be what the difference is"
        )
        assert len(_errors(check_name, scenario)) == outcome.count


# ---------------------------------------------------------------------------
# The resolver, held to a payload a human wrote
# ---------------------------------------------------------------------------


def test_the_resolver_agrees_with_the_shipped_dataset_on_every_check() -> None:
    """The default branch, run through all nine, against what the tree already asserts."""
    verdicts = {name: len(_errors(name, None)) for name in CHECKS}
    with_services = {name: len(_errors(name, "00_services.yml")) for name in CHECKS}
    assert verdicts == with_services, (
        "loading two service requests onto a branch changed a check's verdict. Neither service names a route "
        "or a wavelength, so nothing a check reads has moved, and a difference here means the overlay is "
        "writing more than the file declares"
    )

    assert verdicts["osnr_margin"] == 2, "the sweep should find the Paris to Madrid deficit in both directions"
    for quiet in ("container_capacity", "monitor_completeness", "channel_collision", "carrier_termination"):
        assert verdicts[quiet] == 0, f"{quiet} fails the shipped plant, which nothing else in the suite says"

    par_mad = _errors("osnr_margin", None)
    assert all("oms-par-mad" in message for message in par_mad), par_mad


def test_every_shipped_carrier_is_terminated_at_both_ends_through_the_resolver() -> None:
    """Forty wavelengths, two line ports each, read the way the check reads them."""
    carriers = payload("carrier_termination", None)["OtnOpticalCarrier"]["edges"]
    assert len(carriers) == 40
    counts = {str(edge["node"]["name"]["value"]): len(edge["node"]["line_ports"]["edges"]) for edge in carriers}
    wrong = {name: count for name, count in counts.items() if count != 2}
    assert not wrong, f"shipped wavelengths not terminated at exactly two ends: {wrong}"


def test_a_regenerator_terminates_the_two_segments_it_joins_on_the_scenario_branch() -> None:
    """`demo/06_mad_waw_16qam.yml` read through the check's own query."""
    view = merged("06_mad_waw_16qam.yml")
    regenerators = {
        key[0] for key, record in view["OtnOduSwitch"].items() if str(record.get("switching_mode")) == "regenerator"
    }
    assert regenerators == {"oeo-fra-01", "oeo-fra-02", "oeo-par-01", "oeo-prg-01"}

    # Named rather than matched on a prefix. The shipped plan already holds
    # `oc-ch071-fra-mil` and four more in the same numbering, and a prefix match
    # swept them in and turned a six-segment assertion into eleven.
    scenario_carriers = {
        "oc-ch070-mad-par",
        "oc-ch070-par-waw",
        "oc-ch071-mad-fra",
        "oc-ch071-fra-waw",
        "oc-ch072-mad-prg",
        "oc-ch072-prg-waw",
    }
    carriers = payload("carrier_termination", "06_mad_waw_16qam.yml")["OtnOpticalCarrier"]["edges"]
    segments = {
        str(edge["node"]["name"]["value"]): [
            str(port["node"]["device"]["node"]["name"]["value"]) for port in edge["node"]["line_ports"]["edges"]
        ]
        for edge in carriers
        if str(edge["node"]["name"]["value"]) in scenario_carriers
    }
    assert set(segments) == scenario_carriers, segments
    for name, devices in sorted(segments.items()):
        assert len(devices) == 2, f"{name} is terminated by {devices}"
        assert any(device.startswith("oeo-") for device in devices), f"{name} has no regenerator end: {devices}"
        assert any(device.startswith("xpdr-") for device in devices), f"{name} has no transponder end: {devices}"


def test_no_cross_connect_anywhere_carries_a_line_port() -> None:
    """The distinction the whole feature draws, asserted across every view."""
    for scenario in (None, *SCENARIOS):
        view = merged(scenario)
        cross_connects = {
            key[0]
            for key, record in view.get("OtnOduSwitch", {}).items()
            if str(record.get("switching_mode")) == "cross_connect"
        }
        offenders = sorted(
            f"{record['device']}/{record['name']}"
            for record in view.get("OtnLinePort", {}).values()
            if str(record.get("device")) in cross_connects
        )
        assert not offenders, f"{scenario or 'the default branch'} gives a cross-connect line-side optics: {offenders}"


def test_the_resolver_reads_every_kind_the_object_files_declare() -> None:
    """No kind is dropped on the way in, and none is keyed to nothing."""
    declared = {
        str((document.get("spec") or {}).get("kind"))
        for document in object_documents()
        if (document.get("spec") or {}).get("kind")
    }
    # `Otn` is what this repository declares. `BuiltinTag`, `CoreStandardGroup`
    # and `CoreGeneratorGroup` come from Infrahub and are not in `schemas/`, so
    # the resolver drops them and nothing here misses them: no check query roots
    # on one, and `queries/units_import.gql`'s `CoreAccount` is a probe the check
    # never reads. A typo in an `Otn` kind is the case this catches.
    unknown = sorted(kind for kind in declared - set(schema()) if kind.startswith("Otn"))
    assert not unknown, f"object files declare Otn kinds the schema does not, so the resolver drops them: {unknown}"

    unkeyed = [kind for kind, records in merged(None).items() if any(part == "" for key in records for part in key)]
    assert not unkeyed, f"records whose human-friendly ID resolved to an empty part: {unkeyed}"
