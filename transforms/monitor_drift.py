"""What the plant was configured to do, against what it last reported doing.

Every other report here predicts. This one compares, which is the question a
source of truth is uniquely placed to answer: it is the only system holding both
halves.
"""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.drift import TOLERANCE_MDB, Drift, gain_drift
from infrahub_demo_otn.plant import nodes_of, peers


def _with_reading(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold each device's monitor reading onto the device record.

    A device may carry several ports and only one of them is a monitor, so the
    reading is whichever port actually has one.
    """
    folded = []
    for record in records:
        reading = next(
            (port["measured_gain_mdb"] for port in peers(record, "ports") if port.get("measured_gain_mdb") is not None),
            None,
        )
        folded.append({**record, "measured_gain_mdb": reading})
    return folded


class MonitorDriftTransform(InfrahubTransform):
    query = "monitor_drift"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        drifts: list[Drift] = []
        drifts += gain_drift(_with_reading(list(nodes_of(data, "OtnAmplifier"))), "gain_mdb", "amplifier")
        drifts += gain_drift(_with_reading(list(nodes_of(data, "OtnRamanPump"))), "on_off_gain_mdb", "raman_pump")

        beyond = [d for d in drifts if d.beyond_tolerance]
        return {
            "branch": self.branch_name,
            "tolerance_db": TOLERANCE_MDB / 1000,
            "compared": len(drifts),
            "beyond_tolerance": len(beyond),
            "worst_shortfall_db": max((d.shortfall_mdb for d in drifts), default=0) / 1000,
            "stages": [
                {
                    "device": d.device,
                    "kind": d.kind,
                    "configured_db": d.configured_mdb / 1000,
                    "measured_db": d.measured_mdb / 1000,
                    "shortfall_db": d.shortfall_mdb / 1000,
                }
                for d in beyond
            ],
        }
