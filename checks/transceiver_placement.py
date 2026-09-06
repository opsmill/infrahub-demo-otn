"""A pluggable optic is in a port that can hold one.

Reads every `OtnTransceiver` with the port it names. A module goes in a line, a
client or a router port, because those are the kinds with a cage; an amplifier
port, a ROADM degree or add/drop port and both multiplexer port kinds are fixed
optical interfaces on the equipment and take no module.

**The schema took the other half, two modules in one port, and this check must
not be widened back over it.** `OtnLinePort`, `OtnClientPort` and
`OtnRouterPort` each declare a cardinality-one `transceiver` on the
`otn_optical_port__transceiver` identifier, which is the identifier
`OtnTransceiver.port` already used. Both ends of the edge are cardinality one,
so the server refuses the second write: "has 2 peers for
otn_optical_port__transceiver, maximum of 1 allowed". A check half that can
never fail is worse than no check, because a green result reads as evidence.

The port kind is what the schema cannot take, and it is all this check owns.
`OtnTransceiver.port` peers the generic `OtnOpticalPort`, a relationship to a
generic cannot be filtered by peer kind, and a module written into an amplifier
port or a ROADM degree port is accepted.

Silent about a transceiver with no port, and the count of them is reported as
info. That unit is on a shelf, and a shelf is what the optional relationship
exists to model.

Silent about the cage. A part carries a `form_factor` and a port does not, so
"a QSFP-DD module in a QSFP-DD cage" is a comparison this model cannot make and
this check must not pretend to.
"""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.plant import nodes_of, peer

TRANSCEIVER = "OtnTransceiver"

CAGED_PORT_KINDS = ("OtnLinePort", "OtnClientPort", "OtnRouterPort")
"""The port kinds a pluggable optic can be fitted in.

A line port is the coloured side of a transponder or of a router running a
coloured pluggable, a client port is the grey side of a transponder and a router
port is the grey side of a router. The other five optical port kinds are fixed
interfaces on the equipment.
"""


class TransceiverPlacementCheck(InfrahubCheck):
    query = "transceiver_placement"

    def validate(self, data: dict[str, Any]) -> None:
        examined = 0
        fitted = 0

        for unit in nodes_of(data, TRANSCEIVER):
            examined += 1
            port = _port(unit)
            if port is None:
                continue
            fitted += 1
            kind = str(port.get("__typename") or "")
            if kind not in CAGED_PORT_KINDS:
                self._wrong_kind(unit, port, kind)

        self._summarise(examined, fitted)

    def _wrong_kind(self, unit: dict[str, Any], port: dict[str, Any], kind: str) -> None:
        """A module recorded in a port with no cage to take it."""
        self.log_error(
            message=(
                f"{_part(unit)} {_serial(unit)} is fitted in {_where(port)}, a port of kind {kind}. A pluggable "
                f"optic goes in a line, a client or a router port, and no other port kind has a cage to take "
                f"one, so either this unit is somewhere else or the port on its record is wrong"
            ),
            object_id=str(unit.get("id", "")),
            object_type=str(unit.get("__typename", "")),
        )

    def _summarise(self, examined: int, fitted: int) -> None:
        """One INFO line stating what was judged and what was left alone."""
        if not examined:
            self.log_info(message="No transceiver is on this branch, so no optic can be in the wrong port.")
            return
        self.log_info(
            message=(
                f"{examined} transceiver(s) examined, {fitted} of them fitted in a port and judged, "
                f"{examined - fitted} on a shelf and not judged. An unfitted unit is a spare, an RMA or a "
                f"decommissioned module, and holding one is why the port relationship is optional. Two "
                f"modules in one port is not judged here: the schema refuses that write"
            )
        )


def _port(unit: dict[str, Any]) -> dict[str, Any] | None:
    """The port a unit is fitted in, or `None` where it is on a shelf."""
    try:
        return peer(unit, "port")
    except ValueError:
        return None


def _where(port: dict[str, Any]) -> str:
    """`device port`, from the fragment the port's own kind carried."""
    try:
        device = str(peer(port, "device").get("name") or "an unnamed device")
    except ValueError:
        device = "an unnamed device"
    return f"{device} {port.get('name') or 'an unnamed port'}"


def _part(unit: dict[str, Any]) -> str:
    try:
        return str(peer(unit, "type").get("part_number") or "an unnamed part")
    except ValueError:
        return "an unnamed part"


def _serial(unit: dict[str, Any]) -> str:
    return str(unit.get("serial") or "an unserialled unit")
