"""Proves the task worker can import the shared package.

Every check, generator and transform in this repository imports
infrahub_demo_otn. If the worker image loses the package, this check fails with
a clear message instead of every downstream artifact failing with an opaque
ModuleNotFoundError.

The import at the top of this file is the whole probe. An import that succeeds
is the claim, and the `log_info` is what makes it visible in a pipeline run.

It reads no data. `queries/units_import.gql` exists only because InfrahubCheck
requires `query` to name a registered query.
"""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn import units


class UnitsImportCheck(InfrahubCheck):
    query = "units_import"

    def validate(self, data: dict[str, Any]) -> None:
        self.log_info(
            message=(
                f"infrahub_demo_otn imports in the worker: units.py publishes "
                f"{units.MDB_PER_DB} millidecibels per decibel and a {units.GRID_CHANNEL_COUNT}-channel grid"
            )
        )
