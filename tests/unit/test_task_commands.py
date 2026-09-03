"""The three tasks that need no stack, called in process.

The code underneath two of them is covered elsewhere: the seed in
`test_geant_dataset.py`, the golden renderers in `test_mapdraw.py` and
`test_odudraw.py`. What is covered here is the wrapper around each, and in
particular the `--output` plumbing. Without it a demo command that a reader
runs to look at a render rewrites the committed tree instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

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
