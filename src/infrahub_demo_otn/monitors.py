"""Which monitor belongs to what, and whether what it reports is what is there.

The one implementation of the monitor naming convention and of the two counts a
monitor is judged against. The generator writes the names, `monitor_completeness`
reads them back, and `channel_count_consistency` uses both, so the three cannot
disagree about what a degree monitor is called or about how many channels ride a
fibre.

Pure functions over plain mappings. Nothing here imports `infrahub_sdk`, touches
the network or reads a file, so its tests need no server and no Docker. No scale
factor either: this module converts no units, and every number in it is a count
of things.

## The native-first record

The constitution requires the question "can Infrahub do this" to be asked in
writing before any Python lands. It was, per rule, and the full entries are
`R-001` to `R-005` in `specs/024-monitor-consistency-checks/research.md`.

**Can the schema carry "this device must have a monitor"? No (R-001).** A device
reaches its ports through `ports`, which peers the generic `OtnGenericPort`, and
a relationship to a generic cannot be constrained, filtered or counted by the
kind of its peers. Five probes against Infrahub 1.11.0 are recorded in
`specs/feature-gaps/03-filter-a-nested-relationship-by-kind.md`: `kind`,
`typename__value`, `__typename` and an attribute declared on a sibling generic
are all rejected as arguments. The schema has no way to say "at least one peer of
`ports` is an `OtnAmplifierMonitor`" because it has no way to name a peer's kind
at all. A second, kind-specific relationship beside `ports` would work and would
put one object on two edges that can disagree, so it was refused. The
counterweight applies as well: a schema constrains what is written, so it cannot
notice a gap, and this rule is entirely about absence.

**Can the schema carry "channel_count equals the carriers on the section"? No
(R-002).** The rule compares a value on one node against a count over a many
relationship reached from a different node. `uniqueness_constraints` is over a
node's own attributes and has no aggregate form: no count, no sum, and no way to
name "all the carriers on that section" as an operand. This is measured in
`specs/feature-gaps/05-aggregate-constraints.md`. A transform-backed computed
attribute would work technically and would delete the feature, because
`channel_count` is a measurement and making it a derivation makes the monitor
agree with the topology by construction.

**Can a GraphQL query answer either question on its own? No (R-003).**
`monitor_completeness` cannot even fetch the offending set, because "amplifiers
with no `OtnAmplifierMonitor` among their ports" needs the kind filter R-001
measured absent. `channel_count_consistency` can fetch both sides and cannot
compare them: GraphQL has no aggregate and no cross-node predicate.

**Is `client.traverse_paths` the right tool for the degree-to-section join? No
(R-004).** Traversal answers reachability over a path of relationships. The
degree port and the section have no relationship between them, in either
direction and at any depth, so there is no edge for a traversal to find.

**The degree-to-section join uses a name suffix, and that is a naming convention
doing part of a relationship's job (R-005).** The model holds no relationship
from a degree port to a section. What it holds is the port's name, `DEG-<FAR>`,
the section's `roadm_a` and `roadm_b`, and each ROADM's `site`. So the far end is
recovered structurally and only the *selection* of which section a degree faces
comes from the name. `OtnRoadmDegreePort.section` is the right long-term answer
and is recorded as the next change to make, not as something refused on the
merits. Feature 016 is the scar: a container's owning service was encoded in its
name, and a report listed a neighbour's containers as part of a service's
circuit. What makes the suffix safe in the meantime is that the failure there was
a convention nothing declared and everything assumed. This module declares it
once, in both directions, and `tests/unit/test_monitors.py` binds the two with a
round-trip test, so the generator that writes the name and the check that reads
it cannot drift apart.

**The join folds case, because the schema does not.** `LocationGeneric.shortname`
is a plain `Text` with no regex, so a site may be created as `fra`, `FRA` or
`Fra`, and a degree port name carries the far site in upper case and loses which
one it was. `site_key` is the folded form the two sides meet on, and both
`far_site_of_degree` and `sections_by_roadm` go through it. Folded on one side
only, a site created in any case but lower matched nothing, and every degree
monitor facing it was refused by `channel_count_consistency` as facing a site
with no section: a blocked merge on data that is correct, which is worse than the
defect that check was written to catch. A regex on the shortname would settle it
at write time and is the better answer; it is a migration on live data and so is
recorded here rather than made.

`src/infrahub_demo_otn/drift.py` confirms the gap R-001 describes, in its own
words, about the case this module's caller now covers:

> A device with no monitor is skipped rather than reported as drifting.
> Reporting it here as a shortfall would be inventing a measurement of zero.
> Nothing else ever covered that case either: the completeness check this
> repository used to carry iterated over monitors, so a device with no monitor
> was invisible to it.

## Channels, not carriers

`channels_by_section` returns the size of a set of channel anchors, not a count
of carrier records. An optical channel monitor counts light on a fibre, and two
carriers sharing one anchor are one channel to it. They are also a fault, and
`checks/channel_collision.py` already reports it: counting records here would
raise a second finding for a fault another check has named, against a channel
count that is correct. On the shipped dataset the two counts are identical
because every carrier draws a fresh anchor. They diverge only on a branch that
has collided, which is exactly where the wrong answer would do damage.

## Which direction is a defect

`channel_count_findings` returns every disagreement and says of each one whether
it is somebody's data being wrong. The two directions are not the same fault:

- **A monitor reporting more channels than the fibre carries claims light that is
  not there.** Either the reading is stale, which is the 71 this feature repairs,
  or a wavelength was taken out of the model and the equipment was left alone.
  Both are a record being wrong, and `is_defect` is true.
- **A monitor reporting fewer is a reading the design has moved past.** The
  generator writes a carrier onto a branch and touches no monitor, so the degree
  monitors along that route each sit one channel behind the section they face
  until the field turns the wavelength up. `is_defect` is false.

A reading older than the design can only ever under-report, so the direction the
clock explains is the one that is not a defect. The direction it cannot explain is
left. That is what makes the asymmetry a rule about time rather than a leniency
somebody chose, and the spec records the alternative that was refused: counting
only carriers whose `status` is `active` looks tidier and does not work, because
nothing in this repository moves a carrier from `planned` to `active` on merge.
A generator-provisioned wavelength is written `planned` and stays there until the
field turns it up, so an `active` filter would under-count every corridor the
generator has ever lit.

`reading_can_lag` is per comparison, because the argument is about the subject and
not about the arithmetic. A degree monitor is judged against a live design, so its
reading can lag. A coarse multiplexer monitor is judged against `cwdm_channels`,
the fixed set of wavelengths its filter passes, and nothing provisions one of
those onto a branch. There is no clock to explain either direction there, so both
are defects and the caller passes `reading_can_lag: False`.

## What this module states negatively

A name that does not fit the convention comes back as `None` rather than a guess,
and the caller reports it. A section carrying nothing comes back as `0` rather
than absent, because a monitor skipped for want of a figure is a monitor nobody
checked. A count that could not be taken is never passed off as zero:
`channel_count_findings` refuses a comparison with a missing side instead of
subtracting `None` into a plausible number.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

DEGREE_PREFIX = "DEG-"
"""What every ROADM degree port name starts with. The rest of the name is the far
site's shortname in upper case."""

