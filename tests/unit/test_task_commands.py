"""The three tasks that need no stack, called in process, and what `demo-clean` targets.

The code underneath two of them is covered elsewhere: the seed in
`test_geant_dataset.py`, the golden renderers in `test_mapdraw.py` and
`test_odudraw.py`. What is covered here is the wrapper around each, and in
particular the `--output` plumbing. Without it a demo command that a reader
runs to look at a render rewrites the committed tree instead.

The last test is here rather than on a stack for a reason worth stating.
`demo-clean` with no argument deletes seven branches, and the integration suite
used to run it: 1003 seconds, a third of that suite's wall clock, almost all of
it the recomputation backlog draining rather than any deletion. The mechanism is
still proven there, on one branch. What is left is the list, and a list built
from a module constant is answerable here in a millisecond.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest
import tasks
from invoke import Collection, Config, Context

from .conftest import OBJECT_DIR, REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "unit" / "fixtures"

TASK_NAMES = frozenset(Collection.from_module(tasks).task_names)
DEFAULT_LISTING = frozenset(name for _, listed, _ in tasks.TASK_GROUPS for name in listed)


def _digest(directory: Path, pattern: str) -> dict[str, str]:
    """Every matching file under a directory, mapped to the hash of its bytes."""
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(directory.glob(pattern))}


def _captured() -> Context:
    """A context whose subprocesses do not read the stdin pytest has taken."""
    return Context(Config(overrides={"run": {"in_stream": False}}))


def _names_in_listing(output: str) -> set[str]:
    """The task names the listing printed, read from the first table column."""
    return {name for name in TASK_NAMES if re.search(rf"^\s+{re.escape(name)}\s", output, re.MULTILINE)}


def test_list_names_the_readers_set_by_default_and_everything_with_all(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`list`: the default is the reader's set and `--all` is the whole collection.

    The console is widened first. At the eighty columns rich assumes when
    nothing is attached, a description wraps onto a line beginning with a word
    that reads like a task name, and the reader below counts it as one.
    """
    monkeypatch.setattr(tasks.console, "width", 200)

    tasks.list_tasks(Context())
    default = capsys.readouterr().out
    tasks.list_tasks(Context(), all=True)
    everything = capsys.readouterr().out

    assert _names_in_listing(default) == set(DEFAULT_LISTING)
    assert _names_in_listing(everything) == set(TASK_NAMES)
    assert len(DEFAULT_LISTING) == 27
    assert len(TASK_NAMES) - len(DEFAULT_LISTING) == 21


def test_dataset_generate_writes_into_the_directory_it_is_given(tmp_path: Path) -> None:
    """`dataset-generate --output`: the same file names, and `objects/` untouched."""
    digest = _digest(OBJECT_DIR, "*.yml")
    generated = {path.name for path in OBJECT_DIR.glob("1*.yml")}
    destination = tmp_path / "dataset"

    tasks.dataset_generate(_captured(), output=str(destination))

    assert {path.name for path in destination.glob("*.yml")} == generated
    assert json.loads((destination / "geant_manifest.json").read_text())["OtnSite"] == 15
    assert _digest(OBJECT_DIR, "*.yml") == digest


@pytest.mark.parametrize("case", ["network_map_golden", "odu_map_golden"])
def test_maps_regenerate_writes_into_the_directory_it_is_given(case: str, tmp_path: Path) -> None:
    """`maps-regenerate --case --output`: the render equals the committed fixture.

    Equality both ways. The fixture keeps the bytes it was committed with, and
    the render put beside it is those same bytes, which is what makes this
    evidence that the renderer ran rather than that a file exists.
    """
    fixture = FIXTURE_DIR / f"{case}.svg"
    committed = fixture.read_bytes()
    destination = tmp_path / case

    tasks.maps_regenerate(Context(), case=case, output=str(destination))

    written = destination / f"{case}.svg"
    assert written.is_file()
    assert written.read_bytes() == committed
    assert fixture.read_bytes() == committed


