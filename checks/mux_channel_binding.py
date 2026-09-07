"""A mux client port binds exactly one channel, and a device agrees with its ports.

Judges every `OtnMuxDemux` against the ports it carries. A client port carries
one channel of the multiplexer, so it must name one and only one: neither
binding leaves the port standing for a wavelength nobody can name, and both
bindings claim a dense and a coarse wavelength on one filter slot.

The schema could take neither half. `OtnMuxClientPort` reaches a channel through
two optional relationships, `dwdm_channel` to `OtnFrequencyGrid` and
`cwdm_channel` to `OtnCwdmChannel`, because the dense and coarse plans are
separate kinds and Infrahub has no cross-relationship constraint. It can make one
relationship mandatory or leave both optional; it cannot say "exactly one of
these two". A shared channel generic would buy the constraint and reopen the
decision that keeps `OtnOpticalCarrier.channel` peering the dense grid alone.

The device side is the other half. A coarse multiplexer lists the wavelengths it
lights on `cwdm_channels`, so its client ports and that list must agree: a listed
wavelength with no port is a channel the device claims and cannot reach, and a
port naming a wavelength the device does not list is a filter slot for light that
never arrives.

Silent about dense bindings against any device-side list, because there is no
dense equivalent of `cwdm_channels`, and the count of multiplexers in that
position is reported as info. Silent about a mux line port binding nothing: it
carries every channel the device lights.
"""

from collections.abc import Iterable
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.plant import nodes_of, peer, peers

MUX = "OtnMuxDemux"

CLIENT_PORT = "OtnMuxClientPort"
LINE_PORT = "OtnMuxLinePort"


class MuxChannelBindingCheck(InfrahubCheck):
    query = "mux_channel_binding"

    def validate(self, data: dict[str, Any]) -> None:
        devices = 0
        client_ports = 0
        line_ports = 0
        dense_only = 0

        for device in nodes_of(data, MUX):
            devices += 1
            listed = _listed_wavelengths(device)
            if not listed:
                dense_only += 1
            bound: set[int] = set()

            for port in peers(device, "ports"):
                typename = str(port.get("__typename") or "")
                if typename == LINE_PORT:
                    line_ports += 1
                if typename != CLIENT_PORT:
                    continue
                client_ports += 1
                dense = _one(port, "dwdm_channel", "channel_number")
                coarse = _one(port, "cwdm_channel", "center_wavelength_nm")
                if dense is None and coarse is None:
                    self._unbound(device, port)
                elif dense is not None and coarse is not None:
                    self._double_bound(device, port, dense, coarse)
                if coarse is None:
                    continue
                bound.add(coarse)
                if coarse not in listed:
                    self._unlisted(device, port, coarse, listed)

            missing = sorted(listed - bound)
            if missing:
                self._uncovered(device, missing)

        self._summarise(devices, client_ports, line_ports, dense_only)

    def _unbound(self, device: dict[str, Any], port: dict[str, Any]) -> None:
        """A client port naming neither a dense channel nor a coarse one."""
        self.log_error(
            message=(
                f"{_name(device)} carries client port {_name(port)}, which binds neither a dense channel nor a "
                f"coarse wavelength. The port is one channel of the multiplexer and nothing says which, so no "
                f"query can tell what light it passes and no service can be traced through it"
            ),
            object_id=str(device.get("id", "")),
            object_type=str(device.get("__typename", "")),
        )

    def _double_bound(self, device: dict[str, Any], port: dict[str, Any], dense: int, coarse: int) -> None:
        """A client port on both plans at once."""
        self.log_error(
            message=(
                f"{_name(device)} carries client port {_name(port)}, which binds dense channel {dense} and "
                f"coarse wavelength {coarse} nm at the same time. One filter slot passes one wavelength, so one "
                f"of the two is wrong and the port's own record does not say which"
            ),
            object_id=str(device.get("id", "")),
            object_type=str(device.get("__typename", "")),
        )

    def _unlisted(self, device: dict[str, Any], port: dict[str, Any], coarse: int, listed: set[int]) -> None:
        """A coarse binding the device does not claim to light.

        Two sentences, because the empty list is the commoner half. `listed` is
        the reading `validate` already took rather than a second one, and a
        device with nothing in it is a dense unit: `_listed` renders an empty
        set as the word "nothing", which read as "not among the nothing the
        device lights".
        """
        if listed:
            detail = (
                f"which is not among the {_listed(f'{nm} nm' for nm in sorted(listed))} the device lights. "
                f"Either the filter was never fitted for it or the device's cwdm_channels is short"
            )
        else:
            detail = (
                "and the device lists no coarse wavelength at all, so it is a dense unit carrying a coarse "
                "binding. Either the port belongs on dwdm_channel or the device is missing its cwdm_channels"
            )
        self.log_error(
            message=f"{_name(device)} carries client port {_name(port)} on coarse wavelength {coarse} nm, {detail}",
            object_id=str(device.get("id", "")),
            object_type=str(device.get("__typename", "")),
        )

    def _uncovered(self, device: dict[str, Any], missing: list[int]) -> None:
        """A listed coarse wavelength with no client port behind it."""
        self.log_error(
            message=(
                f"{_name(device)} lights {_listed(f'{nm} nm' for nm in missing)} and carries no client port "
                f"bound to {'it' if len(missing) == 1 else 'them'}. The device claims a wavelength nothing on it "
                f"terminates, so a service groomed onto that wavelength has no port to land on"
            ),
            object_id=str(device.get("id", "")),
            object_type=str(device.get("__typename", "")),
        )

    def _summarise(self, devices: int, client_ports: int, line_ports: int, dense_only: int) -> None:
        """One INFO line stating what was judged and what was left unjudged."""
        if not devices:
            self.log_info(message="No multiplexer is on this branch, so no channel binding can be wrong.")
            return
        self.log_info(
            message=(
                f"{devices} multiplexer(s) examined, {client_ports} client port(s) judged for binding exactly one "
                f"channel and {line_ports} line port(s) skipped, because a line port carries every channel the "
                f"device lights and binds none by design. {dense_only} of the devices list no coarse wavelength, "
                f"so their client ports are judged for the binding alone: the graph holds no dense equivalent of "
                f"cwdm_channels to compare a dense binding against"
            )
        )


def _listed_wavelengths(device: dict[str, Any]) -> set[int]:
    """The coarse wavelengths the device says it lights."""
    return {
        int(channel["center_wavelength_nm"])
        for channel in peers(device, "cwdm_channels")
        if channel.get("center_wavelength_nm") is not None
    }


def _one(port: dict[str, Any], relationship: str, field: str) -> int | None:
    """One channel identifier off a cardinality-one relationship, or `None`."""
    try:
        value = peer(port, relationship).get(field)
    except ValueError:
        return None
    return int(value) if value is not None else None


def _name(record: dict[str, Any]) -> str:
    return str(record.get("name") or "an unnamed record")


def _listed(items: Iterable[str]) -> str:
    """`a`, `a and b`, `a, b and c`. Formatting only."""
    names = list(items)
    if len(names) < 2:
        return names[0] if names else "nothing"
    return f"{', '.join(names[:-1])} and {names[-1]}"
