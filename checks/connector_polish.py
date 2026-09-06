"""Every port terminating a Raman-pumped span carries an angled endface.

Reads each `OtnFiberSpan`, the pumps on it and the ports at its two ends, and
holds those ports to APC wherever a pump is present. A Raman pump fires half a
watt of light backwards up the fibre it is spliced into. A flat endface returns
about 50 dB of that into the span it came from; an angled one sends the
reflection into the cladding instead. On pumped glass that difference decides
whether the section runs or oscillates, which is why the rule is absolute here
and silent everywhere else.

**A span whose `terminating_ports` is empty is reported and never passed.** An
empty relationship and a span with two correct APC ends produce the same
silence, and a check that reads the second out of the first is worse than no
check: it puts a green mark on a traversal that returned nothing. The finding is
INFO rather than an error because an unpopulated relationship is a gap in the
data and not a fault in the plant, and it says which span it could not judge.

**Only APC passes, and that includes `none`.** UPC and PC are flat endfaces and
reflect. `none` means no polished endface at all, which is the right answer for
a fusion splice and a contradiction on a port a span is recorded as terminating
on: a termination is a mated connector, so a port claiming both is a record that
disagrees with itself rather than a clean one.

**The schema could not take any of this.** `polish` is an attribute on the port
and `raman_pumps` is a relationship on the span, two hops apart, and Infrahub
has no cross-relationship constraint: no write can be refused because a port at
the far end of a relationship chain has the wrong value. What the schema does
own is the vocabulary, a Dropdown of four choices, so this check never has to
ask whether a value is a polish at all.

Silent about every port that terminates no pumped span, which is all but a
handful of them. Polish matters on a pumped section and is a preference
elsewhere, so the backfill covers line and ROADM degree ports and every other
port kind may leave the attribute unset without this check saying anything.

Silent about how much gain a pump contributes and whether the section closes.
That is `checks/osnr_margin.py` and the budget engine behind it.
"""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.plant import nodes_of, peer, peers

SPAN = "OtnFiberSpan"

REQUIRED_POLISH = "APC"
"""The one value that passes on a pumped span. Angled physical contact."""


class ConnectorPolishCheck(InfrahubCheck):
    query = "connector_polish"

    def validate(self, data: dict[str, Any]) -> None:
        examined = 0
        pumped = 0
        judged = 0
        unjudgeable = 0

        for span in nodes_of(data, SPAN):
            examined += 1
            if not list(peers(span, "raman_pumps")):
                continue
            pumped += 1

            ports = list(peers(span, "terminating_ports"))
            if not ports:
                unjudgeable += 1
                self._unreachable(span)
                continue

            judged += 1
            for port in ports:
                polish = port.get("polish")
                if polish is None:
                    self._unstated(span, port)
                elif str(polish) != REQUIRED_POLISH:
                    self._flat(span, port, str(polish))

        self._summarise(examined, pumped, judged, unjudgeable)

    def _flat(self, span: dict[str, Any], port: dict[str, Any], polish: str) -> None:
        """A port on pumped glass whose endface is not angled."""
        self.log_error(
            message=(
                f"{_where(port)} terminates {_name(span)}, which is Raman-pumped, and its endface is {polish}. "
                f"A pump puts half a watt into this fibre and a {polish} endface reflects that back down it, "
                f"so this connector has to be {REQUIRED_POLISH} before the pump is turned up"
            ),
            object_id=str(port.get("id", "")),
            object_type=str(port.get("__typename", "")),
        )

    def _unstated(self, span: dict[str, Any], port: dict[str, Any]) -> None:
        """A port on pumped glass whose endface nobody recorded."""
        self.log_error(
            message=(
                f"{_where(port)} terminates {_name(span)}, which is Raman-pumped, and states no endface polish. "
                f"An unset value is not a passing one here: the record does not say whether this connector "
                f"reflects the pump, and the section is running on the assumption that it does not"
            ),
            object_id=str(port.get("id", "")),
            object_type=str(port.get("__typename", "")),
        )

    def _unreachable(self, span: dict[str, Any]) -> None:
        """A pumped span this check could not judge, said out loud."""
        self.log_info(
            message=(
                f"{_name(span)} is Raman-pumped and names no terminating port, so its endfaces could not be "
                f"judged. This is not a clean result: an empty relationship and two correct "
                f"{REQUIRED_POLISH} ends look the same from here, and the generator populates this edge "
                f"precisely so that they do not"
            )
        )

    def _summarise(self, examined: int, pumped: int, judged: int, unjudgeable: int) -> None:
        """One INFO line stating what was judged and what was left alone."""
        if not pumped:
            self.log_info(
                message=(
                    f"{examined} span(s) examined and none of them is Raman-pumped, so no endface was judged. "
                    f"Polish is a preference on unpumped glass and this check says nothing about it"
                )
            )
            return
        self.log_info(
            message=(
                f"{examined} span(s) examined, {pumped} of them Raman-pumped, {judged} judged against their "
                f"terminating ports and {unjudgeable} unjudgeable for naming none. An unjudgeable span is not "
                f"a passing one, and a run reporting no findings over a pumped section it could not reach has "
                f"seen nothing"
            )
        )


def _name(span: dict[str, Any]) -> str:
    return str(span.get("name") or "an unnamed span")


def _where(port: dict[str, Any]) -> str:
    """`device port`, which is how this model identifies a port."""
    try:
        device = str(peer(port, "device").get("name") or "an unnamed device")
    except ValueError:
        device = "an unrecorded device"
    return f"{device} {port.get('name') or 'an unnamed port'}"
