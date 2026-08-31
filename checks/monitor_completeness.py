"""Every device that should carry a monitor must carry one, and no monitor may watch nothing.

**The name is reused deliberately, and it does not ask the old question.** A check
of this name was deleted in commit `ef89265` for asking a question that could not
be answered "no". That one asserted that a monitor carried the readings its
family reports and none it cannot take, and both halves became schema
constraints: every reading is `optional: false` on the monitor kind that declares
it, and a kind has no field for a reading its hardware cannot produce. A check
that can never fail is worse than no check, because a green result reads as
evidence, and removing it was right.

**This one asks whether a device has a monitor at all.** That question is
answered "no" by `demo/10_amplifier_without_monitor.yml`, which loads an
amplifier that is valid on its own and carries nothing to measure it. The deleted
check iterated over monitors, so a device with no monitor was invisible to it.
`src/infrahub_demo_otn/drift.py` says so in its own words, about the case this
check now covers:

    A device with no monitor is skipped rather than reported as drifting.
    Reporting it here as a shortfall would be inventing a measurement of zero.
    Nothing else ever covered that case either: the completeness check this
    repository used to carry iterated over monitors, so a device with no monitor
    was invisible to it.

**Which half the schema could not take.** None of it. A device reaches its ports
through `ports`, which peers the generic `OtnGenericPort`, and a relationship to a
generic cannot be constrained, filtered or counted by the kind of its peers. Five
probes against Infrahub 1.11.0 are recorded in
`specs/feature-gaps/03-filter-a-nested-relationship-by-kind.md`: `kind`,
`typename__value`, `__typename` and an attribute declared on a sibling generic
are all rejected as arguments. The schema has no way to say "at least one peer of
`ports` is an `OtnAmplifierMonitor`" because it has no way to name a peer's kind
at all. A second, kind-specific relationship beside `ports` would work and would
put one object on two edges that can disagree, so it was refused. The
counterweight applies as well: a schema constrains what is written, so it cannot
notice a gap, and this rule is entirely about absence.

The same gap sets the cost. `queries/monitor_completeness.gql` fetches every port
on every device of the five kinds, because an inline fragment reduces the fields
on a node and not the number of nodes. 306 amplifiers carrying three ports each
return roughly 918 port nodes so that 306 booleans can be answered. That is the
same order as the global checks already shipped and it is fine at this size. It
is written down because a site with a hundred ports per device is where it stops
being fine.

**This check owns presence, in both directions.** A device or degree port with no
monitor is one direction. A monitor watching a port that does not exist is the
other, and an orphan monitor is as much a defect as an absent one: it reports a
number about nothing and every report downstream quotes it.
`channel_count_consistency` defers to this check with an INFO when it meets a
monitor with no port, rather than putting two findings on one fault.

**The pairing table is not here.** `monitors.MONITOR_BY_DEVICE_KIND` holds it, so
what in this model is expected to carry a monitor has one answer in one place,
and `monitors.PER_PORT_KINDS` names the one row whose subject is a port rather
than a device. `monitors.missing_monitors` does the matching. This file fetches,
loops and formats.

**Global, not targeted.** An absence is not an edit. A change that deletes a
monitor touches the monitor, and a change that adds a device touches the device,
and neither one is the object a targeted check would be handed for the other
case. Every other check in this repository is global for a related reason.

**The summary states the coverage per kind.** A green result that says only
"passed" is the failure mode the deleted check was removed for, so a passing run
says which kinds it counted, how many of each, and which kinds it did not judge.
`monitors.KINDS_NOT_JUDGED` names the three device kinds that carry no monitor,
so the boundary of what was judged is read rather than inferred from silence.
"""

from collections.abc import Iterable
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.monitors import (
    KINDS_NOT_JUDGED,
    MONITOR_BY_DEVICE_KIND,
    PER_PORT_KINDS,
    MissingMonitor,
    degree_of_monitor,
    missing_monitors,
    monitor_port_name,
)
from infrahub_demo_otn.plant import nodes_of, peers

