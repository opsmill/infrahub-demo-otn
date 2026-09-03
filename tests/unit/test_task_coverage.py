"""The coverage table in `tests/integration/taskcoverage.py`, checked against `tasks.py`.

The table's subject is the integration suite, but the check itself only compares
two lists and needs no stack, so it runs here where it costs milliseconds and
fails before Docker is even asked for. `tests/integration/conftest.py` fails the
session outright when the local image is missing, which would hide this.

The two directions matter for different reasons. A task with no record is a task
nobody tests; a record with no task is coverage claimed for something a rename
already removed, and that reads as evidence when it is not.
"""

import tasks
from invoke import Collection

from tests.integration.taskcoverage import COVERAGE, RUNNABLE_TIERS, CoverageRecord

TASK_NAMES = frozenset(Collection.from_module(tasks).task_names)


def _records(tier: str) -> tuple[CoverageRecord, ...]:
    return tuple(record for record in COVERAGE if record.tier == tier)


def test_every_task_has_exactly_one_record() -> None:
    """Invariant 1. A task added without a record is a task nobody exercises."""
    covered = [record.task for record in COVERAGE]
    missing = sorted(TASK_NAMES - set(covered))
    assert not missing, f"tasks with no coverage record in taskcoverage.py: {missing}"
    duplicated = sorted({name for name in covered if covered.count(name) > 1})
    assert not duplicated, f"tasks with more than one coverage record: {duplicated}"


def test_every_record_names_a_task_that_exists() -> None:
    """Invariant 2. Catches a record left behind by a rename."""
    orphaned = sorted({record.task for record in COVERAGE} - TASK_NAMES)
    assert not orphaned, f"coverage records naming tasks that tasks.py does not define: {orphaned}"


def test_the_exclusions_are_the_named_seven_and_each_carries_a_reason() -> None:
    """Invariant 3. An exclusion nobody named is where an untested task hides.

    Two of the seven are this suite. The other five manage the compose stack,
    and the stack the suite runs against is a testcontainers one, so each of
    them would reach past the suite and into a stack it does not own.
    """
    excluded = _records("excluded")
    assert sorted(record.task for record in excluded) == [
        "destroy",
        "init",
        "restart",
        "start",
        "stop",
        "test",
        "test-integration",
    ]
    unexplained = sorted(record.task for record in excluded if not record.reason)
    assert not unexplained, f"excluded without a reason: {unexplained}"


def test_ci_records_carry_a_job_name() -> None:
    """Invariant 4. A job name is what makes the claim checkable against ci.yml."""
    nameless = sorted(record.task for record in _records("ci") if not record.job)
    assert not nameless, f"ci records naming no CI job: {nameless}"


def test_runnable_records_carry_an_invocation_and_a_postcondition() -> None:
    """Invariant 5, plus the postcondition the plan adds to it."""
    runnable = [record for record in COVERAGE if record.tier in RUNNABLE_TIERS]
    silent = sorted(record.task for record in runnable if not record.invocations)
    assert not silent, f"runnable records with nothing to invoke: {silent}"
    unasserted = sorted(record.task for record in runnable if not record.postcondition)
    assert not unasserted, f"runnable records with no postcondition: {unasserted}"


def test_any_record_that_invokes_something_says_what_it_proves() -> None:
    """An exit code is not evidence, whatever tier the record sits in."""
    unasserted = sorted(record.task for record in COVERAGE if record.invocations and not record.postcondition)
    assert not unasserted, f"records invoking a task without a postcondition: {unasserted}"


def test_covered_by_records_name_a_coverer_that_is_itself_exercised() -> None:
    """A chain ending in a task nobody runs covers nothing."""
    exercised = {record.task for record in COVERAGE if record.tier in RUNNABLE_TIERS}
    for record in _records("covered_by"):
        assert record.covered_by, f"{record.task} is covered_by nothing"
        assert record.covered_by in exercised, (
            f"{record.task} is covered by {record.covered_by}, which no runnable record exercises"
        )


def test_the_covered_by_records_are_exactly_the_walkthrough() -> None:
    """The saving is the walkthrough and only the walkthrough.

    `demo` runs `WALKTHROUGH` in order, so those ten need no second run. Any
    other task given the same tier would be skipped rather than covered.
    """
    delegated = sorted(record.task for record in _records("covered_by"))
    assert delegated == sorted(tasks.WALKTHROUGH)


def test_the_tiers_partition_the_task_list() -> None:
    """Every task lands in exactly one tier, and the tiers sum to the live count."""
    counts = {record.tier: len(_records(record.tier)) for record in COVERAGE}
    assert sum(counts.values()) == len(TASK_NAMES)
