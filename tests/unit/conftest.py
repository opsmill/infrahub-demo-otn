"""Shared readers for the schema YAML and the object YAML.

What earns a test here: a test earns its place if a change to a file in this
repository can make it fail, and a change to Infrahub alone cannot. So the
suite tests the schema, the objects, the menu, the checks, the generators, the
transforms and the shared package. It does not test that Infrahub stores what
it was told to store, that a version pin is pinned, or that another test would
fail if it were wrong. The one deliberate exception is in
`tests/integration/test_infrahub.py` and its docstring says so.

Two modules now read `objects/`: `test_schema_contract.py`, which guards the
project's own conventions, and `test_geant_dataset.py`, which recomputes the
design's numeric claims from the generated dataset. They ask different
questions of the same bytes, and two copies of a YAML loader is how one of them
starts globbing a different directory.

`demo/` is read by one module, `test_demo_scenarios.py`, and through a reader
that takes a file name rather than a glob. The scenarios there are alternative
branches and not one state, so merging them would produce a network nobody ever
loads.

These are plain cached functions rather than pytest fixtures on purpose. Most
callers need them inside a comprehension or a helper, where a fixture argument
would have to be threaded through three layers to reach the place it is used.

The cache is not an optimisation, it is what makes the dataset tests runnable.
`objects/` is 11,500 lines and `test_geant_dataset.py` enumerates simple paths
between all 91 site pairs; re-parsing per call turns a half-second module into
one that does not finish. Callers must treat the returned tuples as read-only.
"""

from functools import cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"
OBJECT_DIR = REPO_ROOT / "objects"
SCRIPT_DIR = REPO_ROOT / "scripts"
DEMO_DIR = REPO_ROOT / "demo"
DOC_DIR = REPO_ROOT / "docs" / "docs" / "demo-otn"


@cache
def doc_text(name: str) -> str:
    """One documentation page, as text, for `test_doc_claims.py` to read figures out of.

    The other readers here parse a format. This one deliberately does not: the
    pages are prose, and the only thing a test wants from them is the number a
    reader will see. Handing back the raw text keeps the extraction in the test
    that owns the claim, where the regex sits next to the sentence it matches.
    """
    return (DOC_DIR / name).read_text()


def schema_files() -> list[Path]:
    """Every schema file, sorted. An empty result makes its callers vacuous."""
    return sorted(SCHEMA_DIR.glob("*.yml"))


def object_files() -> list[Path]:
    """Every object file, sorted. Sorted order is also the load order, which is
    why the file names carry numeric prefixes."""
    return sorted(OBJECT_DIR.glob("*.yml"))


@cache
def object_documents() -> tuple[dict[str, Any], ...]:
    """Every YAML document in every object file.

    Object files may hold several documents separated by `---`; the generated
    device and port files hold one per kind.
    """
    documents: list[dict[str, Any]] = []
    for path in object_files():
        for parsed in yaml.safe_load_all(path.read_text()):
            if isinstance(parsed, dict):
                documents.append(parsed)
    return tuple(documents)


@cache
def objects_of_kind(kind: str) -> tuple[dict[str, Any], ...]:
    """Every object record declared for one `spec.kind`, across all files."""
    collected: list[dict[str, Any]] = []
    for document in object_documents():
        spec = document.get("spec") or {}
        if spec.get("kind") != kind:
            continue
        for entry in spec.get("data") or []:
            if isinstance(entry, dict):
                collected.append(entry)
    return tuple(collected)


@cache
def demo_objects_of_kind(file_name: str, kind: str) -> tuple[dict[str, Any], ...]:
    """Every record of one `spec.kind` in one file under `demo/`.

    Named per file rather than globbed, and that is the point of it living here
    beside `objects_of_kind` rather than as a second loader in a test module.
    `demo/` is a set of independent scenarios, several of which contradict each
    other on purpose: `06_mad_waw_16qam.yml` and `07_mad_waw_qpsk.yml` describe
    two branches, not one state, so a reader that merged every file would be
    asserting against a network that never exists. A caller names the file it
    means.
    """
    collected: list[dict[str, Any]] = []
    for parsed in yaml.safe_load_all((DEMO_DIR / file_name).read_text()):
        if not isinstance(parsed, dict):
            continue
        spec = parsed.get("spec") or {}
        if spec.get("kind") != kind:
            continue
        for entry in spec.get("data") or []:
            if isinstance(entry, dict):
                collected.append(entry)
    return tuple(collected)
