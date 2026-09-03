"""A channel monitor must not claim more light than the topology puts on its fibre.

A ROADM degree monitor says how many channels it sees. The carriers attached to
the section that degree faces say how many are there. Nothing compared the two,
so a monitor could report 71 channels on a fibre carrying 40 and every report in
this repository would keep quoting the 71.

**The comparison is asymmetric, and the clock is why.** A monitor reading is
dated. The shipped ones are stamped `2026-08-26T06:00:00Z`, and no reading can
know about a wavelength somebody designed after it was taken. So the two
directions of a disagreement are not the same fault:

- **Over-reporting is a defect and blocks the merge.** The monitor claims light
  the topology says is not on that fibre, and no reading of any age can invent
  light. Either the count is stale, which is precisely the 71 this feature
  repairs, or a wavelength was removed from the model while the equipment was
  left alone. Both are somebody's data being wrong.
- **Under-reporting is a branch mid-flight and is reported without blocking.**
  The topology holds a wavelength the last reading predates.
  `generators/optical_service.py` writes a carrier and touches no monitor, so
  every degree along a newly provisioned route sits one channel behind the
  section it faces until the field turns the wavelength up. Gating on that would
  put a red validator on every provisioning branch in this demo and refuse the
  merge the walkthrough is built around.

A reading older than the design can only ever under-report, so the gated
direction is the one the clock cannot explain. That is what makes it a defect
rather than a lag.

**Do not swap this for a status filter.** `OtnOpticalCarrier.status`
distinguishes `planned` from `active`, and counting only the `active` ones looks
like the tidier fix. It is not available, and the reason has got stronger rather
than weaker. `generators/optical_service.py` used to write `status: "active"` on
a new carrier and now writes `planned`, because it allocates spectrum and places
no hardware, but nothing anywhere moves a carrier from `planned` to `active` when
a change merges. So a filter on `active` would now miss every wavelength the
generator has ever lit, not just the ones it lit on this branch, and this check
would under-count against a genuinely lit wavelength. That is a worse failure
than the one being fixed. The full reasoning is in "Which direction is a defect"
in `specs/024-monitor-consistency-checks/spec.md`.

**The two coarse multiplexer monitors are judged in both directions.** A degree
monitor is compared against a live design, which a branch can add to while the
reading stands still. A coarse multiplexer monitor is compared against
`cwdm_channels`, the fixed set of wavelengths its filter passes, and nothing in
this repository provisions one of those onto a branch. There is no clock to
explain either direction there, so both are errors. The asymmetry follows the
subject, not the arithmetic, which is why it travels on the comparison as
`reading_can_lag` rather than being decided by a second subtraction here.

**Why this gates at all when gain drift only reports.** `monitor_drift` compares
a reading against the physical world. A pump ages, the gain droops, and an
operator may have accepted that until the next maintenance window, so it is
reported and blocks nothing. This check compares two records that are both
authored data inside Infrahub, the count on the monitor and the carriers attached
to the section, so an over-report is one of them being wrong rather than a
condition somebody chose.

If `channel_count` ever arrives from a live telemetry feed instead of from the
generator, that argument flips and this check has to become a report. A real
monitor can see 39 where the database says 40 because a transmitter is down, and
that is a thing to report and not a merge to block. The line to watch is where
the number is written, not what it is called.

**Which half the schema already took.** `channel_count` is `optional: false` on
`OtnChannelMonitor`, so a monitor without one cannot be written. Its bounds are 0
to 96, so a count outside the C-band grid is refused at write time and never
reaches this file. `(device, name)` is unique on `OtnGenericPort`, and a
constraint on a generic binds across every kind that inherits it, so two monitors
on one device cannot share a name and the degree-to-monitor pairing below cannot
be ambiguous. What is left for Python is the comparison itself: R-002 in
`specs/024-monitor-consistency-checks/research.md` records that
`uniqueness_constraints` has no aggregate form, no count and no way to name "all
the carriers on that section" as an operand, so no constraint can carry a value
on one node against a count over a many relationship reached from another.

**The fourteen dense multiplexers are silent, and that is the model talking.**
Sixteen `OtnMuxDemux` units ship. Two are coarse and say which wavelengths they
light through `cwdm_channels`, so their monitors are comparable against that
list. The other fourteen are dense AWG units, and the graph holds no relationship
from such a unit to the carriers passing through it, in either direction and at
any depth. There is nothing to compare their counts against, so the check says so
once and compares nothing. The silence is a property of the model holding no
relationship, not of an `if` somebody can delete: adding the relationship would
make those fourteen comparable with no change to the branching here.

**Do not widen.** Specifically, do not start reporting the duplicate when two
carriers share a channel anchor. `monitors.channels_by_section` counts distinct
anchors because an optical channel monitor counts light on a fibre, and two
carriers on one anchor are one channel to it. They are also a fault, and
`checks/channel_collision.py` already reports it. Counting carrier records here
would raise a second finding for a fault another check has named, against a
channel count that is correct. On the shipped dataset the two counts are
identical, because the generator draws every carrier a fresh anchor; they diverge
only on a branch that has collided, which is exactly where the wrong answer would
do damage.

**One condition, one owner.** This check owns the number. `monitor_completeness`
owns presence in both directions, so a monitor with no degree port is an INFO
here that defers to it rather than a second error over one fault. What this check
does own alone is a degree whose port exists and whose far site resolves to no
section: no other check looks at that pair, and a monitor reporting into nothing
would otherwise pass.

**The subtraction is not here, and neither is the direction.**
`monitors.channel_count_findings` does both, and it refuses a comparison with a
missing side rather than reading `None` as zero. Each finding carries
`is_defect`, so this file never compares the two numbers itself and cannot reach
a verdict the decision layer did not take. It assembles the two sides, hands them
over and formats what comes back, which is the rule
`checks/container_capacity.py` follows against `containers.free_slots`.

**Global, not targeted.** A count is a property of a monitor plus every carrier
on a section, and a change may add one carrier while the monitor and the rest of
the carriers sit outside it. A targeted check bound to the objects a change
touched would look at the new carrier, find nothing wrong with it, and report
green over a monitor that is now out by one.
"""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.monitors import (
    ChannelCountFinding,
    channel_count_findings,
    channels_by_section,
    degree_of_monitor,
    far_site_of_degree,
    sections_by_roadm,
)
from infrahub_demo_otn.plant import nodes_of, peer, peers

