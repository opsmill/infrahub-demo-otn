"""Configured intent against what the equipment reports.

Every other report in this repository predicts. This one compares. It is the
question a source of truth is uniquely placed to answer, because it is the only
system that holds both halves: what the network was configured to do, and what
it last said it was doing.

The comparison is against configured intent rather than against a provisioned
path, deliberately. A path comparison needs a service, and the shipped network
carries none until the walkthrough provisions one, so a report that needed a
path would have nothing to say about the network as it ships.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

TOLERANCE_MDB = 1000
"""How far a stage may sit under its configured gain before it is worth naming.
Wide enough that the ordinary droop of an ageing pump is not a finding, narrow
enough that a stage heading for a maintenance visit is."""


@dataclass(frozen=True)
class Drift:
    """One device whose measurement disagrees with its configuration."""

    device: str
    kind: str
    configured_mdb: int
    measured_mdb: int

    @property
    def shortfall_mdb(self) -> int:
        return self.configured_mdb - self.measured_mdb

    @property
    def beyond_tolerance(self) -> bool:
        return self.shortfall_mdb > TOLERANCE_MDB


def gain_drift(devices: Iterable[Mapping[str, Any]], configured_field: str, kind: str) -> list[Drift]:
    """Compare each device's configured gain against its monitor's reading.

    A device with no monitor is skipped rather than reported as drifting.
    Reporting it here as a shortfall would be inventing a measurement of zero.
    Nothing else ever covered that case either: the completeness check this
    repository used to carry iterated over monitors, so a device with no monitor
    was invisible to it.

    A monitor that exists always carries a reading now. `measured_gain_mdb` is
    mandatory on the two kinds that declare it, and the query names those kinds,
    so a matched fragment cannot hand back a null. The `measured is None` branch
    below survives for the no-monitor case alone.
    """
    drifts = []
    for record in devices:
        measured = record.get("measured_gain_mdb")
        configured = record.get(configured_field)
        if measured is None or configured is None:
            continue
        drifts.append(
            Drift(
                device=str(record.get("name", "<unnamed>")),
                kind=kind,
                configured_mdb=int(configured),
                measured_mdb=int(measured),
            )
        )
    return sorted(drifts, key=lambda d: (-d.shortfall_mdb, d.device))
