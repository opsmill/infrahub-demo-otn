"""The offline tier of `taskcoverage.py`, run and then asserted.

Nothing here needs Infrahub, so the module carries no `integration` marker. It
lives beside the others because it exercises the same command surface and reads
the same coverage records.

Nothing here may dirty the repository either. The two regeneration tasks write
into temporary directories and `format` runs against an export of the committed
tree, so a mistake here is a red test rather than an edit a reader has to notice.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess  # noqa: S404
from collections.abc import Iterator
from pathlib import Path

import pytest
import tasks
from invoke import Collection

from .conftest import REPO_ROOT, run_task, task_output
from .taskcoverage import records_by_task

RECORDS = records_by_task()

FIXTURE_DIRECTORY = REPO_ROOT / "tests" / "unit" / "fixtures"
OBJECT_DIRECTORY = REPO_ROOT / "objects"
SCHEMA_DIRECTORY = REPO_ROOT / "schemas"

GENERATED_OBJECT_FILES = frozenset(path.name for path in OBJECT_DIRECTORY.glob("1*.yml"))
"""What `dataset-generate` produces, named from the committed tree."""

TASK_NAMES = frozenset(Collection.from_module(tasks).task_names)
DEFAULT_LISTING = frozenset(name for _, listed, _ in tasks.TASK_GROUPS for name in listed)


def _names_in_listing(output: str) -> set[str]:
    """The task names `invoke list` printed, read from the first table column."""
    return {name for name in TASK_NAMES if re.search(rf"^\s+{re.escape(name)}\s", output, re.MULTILINE)}


def _digest(directory: Path, pattern: str = "**/*") -> dict[str, str]:
    """Every matching file under a directory, mapped to the hash of its bytes."""
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob(pattern))
        if path.is_file()
    }


def _porcelain() -> str:
    """`git status --porcelain` in the repository."""
    return subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def exported_tree(tmp_path: Path) -> Iterator[Path]:
    """The committed tree, extracted where a task may write over it."""
    export = tmp_path / "export"
    export.mkdir()
    archive = subprocess.run(  # noqa: S603
        ["git", "archive", "--format=tar", "HEAD"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(export)], input=archive.stdout, check=True)  # noqa: S603, S607
    yield export


def test_list_names_the_readers_set_by_default_and_everything_with_all() -> None:
    """`list`: the default is the reader's set and `--all` is the whole collection."""
    listings = [
        task_output(run_task("list", arguments), f"list {arguments}") for arguments in RECORDS["list"].invocations
    ]
    default, everything = listings

    assert _names_in_listing(default) == set(DEFAULT_LISTING)
    assert _names_in_listing(everything) == set(TASK_NAMES)
    assert len(DEFAULT_LISTING) == 27
    assert len(TASK_NAMES) - len(DEFAULT_LISTING) == 21


def test_test_unit_runs_the_unit_suite_and_reports_no_failures() -> None:
    """`test-unit`: exits zero, and pytest's summary names no failure."""
    output = task_output(run_task("test-unit"), "test-unit")
    assert " passed" in output
    assert " failed" not in output
    assert " error" not in output


def test_format_leaves_an_already_formatted_tree_alone(exported_tree: Path) -> None:
    """`format`: run against an export it changes nothing, and the repository stays clean.

    The export borrows the repository's virtualenv through
    `UV_PROJECT_ENVIRONMENT` and `UV_NO_SYNC`. Without them `uv run` in a copied
    tree builds a second one from the network.
    """
    before = _porcelain()
    digest = _digest(exported_tree)

    task_output(
        run_task(
            "format",
            cwd=exported_tree,
            env={"UV_PROJECT_ENVIRONMENT": str(REPO_ROOT / ".venv"), "UV_NO_SYNC": "1"},
        ),
        "format",
    )

    # Compared over the exported file names only. Ruff writes a `.ruff_cache`
    # into whatever tree it runs in, and a new cache is not a formatting change.
    after = _digest(exported_tree)
    assert {name: value for name, value in after.items() if name in digest} == digest, (
        "formatting the committed tree changed it"
    )
    assert _porcelain() == before


def test_lint_passes_and_says_when_it_skipped_the_prose() -> None:
    """`lint`: exits zero, and names the prose step whenever `vale` is absent."""
    output = task_output(run_task("lint"), "lint")
    assert "every linter passed" in output
    if "except vale" in output:
        assert "Prose was not linted" in output


def test_schema_check_passes_against_the_committed_schemas() -> None:
    """`schema-check`: exits zero and writes nothing into `schemas/`."""
    digest = _digest(SCHEMA_DIRECTORY, "*.yml")
    task_output(run_task("schema-check"), "schema-check")
    assert _digest(SCHEMA_DIRECTORY, "*.yml") == digest


def test_dataset_generate_writes_the_generated_files_somewhere_else(tmp_path: Path) -> None:
    """`dataset-generate --output`: the same file names, and `objects/` untouched."""
    digest = _digest(OBJECT_DIRECTORY, "*.yml")
    destination = tmp_path / "dataset"
    task_output(run_task("dataset-generate", f"--output {destination}"), "dataset-generate")

    assert {path.name for path in destination.glob("*.yml")} == set(GENERATED_OBJECT_FILES)
    assert json.loads((destination / "geant_manifest.json").read_text())["OtnSite"] == 15
    assert _digest(OBJECT_DIRECTORY, "*.yml") == digest


def test_dataset_check_finds_the_committed_files_reproducible() -> None:
    """`dataset-check`: a fresh run of the seed reproduces `objects/1*.yml`."""
    output = task_output(run_task("dataset-check"), "dataset-check")
    assert "clean" in output


@pytest.mark.parametrize("case", ["network_map_golden", "odu_map_golden"])
def test_maps_regenerate_reproduces_the_golden_fixture_elsewhere(case: str, tmp_path: Path) -> None:
    """`maps-regenerate --case --output`: the render equals the committed fixture.

    Equality both ways. The fixture keeps the bytes it was committed with, and
    the render put beside it is those same bytes, which is what makes this
    evidence that the renderer ran rather than that a file exists.
    """
    fixture = FIXTURE_DIRECTORY / f"{case}.svg"
    committed = fixture.read_bytes()
    destination = tmp_path / case
    task_output(run_task("maps-regenerate", f"--case {case} --output {destination}"), f"maps-regenerate {case}")

    written = destination / f"{case}.svg"
    assert written.is_file()
    assert written.read_bytes() == committed
    assert fixture.read_bytes() == committed