MONITOR_PREFIX = "MON-"
"""What a monitor's name puts in front of the name of the port it watches."""

MONITOR_BY_DEVICE_KIND: Mapping[str, str] = {
    "OtnAmplifier": "OtnAmplifierMonitor",
    "OtnRamanPump": "OtnRamanMonitor",
    "OtnTransponder": "OtnReceiverMonitor",
    "OtnMuxDemux": "OtnMuxDemuxMonitor",
    "OtnRoadmDegreePort": "OtnRoadmDegreeMonitor",
}
"""What carries a monitor, and which monitor kind it carries.

Five pairings, and the fifth key is a **port** kind rather than a device kind. A
ROADM carries one monitor per degree rather than one per device, so the fifth row
is applied per port while the other four are applied per device.

It is in this table anyway, and the name of the constant is the compromise. A
reader asking "what in this model is expected to carry a monitor" must find one
answer in one place; a second table for the one row that is shaped differently is
how a sixth pairing gets added to whichever table the next person happened to
open. `PER_PORT_KINDS` names the row that needs the other loop, so a caller can
tell the two apart without matching on a kind name it typed itself.

Everything absent from this table is absent deliberately, and `KINDS_NOT_JUDGED`
names the three device kinds a reader is most likely to expect here.
"""

PER_PORT_KINDS: frozenset[str] = frozenset({"OtnRoadmDegreePort"})
"""The rows of `MONITOR_BY_DEVICE_KIND` whose subject is a port on a device
rather than the device itself. The table cannot say this about itself, because
both halves are kind names and nothing in the string distinguishes them."""

