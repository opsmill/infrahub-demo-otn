"""A lit wavelength must be terminated at both of its ends, by a line port at each.

**What this covers.** Every `OtnOpticalCarrier` whose `status` is `active`, and
the question is whether `line_ports` holds the two ports that terminate it. Two,
not one: `schemas/otn_carrier.yml` says why the relationship is cardinality many
in the first place, "a wavelength is terminated at each of its ends, and those
are two ports on two devices at two sites". A carrier holding one port is
terminated at one end and dark at the other, and that is the state this check
exists to catch. A carrier holding none is the degenerate case of the same rule
rather than a second rule.

**The verdict counts ends, not ports, and the two come apart.** A port count
alone reads two ports as two ends, and they are not the same thing. Pull the
Amsterdam transponder off `oc-ch002-ams-mil`, let `OtnGenericDevice.ports`
cascade its line ports away, and an operator re-binding the wavelength to the
spare `L2` on `xpdr-mil-01` leaves it holding two ports that are both at Milan
while Amsterdam is dark. Counting ports calls that terminated. So the sites the
ports sit at are compared against the ends the route derives, and a route end
nothing sits at is the finding. The other direction is the same mistake
mirrored: `OtnLinePort.carrier` is `cardinality: one` on the port side only, so
nothing stops a third port naming a wavelength that has two ends, and a count
that only asks for at least two would pass that as well. Three ports is its own
finding, worded as its own fault.

**Scoped to `status: active`, and the scope is the check.** A carrier the service
generator has just provisioned on a branch is `planned` and binds no line port at
all, by design: `generators/optical_service.py` allocates spectrum on a route and
does not place hardware. Judging a planned carrier would fail the provisioning
flow this check is not about, on every proposed change that runs it. A
`decommissioned` carrier is skipped for the mirror-image reason, that its
equipment being gone is the point of the status. The summary states how many
carriers each skipped status accounted for, so the boundary of what was judged is
read rather than inferred from silence.

**Which half the schema already carries, so a later reader does not widen this.**
The port side, entirely. `OtnLinePort.carrier` is `cardinality: one`, so no port
can name two wavelengths, and the peer it names is guaranteed to exist by the
graph. The check says nothing about a port, and it must not grow to: that would
be re-asserting a constraint the server already refuses writes against.

What the schema cannot carry is the carrier side, for two reasons either of which
is sufficient. `line_ports` cannot be `optional: false`, because a freshly
provisioned carrier legally has an empty list and the load would fail on it,
which is R-004a's second argument for `on_delete: no-action` as well. And
Infrahub has no minimum-cardinality constraint, so "at least two peers" is not a
sentence the schema can say even where the emptiness is not legal. The
constitution's counterweight covers the rest: a schema constrains what is
written, and nothing is written to the carrier when a transponder is deleted, so
there is no write for a constraint to refuse.

**The state is reachable because the schema was built to leave it visible.**
`OtnGenericDevice.ports` is `kind: Component` with `on_delete: cascade`, so
deleting a transponder deletes its line ports. The deletion stops there:
`OtnLinePort.carrier` and `OtnOpticalCarrier.line_ports` both carry
`on_delete: no-action`, chosen deliberately over cascade in R-004a because a
cascade would delete the wavelength out from under the far-end transponder, take
the line container and the services groomed into it with it, and make the fault
disappear instead of showing it. Leaving a fault reachable and then not looking
at it would be the worse half of that decision. This check is the other half.

**The frequency check is not this check.** FR-027 originally proposed asserting
that a line port's `center_frequency_mhz` matches its carrier's channel. R-014
dropped it: the generator writes both from `channel_to_frequency_mhz`, so they
cannot disagree by construction, and a hand edit under `objects/` already fails
`tests/unit/test_geant_dataset.py` on the next run. A check that cannot fail is
worse than no check, because a green result reads as evidence.

**The missing end is named, and it is derived rather than parsed.** A carrier
holds no endpoints. Its route does: each section names two ROADMs, each ROADM
names its site, and the sites touched by exactly one of the carrier's sections
are the two ends of the route. Subtract the sites that still terminate it and
what is left is the site that no longer does. Reading `ams` out of the name
`oc-ch002-ams-mil` would have been shorter and would be a naming convention doing
a relationship's job, which this repository has already been bitten by once.
Where the sections do not form a simple two-ended path the derivation returns
nothing and the finding says which ends remain instead of guessing.

**Global, not targeted.** An absence is not an edit. Deleting a transponder
touches the transponder, and the carrier left with one end is not in the change
at all, so a targeted check bound to the edited objects would never be handed it.
`monitor_completeness` is global on exactly this argument and asks the same shape
of question about a device with no monitor.

**The logic is here and not in `src/`.** It is one status filter, one length
test and a degree count over a handful of sections, used by nothing else. The
shared package is where a rule goes when a generator and a check must not be able
to disagree about it, which is the case `containers.free_slots` exists for. There
is no second reader here to disagree with.
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
        """One error per carrier, naming the wavelength, its channel and the gap.

        Logged against the carrier rather than against the surviving port. The
        port is fine; the wavelength is the object with the defect, and it is the
        page an operator would open to decide whether to re-terminate it or
        release the spectrum.
        """
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
        """Both ends terminated and at least one port more than the wavelength has ends.

        A separate message rather than a branch of the one above, because it is
        the opposite fault and the consequence is the opposite too: nothing is
        dark, and instead a port is holding spectrum on a wavelength it does not
        terminate. The schema cannot refuse it. `OtnLinePort.carrier` is
        `cardinality: one` on the port side, which stops a port naming two
        wavelengths and says nothing about how many ports name one.
        """
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
        """One INFO line stating how many carriers were judged and how many were not.

        Both halves are the requirement. The count of active carriers is what
        makes a green result mean something rather than read as "the query
        returned nothing". The skipped counts are what stop the scope being
        inferred from silence, which matters most on a branch that has just
        provisioned a wavelength: one planned carrier named here is one nobody
        has to wonder about.
        """
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
    """The channel number, or a word saying it is absent.

    `channel` is `optional: false`, so a carrier without one cannot be written.
    A payload without one can still be handed to the check, and a finding reading
    "channel None" would be worse than one that says so.
    """
    try:
        return str(peer(carrier, "channel").get("channel_number", "unknown"))
    except ValueError:
        return "unknown"


def _site_of(record: dict[str, Any]) -> str | None:
    """The site name one hop off a device or a ROADM, or `None`.

    `OtnGenericDevice.site` is `optional: true`, deliberately: the schema says a
    device has to be creatable before its site record exists. So this is a real
    state and not a defensive guess, and a device with no site drops out of the
    naming rather than raising.
    """
    try:
        name = peer(record, "site").get("name")
    except ValueError:
        return None
    return str(name) if name else None


def _terminating_sites(carrier: dict[str, Any]) -> list[str]:
    """One entry per line port, named by the site the port's device sits at.

    Per port, not per distinct site, and the reason is naming rather than the
    verdict. Two ports at one site is a different fault from one port, and a
    finding built off a set would word the first as the second. The verdict is
    taken over the distinct sites in `validate`, against the ends the route
    derives, because two ports at one site terminate one end.
    """
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
    """The two sites at the ends of the carrier's route, or nothing.

    A section is an edge between two ROADMs, so the carrier's sections are a
    walk over sites and the ends are the two sites that exactly one section
    touches. Anything else, a single site, a branch, a loop or a carrier with no
    sections at all, is not a two-ended path, and naming an end off a shape this
    does not understand would be a guess presented as a fact.
    """
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
    """What does terminate the wavelength, counted per site.

    Two ports at Milan reads as two ports at Milan and not as Milan, which is the
    whole reason `_terminating_sites` returns one entry per port. An operator
    told only that Milan terminates it would go looking for a missing port at
    Milan, and the port at Milan is not what is missing.
    """
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
