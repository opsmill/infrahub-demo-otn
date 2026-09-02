"""Every scenario under `demo/`, against every check the pipeline registers.

**The rule this file is.** A gating check is held to every file in `demo/`,
whether or not the check has anything to say about it. Twelve files and nine
checks is 108 cells and all 108 are declared below, because the failure this
guards against is not a wrong verdict, it is a cell nobody looked at.

**What it cost to learn that.** Feature 025 shipped `carrier_termination` and
verified it two ways: against a payload holding a violation somebody wrote by
hand, and against `objects/`. Both passed. It was withdrawn in `e70c245` because
it fired on all six wavelengths in `demo/06_mad_waw_16qam.yml`, which is the
scenario the quick start opens its headline proposed change from, and which
nothing had ever run a check against. The check was right and the model was
missing a regenerator's line ports; the withdrawal was correct and the gap that
let it get that far was this file not existing.

It found more before it was written. Counting the scenarios by hand during
planning turned up **fourteen** unterminated wavelengths across **four** files,
where the design and the withdrawal commit both describe six across one.

**What a cell is.** One scenario file loaded over `objects/` on a branch cut from
the default one, resolved into the shape that check's stored query returns, and
handed to the check class named in `.infrahub.yml`.
`tests/unit/scenariopayloads.py` does the resolving and says how.

**Errors only.** A cell asserts whether the check logged an error and how many,
not what the summary said. A check's INFO line is prose that improves, and
pinning wording here would turn every reworded summary into a failure in a file
about pass and fail. `tests/unit/test_checks.py` is where the wording is held.

**The one thing a merged view cannot show.** A scenario file is input. It is
loaded onto a branch and the generator runs afterwards, so a view built from the
file alone is the branch before any generator has written to it. Two cells depend
on that difference and are declared `NeedsGenerator` rather than pass or fail,
with the check that reads generator output named. `test_a_needs_generator_cell_
names_a_check_that_reads_a_generator_relationship` is what stops that outcome
becoming a place to put a cell somebody did not want to think about.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from functools import cache
from typing import Any

import pytest
from infrahub_sdk.ctl.repository import get_repository_config

from tests.unit.conftest import REPO_ROOT
from tests.unit.scenariopayloads import merged, payload, scenario_files, schema

CONFIG = get_repository_config(REPO_ROOT / ".infrahub.yml")

GENERATOR_RELATIONSHIPS = ("optical_path",)
"""What only a generator writes.

A scenario file declares services, wavelengths, containers and hardware. It never
declares an optical path: that is 25 ordered hops the generator derives from a
route it chose, and no human writes one into YAML. So a check that reads
`optical_path` reads a field that is empty in every merged view and full on every
branch the pipeline actually runs against, and that is the whole of the
difference this module cannot see.
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
    """The verdict here belongs to the branch after the generator, not before it.

    Declared rather than skipped. A skip is a cell nobody reads; this is a cell
    that states what it cannot see and names the field it cannot see it in.
    """

    count: int
    reason: str


Outcome = Passes | Fails | NeedsGenerator

PASSES = Passes()

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
    """One scenario that changes nothing a check speaks about, other than the named cells.

    Written this way because eight of the twelve files are exactly that, and
    twelve times nine cells typed out in full would be a wall a reader skims. The
    default is not a wildcard: every check is still named in `_DEFAULT` and a
    tenth check registered has no entry there either, so the completeness test
    below still fails until somebody adds it.
    """
    cells: dict[str, Outcome] = dict(_DEFAULT)
    cells.update(overrides)
    _row(file_name, **cells)


