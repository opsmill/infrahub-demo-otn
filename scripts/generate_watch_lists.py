#!/usr/bin/env python
"""Derive the `watch.files` list of every definition in `.infrahub.yml`.

Infrahub never analyses Python imports. A definition that declares nothing has
its fingerprint tied to the repository's current commit, so every commit
re-fingerprints it, and a definition that declares the whole package
re-fingerprints on a change to any of the fourteen modules. Eleven definitions
did the second, so a change to `odudraw.py` re-rendered the network map.

What is written here is the transitive import closure of each definition over
the package, and nothing else. The closure is measured from the AST: the
definition's own imports of `infrahub_demo_otn`, then the package's imports of
itself, walked until it stops growing. `__init__.py` joins every non-empty
closure because importing a submodule runs it.

Run with no arguments to rewrite `.infrahub.yml` in place. Run with `--check` to
derive and diff, writing nothing and exiting non-zero on any difference.
`tests/unit/test_repository_config.py` runs `--check`, so a hand edit to a watch
list, or a new import that widens a closure, fails on the next test run. A
hand-maintained list is refused on purpose: it is right the day it is written and
silently wrong afterwards, and the symptom is an artifact that renders cleanly
from code that is not on the branch.

The walk is static, so it is only sound while every import is static. The scan
below runs first on every derivation and stops the whole run on an `importlib`,
an `__import__`, an `exec` or an `eval` anywhere under `src/`, `transforms/`,
`generators/` or `checks/`. A module reached that way is invisible here, and a
watch list that misses it is the stale-artifact failure this script exists to
avoid. Failing is the answer, not narrowing what is left.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import deque
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = REPO_ROOT / ".infrahub.yml"

PACKAGE = "infrahub_demo_otn"
PACKAGE_DIR = REPO_ROOT / "src" / PACKAGE
PACKAGE_WATCH = f"src/{PACKAGE}/"
"""The whole-folder declaration, kept only by an exempt definition."""

WATCHABLE_SECTIONS = ("generator_definitions", "jinja2_transforms", "python_transforms")
"""The three sections the repository config model accepts a `watch` key on.

`check_definitions` is not one of them. The model has no such field there and
uses `extra="forbid"`, so a `watch` on a check fails the whole file at sync.
"""

WHOLE_PACKAGE_EXEMPT = frozenset({"units_import"})
"""Definitions whose closure is the package by design, not by omission.

`units_import` exists to prove the task worker can import the shared package at
all, so the package is what it depends on and narrowing it would defeat it. It
is registered as a `check_definition` today, which carries no `watch` key, so the
exemption changes nothing in the file as it stands. It is named anyway, because
the day that probe moves to a section that can watch, the whole-folder form is
the correct one for it and nothing else in this script would say so.
"""

DYNAMIC_NAMES = frozenset({"__import__", "exec", "eval"})
SCANNED_DIRECTORIES = ("src", "transforms", "generators", "checks")


class DynamicImportFound(Exception):
    """A module reaches code by a route the static walk cannot follow."""


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py"))


def dynamic_import_sites(tree: ast.Module) -> list[str]:
    """Report every construct that reaches a module without a visible import.

    `tests/` is not scanned and is in no definition's closure. It loads modules
    by path with `importlib` freely, and that is a test loading a file rather
    than the package importing itself.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name.split(".")[0] == "importlib"
            ]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "importlib":
            found.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in DYNAMIC_NAMES:
            found.append(f"line {node.lineno}: {node.func.id}(...)")
    return found


def scan_for_dynamic_imports() -> None:
    """Stop the derivation dead if any import is invisible to the AST.

    Raising here rather than dropping the affected definition is the point. A
    partial narrowing hides the one thing the scan found, and the definition it
    silently under-reports is the one that goes stale.
    """
    offences: list[str] = []
    for name in SCANNED_DIRECTORIES:
        for path in python_files(REPO_ROOT / name):
            offences += [f"{path.relative_to(REPO_ROOT)}, {site}" for site in dynamic_import_sites(_parse(path))]
    if offences:
        raise DynamicImportFound(
            "a static walk cannot see every import, so no watch list may be narrowed:\n  " + "\n  ".join(offences)
        )


# ---------------------------------------------------------------------------
# The import graph.
# ---------------------------------------------------------------------------
def package_modules() -> dict[str, Path]:
    """Every module of the shared package, keyed by its name within it."""
    return {path.stem: path for path in python_files(PACKAGE_DIR)}