KINDS_NOT_JUDGED: tuple[str, ...] = ("OtnRouter", "OtnPatchPanel", "OtnOduSwitch")
"""Device kinds that carry no monitor and are not expected to.

Named rather than merely left out, because a completeness check whose summary
says only "passed" is worth nothing. The summary names these so a reader can see
the boundary of what was judged instead of inferring it from silence."""


@dataclass(frozen=True)
class ChannelCountFinding:
    """One monitor whose reported channel count disagrees with the topology.

    Carries both numbers, what they were compared against and which way the
    disagreement points, not a boolean. A finding that said only "wrong" would
    send the reader to look up the two figures the check already held, and a
    caller that had to subtract the two again to learn the direction would be
    free to reach a different verdict from the one this module took.
    """

    monitor: str
    device: str
    compared_against: str
    reported: int
    observed: int
    reading_can_lag: bool = True
    """Whether this subject is judged against something a branch can add to while
    the reading stands still. True for a degree monitor against its section,
    False for a coarse multiplexer against the wavelengths its filter passes."""

    @property
    def difference(self) -> int:
        """Reported less observed. Positive means the monitor claims more light
        than the topology puts on that fibre."""
        return self.reported - self.observed

    @property
    def over_reports(self) -> bool:
        """Whether the monitor claims more channels than were counted against it."""
        return self.difference > 0

    @property
    def is_defect(self) -> bool:
        """Whether this disagreement is a record being wrong rather than a lag.

        Over-reporting always is: no reading can invent light. Under-reporting is
        only a defect where the clock cannot account for it, which is where the
        thing being counted cannot change after the reading was taken.
        """
        return self.over_reports or not self.reading_can_lag


@dataclass(frozen=True)
class MissingMonitor:
    """One object of a kind that should carry a monitor, carrying none."""

    name: str
    kind: str
    monitor_kind: str
    device: str = ""
    """The device holding the subject, for the one row whose subject is a port.

    A port name is unique on its device and nowhere else. `(device, name)` is the
    uniqueness constraint on `OtnGenericPort`, and thirteen degree names repeat
    across the fifteen shipped sites, `DEG-FRA` on six ROADMs. So a finding that
    carried the port name alone could not say which of the six is uncovered, and
    a caller keying its owner lookup on that name would send an operator to
    whichever ROADM it read last. That is what this field exists to stop.

    Empty for the four rows whose subject is the device itself, because there the
    name is the identity on its own and there is no second object to name.
    """


def site_key(shortname: str) -> str:
    """The form a site shortname takes when it is used as a degree join key.

    `LocationGeneric.shortname` in `schemas/location.yml` is a plain `Text` with
    no regex and no case constraint, so a site may be created as `fra`, `FRA` or
    `Fra`. `degree_port_name` upper-cases whatever it is handed and
    `far_site_of_degree` folds it back down, so the two sides of the join can
    only meet on a folded form, and this is that form.

    One function rather than a `.lower()` at each end. The day the two ends fold
    differently is the day every degree at a site resolves to no section and
    `channel_count_consistency` blocks a merge on correct data, which is the
    failure this replaced. A regex on the schema would remove the ambiguity at
    write time and is the better answer, and it is a migration on live data
    rather than a change this feature can make.
    """
    return shortname.strip().casefold()