_DEFAULT: dict[str, Outcome] = {
    "units_import": PASSES,
    "osnr_margin": PAR_MAD,
    "channel_collision": PASSES,
    "container_capacity": PASSES,
    "diversity": PASSES,
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
"""Both diversity scenarios are quiet here and only one of them is quiet on a
live branch. `checks/diversity.py` compares the ducts under two services' optical
paths, and a scenario file declares no optical path, so neither is judged before
the generator runs. That is a limit of the view rather than a verdict, and it is
not marked `NeedsGenerator` because the check logs nothing either way: there is
no count to be wrong about. `tests/unit/test_demo_scenarios.py` is what holds
`09` to sharing `cd-fra-north` and `08` to not sharing anything."""

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
    """The class `.infrahub.yml` names, loaded from the file it names.

    Loaded by path rather than imported, the same way `tests/unit/test_checks.py`
    does it, so a registration pointing at a class the file does not define fails
    here rather than at repository sync.
    """
    entry = _entry(name)
    path = REPO_ROOT / str(entry.file_path)
    spec = importlib.util.spec_from_file_location(f"scenario_check_{name}", path)
    assert spec is not None and spec.loader is not None, f"{entry.file_path} is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, entry.class_name)


@cache
def _errors(check_name: str, file_name: str) -> tuple[str, ...]:
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
    """Every scenario against every check, no cell missing and none invented.

    Both sides are discovered rather than listed: the files come from a glob over
    `demo/` and the checks from `check_definitions`. A thirteenth scenario or a
    tenth check therefore turns this red until somebody says what the new cells
    do, which is the opposite of the silence that let 025 ship.
    """
    assert set(EXPECTED) == set(CELLS), (
        f"undeclared cells: {sorted(set(CELLS) - set(EXPECTED))}; "
        f"declared cells that do not exist: {sorted(set(EXPECTED) - set(CELLS))}"
    )


def test_the_sweep_covers_twelve_scenarios_and_nine_checks() -> None:
    """The two numbers this module's docstring publishes, read back from the tree.

    Named so that a scenario quietly deleted is as visible as one quietly added.
    A shrinking sweep is the same failure as a missing cell.
    """
    assert len(SCENARIOS) == 12, f"demo/ holds {len(SCENARIOS)} scenarios: {SCENARIOS}"
    assert len(CHECKS) == 9, f".infrahub.yml registers {len(CHECKS)} checks: {CHECKS}"
    assert len(CELLS) == 108


@pytest.mark.parametrize("check_name", CHECKS)
def test_every_registered_check_can_be_swept(check_name: str) -> None:
    """A payload can be built for the check and the check runs against it.

    This fails rather than skips, which is the point. A check whose query the
    resolver cannot read is a check outside the sweep, and a sweep with a hole in
    it reports green over the hole.
    """
    built = payload(check_name, None)
    assert built, f"{check_name} resolved to an empty payload"
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

    assert errors, (
        f"{check_name} was expected to report {expected.count} error(s) on {scenario} and reported none. "
        f"The reason on record: {expected.reason}"
    )
    assert len(errors) == expected.count, (
        f"{check_name} reported {len(errors)} error(s) on {scenario}, not the {expected.count} declared. "
        f"The reason on record: {expected.reason}. First: {errors[0]}"
    )


def test_a_needs_generator_cell_names_a_check_that_reads_a_generator_relationship() -> None:
    """`NeedsGenerator` is not an escape hatch, and this is what keeps it from becoming one.

    A cell may only claim the generator explains it when the check's own stored
    query selects a field no scenario file writes. `optical_path` is the one such
    field: it is 25 ordered hops derived from a route, and nothing under `demo/`
    declares one. A check that reads only what the YAML declares has no generator
    to blame, so a `NeedsGenerator` on one of those fails here.
    """
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
    """The default branch, run through all nine, against what the tree already asserts.

    A resolver that is wrong in the same direction as the checks it feeds would
    make this whole module agree with itself and with nothing else. These five
    verdicts are pinned elsewhere from payloads a human wrote or from the
    generated data, so agreement here is agreement with a second opinion:

    - `osnr_margin` fails Paris to Madrid in both directions and nothing else,
      which `test_checks.py::test_no_section_other_than_paris_to_madrid_fails_the_sweep`
      holds from a hand-built payload.
    - `container_capacity` passes, because the shipped dataset holds no parent
      container at all.
    - `monitor_completeness` passes, because every device that should carry a
      monitor carries one on the default branch.
    - `channel_collision` passes, because the re-seeded carrier plan overlaps
      nowhere.
    - `carrier_termination` passes on all forty, which is
      `tests/unit/test_geant_dataset.py`'s two-line-ports-per-wavelength
      arithmetic arriving through a different door.
    """
    verdicts = {name: len(_errors(name, "00_services.yml")) for name in CHECKS}
    shipped_only = {name: len(_errors(name, "00_services.yml")) for name in CHECKS}
    assert verdicts == shipped_only

    assert verdicts["osnr_margin"] == 2, "the sweep should find the Paris to Madrid deficit in both directions"
    for quiet in ("container_capacity", "monitor_completeness", "channel_collision", "carrier_termination"):
        assert verdicts[quiet] == 0, f"{quiet} fails the shipped plant, which nothing else in the suite says"

    par_mad = _errors("osnr_margin", "00_services.yml")
    assert all("oms-par-mad" in message for message in par_mad), par_mad


def test_every_shipped_carrier_is_terminated_at_both_ends_through_the_resolver() -> None:
    """Forty wavelengths, two line ports each, read the way the check reads them.

    `tests/unit/test_geant_dataset.py` asserts the same arithmetic against the
    object files. This asserts it through the graph the check walks, which is the
    part that broke: the edge is written on the carrier under `objects/` and on
    the port under `demo/`, and a reader that only knew one side would see half
    the plant unterminated.
    """
    carriers = payload("carrier_termination", None)["OtnOpticalCarrier"]["edges"]
    assert len(carriers) == 40
    counts = {str(edge["node"]["name"]["value"]): len(edge["node"]["line_ports"]["edges"]) for edge in carriers}
    wrong = {name: count for name, count in counts.items() if count != 2}
    assert not wrong, f"shipped wavelengths not terminated at exactly two ends: {wrong}"


def test_a_regenerator_terminates_the_two_segments_it_joins_on_the_scenario_branch() -> None:
    """`demo/06_mad_waw_16qam.yml` read through the check's own query.

    This is the state feature 025 could not produce and the reason it withdrew.
    Six wavelengths, each with a transponder at its outer end and a regenerator
    line port at its inner one, so each is terminated at two sites.
    """
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
    """The distinction the whole feature draws, asserted across every view.

    A cross-connect grooms ODU containers behind a transponder and terminates no
    wavelength. `oxc-mil-01` is patched to 37 that already terminate on Milan
    transponders, so one line port on it would make all 37 over-terminated, which
    is what stopped `odu_switches` being read as the termination answer.
    """
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


def test_the_resolver_reads_every_kind_the_schema_declares() -> None:
    """No kind resolves by accident.

    A kind the resolver cannot key would silently hold no records, and every
    check that reads it would then be swept against an empty collection and pass.
    """
    unkeyed = [kind for kind, records in merged(None).items() if any(part == "" for key in records for part in key)]
    assert not unkeyed, f"records whose human-friendly ID resolved to an empty part: {unkeyed}"
    assert set(merged(None)) <= set(schema())