def package_imports(tree: ast.Module) -> set[str]:
    """The package modules a single file imports directly.

    Both spellings count, `import infrahub_demo_otn.units` and
    `from infrahub_demo_otn import units`, and so does an import inside a
    function body: `ast.walk` covers the whole tree, not the top level.
    """
    modules = package_modules()
    reached: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == PACKAGE and len(parts) > 1:
                    reached.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            parts = (node.module or "").split(".")
            if parts[0] != PACKAGE:
                continue
            if len(parts) > 1:
                reached.add(parts[1])
            else:
                # `from infrahub_demo_otn import units, budget`: the names are
                # modules where a module of that name exists, and package
                # attributes otherwise.
                reached |= {alias.name for alias in node.names if alias.name in modules}
    return {name for name in reached if name in modules}


def closure(entry_file: Path) -> list[str]:
    """The package modules one definition can reach, transitively."""
    modules = package_modules()
    seen: set[str] = set()
    queue = deque(package_imports(_parse(entry_file)))
    while queue:
        name = queue.popleft()
        if name in seen:
            continue
        seen.add(name)
        queue.extend(package_imports(_parse(modules[name])) - seen)
    if not seen:
        return []
    # Importing any submodule runs the package's `__init__.py` first, so it is
    # part of every closure that reaches the package at all.
    seen.add("__init__")
    return sorted(str(modules[name].relative_to(REPO_ROOT)) for name in seen)


# ---------------------------------------------------------------------------
# The lists, and the file they are written into.
# ---------------------------------------------------------------------------
def watch_lists() -> dict[tuple[str, str], list[str]]:
    """Derive one list per watchable definition, keyed by section and name."""
    scan_for_dynamic_imports()
    config = yaml.safe_load(CONFIG_FILE.read_text())
    derived: dict[tuple[str, str], list[str]] = {}
    for section in WATCHABLE_SECTIONS:
        for entry in config.get(section) or []:
            name = entry["name"]
            if name in WHOLE_PACKAGE_EXEMPT:
                derived[(section, name)] = [PACKAGE_WATCH]
                continue
            files = closure(REPO_ROOT / str(entry["file_path"]).removeprefix("./"))
            if not files:
                raise DynamicImportFound(
                    f"{section}.{name} imports nothing from {PACKAGE}, and an empty list is a positive "
                    "claim of no dependencies. Decide by hand what it watches."
                )
            derived[(section, name)] = files
    return derived


def rewrite(text: str, derived: dict[tuple[str, str], list[str]]) -> str:
    """Replace the items under each `watch: files:` block, comments intact.

    Line surgery rather than a YAML round trip. `.infrahub.yml` is more comment
    than configuration, and every dumper in reach either drops those comments or
    reflows the file into a diff nobody can read.
    """
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    section = ""
    name = ""
    written: set[tuple[str, str]] = set()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.rstrip("\n")
        if stripped and not stripped[0].isspace() and stripped.endswith(":"):
            section = stripped[:-1]
            name = ""
        elif stripped.startswith("  - name: "):
            name = stripped.removeprefix("  - name: ").strip()
        if stripped == "    watch:" and (section, name) in derived:
            if lines[index + 1].rstrip("\n") != "      files:":
                raise ValueError(f"{section}.{name}: expected `files:` under `watch:`")
            output += [line, lines[index + 1]]
            output += [f"        - {path}\n" for path in derived[(section, name)]]
            written.add((section, name))
            index += 2
            while index < len(lines) and lines[index].startswith("        - "):
                index += 1
            continue
        output.append(line)
        index += 1
    missing = sorted(key for key in derived if key not in written)
    if missing:
        raise ValueError(f"no `watch:` block to rewrite for: {missing}")
    return "".join(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="derive and diff, writing nothing")
    arguments = parser.parse_args(argv)

    committed = CONFIG_FILE.read_text()
    try:
        fresh = rewrite(committed, watch_lists())
    except (DynamicImportFound, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1

    if not arguments.check:
        CONFIG_FILE.write_text(fresh)
        print(f"wrote {len(watch_lists())} watch lists into {CONFIG_FILE.name}")
        return 0

    if fresh != committed:
        print(
            f"the watch lists in {CONFIG_FILE.name} do not match a fresh derivation. "
            f"Run `uv run python scripts/generate_watch_lists.py` and commit the result.",
            file=sys.stderr,
        )
        return 1
    print(f"clean: every watch list in {CONFIG_FILE.name} matches a fresh derivation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
