"""One coverage record per task, and what each record's test must assert.

Transcribed from `specs/028-command-surface/contracts/task-surface.md`. The
records are data only; `tests/unit/test_task_coverage.py` checks them against
the live task list and the three task modules here consume them.

`postcondition` is the field that keeps this honest. An invocation that exits
zero proves nothing: `demo-clean --branch does-not-exist` would otherwise cover
`demo-clean` while doing no work. Every record that runs something names what
the test asserts after it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["offline", "stack", "lifecycle", "covered_by", "ci", "excluded"]

RUNNABLE_TIERS: tuple[Tier, ...] = ("offline", "stack", "lifecycle")
"""Tiers whose records the suite invokes itself, so each needs an invocation."""


@dataclass(frozen=True)
class CoverageRecord:
    """How one task is covered, and what proves it ran.

    `invocations` holds argument strings, empty string meaning no arguments.
    `reason`, `job` and `covered_by` are each required by exactly one tier.
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
    # Stack lifecycle: a throwaway compose project on non-default ports.
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="build",
        tier="lifecycle",
        invocations=("",),
        postcondition="`docker image inspect` finds the tagged image afterwards",
    ),
    CoverageRecord(
        task="start",
        tier="lifecycle",
        invocations=("",),
        postcondition="`/api/schema/summary` answers on the overridden port",
    ),
    CoverageRecord(
        task="stop",
        tier="lifecycle",
        invocations=("",),
        postcondition="no container of the scratch project runs, and its named volumes still exist",
    ),
    CoverageRecord(
        task="restart",
        tier="lifecycle",
        invocations=("--component infrahub-server", ""),
        postcondition="`/api/schema/summary` answers again on the overridden port after each form",
    ),
    CoverageRecord(
        task="destroy",
        tier="lifecycle",
        invocations=("",),
        postcondition="no container and no named volume of the scratch project remains",
    ),
    CoverageRecord(
        task="init",
        tier="lifecycle",
        invocations=("",),
        postcondition="the stack answers and `main` holds the manifest's `OtnSite` count",
    ),
    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="list",
        tier="offline",
        invocations=("", "--all"),
        postcondition="`--all` names every task in the collection; the default names 27, so the listings differ by 21",
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
        postcondition="the branch exists in the graph and a git branch of that name exists too",
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
        tier="offline",
        invocations=("",),
        postcondition="exits zero and the output reports no failures",
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
        tier="offline",
        invocations=("",),
        postcondition="run against a copied tree; `git status --porcelain` in the repository stays empty",
    ),
    CoverageRecord(
        task="lint",
        tier="offline",
        invocations=("",),
        postcondition="exits zero, and where `vale` is absent the output names the prose step it skipped",
    ),
    CoverageRecord(
        task="schema-check",
        tier="offline",
        invocations=("",),
        postcondition="exits zero against the committed `schemas/`, which stays unmodified",
    ),
    CoverageRecord(
        task="docs",
        tier="ci",
        job="documentation",
        reason="needs pnpm, which the integration job does not install",
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
        tier="offline",
        invocations=("--output <tmpdir>",),
        postcondition="the temporary directory holds the file names `objects/1*.yml` has, and `objects/` is unchanged",
    ),
    CoverageRecord(
        task="dataset-check",
        tier="offline",
        invocations=("",),
        postcondition="exits zero: regenerating the seed reproduces the committed `objects/1*.yml`",
    ),
    CoverageRecord(
        task="maps-regenerate",
        tier="offline",
        invocations=(
            "--case network_map_golden --output <tmpdir>",
            "--case odu_map_golden --output <tmpdir>",
        ),
        postcondition="an SVG appears in the temporary directory and the committed golden fixture is byte-identical",
    ),
    # ------------------------------------------------------------------ #
    # The walkthrough. `demo` runs the ten in WALKTHROUGH, so running them
    # again individually costs 10 to 15 minutes and proves nothing new.
    # ------------------------------------------------------------------ #
    CoverageRecord(
        task="demo",
        tier="stack",
        invocations=("",),
        postcondition="all ten walkthrough steps ran in order and the branch holds the five provisioned services",
    ),
    CoverageRecord(task="demo-capacity", tier="covered_by", covered_by="demo"),
    CoverageRecord(task="demo-reach", tier="covered_by", covered_by="demo"),
    CoverageRecord(
        task="demo-provision",
        tier="covered_by",
        covered_by="demo",
        invocations=("--service svc-fra-mil-ai-400g",),
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
        postcondition="`odu-demo` holds both ODU files and `check container_capacity` reports a finding",
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
        postcondition="`diversity-demo` provisions both feeds and `check diversity` reports the shared resource",
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
        invocations=("--branch demo", ""),
        postcondition="`--branch demo` removes only that branch; the bare form leaves no branch in SCENARIO_BRANCHES",
    ),
)


def records_by_task() -> dict[str, CoverageRecord]:
    """The records keyed by task name, for a test that has a task and wants its record."""
    return {record.task: record for record in COVERAGE}
