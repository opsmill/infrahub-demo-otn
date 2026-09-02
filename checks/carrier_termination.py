"""A lit wavelength must be terminated at both of its ends, by a line port at each.

Judges `OtnOpticalCarrier` with `status: active`. A planned carrier binds no
port by design and a decommissioned one has lost its equipment on purpose, so
both are skipped and the summary states how many.

The verdict counts ends, not ports. Two ports both sitting at Milan is not a
terminated wavelength, so the sites the ports sit at are compared against the
ends derived from the route: each section names two ROADMs, each ROADM names
its site, and a site touched by exactly one section is an end. Three ports is
its own finding.

The schema cannot carry this half. `line_ports` cannot be mandatory, because a
freshly provisioned carrier legally has none, and Infrahub has no minimum
cardinality. Nothing is written to the carrier when a transponder is deleted,
so there is no write for a constraint to refuse. `on_delete: no-action` on both
sides is what leaves the fault visible instead of cascading the wavelength away.
"""

from collections import Counter
from collections.abc import Iterable
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.plant import nodes_of, peer, peers

CARRIER = "OtnOpticalCarrier"

ACTIVE = "active"
"""The one status this check judges. See the docstring for the other two."""

ENDS = 2
"""How many line ports terminate a wavelength. The schema comment on
`OtnOpticalCarrier.line_ports` is where this number comes from: a wavelength is
terminated at each of its ends, and those are two ports on two devices at two
sites."""


class CarrierTerminationCheck(InfrahubCheck):
    query = "carrier_termination"

    def validate(self, data: dict[str, Any]) -> None:
        examined = 0
        skipped: Counter[str] = Counter()
        unterminated = 0
        over_terminated = 0

        for carrier in nodes_of(data, CARRIER):
            status = str(carrier.get("status") or "unset")
            if status != ACTIVE:
                skipped[status] += 1
                continue
            examined += 1
            terminating = _terminating_sites(carrier)
            dark = [site for site in _route_ends(carrier) if site not in terminating]
            if dark or len(set(terminating)) < ENDS:
                unterminated += 1
                self._unterminated(carrier, terminating, dark)
            elif len(terminating) > ENDS:
                over_terminated += 1
                self._over_terminated(carrier, terminating)

        self._summarise(examined, skipped, unterminated, over_terminated)

    def _unterminated(self, carrier: dict[str, Any], terminating: list[str], dark: list[str]) -> None:
        """One error per carrier, naming the wavelength, its channel and the gap."""
        name = str(carrier.get("name") or "")
        held = _held(terminating)
        if dark:
            gap = f"Nothing at {_listed(dark)} terminates it"
        elif terminating:
            gap = "Its far end is unterminated and its route does not say where that end is"
        else:
            gap = "Neither end of it is terminated"
        self.log_error(
            message=(
                f"{name} is active on channel {_channel(carrier)} and {held}. {gap}, so the wavelength is lit in "
                f"the plan and reaches no equipment there: no receiver monitor reports its OSNR, BER or Q at that "
                f"end, and its spectrum stays reserved on every section it crosses"
            ),
            object_id=str(carrier.get("id", "")),
            object_type=str(carrier.get("__typename", "")),
        )

    def _over_terminated(self, carrier: dict[str, Any], terminating: list[str]) -> None:
        """Both ends terminated and at least one port more than the wavelength has ends."""
        name = str(carrier.get("name") or "")
        self.log_error(
            message=(
                f"{name} is active on channel {_channel(carrier)} and {len(terminating)} line ports bind it, at "
                f"{_listed(sorted(set(terminating)))}, where a wavelength has {ENDS} ends. Both ends are "
                f"terminated, so the extra port claims a wavelength it does not terminate: its receiver reports "
                f"readings for light that does not reach it, and the port reads as used to anyone looking for a "
                f"spare to bind the next service to"
            ),
            object_id=str(carrier.get("id", "")),
            object_type=str(carrier.get("__typename", "")),
        )

    def _summarise(self, examined: int, skipped: Counter[str], unterminated: int, over_terminated: int) -> None:
        """One INFO line stating how many carriers were judged and how many were not."""
        boundary = (
            "Every carrier on this branch is active, so none was skipped."
            if not skipped
            else (
                f"{_listed(f'{count} {status}' for status, count in sorted(skipped.items()))} carrier(s) are "
                f"outside the scope, which is status active: a wavelength just provisioned onto a branch is "
                f"planned and binds no line port by design."
            )
        )
        if not examined:
            self.log_info(message=f"No active carrier is on this branch, so none can be unterminated. {boundary}")
            return
        faults = []
        if unterminated:
            faults.append(f"{unterminated} of them terminated at fewer than {ENDS} ends")
        if over_terminated:
            faults.append(f"{over_terminated} bound to more line ports than a wavelength has ends")
        verdict = _listed(faults) if faults else "every one terminated at both ends"
        self.log_info(message=f"{examined} active carrier(s) examined, {verdict}. {boundary}")


def _channel(carrier: dict[str, Any]) -> str:
    """The channel number, or a word saying it is absent."""
    try:
        return str(peer(carrier, "channel").get("channel_number", "unknown"))
    except ValueError:
        return "unknown"


def _site_of(record: dict[str, Any]) -> str | None:
    """The site name one hop off a device or a ROADM, or `None`."""
    try:
        name = peer(record, "site").get("name")
    except ValueError:
        return None
    return str(name) if name else None


def _terminating_sites(carrier: dict[str, Any]) -> list[str]:
    """One entry per line port, named by the site the port's device sits at."""
    sites: list[str] = []
    for port in peers(carrier, "line_ports"):
        try:
            device = peer(port, "device")
        except ValueError:
            sites.append("an unnamed device")
            continue
        sites.append(_site_of(device) or str(device.get("name") or "an unnamed device"))
    return sites


def _route_ends(carrier: dict[str, Any]) -> list[str]:
    """The two sites at the ends of the carrier's route, or nothing."""
    touched: Counter[str] = Counter()
    for section in peers(carrier, "sections"):
        for side in ("roadm_a", "roadm_b"):
            try:
                roadm = peer(section, side)
            except ValueError:
                continue
            site = _site_of(roadm)
            if site:
                touched[site] += 1
    ends = sorted(site for site, count in touched.items() if count == 1)
    return ends if len(ends) == ENDS else []


def _held(terminating: list[str]) -> str:
    """What does terminate the wavelength, counted per site."""
    if not terminating:
        return "no line port terminates it"
    counts = Counter(terminating)
    named = _listed(site if ports == 1 else f"{ports} line ports at {site}" for site, ports in counts.items())
    return f"only {named} {'terminates' if len(terminating) == 1 else 'terminate'} it"


def _listed(items: Iterable[str]) -> str:
    """`a`, `a and b`, `a, b and c`. Formatting only."""
    names = list(items)
    if len(names) < 2:
        return names[0] if names else "Nothing"
    return f"{', '.join(names[:-1])} and {names[-1]}"