DEGREE_MONITOR = "OtnRoadmDegreeMonitor"
DEGREE_PORT = "OtnRoadmDegreePort"
MUX_MONITOR = "OtnMuxDemuxMonitor"


class ChannelCountConsistencyCheck(InfrahubCheck):
    query = "channel_count_consistency"

    def validate(self, data: dict[str, Any]) -> None:
        sites = self._sites_by_roadm(data)
        faced = sections_by_roadm(self._sections(data, sites))
        occupancy = channels_by_section(self._carriers(data), self._section_names(data))

        comparisons: list[dict[str, Any]] = []
        subjects: dict[tuple[str, str], dict[str, Any]] = {}
        deferred = 0
        unresolved = 0

        for roadm in nodes_of(data, "OtnRoadm"):
            device = str(roadm["name"])
            ports = list(peers(roadm, "ports"))
            degrees = {str(port["name"]) for port in ports if port.get("__typename") == DEGREE_PORT}
            for monitor in (port for port in ports if port.get("__typename") == DEGREE_MONITOR):
                name = str(monitor["name"])
                subjects[(device, name)] = monitor
                watched = degree_of_monitor(name)
                if watched is None or watched not in degrees:
                    deferred += 1
                    self._deferred(monitor, device, watched)
                    continue
                far = far_site_of_degree(watched)
                # Both sides of the far-site half of this lookup are folded by
                # `monitors.site_key`: `far_site_of_degree` returns that form and
                # `sections_by_roadm` keys by it. Folded on one side only, a site
                # whose shortname is not lower case resolves to no section and
                # every degree monitor facing it is refused below on correct data.
                section = faced.get(device, {}).get(far) if far else None
                if section is None:
                    unresolved += 1
                    self._unresolved(monitor, device, watched, far)
                    continue
                comparisons.append(
                    {
                        "monitor": name,
                        "device": device,
                        "compared_against": section,
                        "reported": monitor.get("channel_count"),
                        "observed": occupancy[section],
                        "reading_can_lag": True,
                    }
                )

        dense = 0
        for mux in nodes_of(data, "OtnMuxDemux"):
            device = str(mux["name"])
            lit = len(list(peers(mux, "cwdm_channels")))
            for monitor in (port for port in peers(mux, "ports") if port.get("__typename") == MUX_MONITOR):
                name = str(monitor["name"])
                subjects[(device, name)] = monitor
                if not lit:
                    dense += 1
                    continue
                comparisons.append(
                    {
                        "monitor": name,
                        "device": device,
                        "compared_against": f"the coarse grid on {device}",
                        "reported": monitor.get("channel_count"),
                        "observed": lit,
                        "reading_can_lag": False,
                    }
                )

        findings = channel_count_findings(comparisons)
        defects = 0
        for finding in findings:
            subject = subjects.get((finding.device, finding.monitor))
            if finding.is_defect:
                defects += 1
                self._defect(finding, subject)
            else:
                self._lagging(finding, subject)
        if dense:
            self._dense(dense)
        self._summarise(len(comparisons), defects, len(findings) - defects, dense, deferred, unresolved)

    # ------------------------------------------------------------------
    # Assembling the two sides
    # ------------------------------------------------------------------
    @staticmethod
    def _sites_by_roadm(data: dict[str, Any]) -> dict[str, str]:
        """Which site each ROADM sits at.

        The section collection names its two ROADMs and not their sites, and the
        degree-to-section join is by far site, so the two have to be brought
        together here. `site` is optional on `OtnGenericDevice`, so a ROADM
        without one is absent from this mapping and every degree on it resolves
        to no section, which is reported per degree rather than raised once.

        The shortname is passed on as stored. `sections_by_roadm` folds it into
        the join key, which is the only place that folding belongs: doing it here
        as well would put two definitions of the key in two files, and the
        `_unresolved` message below reads better naming the site as the port
        named it.
        """
        located = {}
        for roadm in nodes_of(data, "OtnRoadm"):
            site = (roadm.get("site") or {}).get("node")
            shortname = (site or {}).get("shortname", {}).get("value")
            if shortname:
                located[str(roadm["name"])] = str(shortname)
        return located

    @staticmethod
    def _sections(data: dict[str, Any], sites: dict[str, str]) -> list[dict[str, Any]]:
        """Every section in the shape `sections_by_roadm` reads.

        An end with no ROADM contributes no site, and `sections_by_roadm` skips
        that direction alone rather than dropping the section.
        """
        shaped = []
        for section in nodes_of(data, "OtnOpticalMultiplexSection"):
            record: dict[str, Any] = {"name": str(section["name"])}
            for end in ("a", "b"):
                node = (section.get(f"roadm_{end}") or {}).get("node")
                roadm = (node or {}).get("name", {}).get("value")
                record[f"roadm_{end}"] = str(roadm) if roadm else None
                record[f"site_{end}"] = sites.get(str(roadm)) if roadm else None
            shaped.append(record)
        return shaped

    @staticmethod
    def _section_names(data: dict[str, Any]) -> list[str]:
        """Every section name, passed so an empty section reports 0 rather than
        falling out of the occupancy mapping. A missing key read as "nothing to
        check" is a monitor nobody compared."""
        return [str(section["name"]) for section in nodes_of(data, "OtnOpticalMultiplexSection")]

    @staticmethod
    def _carriers(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Every carrier as an anchor and the fibres it rides.

        The anchor is the channel's centre frequency, which is what makes the
        count a count of channels. `channel` is `optional: false` on
        `OtnOpticalCarrier`, so `peer` finding no peer here is a schema
        violation and not a case to be tolerated: it raises, and the run stops
        rather than counting the carrier as riding nothing.
        """
        return [
            {
                "name": str(carrier["name"]),
                "channel": peer(carrier, "channel")["center_frequency_mhz"],
                "sections": [str(section["name"]) for section in peers(carrier, "sections")],
            }
            for carrier in nodes_of(data, "OtnOpticalCarrier")
        ]

    # ------------------------------------------------------------------
    # Saying what was found
    # ------------------------------------------------------------------
    def _defect(self, finding: ChannelCountFinding, monitor: dict[str, Any] | None) -> None:
        """One error per monitor whose disagreement the clock cannot explain.

        A degree monitor claiming more light than its section carries, or a
        coarse multiplexer monitor disagreeing in either direction. Both numbers
        and the difference, because a refusal that does not say by how much
        cannot be acted on.

        Named against the monitor rather than the section, because the monitor is
        the object holding the figure a reader would go and correct, and because
        one section has a monitor at each of its two ends that may disagree by
        different amounts.
        """
        claim = (
            "The monitor claims light the topology does not put on that fibre, and no reading of any age can "
            "invent light. Either the count is stale or a wavelength was removed from the model and the "
            "equipment was left alone"
            if finding.over_reports
            else "A coarse multiplexer's lit wavelengths are a fixed property of its filter, not a design a "
            "branch adds to, so a disagreement in either direction is a record being wrong"
        )
        self.log_error(
            message=(
                f"{finding.device} {finding.monitor} reports {finding.reported} channels and "
                f"{finding.compared_against} carries {finding.observed}, a difference of "
                f"{abs(finding.difference)}. {claim}"
            ),
            object_id=str((monitor or {}).get("id", "")),
            object_type=str((monitor or {}).get("__typename", "")),
        )

    def _lagging(self, finding: ChannelCountFinding, monitor: dict[str, Any] | None) -> None:
        """A degree monitor whose section has moved on since the reading was taken.

        Reported and not gated. The reading is dated and the wavelength is newer
        than it, so the monitor cannot have seen it. That is the ordinary gap
        between designing a wavelength and turning it up, and it is what every
        provisioning branch in this demo looks like between the generator running
        and the field lighting the channel. The message says why it does not
        block, because a finding a reader cannot tell apart from a refusal is one
        they will treat as a refusal.
        """
        behind = abs(finding.difference)
        self.log_info(
            message=(
                f"{finding.device} {finding.monitor} reports {finding.reported} channels and "
                f"{finding.compared_against} carries {finding.observed}, so the monitor is {behind} behind "
                "the topology. A reading older than the design can only under-report, so this is a wavelength "
                "designed and not yet turned up rather than a record being wrong, and it does not block the "
                "merge. Take a fresh reading when the channel is lit"
            ),
            object_id=str((monitor or {}).get("id", "")),
            object_type=str((monitor or {}).get("__typename", "")),
        )

    def _deferred(self, monitor: dict[str, Any], device: str, watched: str | None) -> None:
        """A monitor with no port to watch, deferred rather than reported twice.

        `monitor_completeness` owns presence in both directions and reports this
        one as an error. Raising here as well would put two findings on one
        fault, and a reader fixing the second would find the first already
        answered. There is no level between the two: `InfrahubCheck` offers
        `log_error`, which blocks, and `log_info`, which does not.
        """
        reading = monitor.get("channel_count")
        missing = f"no degree port called {watched}" if watched else "a name that fits no degree port"
        self.log_info(
            message=(
                f"{device} {monitor['name']} reports {reading} channels and the device carries {missing}, "
                "so there is nothing here to compare it against. An orphan monitor is reported by "
                "monitor_completeness, which owns presence in both directions"
            ),
            object_id=str(monitor.get("id", "")),
            object_type=str(monitor.get("__typename", "")),
        )

    def _unresolved(self, monitor: dict[str, Any], device: str, watched: str, far: str | None) -> None:
        """A degree that exists and faces nowhere. No other check covers it.

        Two ways in and they read the same to an operator: the port name fits no
        far site, or it names one that no section reaches from this ROADM. Both
        leave a monitor reporting a number about a fibre nobody can find, so both
        are errors rather than an INFO. `monitor_completeness` is silent here,
        because the port and its monitor are both present.
        """
        facing = f"faces site {far} and no optical multiplex section joins the two" if far else "names no far site"
        self.log_error(
            message=(
                f"{device} {monitor['name']} watches degree port {watched}, which {facing}, so its "
                f"{monitor.get('channel_count')} channels are reported about a fibre that cannot be identified. "
                "Nothing else reports a degree pointing at no section"
            ),
            object_id=str(monitor.get("id", "")),
            object_type=str(monitor.get("__typename", "")),
        )

    def _dense(self, dense: int) -> None:
        """One INFO for the whole set of dense multiplexers, said out loud.

        Once rather than per monitor, because it is one statement about the model
        and fourteen copies of it would bury the findings that are about data.
        """
        self.log_info(
            message=(
                f"{dense} dense multiplexer monitor(s) were not compared. A dense AWG holds no relationship to "
                "the carriers passing through it, in either direction and at any depth, so the model offers "
                "nothing to compare their counts against. Their coarse siblings are compared against the "
                "wavelengths they light"
            )
        )

    def _summarise(self, compared: int, defects: int, lagging: int, dense: int, deferred: int, unresolved: int) -> None:
        """One INFO line saying what was judged, including when that is nothing.

        The counts are separate statements. A single "checked N monitors" would
        let them hide inside each other, and a branch where every monitor was
        uncomparable is not a branch where every monitor agrees.

        The two directions are counted apart for the same reason. A green run on
        a provisioning branch has monitors that have not caught up with the
        topology, and folding those into the number that agreed would report a
        network in a state nobody can see from here.
        """
        if not compared and not dense and not deferred and not unresolved:
            self.log_info(
                message=(
                    "No channel monitor on this branch, so no reported count can disagree with the topology. "
                    "Whether a device should be carrying one is monitor_completeness"
                )
            )
            return
        agreeing = compared - defects - lagging
        self.log_info(
            message=(
                f"Compared {compared} channel monitor(s) against what the topology puts on their fibre. "
                f"{agreeing} agree, {defects} disagree in a way a dated reading cannot explain and are "
                f"refused, and {lagging} have not yet seen a wavelength the topology holds and are not. A "
                "reading older than the design can only under-report. "
                f"{dense + deferred + unresolved} were not compared: {dense} on a dense multiplexer the model "
                f"holds no carrier relationship for, {deferred} watching no port on their device and "
                f"{unresolved} facing a site with no section"
            )
        )
