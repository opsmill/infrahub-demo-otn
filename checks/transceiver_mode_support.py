"""A wavelength runs a mode the optics fitted at both of its ends can produce.

Reads every `OtnOpticalCarrier`, the mode it runs and the line ports terminating
it, and holds each pluggable fitted at one of those ports against its own part's
`supported_modes`. A part that does not list the mode cannot run it.

**The one fact that separates two modules.** A 400ZR and an OpenZR+ 400G part
are the same QSFP-DD cage, the same DP-16QAM constellation and the same 400G
line rate. They differ in forward error correction, cFEC against oFEC, and
therefore in reach, 120 km against 1000 km. Nothing on the outside of either
module says which one is in your hand, so a finding names the part number and
the mode.

**The schema took none of this, and could not.** `supported_modes` is a
cardinality-many relationship on the part and the mode is a cardinality-one
relationship on the carrier, two hops apart through a port, and Infrahub has no
cross-relationship constraint.

**Silent about a carrier whose line ports hold no pluggable, and the number of
them is reported as info.** That is a transponder with integrated optics: no
part number to compare anything against. On the shipped dataset that is the
forty transponder wavelengths against the three run straight out of a router,
and a run reporting no findings and forty-three skips has seen nothing.

Reports one end fitted and the other not as info rather than as an error. That
is unusual and is not wrong, and the two ends are judged on their own terms.

Silent about reach, dispersion and OSNR. Whether the mode closes over the plant
is what `checks/osnr_margin.py` decides; this check asks only whether the part
in the cage can produce the mode at all.
"""

from collections.abc import Iterable
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.plant import nodes_of, peer, peers

CARRIER = "OtnOpticalCarrier"
TRANSCEIVER = "OtnTransceiver"


class TransceiverModeSupportCheck(InfrahubCheck):
    query = "transceiver_mode_support"

    def validate(self, data: dict[str, Any]) -> None:
        fitted = _fitted_by_port(data)
        examined = 0
        judged = 0
        skipped = 0
        unmoded = 0

        for carrier in nodes_of(data, CARRIER):
            examined += 1
            occupied = [
                (port, unit)
                for port in peers(carrier, "line_ports")
                for unit in fitted.get(str(port.get("id") or ""), [])
            ]
            if not occupied:
                skipped += 1
                continue

            # Before the mode is read, because a mixed termination is true
            # either way. Reported after it, a carrier declaring no mode would
            # leave this side of the check silent about an end that is fitted
            # and an end that is not.
            bare = [port for port in peers(carrier, "line_ports") if not fitted.get(str(port.get("id") or ""))]
            if bare:
                self._half_fitted(carrier, occupied, bare)

            mode = _mode(carrier)
            if mode is None:
                unmoded += 1
                self._no_mode(carrier, occupied)
                continue

            judged += 1
            for port, unit in occupied:
                supported = _supported_modes(unit)
                if mode not in supported:
                    self._unsupported(carrier, mode, port, unit, supported)

        self._summarise(examined, judged, skipped, unmoded)

    def _unsupported(
        self,
        carrier: dict[str, Any],
        mode: str,
        port: dict[str, Any],
        unit: dict[str, Any],
        supported: list[str],
    ) -> None:
        """A part in the cage that cannot produce the mode the wavelength runs."""
        self.log_error(
            message=(
                f"{_name(carrier)} runs {mode} and {_where(port)} holds {_part(unit)} {_serial(unit)}, which "
                f"supports {_listed(supported)}. The part does not list {mode}, so the wavelength is planned "
                f"on an optic that cannot produce it. Cage, constellation and line rate say nothing here: the "
                f"part number and the mode are the whole of the difference"
            ),
            object_id=str(carrier.get("id", "")),
            object_type=str(carrier.get("__typename", "")),
        )

    def _no_mode(self, carrier: dict[str, Any], occupied: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        """A pluggable fitted for a wavelength that says nothing about what it runs.

        `optical_mode` is optional on the carrier, so this is reachable, though
        no shipped wavelength is in this state. It is an error rather than a
        skip because a carrier the check could not judge is not a carrier it
        passed.
        """
        ends = _listed(sorted(_where(port) for port, _ in occupied))
        self.log_error(
            message=(
                f"{_name(carrier)} declares no optical mode and {ends} holds a pluggable for it. Nothing says "
                f"what the optic has to produce, so the fit cannot be judged in either direction"
            ),
            object_id=str(carrier.get("id", "")),
            object_type=str(carrier.get("__typename", "")),
        )

    def _half_fitted(
        self,
        carrier: dict[str, Any],
        occupied: list[tuple[dict[str, Any], dict[str, Any]]],
        bare: list[dict[str, Any]],
    ) -> None:
        """One end on a pluggable and the other on integrated optics."""
        self.log_info(
            message=(
                f"{_name(carrier)} is terminated by a pluggable at "
                f"{_listed(sorted(_where(port) for port, _ in occupied))} and by integrated optics at "
                f"{_listed(sorted(_where(port) for port in bare))}. The fitted end is judged against its own "
                f"part and the other end carries no part number to judge, which is a mixed termination rather "
                f"than a fault"
            ),
            object_id=str(carrier.get("id", "")),
            object_type=str(carrier.get("__typename", "")),
        )

    def _summarise(self, examined: int, judged: int, skipped: int, unmoded: int) -> None:
        """One INFO line stating what was judged and what was left unjudged.

        The three figures sum to the number examined. That is why a carrier
        declaring no mode is counted here rather than only reported as an
        error: a summary whose parts do not add up sends a reader looking for
        a row the check never names.
        """
        if not examined:
            self.log_info(message="No wavelength is on this branch, so no optic can be fitted for the wrong mode.")
            return
        self.log_info(
            message=(
                f"{examined} wavelength(s) examined, {judged} judged against the parts fitted at their line "
                f"ports, {skipped} skipped for holding no pluggable at either end and {unmoded} left unjudged "
                f"for declaring no optical mode. The three add up to the number examined. A skipped wavelength "
                f"is terminated on integrated optics, which carry no part number and no mode list, so this "
                f"check's silence about them is not a verdict"
            )
        )


def _fitted_by_port(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Port identity against the pluggables recorded in it.

    A list rather than one unit, because two records claiming one port is a
    state the model allows. `checks/transceiver_placement.py` owns that fault;
    keeping both here means neither is dropped out of this check's own answer.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for unit in nodes_of(data, TRANSCEIVER):
        try:
            port = peer(unit, "port")
        except ValueError:
            continue
        index.setdefault(str(port.get("id") or ""), []).append(unit)
    return index


def _mode(carrier: dict[str, Any]) -> str | None:
    try:
        name = peer(carrier, "optical_mode").get("name")
    except ValueError:
        return None
    return None if name is None else str(name)


def _supported_modes(unit: dict[str, Any]) -> list[str]:
    """The modes the unit's part lists, in the order the catalog holds them."""
    try:
        part = peer(unit, "type")
    except ValueError:
        return []
    return [str(mode.get("name")) for mode in peers(part, "supported_modes") if mode.get("name") is not None]


def _where(port: dict[str, Any]) -> str:
    """`device port`, as the carrier's own line-port selection returns it."""
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


def _name(record: dict[str, Any]) -> str:
    return str(record.get("name") or "an unnamed record")


def _listed(items: Iterable[str]) -> str:
    """`a`, `a and b`, `a, b and c`. Formatting only."""
    names = list(items)
    if len(names) < 2:
        return names[0] if names else "nothing"
    return f"{', '.join(names[:-1])} and {names[-1]}"
