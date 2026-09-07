"""A variable attenuator is not dialled past the range its hardware has.

Reads `attenuation_mdb` and `max_attenuation_mdb` off every
`OtnVariableAttenuator` and fails where the setting is the larger of the two.
The bound is inclusive: a VOA sitting exactly at its maximum is still a setting
the device can hold, so equality passes.

**The schema already owns the absolute range, and this check must not be
widened back over it.** `min_value: 0` and `max_value: 30000` on
`attenuation_mdb` refuse a physically impossible figure at write time, from
every direction. What the schema cannot say is "not more than this device's own
maximum", because that bound is a sibling attribute's value and Infrahub has no
cross-attribute constraint. That one comparison is all that is left here.

Silent about `OtnFixedAttenuator`, and the query does not fetch it: a pad has no
maximum to be past, and the two kinds were split so the field would not exist
where it means nothing.

Silent about the loss an attenuator contributes, which is
`insertion_loss_mdb + attenuation_mdb` and is the budget engine's business.
`max_attenuation_mdb` is range and never appears in that sum.
"""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.plant import nodes_of
from infrahub_demo_otn.units import mdb_to_db

VARIABLE_ATTENUATOR = "OtnVariableAttenuator"


class AttenuatorRangeCheck(InfrahubCheck):
    query = "attenuator_range"

    def validate(self, data: dict[str, Any]) -> None:
        examined = 0
        at_the_stop = 0

        for device in nodes_of(data, VARIABLE_ATTENUATOR):
            examined += 1
            setting = _reading(device, "attenuation_mdb")
            maximum = _reading(device, "max_attenuation_mdb")
            if setting is None or maximum is None:
                self._unreadable(device)
                continue
            if setting > maximum:
                self._over_range(device, setting, maximum)
            elif setting == maximum:
                at_the_stop += 1

        self._summarise(examined, at_the_stop)

    def _over_range(self, device: dict[str, Any], setting: int, maximum: int) -> None:
        """A VOA asked for more attenuation than it can produce."""
        self.log_error(
            message=(
                f"{_name(device)} is set to {mdb_to_db(setting):.3f} dB and can produce "
                f"{mdb_to_db(maximum):.3f} dB. The device will sit at its stop instead, so the plant delivers "
                f"{mdb_to_db(setting - maximum):.3f} dB more power than this record says it does and every "
                f"budget computed from it is optimistic by that much"
            ),
            object_id=str(device.get("id", "")),
            object_type=str(device.get("__typename", "")),
        )

    def _unreadable(self, device: dict[str, Any]) -> None:
        """One of the two figures came back empty.

        Both attributes are mandatory, so this is unreachable against a loaded
        branch. It is reported rather than skipped because a device the check
        could not read is not a device it passed.
        """
        self.log_error(
            message=(
                f"{_name(device)} does not carry both a setting and a maximum, so its range cannot be judged. "
                f"Both attributes are mandatory on this kind, so a record missing one arrived by a path that "
                f"did not go through the schema"
            ),
            object_id=str(device.get("id", "")),
            object_type=str(device.get("__typename", "")),
        )

    def _summarise(self, examined: int, at_the_stop: int) -> None:
        """One INFO line stating what was judged and what was left alone."""
        if not examined:
            self.log_info(message="No variable attenuator is on this branch, so no setting can be out of range.")
            return
        self.log_info(
            message=(
                f"{examined} variable attenuator(s) judged against their own maximum, {at_the_stop} of them "
                f"dialled to exactly that maximum and passing, because the bound is inclusive. Fixed pads are "
                f"not judged here and carry no range to judge: the absolute 0 to 30 dB limit is a schema "
                f"constraint on both kinds and is refused at write time"
            )
        )


def _reading(device: dict[str, Any], field: str) -> int | None:
    """One millidecibel figure off the node, or `None` where it is absent."""
    value = device.get(field)
    return None if value is None else int(value)


def _name(device: dict[str, Any]) -> str:
    return str(device.get("name") or "an unnamed attenuator")
