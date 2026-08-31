"""Extract every requirement and acceptance claim from the feature specs.

A spec is written before the code exists, so every number in one is a target
rather than a measurement. Some of those targets moved, and the ones that moved
are exactly the ones nobody re-read. This is what makes them re-readable.

**Three shapes, and the third is the trap.** Most specs write requirements as
bullets and acceptance criteria as `Given` clauses. `009-audit-and-menu` writes
its requirements as a markdown table, and the first version of this extractor
reported it as having **zero** claims. A spec that yields nothing looks like a
spec with nothing to check, which is the silent failure this whole exercise is
about.

So the floor is not advisory: a prior spec yielding zero fails the run. There is
no spec in this repository with no requirements.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_GLOB = "specs/*/spec.md"

BULLET = re.compile(r"^-\s+\*\*(FR|SC|NFR|SP)-\d+")
TABLE = re.compile(r"^\|\s*(FR|SC|NFR|SP)-\d+\s*\|")
GIVEN = re.compile(r"\*\*Given\*\*")

EXPECTED = {
    "002-otn-ports-devices": 63,
    "003-otn-optical-plant": 86,
    "004-geant-data": 80,
    "005-budget-engine": 45,
    "006-provisioning": 65,
    "007-impact-capacity": 81,
    "008-developer-experience": 32,
    "009-audit-and-menu": 42,
    "010-network-topology-map": 47,
    "011-fiber-direction-raman": 76,
    "012-cwdm-infiniband": 89,
    "013-simplify": 95,
    "015-monitoring-ports": 121,
    "016-odu-map-grooming": 63,
    "017-oeo-cross-connect": 62,
}
"""Pinned so a regression in the extractor is visible, not only a zero.

Measured on 2026-08-28 against the fifteen specs that predate feature 019.
Specs 001 and 014 never existed. Total 1,047.
"""


def claims(path: Path) -> list[tuple[int, str, str]]:
    found = []
    for number, line in enumerate(path.read_text().splitlines(), 1):
        stripped = line.strip()
        if BULLET.match(stripped):
            found.append((number, "bullet", stripped))
        elif TABLE.match(stripped):
            found.append((number, "table", stripped))
        elif GIVEN.search(stripped):
            found.append((number, "given", stripped))
    return found


def main() -> int:
    total = 0
    empty = []
    shapes = {"bullet": 0, "table": 0, "given": 0}

    for path in sorted(REPO_ROOT.glob(SPEC_GLOB)):
        feature = path.parent.name
        if feature not in EXPECTED:
            continue
        found = claims(path)
        for _, shape, _ in found:
            shapes[shape] += 1
        total += len(found)
        expected = EXPECTED[feature]
        drift = "" if len(found) == expected else f"  DRIFT, expected {expected}"
        print(f"{len(found):>4}  {feature}{drift}")
        if not found:
            empty.append(feature)

    print(f"\n{total} claims across {len(EXPECTED)} prior specs")
    print(f"shapes: {shapes['bullet']} bullet, {shapes['given']} given, {shapes['table']} table")

    if empty:
        print(f"\nFAIL: {', '.join(empty)} yielded no claims. No spec here has no requirements,")
        print("so a zero is a broken extractor rather than a simple spec.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