def test_demo_clean_targets_every_branch_a_scenario_task_opens() -> None:
    """`demo-clean` with no argument names every scenario branch, and only those.

    A scenario task opens its branch one of two ways: through the
    `SCENARIO_BRANCHES` row it looks up, which cannot escape this set, or
    through a `branch` parameter whose default is a module constant, which can.
    A new scenario that defaults to a branch nobody added a row for would be
    created by `invoke demo` and left behind by `invoke demo-clean`, and the
    reader who followed the walkthrough would be the one to find it.
    """
    targets = {row.branch for row in tasks.SCENARIO_BRANCHES}
    collection = Collection.from_module(tasks)

    defaults = {
        name: signature.parameters["branch"].default
        for name in collection.task_names
        for signature in [inspect.signature(collection[name].body)]
        if "branch" in signature.parameters
    }
    opened = {
        name: default for name, default in defaults.items() if name.startswith("demo-") and default not in {"main", ""}
    }

    assert opened, "no demo task defaults to a branch of its own, so this test reads nothing"
    orphans = sorted(f"{name} opens {default!r}" for name, default in opened.items() if default not in targets)
    assert not orphans, f"branches `demo-clean` would not delete: {orphans}"


class FakeResult:
    """What `context.run(..., warn=True)` hands back, reduced to what is read."""

    def __init__(self, *, ok: bool) -> None:
        self.ok = ok


class RecordingContext:
    """Stands in for a `Context`, answering `run` from a script."""

    def __init__(self, *outcomes: bool) -> None:
        self.outcomes = list(outcomes)
        self.commands: list[str] = []

    def run(self, command: str, **_: object) -> FakeResult:
        self.commands.append(command)
        return FakeResult(ok=self.outcomes[min(len(self.commands) - 1, len(self.outcomes) - 1)])


def undecorated(name: str) -> Any:
    """One task's plain function.

    Reached through the collection rather than off the module, because the
    `@task` wrapper type-checks its first argument and `RecordingContext` is not
    a `Context`. This is also how the signature reader above gets at a task.
    """
    return Collection.from_module(tasks)[name].body


@pytest.fixture
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Collect the backoff instead of serving it."""
    waits: list[float] = []
    monkeypatch.setattr(tasks.time, "sleep", waits.append)
    monkeypatch.setattr(tasks, "_env", lambda: {"INFRAHUB_ADDRESS": "http://x", "INFRAHUB_API_TOKEN": "t"})
    return waits


def test_a_branch_create_that_times_out_is_checked_against_the_server(
    monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    """The failure in run 33863228168: the mutation landed, the read gave up.

    `infrahubctl` exited 1 after a 300 second read timeout while the branch it
    was asked for had been made. Judged on the exit code that is a dead task;
    judged on the server it is done.
    """
    monkeypatch.setattr(tasks, "_branch_exists", lambda _: True)
    context = RecordingContext(False)

    undecorated("branch-create")(context, "raman-par-mad")

    assert len(context.commands) == 1, "the create was repeated against a branch that was already there"
    assert no_waiting == [], "it waited before looking"


def test_a_branch_create_that_really_failed_is_asked_again(
    monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]
) -> None:
    """Not there and not created is the case a retry is for."""
    seen: list[bool] = [False, True]
    monkeypatch.setattr(tasks, "_branch_exists", lambda _: seen.pop(0))
    context = RecordingContext(False, False)

    undecorated("branch-create")(context, "raman-par-mad")

    assert len(context.commands) == 2
    assert no_waiting == [tasks.BRANCH_CREATE_BACKOFF_SECONDS], f"backed off {no_waiting}"


def test_a_branch_that_never_appears_stops_the_task(monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]) -> None:
    """A retry that cannot fail is a task that hangs instead of reporting."""
    monkeypatch.setattr(tasks, "_branch_exists", lambda _: False)
    context = RecordingContext(False)

    with pytest.raises(SystemExit):
        undecorated("branch-create")(context, "raman-par-mad")

    assert len(context.commands) == tasks.BRANCH_CREATE_ATTEMPTS


def test_the_sync_with_git_flag_survives_the_retry(monkeypatch: pytest.MonkeyPatch, no_waiting: list[float]) -> None:
    """A branch made without Git runs the built-in validators, not this repository's."""
    monkeypatch.setattr(tasks, "_branch_exists", lambda _: False)
    context = RecordingContext(False, False, True)

    undecorated("branch-create")(context, "demo")

    assert all("--sync-with-git" in command for command in context.commands), context.commands


CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"

FULL_ONLY_MODULES = {"test_tasks_stack.py"}
"""Integration modules the core tier deliberately leaves out.

`test_tasks_stack.py` drives all 48 tasks and every scenario against the stack.
It is forty of the suite's fifty minutes and only a change to the demo's own
logic can break it, which is the whole reason the tiers exist.
"""


def test_every_tier_ci_asks_for_is_a_tier_the_task_knows() -> None:
    """`ci.yml` picks a tier and `tasks.py` maps it to a pytest selection.

    Two files, one vocabulary. A tier renamed in the table and not in the
    workflow reaches `test-integration` as an unknown word, and the job fails on
    a green tree.
    """
    workflow = CI_WORKFLOW.read_text()
    assigned = set(re.findall(r'TIER="(\w+)"', workflow))
    offered = set(re.findall(r'options: \["core", "full"\]', workflow) and ["core", "full"])

    assert assigned, "ci.yml assigns no tier, so this test reads nothing"
    unknown = sorted((assigned | offered) - set(tasks.TIERS))
    assert not unknown, f"tiers ci.yml uses that tasks.py does not define: {unknown}"
    assert offered == set(tasks.TIERS), "the dispatch offers a different set of tiers from the table"


def test_the_core_tier_selects_something() -> None:
    """A `-m` expression that matches nothing exits 5, which reads as broken.

    Asserted by reading the source rather than by collecting, because
    `tests/unit` must not import the testcontainers plugin.
    """
    marked = [path.name for path in INTEGRATION_DIR.glob("test_*.py") if "@pytest.mark.core" in path.read_text()]
    assert marked, "no integration module carries the core marker, so `--tier=core` would collect nothing"


def test_every_integration_module_is_in_a_tier_on_purpose() -> None:
    """A new module is in the full tier by default, and has to say so.

    The safe default: a module nobody placed runs in `full` and is missed by the
    pull requests that run `core`. Naming it here is what turns that from an
    oversight into a decision.
    """
    modules = {path.name for path in INTEGRATION_DIR.glob("test_*.py")}
    core = {path.name for path in INTEGRATION_DIR.glob("test_*.py") if "@pytest.mark.core" in path.read_text()}

    unplaced = sorted(modules - core - FULL_ONLY_MODULES)
    assert not unplaced, (
        f"integration modules in no tier: {unplaced}. Mark the class `@pytest.mark.core` "
        "or add the module to FULL_ONLY_MODULES with the reason."
    )
    stale = sorted(FULL_ONLY_MODULES - modules)
    assert not stale, f"FULL_ONLY_MODULES names modules that are gone: {stale}"
    assert not (core & FULL_ONLY_MODULES), "a module cannot be both core and full-only"


def test_an_unknown_tier_stops_before_the_stack_is_started() -> None:
    """A typo must fail on the word, not after booting Infrahub."""
    with pytest.raises(SystemExit):
        undecorated("test-integration")(RecordingContext(True), tier="coer")


def test_the_marker_is_registered_so_a_typo_fails_collection() -> None:
    """`--strict-markers` is what turns a misspelled marker into an error."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert '"core: ' in pyproject, "the core marker is not registered, so it warns instead of selecting"
    assert "--strict-markers" in pyproject, "without this an unregistered marker is a warning, not a failure"