DEGREE_PORT = "OtnRoadmDegreePort"
DEGREE_MONITOR = "OtnRoadmDegreeMonitor"

PLURAL: dict[str, str] = {
    "OtnAmplifier": "amplifiers",
    "OtnRamanPump": "Raman pumps",
    "OtnTransponder": "transponders",
    "OtnMuxDemux": "multiplexers",
    "OtnRoadmDegreePort": "ROADM degrees",
    "OtnRouter": "Routers",
    "OtnPatchPanel": "patch panels",
    "OtnOduSwitch": "ODU switches",
}
"""How each kind is named in a sentence a person reads.

Formatting, not policy. Which kinds are expected to carry a monitor is
`MONITOR_BY_DEVICE_KIND` and which are not is `KINDS_NOT_JUDGED`; this table only
decides how those two are spelled in the summary. A kind absent from it falls
back to its own name, so a sixth pairing added to `monitors.py` appears in the
summary before anybody touches this file.
"""

CONSEQUENCE: dict[str, str] = {
    "OtnAmplifier": (
        "nothing can compare its configured gain against what it is delivering. The drift report skips a "
        "stage with no monitor rather than reporting it"
    ),
    "OtnRamanPump": (
        "nothing can compare its on-off gain against what it is delivering. The drift report skips a stage "
        "with no monitor rather than reporting it"
    ),
    "OtnTransponder": (
        "nothing reports the power arriving at its receiver, so the margin the optical budget predicts for "
        "every service ending here is never measured against anything"
    ),
    "OtnMuxDemux": (
        "nothing reports how many channels it passes, so channel_count_consistency has no reading to "
        "compare against the wavelengths it lights"
    ),
    "OtnRoadmDegreePort": (
        "nothing reports how many channels leave that degree, so channel_count_consistency has no reading "
        "to compare against the section it faces"
    ),
}
"""What is lost when each pairing is missing, in the words of the thing that
stops working. A finding that said only "no monitor" would leave the reader to
work out whether that matters."""


