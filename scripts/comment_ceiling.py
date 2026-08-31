"""Report every file whose longest run of comment lines exceeds the ceiling.

A comment earns its length. This does not judge whether one does; it finds the
places worth looking at, which is a different and cheaper job.

**Why 12.** Chosen from the existing distribution rather than as a round number.
The short comments in this repository cluster well under it and the long runs are
conspicuous: when this was first run, 30 files were over, the worst at 39 lines,
and the shortest offender at 13. Twelve separates those two populations.

**Why a run and not a density.** Overall density here is nine percent, which is
unremarkable, and lowering it would be a project with no benefit. The cost a
reader actually pays is scrolling past an essay to reach the attribute they came
for, and that cost is a property of the longest run in a file.

**The escape hatch is deliberate.** A file may exceed the ceiling when it says
why, in a line matching `CEILING:`. Some comments are load bearing: a measured
figure, a platform trap, a decision reversed after being measured wrong. Those
are the comments that stopped a bug recurring, and a script that forced them
shorter would be removing the value and keeping the cost.

Run it with no arguments to list the offenders, or `--check` to exit non-zero
when an unexcused one exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

CEILING = 12
"""Longest permitted run of consecutive comment lines."""

EXCUSE = "CEILING:"
"""A file over the ceiling states its reason in a comment containing this."""

REPO_ROOT = Path(__file__).resolve().parents[1]

SEARCH = (
    "src/infrahub_demo_otn",
    "checks",
    "transforms",
    "generators",
    "schemas",
    "queries",
    "objects",
)

SUFFIXES = (".py", ".yml", ".gql")


def longest_run(path: Path) -> tuple[int, int]:
    """The longest run of comment lines in a file, and the line it starts on."""
    best = best_at = current = start = 0
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.strip().startswith("#"):
            if current == 0:
                start = number
            current += 1
            if current > best:
                best, best_at = current, start
        else:
            current = 0
    return best, best_at


def excused(path: Path) -> bool:
    return EXCUSE in path.read_text()


def offenders() -> list[tuple[int, int, Path, bool]]:
    found = []
    for directory in SEARCH:
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if path.suffix not in SUFFIXES:
                continue
            run, at = longest_run(path)
            if run > CEILING:
                found.append((run, at, path.relative_to(REPO_ROOT), excused(path)))
    return sorted(found, reverse=True)


def main() -> int:
    found = offenders()
    unexcused = [item for item in found if not item[3]]

    for run, at, path, is_excused in found:
        mark = "excused" if is_excused else ""
        print(f"{run:>4} lines at L{at:<5} {path}  {mark}")

    print(f"\n{len(found)} file(s) over the {CEILING}-line ceiling, {len(unexcused)} of them unexcused")

    if "--check" in sys.argv and unexcused:
        print(f"\nEach one either loses the run or states its reason in a comment containing {EXCUSE!r}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