def degree_port_name(far: str) -> str:
    """The name of the degree port a ROADM points at the site `far`.

    `far` is a site shortname as the model stores it and the name is that
    shortname in upper case behind `DEG-`. The shipped shortnames are lower case
    and the schema does not require it, so the round trip through
    `far_site_of_degree` is case-folding rather than identity: `FRA`, `Fra` and
    `fra` all write `DEG-FRA` and all read back as `site_key`'s folded form. A
    test binds the pair over mixed case for exactly that reason.

    Raises `ValueError` on an empty shortname, which is the one asymmetry with
    the parsing direction and it is deliberate. Parsing reads data that may be
    wrong and must hand back `None` rather than take a whole check run down.
    Building is called by the generator with a value it already holds, and an
    empty one there is a bug that would otherwise write `DEG-`, a name shaped
    like a real one that resolves to nothing.
    """
    shortname = far.strip()
    if not shortname:
        raise ValueError("a degree port name needs a far site shortname and was given an empty one")
    return f"{DEGREE_PREFIX}{shortname.upper()}"


def far_site_of_degree(port_name: str) -> str | None:
    """The shortname of the site a degree port faces, folded, or `None`.

    `None` for any name whose prefix does not fit `DEG-`, including one that
    differs only in case: the convention is upper case behind an upper-case
    prefix, and accepting `deg-fra` here would be guessing that the writer meant
    the convention. The caller reports the name it could not read.

    The suffix is a different matter, and it is folded rather than refused. The
    schema puts no case constraint on a site shortname, so the writer's case is
    not evidence of anything and the only usable answer is the join key. The
    result is `site_key`'s form, which is what `sections_by_roadm` keys by.
    """
    if not port_name.startswith(DEGREE_PREFIX):
        return None
    return site_key(port_name[len(DEGREE_PREFIX) :]) or None


def monitor_port_name(port_name: str) -> str:
    """The name of the monitor that watches the port `port_name`.

    Raises on an empty port name, on the same terms as `degree_port_name`.
    """
    name = port_name.strip()
    if not name:
        raise ValueError("a monitor name needs the name of the port it watches and was given an empty one")
    return f"{MONITOR_PREFIX}{name}"


def degree_of_monitor(monitor_name: str) -> str | None:
    """The name of the port a monitor watches, or `None` if the name does not fit.

    The inverse of `monitor_port_name`, and it strips one prefix rather than
    parsing, so it is the port name back exactly. It says nothing about whether a
    port of that name exists: that is presence, and `monitor_completeness` owns
    presence in both directions.
    """
    if not monitor_name.startswith(MONITOR_PREFIX):
        return None
    suffix = monitor_name[len(MONITOR_PREFIX) :]
    return suffix or None


def channels_by_section(
    carriers: Iterable[Mapping[str, Any]],
    sections: Iterable[str],
) -> dict[str, int]:
    """How many distinct channels ride each section.

    Each carrier is a mapping with `channel`, whatever identifies the anchor it
    sits on, and `sections`, the names of the fibres it rides. The anchor only
    has to be hashable and consistent: the check passes the channel's centre
    frequency and the generator passes the same, and neither cares what the value
    is beyond two carriers on one anchor comparing equal.

    `sections` is every section name that must appear in the result. Passing it
    is what makes an empty section report `0` rather than fall out of the mapping
    entirely, and the two are not the same statement: a caller reading a missing
    key as "nothing to check" skips a monitor, and a skipped monitor is an
    unchecked monitor. A section named by a carrier and not by the caller is
    added anyway, because dropping it would report that fibre as carrying less
    than it does.

    Correlation runs from the carrier side because
    `OtnOpticalMultiplexSection` declares no inverse of `OtnOpticalCarrier.sections`,
    so a section cannot be asked what rides it. `queries/channel_occupancy.gql`
    already fetches the two as separate collections for the same reason.
    """
    return _distinct_by_group(carriers, "sections", sections)


