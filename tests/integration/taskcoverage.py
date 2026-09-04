"""One coverage record per task, and what each record's test must assert.

One record per `invoke` task: the tier it belongs to, the arguments the suite
runs and the postcondition the test asserts afterwards. The records are data
only; `tests/unit/test_task_coverage.py` checks them against the live task list
and the task modules here consume them.

`postcondition` is the field that keeps this honest. An invocation that exits
zero proves nothing: `demo-clean --branch does-not-exist` would otherwise cover
`demo-clean` while doing no work. Every record that runs something names what
the test asserts after it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["stack", "covered_by", "ci", "excluded"]

RUNNABLE_TIERS: tuple[Tier, ...] = ("stack",)
"""Tiers whose records the suite invokes itself, so each needs an invocation."""


@dataclass(frozen=True)
class CoverageRecord:
    """How one task is covered, and what proves it ran.

    `invocations` holds argument strings, empty string meaning no arguments.
    `job` is required of a `ci` record and `covered_by` of a `covered_by` one.
    `reason` is required of an exclusion and welcome anywhere it helps.
    """

    task: str
    tier: Tier
    invocations: tuple[str, ...] = ()
    postcondition: str = ""
    reason: str = ""
    job: str = ""
    covered_by: str = ""


COVERAGE: tuple[CoverageRecord, ...] = (
    # ------------------------------------------------------------------ #
    # The stack managers. Each one acts on the compose project, and the
    # stack this suite runs against is a testcontainers one, so running any
    # of them here would reach past the suite and into whatever stack the
    # machine already has up. `build` is the exception: it only writes an
    # image, and the integration job runs it.
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="build",
        tier="ci",
        job="integration-test",
        reason="the job builds the image with this task before it starts the stack",
    ),
    CoverageRecord(
        task="start",
        tier="excluded",
        reason="brings the compose stack up, which would sit beside the testcontainers one and compete with it",
    ),
    CoverageRecord(
        task="stop",
        tier="excluded",
        reason="takes the compose stack down, and the only one on the machine is a developer's",
    ),
    CoverageRecord(
        task="restart",
        tier="excluded",
        reason="restarts the compose stack, which is not the stack the suite runs against",
    ),
    CoverageRecord(
        task="destroy",
        tier="excluded",
        reason="deletes the compose stack and its volumes, which is every object a developer has loaded",
    ),
    CoverageRecord(
        task="init",
        tier="excluded",
        reason="destroys the compose stack before rebuilding it, so it takes the running stack and its data with it",
    ),
    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="list",
        tier="ci",
        job="budget-unit-tests",
        reason="the unit suite calls it in process and reads both listings back against the task collection",
    ),
    CoverageRecord(
        task="info",
        tier="stack",
        invocations=("", "--branch demo"),
        postcondition="the output names the testcontainers address, not the one in `.env`",
    ),
    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="load",
        tier="stack",
        invocations=("--branch <scratch>",),
        postcondition="the branch holds the manifest's site and carrier counts",
    ),
    CoverageRecord(
        task="load-schema",
        tier="stack",
        invocations=("--branch <scratch>",),
        postcondition="the branch's schema summary lists every kind `schemas/` defines",
    ),
    CoverageRecord(
        task="load-menu",
        tier="stack",
        invocations=("--branch <scratch>",),
        postcondition="the branch holds one menu item per entry in `menus/`",
    ),
    CoverageRecord(
        task="load-objects",
        tier="stack",
        invocations=("--branch <scratch>", "--branch <scratch> --file objects/04_client_signals.yml"),
        postcondition="the directory form loads the manifest total; `--file` adds only that file's client signals",
    ),
    CoverageRecord(
        task="load-repository",
        tier="stack",
        invocations=("",),
        postcondition="the repository appears as a `CoreRepository` and reaches a synced state",
    ),
    # ------------------------------------------------------------------ #
    # Branches
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="branch-create",
        tier="stack",
        invocations=("--name <scratch>",),
        postcondition="the branch exists in the graph with `sync_with_git` set, which is what the task adds",
    ),
    CoverageRecord(
        task="branch-list",
        tier="stack",
        invocations=("",),
        postcondition="the output names the scratch branch created above",
    ),
    CoverageRecord(
        task="branch-delete",
        tier="stack",
        invocations=("--name <scratch>",),
        postcondition="the branch is gone from the graph and from `branch-list`",
    ),
    # ------------------------------------------------------------------ #
    # Checks
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="check",
        tier="stack",
        invocations=("--name units_import", ""),
        postcondition="the named form reports that one check; the bare form names all nine in `CHECKS`",
    ),
    # ------------------------------------------------------------------ #
    # Tests
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="test-unit",
        tier="ci",
        job="budget-unit-tests",
        reason="the job runs the same pytest command over the same directory",
    ),
    CoverageRecord(
        task="test",
        tier="excluded",
        reason="runs test-integration, which is this suite",
    ),
    CoverageRecord(
        task="test-integration",
        tier="excluded",
        reason="this suite; a suite cannot contain itself",
    ),
    # ------------------------------------------------------------------ #
    # Quality
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="format",
        tier="ci",
        job="python-lint",
        reason="the job runs the same formatter in check mode, so a tree this task would rewrite fails there",
    ),
    # No single job runs this one. It chains six linters and CI runs each of
    # them, spread over four jobs, so a linter that would fail here fails there.
    CoverageRecord(
        task="lint",
        tier="ci",
        job="python-lint, yaml-lint, markdown-lint, prose-lint",
        reason="the four jobs between them run every linter this task chains",
    ),
    CoverageRecord(
        task="schema-check",
        tier="ci",
        job="schema-validate",
        reason="the job runs the same infrahubctl formatting check over the same directory",
    ),
    CoverageRecord(
        task="docs",
        tier="ci",
        job="documentation",
        reason="the job builds the same site with pnpm, which the integration job does not install",
    ),
    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="inventory",
        tier="stack",
        invocations=("--branch main",),
        postcondition="the printed element and carrier figures match the manifest",
    ),
    CoverageRecord(
        task="dataset-generate",
        tier="ci",
        job="budget-unit-tests",
        reason="the unit suite calls it with a temporary destination and finds `objects/` unchanged afterwards",
    ),
    CoverageRecord(
        task="dataset-check",
        tier="ci",
        job="budget-unit-tests",
        reason="the unit suite regenerates the seed and diffs it against the committed `objects/1*.yml`",
    ),
    CoverageRecord(
        task="maps-regenerate",
        tier="ci",
        job="budget-unit-tests",
        reason="the unit suite calls it with a temporary destination and compares the render against the fixture",
    ),
    # ------------------------------------------------------------------ #
    # The walkthrough. `demo` runs the ten in WALKTHROUGH, so running them
    # again individually costs 10 to 15 minutes and proves nothing new.
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="demo",
        tier="stack",
        invocations=("",),
        postcondition="the ten steps ran in order and each of the five demo services is decided, not still planned",
    ),
    CoverageRecord(task="demo-capacity", tier="covered_by", covered_by="demo"),
    CoverageRecord(task="demo-reach", tier="covered_by", covered_by="demo"),
    CoverageRecord(
        task="demo-provision",
        tier="covered_by",
        covered_by="demo",
        invocations=("--branch <scratch> --service svc-fra-mil-ai-400g",),
        postcondition="`demo` covers the default path; the `--service` form provisions that one service and no other",
    ),
    CoverageRecord(task="demo-provision-all", tier="covered_by", covered_by="demo"),
    CoverageRecord(task="demo-trace", tier="covered_by", covered_by="demo"),
    CoverageRecord(task="demo-impact", tier="covered_by", covered_by="demo"),
    CoverageRecord(task="demo-srlg", tier="covered_by", covered_by="demo"),
    CoverageRecord(task="demo-latency", tier="covered_by", covered_by="demo"),
    CoverageRecord(task="demo-infiniband", tier="covered_by", covered_by="demo"),
    CoverageRecord(task="demo-refusal", tier="covered_by", covered_by="demo"),
    # Three demo tasks are not in WALKTHROUGH, so they run directly.
    CoverageRecord(
        task="demo-setup",
        tier="stack",
        invocations=("",),
        postcondition="the `demo` branch exists and holds the dataset plus the service group memberships",
    ),
    CoverageRecord(
        task="demo-budget",
        tier="stack",
        invocations=("",),
        postcondition="the output names a worst margin and the section it belongs to",
    ),
    CoverageRecord(
        task="demo-drift",
        tier="stack",
        invocations=("",),
        postcondition="the output lists at least the seeded amplifier droop",
    ),
    # ------------------------------------------------------------------ #
    # The loadable scenarios. Each owns the branch SCENARIO_BRANCHES names.
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="demo-raman",
        tier="stack",
        invocations=("",),
        postcondition="`raman-par-mad` holds `02_par_mad_raman.yml` and `check osnr_margin` passes on it",
    ),
    CoverageRecord(
        task="demo-odu",
        tier="stack",
        invocations=("",),
        postcondition="`odu-demo` holds both ODU files, ten circuits groom, the eleventh is refused for slots",
    ),
    CoverageRecord(
        task="demo-regenerator",
        tier="stack",
        invocations=("",),
        postcondition="`oeo-refused` refuses `svc-mad-waw-400g`, `oeo-closed` provisions it, `provisionable` agrees",
    ),
    CoverageRecord(
        task="demo-diversity",
        tier="stack",
        invocations=("",),
        postcondition="`diversity-demo` provisions all four feeds and `check diversity` names the shared duct",
    ),
    CoverageRecord(
        task="demo-monitor-gap",
        tier="stack",
        invocations=("",),
        postcondition="`monitor-gap` holds the unmonitored amplifier and `check monitor_completeness` names it",
    ),
    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="demo-clean",
        tier="stack",
        invocations=("--branch demo",),
        postcondition="`demo` is gone from the branch listing and the other scenario branches are still on it",
        reason=(
            "the bare form is not run here. It deleted seven branches for 1003 seconds, a third of this "
            "suite, and about 157 of every 165 was the recomputation backlog draining rather than any "
            "deletion. --branch demo proves every mechanism it uses; the list it adds is a comprehension "
            "over SCENARIO_BRANCHES, which tests/unit/test_task_commands.py reads directly"
        ),
    ),
)


def records_by_task() -> dict[str, CoverageRecord]:
    """The records keyed by task name, for a test that has a task and wants its record."""
    return {record.task: record for record in COVERAGE}
