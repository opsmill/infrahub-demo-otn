"""A pluggable optic is in a port that can hold one, and no port holds two.

Reads every `OtnTransceiver` with the port it names. A module goes in a line, a
client or a router port, because those are the kinds with a cage; an amplifier
port, a ROADM degree or add/drop port and both multiplexer port kinds are fixed
optical interfaces on the equipment and take no module. The second rule is
plainer still: one cage holds one optic, so two units naming one port means one
of the two records is stale.

**The schema took neither half, and the check must not be narrowed on the
assumption that it did.** Both were tried. `uniqueness_constraints: [["port"]]`
is the native answer to a duplicate and Infrahub accepts it only where the
relationship is mandatory: with `optional: true` the load is refused with
"cannot use port relationship, relationship must be mandatory". Making `port`
mandatory buys the constraint and costs the ability to hold an unfitted optic,
which is a spare, an RMA and a decommissioned unit, and is most of what an optic
inventory is for. The constraint is available and it is the wrong trade.

The port kind is the other half, and it is refused for a different reason. A
reverse edge on `OtnOpticalPort` would let the schema carry the restriction,
except that a relationship to a generic cannot be filtered by peer kind, so the
field would land on all eight optical port kinds including the five that never
hold a module. The edge is declared on the transceiver alone and this check is
the only thing between a pluggable and an amplifier port.

Silent about a transceiver with no port, and the count of them is reported as
info. That unit is on a shelf, and a shelf is what the optional relationship
exists to model.

Silent about the cage. A part carries a `form_factor` and a port does not, so
"a QSFP-DD module in a QSFP-DD cage" is a comparison this model cannot make and
this check must not pretend to.
"""

from collections.abc import Iterable
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
        occupants: dict[str, list[dict[str, Any]]] = {}

        for unit in nodes_of(data, TRANSCEIVER):
            examined += 1
            port = _port(unit)
            if port is None:
                continue
            fitted += 1
            occupants.setdefault(str(port.get("id") or ""), []).append(unit)
            kind = str(port.get("__typename") or "")
            if kind not in CAGED_PORT_KINDS:
                self._wrong_kind(unit, port, kind)

        for units in occupants.values():
            if len(units) > 1:
                self._shared_port(units)

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

    def _shared_port(self, units: list[dict[str, Any]]) -> None:
        """Two records claiming one cage."""
        port = _port(units[0]) or {}
        self.log_error(
            message=(
                f"{_where(port)} holds {_listed(sorted(_serial(unit) for unit in units))} at once. One cage "
                f"takes one module, so at least one of these records is stale, and nothing on either of them "
                f"says which. No uniqueness constraint can refuse this: Infrahub accepts one on the port "
                f"relationship only where the relationship is mandatory, and a mandatory port leaves a spare "
                f"unmodellable"
            ),
            object_id=str(units[0].get("id", "")),
            object_type=str(units[0].get("__typename", "")),
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
                f"decommissioned module, and holding one is why the port relationship is optional. That "
                f"optionality is also why the duplicate rule is here rather than in the schema"
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


def _listed(items: Iterable[str]) -> str:
    """`a`, `a and b`, `a, b and c`. Formatting only."""
    names = list(items)
    if len(names) < 2:
        return names[0] if names else "nothing"
    return f"{', '.join(names[:-1])} and {names[-1]}"