def channels_terminating_by_site(
    carriers: Iterable[Mapping[str, Any]],
    sites: Iterable[str],
) -> dict[str, int]:
    """How many distinct channels terminate at each site.

    Each carrier is a mapping with `channel`, the same anchor as above, and
    `endpoints`, the shortnames of the sites its route starts and ends at. A
    carrier counts at both of its ends, so the figures across all sites sum to
    twice the number of channels.

    This is what a dense AWG multiplexer at a site should see. The graph holds no
    relationship from such a multiplexer to the carriers it lights, so the number
    comes from the carrier plan the generator holds rather than from anything a
    check can query. That is why `channel_count_consistency` is silent about
    those fourteen units: the silence is a property of the model holding no
    relationship, not of an `if` somebody can delete.

    `sites` is every site shortname that must appear in the result, on the same
    terms and for the same reason as `sections` above.
    """
    return _distinct_by_group(carriers, "endpoints", sites)


def _distinct_by_group(
    carriers: Iterable[Mapping[str, Any]],
    group_field: str,
    groups: Iterable[str],
) -> dict[str, int]:
    """Distinct channel anchors per group, for whichever field names the groups.

    The two public callers differ in one key and in nothing else. Written twice,
    one of them starts counting carrier records the day somebody simplifies it,
    and that is the mistake this module exists to prevent.
    """
    anchors: dict[str, set[Any]] = {str(name): set() for name in groups}
    for carrier in carriers:
        anchor = carrier.get("channel")
        if anchor is None:
            name = carrier.get("name", "<unnamed>")
            raise ValueError(f"carrier {name} names no channel, so the light on it cannot be counted")
        for group in carrier.get(group_field) or ():
            anchors.setdefault(str(group), set()).add(anchor)
    return {name: len(seen) for name, seen in anchors.items()}