class MonitorCompletenessCheck(InfrahubCheck):
    query = "monitor_completeness"

    def validate(self, data: dict[str, Any]) -> None:
        subjects: list[dict[str, Any]] = []
        owners: dict[tuple[str, str, str], dict[str, Any]] = {}
        totals: dict[str, int] = {kind: 0 for kind in MONITOR_BY_DEVICE_KIND}

        for kind in MONITOR_BY_DEVICE_KIND:
            if kind in PER_PORT_KINDS:
                continue
            for device in nodes_of(data, kind):
                name = str(device["name"])
                totals[kind] += 1
                # The device rows are their own subject, so the middle element of
                # the key is empty and the kind and the name are the identity.
                owners[(kind, "", name)] = device
                subjects.append(
                    {
                        "name": name,
                        "kind": kind,
                        "monitors": {str(port.get("__typename")) for port in peers(device, "ports")},
                    }
                )

        orphans = 0
        for roadm in nodes_of(data, "OtnRoadm"):
            holder = str(roadm["name"])
            ports = list(peers(roadm, "ports"))
            degrees = {str(port["name"]) for port in ports if port.get("__typename") == DEGREE_PORT}
            watchers = {str(port["name"]) for port in ports if port.get("__typename") == DEGREE_MONITOR}
            for degree in sorted(degrees):
                totals[DEGREE_PORT] += 1
                # Keyed by the ROADM as well as the port, because a degree port
                # name is unique on its device and nowhere else: `(device, name)`
                # is the uniqueness constraint on `OtnGenericPort` and six shipped
                # ROADMs carry a degree called `DEG-FRA`. Keyed by the name alone,
                # the last ROADM read wins and the finding names a device that is
                # fine. `channel_count_consistency` keys its own subjects the same
                # way, on the same argument.
                owners[(DEGREE_PORT, holder, degree)] = roadm
                # The fifth row of the pairing table is a port kind, so a ROADM
                # is passed one subject per degree. The name matching uses
                # `monitors.monitor_port_name`, so there is still one definition
                # of what a degree monitor is called.
                attached = {DEGREE_MONITOR} if monitor_port_name(degree) in watchers else set()
                subjects.append({"name": degree, "device": holder, "kind": DEGREE_PORT, "monitors": attached})
            for watcher in sorted(watchers):
                watched = degree_of_monitor(watcher)
                if watched is None or watched not in degrees:
                    orphans += 1
                    self._orphan(roadm, watcher, watched)

        findings = missing_monitors(subjects)
        for finding in findings:
            self._missing(finding, owners.get((finding.kind, finding.device, finding.name)))
        self._summarise(totals, findings, orphans)

    def _missing(self, finding: MissingMonitor, owner: dict[str, Any] | None) -> None:
        """One error per subject, named against the device a reader would open.

        For four of the five pairings the subject is the device and the message
        names it. For the fifth the subject is a degree port, and a port name is
        unique on its device and nowhere else, so the message names the ROADM as
        well and the finding is logged against that ROADM: the one holding the
        uncovered degree, not whichever one the loop read last. Two ROADMs short
        of the same degree monitor produce two findings a reader can tell apart.
        """
        consequence = CONSEQUENCE.get(finding.kind, "nothing measures what it is doing")
        subject = f"{finding.device} {finding.name}" if finding.device else finding.name
        self.log_error(
            message=(f"{subject} is an {finding.kind} and carries no {finding.monitor_kind}, so {consequence}"),
            object_id=str((owner or {}).get("id", "")),
            object_type=str((owner or {}).get("__typename", "")),
        )

    def _orphan(self, roadm: dict[str, Any], watcher: str, watched: str | None) -> None:
        """A monitor watching a port that is not there. Presence, other direction.

        An error rather than an INFO, on the same terms as an absent monitor: the
        reading it carries is mandatory, so it is always a number, and a number
        about a port nobody can find is quoted by every report downstream as
        though it were about something.
        """
        missing = f"no degree port called {watched}" if watched else "a name that fits no degree port"
        self.log_error(
            message=(
                f"{roadm['name']} carries degree monitor {watcher} and {missing}, so that monitor reports a "
                "channel count about a fibre this ROADM does not reach. An orphan monitor is a defect in the "
                "other direction from an absent one"
            ),
            object_id=str(roadm.get("id", "")),
            object_type=str(roadm.get("__typename", "")),
        )

    def _summarise(self, totals: dict[str, int], findings: list[MissingMonitor], orphans: int) -> None:
        """One INFO line stating the coverage per kind, including when it is nothing.

        Per kind rather than as one total, because the totals differ by two
        orders of magnitude and 306 covered amplifiers would hide nine
        uncovered Raman pumps inside a single percentage. The kinds that carry no
        monitor are named at the end, so the boundary of what was judged is read
        rather than inferred.
        """
        absent = {kind: 0 for kind in totals}
        for finding in findings:
            absent[finding.kind] = absent.get(finding.kind, 0) + 1

        judged = sum(totals.values())
        boundary = (
            f"{_listed(PLURAL.get(kind, kind) for kind in KINDS_NOT_JUDGED)} carry no monitor and are not judged here"
        )
        if not judged:
            self.log_info(
                message=(
                    f"No device of a kind that carries a monitor is on this branch, so nothing is missing one. "
                    f"{boundary}"
                )
            )
            return

        coverage = ", ".join(
            f"{totals[kind] - absent[kind]}/{totals[kind]} {PLURAL.get(kind, kind)}" for kind in totals
        )
        opening = "Monitor coverage complete" if not findings and not orphans else "Monitor coverage"
        orphaned = "" if not orphans else f" {orphans} monitor(s) watch a port that does not exist."
        self.log_info(message=f"{opening}: {coverage}.{orphaned} {boundary}")


def _listed(items: Iterable[str]) -> str:
    """`a`, `a and b`, `a, b and c`. Formatting only."""
    names = list(items)
    if len(names) < 2:
        return names[0] if names else "Nothing"
    return f"{', '.join(names[:-1])} and {names[-1]}"