def sections_by_roadm(sections: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Every ROADM, against the far sites it faces and the section at each.

    Each section is a mapping with `name`, `roadm_a`, `site_a`, `roadm_b` and
    `site_b`, where the two site keys are the shortnames of the sites the two
    ROADMs sit at. The result is the lookup a degree port needs:
    `sections_by_roadm(...)[near_roadm][far_site_of_degree(port_name)]`.

    **The inner mapping is keyed by `site_key`, not by the shortname as stored.**
    A degree port name carries the far site in upper case and nothing else, so
    the case the site was created with is not recoverable from it, and the schema
    puts no case constraint on a shortname. Keyed as stored, a site created as
    `FRA` matched nothing the parsing direction could ever produce, and every
    degree monitor facing it was refused as facing a site with no section, which
    blocks a merge on data that is correct. The ROADM names are keyed as they
    come, because both sides of that half of the lookup read the same field.

    An end with no ROADM or no site is skipped in that direction alone. It
    surfaces as a degree resolving to no section, which the check reports as an
    error, so raising here would take a whole run down to say something the
    caller already says about one degree.

    **Two sections between the same pair of ROADMs cannot both be held.** The
    inner mapping is keyed by far site, so the second overwrites the first. The
    shipped topology has one section per pair and this never fires, but it is the
    second reason `OtnRoadmDegreePort.section` is the right long-term answer:
    resolving by site can only ever name one fibre between two places, and a
    relationship would name the one the degree is actually spliced to.
    """
    faced: dict[str, dict[str, str]] = {}
    for section in sections:
        name = str(section.get("name") or "")
        if not name:
            raise ValueError("a section with no name cannot be resolved to a degree")
        for near, far in (("a", "b"), ("b", "a")):
            near_roadm = section.get(f"roadm_{near}")
            far_site = section.get(f"site_{far}")
            if not near_roadm or not far_site:
                continue
            faced.setdefault(str(near_roadm), {})[site_key(str(far_site))] = name
    return faced


def channel_count_findings(comparisons: Iterable[Mapping[str, Any]]) -> list[ChannelCountFinding]:
    """The comparisons where the monitor and the topology disagree.

    Each comparison is a mapping with `monitor`, `device`, `compared_against`,
    `reported` and `observed`, and optionally `reading_can_lag`, which defaults to
    true. The caller decides what a monitor is compared against, using the naming
    convention and the two lookups above; this function does the subtraction and
    says which of the two directions the result points, so no caller does either
    twice and reaches a different verdict from the generator that wrote the
    number.

    Both directions come back. A finding is not a refusal: `is_defect` on each one
    says whether it is a record being wrong or a reading the design has moved
    past, and the caller turns the first into an error and the second into an
    observation that blocks nothing.

    A comparison with a missing side raises rather than being skipped or read as
    zero. A monitor that could not be compared is not a comparison, and the
    caller has a verdict for that case: an unresolvable degree is an error and an
    unmatched monitor defers to `monitor_completeness`. Letting `None` through
    here would turn either into a confident finding of the difference between a
    real reading and a number nobody took.

    Sorted with the defects first, then by the size of the disagreement, then by
    monitor name, so the findings a reader has to act on come before the ones
    that resolve themselves when somebody takes a fresh reading. Agreement
    produces no finding, and an empty list is the whole of a passing run.
    """
    findings = []
    for comparison in comparisons:
        monitor = str(comparison.get("monitor", "<unnamed>"))
        reported = comparison.get("reported")
        observed = comparison.get("observed")
        if reported is None or observed is None:
            raise ValueError(f"{monitor} has no count on one side, so there is nothing to compare")
        findings.append(
            ChannelCountFinding(
                monitor=monitor,
                device=str(comparison.get("device", "<unnamed>")),
                compared_against=str(comparison.get("compared_against", "<unnamed>")),
                reported=int(reported),
                observed=int(observed),
                reading_can_lag=bool(comparison.get("reading_can_lag", True)),
            )
        )
    return sorted(
        (finding for finding in findings if finding.difference != 0),
        key=lambda finding: (not finding.is_defect, -abs(finding.difference), finding.monitor),
    )


def missing_monitors(subjects: Iterable[Mapping[str, Any]]) -> list[MissingMonitor]:
    """Every subject of a kind that should carry a monitor and carries none.

    Each subject is a mapping with `name`, `kind` and `monitors`, the kinds of
    the monitors attached to it, and optionally `device`. A caller may pass the
    kinds of every port on the device: anything that is not the expected monitor
    kind is ignored rather than counted against the subject.

    The four device rows are passed one subject per device and name no `device`,
    because there the subject is the device. The fifth row is a port kind, so a
    ROADM is passed one subject per degree port, with `monitors` holding
    `OtnRoadmDegreeMonitor` when a monitor named `monitor_port_name(port)` is on
    that ROADM. The name matching stays with the caller and uses this module's
    convention, so there is still one definition of what a degree monitor is
    called.

    **A per-port subject must name its device.** A port name is unique on its
    device and nowhere else, and six shipped ROADMs carry a degree called
    `DEG-FRA`. Two of them uncovered are two findings, and without `device` they
    are two identical ones that no reader can tell apart.

    A subject of a kind outside `MONITOR_BY_DEVICE_KIND` produces nothing. That
    silence is deliberate and the caller states it: `KINDS_NOT_JUDGED` names the
    three device kinds that carry no monitor, so a passing run says what it
    judged instead of leaving a reader to infer it.

    Sorted by kind and then by name, with the device breaking the tie, so a run
    over a whole network groups the findings by what went missing and two ROADMs
    short of the same degree monitor still come back in a stable order.
    """
    missing = []
    for subject in subjects:
        kind = str(subject.get("kind") or "")
        expected = MONITOR_BY_DEVICE_KIND.get(kind)
        if expected is None:
            continue
        attached = {str(monitor) for monitor in subject.get("monitors") or ()}
        if expected in attached:
            continue
        missing.append(
            MissingMonitor(
                name=str(subject.get("name") or "<unnamed>"),
                kind=kind,
                monitor_kind=expected,
                device=str(subject.get("device") or ""),
            )
        )
    return sorted(missing, key=lambda finding: (finding.kind, finding.name, finding.device))
