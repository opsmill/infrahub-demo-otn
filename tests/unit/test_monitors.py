"""`monitors.py`, which is one naming convention and four counts over it.

The convention is the reason this module exists. `OtnRoadmDegreePort` holds no
relationship to the section it faces, so the join runs through the port's name
suffix, and a naming convention doing part of a relationship's job is an
anti-pattern this repository has already been bitten by: feature 016 encoded a
container's owning service in its name and a report listed a neighbour's
containers as part of a service's circuit.

The failure there was a convention that nothing declared and everything assumed.
So the first test here is a round trip. `degree_port_name` writes the name and
`far_site_of_degree` reads it back, over every site in the shipped dataset, and
the same for the monitor prefix over every degree port that ships. The generator
calls one and the check calls the other, and this is what stops the two drifting
apart while both keep passing their own tests.

What else earns a place, each being a change somebody will make in good faith:

- Counting carriers instead of channel anchors. The two are identical on the
  shipped dataset and diverge exactly on a branch that has collided, which is
  where the wrong answer would do damage.
- A section with no carriers falling out of the mapping instead of reporting
  zero. A caller that reads a missing key as "nothing to check" skips the
  monitor, and a skipped monitor is an unchecked monitor.
- A name that does not fit being guessed at rather than refused.
- A finding that carries a boolean instead of both numbers and the difference.
- The comparison going back to being symmetric. A monitor reading is dated and
  can only ever under-report a design newer than itself, so over-reporting is a
  record being wrong and under-reporting is a branch mid-flight. Collapsing the
  two puts a refusal on every provisioning branch in this demo.

The shipped topology is read rather than restated, so a fifteenth site or a
twenty-second section fails here instead of in a check nobody runs until the
pipeline does.
"""

from typing import Any

import pytest
import yaml

from infrahub_demo_otn.monitors import (
    KINDS_NOT_JUDGED,
    MONITOR_BY_DEVICE_KIND,
    PER_PORT_KINDS,
    ChannelCountFinding,
    MissingMonitor,
    channel_count_findings,
    channels_by_section,
    channels_terminating_by_site,
    degree_of_monitor,
    degree_port_name,
    far_site_of_degree,
    missing_monitors,
    monitor_port_name,
    sections_by_roadm,
    site_key,
)
from tests.unit.conftest import objects_of_kind, schema_files

SECTION_LOAD = {
    "oms-fra-mil": 40,
    "oms-ams-fra": 7,
    "oms-ber-fra": 5,
    "oms-par-fra": 3,
    "oms-vie-mil": 3,
}
"""The five loaded sections and what rides them. The other sixteen carry
nothing, and the test below asserts that rather than ignoring them."""


def _site_of_roadm() -> dict[str, str]:
    """Every ROADM against the shortname of the site it sits at."""
    return {str(roadm["name"]): str(roadm["site"]) for roadm in objects_of_kind("OtnRoadm")}


def _sections() -> list[dict[str, Any]]:
    """The shipped sections in the shape `sections_by_roadm` reads.

    The two site keys come from the ROADM records rather than from the section
    name, which is the point: the far end is recovered structurally and only the
    selection of which section a degree faces comes from a name.
    """
    site_of = _site_of_roadm()
    return [
        {
            "name": section["name"],
            "roadm_a": section["roadm_a"],
            "site_a": site_of[section["roadm_a"]],
            "roadm_b": section["roadm_b"],
            "site_b": site_of[section["roadm_b"]],
        }
        for section in objects_of_kind("OtnOpticalMultiplexSection")
    ]


def _carriers() -> list[dict[str, Any]]:
    """The shipped carriers in the shape `channels_by_section` reads.

    `channel` is the frequency grid the carrier is anchored to. The check passes
    the anchor's centre frequency instead; both are identities of the same
    anchor, and the function only requires that two carriers on one anchor
    compare equal.
    """
    return [
        {"name": carrier["name"], "channel": carrier["channel"], "sections": carrier.get("sections") or []}
        for carrier in objects_of_kind("OtnOpticalCarrier")
    ]


def _declared_kinds() -> set[str]:
    """Every kind the schema declares, as namespace and name joined."""
    kinds: set[str] = set()
    for path in schema_files():
        document = yaml.safe_load(path.read_text())
        for section in ("generics", "nodes"):
            for entry in document.get(section) or []:
                namespace, name = entry.get("namespace"), entry.get("name")
                if namespace and name:
                    kinds.add(f"{namespace}{name}")
    return kinds


# ---------------------------------------------------------------------------
# Invariant 1: the convention round trips, over the shipped dataset
# ---------------------------------------------------------------------------


def test_every_site_shortname_survives_being_written_into_a_degree_port_name() -> None:
    """The generator writes `degree_port_name` and the check reads
    `far_site_of_degree`. If either drifts, the check stops resolving degrees to
    sections and reports errors against a topology that is correct.

    Read from `objects/` rather than from a list here, so a sixteenth site fails
    this test instead of failing silently in a check."""
    shortnames = sorted(str(site["shortname"]) for site in objects_of_kind("OtnSite"))
    assert len(shortnames) == 15, shortnames
    for shortname in shortnames:
        assert far_site_of_degree(degree_port_name(shortname)) == shortname


def test_every_shipped_degree_port_name_survives_being_written_into_a_monitor_name() -> None:
    """The other half of the pair, over the 42 degree ports that ship.
    `monitor_port_name` is what the generator names the monitor and
    `degree_of_monitor` is what the check matches it back with."""
    port_names = sorted(str(port["name"]) for port in objects_of_kind("OtnRoadmDegreePort"))
    assert len(port_names) == 42, len(port_names)
    for port_name in port_names:
        assert degree_of_monitor(monitor_port_name(port_name)) == port_name


@pytest.mark.parametrize("shortname", ["FRA", "Fra", "fRa", "MIL", "Mil", "fra"])
def test_a_shortname_in_any_case_round_trips_to_one_join_key(shortname: str) -> None:
    """The shipped shortnames are all lower case, so iterating them proves nothing
    about case and gives false confidence.

    `LocationGeneric.shortname` in `schemas/location.yml` is a plain `Text` with
    no regex and no case constraint, so a site can be created as `FRA` or `Fra`.
    `degree_port_name` upper-cases whatever it is handed and `far_site_of_degree`
    folds it back down, so the round trip is case-folding rather than identity,
    and every case of one shortname has to land on the same join key. It did not
    before: the section map was keyed by the shortname as stored, so a degree at
    an upper-case site resolved to no section and its monitor was refused on data
    that was correct.
    """
    port_name = degree_port_name(shortname)
    assert port_name == f"DEG-{shortname.upper()}"
    assert far_site_of_degree(port_name) == site_key(shortname)
    assert far_site_of_degree(port_name) == far_site_of_degree(degree_port_name(shortname.lower()))


def test_a_section_end_at_an_upper_case_site_is_keyed_the_same_as_a_lower_case_one() -> None:
    """The other half of the fold, at the place the lookup is built.

    Folding only in `far_site_of_degree` leaves the two sides normalised
    differently, which is the bug in a different spot rather than a fix."""
    faced = sections_by_roadm(
        [
            {
                "name": "oms-fra-mil",
                "roadm_a": "roadm-fra-01",
                "site_a": "FRA",
                "roadm_b": "roadm-mil-01",
                "site_b": "Mil",
            }
        ]
    )
    assert faced == {
        "roadm-fra-01": {"mil": "oms-fra-mil"},
        "roadm-mil-01": {"fra": "oms-fra-mil"},
    }
    assert faced["roadm-mil-01"][far_site_of_degree("DEG-FRA") or ""] == "oms-fra-mil"


def test_the_shipped_degree_port_names_are_the_ones_the_convention_would_write() -> None:
    """Not only that the pair is self-consistent, but that it agrees with the
    dataset on disk. A convention that round trips against itself and matches
    nothing in `objects/` would pass the two tests above and resolve no degree."""
    for port in objects_of_kind("OtnRoadmDegreePort"):
        far = far_site_of_degree(str(port["name"]))
        assert far is not None, port["name"]
        assert degree_port_name(far) == port["name"]


# ---------------------------------------------------------------------------
# Invariant 2: a name that does not fit is refused, not guessed at
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("port_name", ["DEGFRA", "deg-fra", "MON-DEG-FRA", "DEG-", "", "CLIENT-1"])
def test_a_degree_port_name_that_does_not_fit_returns_none(port_name: str) -> None:
    """`None` is a verdict the caller reports. Guessing at `deg-fra` would let a
    name that breaks the convention resolve anyway, which is how a convention
    stops being one."""
    assert far_site_of_degree(port_name) is None


@pytest.mark.parametrize("monitor_name", ["DEG-FRA", "mon-DEG-FRA", "MON-", "", "MONDEG-FRA"])
def test_a_monitor_name_that_does_not_fit_returns_none(monitor_name: str) -> None:
    """Same rule in the other direction. An unreadable name is a finding, and a
    finding needs the check to still be running to report it."""
    assert degree_of_monitor(monitor_name) is None


def test_building_a_name_from_nothing_raises_rather_than_producing_a_shape() -> None:
    """The one asymmetry, and the reason for it. `DEG-` looks like a name and
    resolves to nothing, so the generator must not be allowed to write one. The
    parsing direction reads data that may already be wrong and hands back `None`
    instead of taking a whole check run down."""
    with pytest.raises(ValueError, match="far site shortname"):
        degree_port_name("")
    with pytest.raises(ValueError, match="name of the port"):
        monitor_port_name("   ")


# ---------------------------------------------------------------------------
# Invariant 3: channels are counted, not carrier records
# ---------------------------------------------------------------------------


def test_two_carriers_on_one_anchor_are_one_channel() -> None:
    """An optical channel monitor counts light on a fibre. Two carriers sharing
    an anchor are one channel to it, and they are also a collision that
    `checks/channel_collision.py` already reports. Counting records here would
    raise a second finding for that fault, against a channel count that is
    correct."""
    carriers = [
        {"name": "oc-a", "channel": "17", "sections": ["oms-fra-mil"]},
        {"name": "oc-b", "channel": "17", "sections": ["oms-fra-mil"]},
        {"name": "oc-c", "channel": "18", "sections": ["oms-fra-mil"]},
    ]
    assert channels_by_section(carriers, ["oms-fra-mil"]) == {"oms-fra-mil": 2}


def test_a_section_with_no_carriers_reports_zero_rather_than_being_absent() -> None:
    """Zero is a real reading: a dark degree sees no channels, and the schema's
    minimum is zero for that reason. A caller that read a missing key as nothing
    to check would skip the monitor, and a skipped monitor is an unchecked
    one."""
    counts = channels_by_section([], ["oms-ams-bru", "oms-fra-mil"])
    assert counts == {"oms-ams-bru": 0, "oms-fra-mil": 0}


def test_a_carrier_riding_two_sections_counts_on_both() -> None:
    """A wavelength from Amsterdam to Milan is light on every fibre it crosses,
    so both degree monitors along the way should see it."""
    carriers = [{"name": "oc-ch002-ams-mil", "channel": "2", "sections": ["oms-ams-fra", "oms-fra-mil"]}]
    assert channels_by_section(carriers, ["oms-ams-fra", "oms-fra-mil"]) == {
        "oms-ams-fra": 1,
        "oms-fra-mil": 1,
    }


def test_a_carrier_with_no_channel_raises_rather_than_counting_as_one() -> None:
    """`channel` is mandatory and cardinality one on the schema, so this is a
    programming error rather than data. Reading `None` as an anchor would merge
    every such carrier into one channel and under-report the fibre."""
    with pytest.raises(ValueError, match="names no channel"):
        channels_by_section([{"name": "oc-broken", "sections": ["oms-fra-mil"]}], ["oms-fra-mil"])


def test_the_shipped_carriers_produce_the_measured_section_loads() -> None:
    """The distribution the whole repair is aimed at: five loaded sections and
    sixteen dark ones, against 42 monitors that all report 71 today.

    Recomputed from `objects/` rather than restated, so a carrier added to the
    dataset without a matching monitor count fails here."""
    counts = channels_by_section(_carriers(), [str(section["name"]) for section in _sections()])
    assert len(counts) == 21, sorted(counts)
    assert {name: load for name, load in counts.items() if load} == SECTION_LOAD
    assert sum(1 for load in counts.values() if load == 0) == 16


def test_a_channel_terminating_at_a_site_counts_at_both_of_its_ends() -> None:
    """What a dense multiplexer at a site should see. A carrier terminates at two
    sites, so the figures across all sites sum to twice the channel count, and a
    site nobody terminates at reports zero rather than dropping out."""
    carriers = [
        {"name": "oc-a", "channel": "2", "endpoints": ["ams", "mil"]},
        {"name": "oc-b", "channel": "5", "endpoints": ["ams", "mil"]},
        {"name": "oc-c", "channel": "2", "endpoints": ["fra", "mil"]},
    ]
    counts = channels_terminating_by_site(carriers, ["ams", "mil", "fra", "par"])
    assert counts == {"ams": 2, "mil": 2, "fra": 1, "par": 0}
    # Milan terminates three carriers and sees two channels, because channel 2
    # arrives there twice: once from Amsterdam and once from Frankfurt. Counting
    # records would give it three and the multiplexer would be told to expect a
    # channel that is not lit.
    assert sum(counts.values()) == 5


# ---------------------------------------------------------------------------
# Invariant 4: every shipped degree resolves to a section
# ---------------------------------------------------------------------------


def test_every_degree_on_the_shipped_topology_resolves_to_exactly_one_section() -> None:
    """42 degree ports, 21 sections, two degrees per section and none unmatched.

    This is the measurement R-005 rests on. If it stops holding, the naming
    convention has stopped doing the relationship's job and the check starts
    reporting errors against a topology that is correct."""
    faced = sections_by_roadm(_sections())
    resolved: list[str] = []
    for port in objects_of_kind("OtnRoadmDegreePort"):
        near = str(port["device"])
        far = far_site_of_degree(str(port["name"]))
        assert far is not None, port["name"]
        assert near in faced, near
        section = faced[near].get(far)
        assert section is not None, f"{near} {port['name']} faces {far} and no section goes there"
        resolved.append(section)
    assert len(resolved) == 42
    assert len(set(resolved)) == 21
    assert sorted(set(resolved)) == sorted(str(section["name"]) for section in _sections())
    # Two degrees per section, one at each end, which is what makes the count of
    # distinct sections exactly half the count of degrees.
    assert all(resolved.count(section) == 2 for section in set(resolved))


def test_a_roadm_faces_the_far_site_and_not_its_own() -> None:
    """The lookup is keyed by the site at the other end. Keyed by the near site
    it would still resolve 42 degrees and match every one of them to the wrong
    fibre."""
    faced = sections_by_roadm(
        [
            {
                "name": "oms-fra-mil",
                "roadm_a": "roadm-fra-01",
                "site_a": "fra",
                "roadm_b": "roadm-mil-01",
                "site_b": "mil",
            }
        ]
    )
    assert faced == {
        "roadm-fra-01": {"mil": "oms-fra-mil"},
        "roadm-mil-01": {"fra": "oms-fra-mil"},
    }


def test_a_section_end_with_no_site_is_skipped_in_that_direction_only() -> None:
    """A ROADM with no site can still be faced, because facing it needs the site
    at the other end. Only the direction that would have to name the missing site
    drops out, and it drops out as a degree resolving to no section, which the
    check reports as an error. Raising here would take a whole run down to say
    something the caller already says about one degree."""
    faced = sections_by_roadm(
        [
            {
                "name": "oms-fra-mil",
                "roadm_a": "roadm-fra-01",
                "site_a": None,
                "roadm_b": "roadm-mil-01",
                "site_b": "mil",
            }
        ]
    )
    assert faced == {"roadm-fra-01": {"mil": "oms-fra-mil"}}


# ---------------------------------------------------------------------------
# Invariant 5: a finding carries both numbers, the difference and the direction
# ---------------------------------------------------------------------------


def test_a_disagreement_carries_the_difference_and_not_a_boolean() -> None:
    """The defect as it ships: a degree monitor reporting the stale 71 against a
    section carrying 40. The message a reader gets has to hold both numbers and
    the gap, or the check sends them to look up what it already knew."""
    findings = channel_count_findings(
        [
            {
                "monitor": "MON-DEG-MIL",
                "device": "roadm-fra-01",
                "compared_against": "oms-fra-mil",
                "reported": 71,
                "observed": 40,
            }
        ]
    )
    assert findings == [
        ChannelCountFinding(
            monitor="MON-DEG-MIL",
            device="roadm-fra-01",
            compared_against="oms-fra-mil",
            reported=71,
            observed=40,
        )
    ]
    assert findings[0].difference == 31
    assert findings[0].over_reports is True
    assert findings[0].is_defect is True


def test_agreement_produces_no_finding_including_at_zero() -> None:
    """Zero against zero is agreement, not an absence of data. A dark degree that
    reports no channels is correct and must stay silent."""
    assert (
        channel_count_findings(
            [
                {
                    "monitor": "MON-DEG-BRU",
                    "device": "roadm-ams-01",
                    "compared_against": "oms-ams-bru",
                    "reported": 0,
                    "observed": 0,
                },
                {
                    "monitor": "MON-DEG-MIL",
                    "device": "roadm-fra-01",
                    "compared_against": "oms-fra-mil",
                    "reported": 40,
                    "observed": 40,
                },
            ]
        )
        == []
    )


def test_a_monitor_reporting_fewer_channels_than_the_fibre_carries_is_a_finding_that_is_not_a_defect() -> None:
    """Both directions come back, and only one of them is somebody's data being wrong.

    This is the provisioning case reduced to two numbers. The generator writes a
    carrier onto a branch and touches no monitor, so the degrees along that route
    sit one channel behind the section they face. A reading older than the design
    can only ever under-report, so the clock explains this one and it is not a
    defect. Reported all the same: a monitor that has fallen behind is a thing a
    reader wants to see, it is just not a merge to refuse.
    """
    findings = channel_count_findings(
        [
            {"monitor": "MON-DEG-A", "device": "r1", "compared_against": "oms-a", "reported": 0, "observed": 1},
        ]
    )
    assert len(findings) == 1, findings
    assert findings[0].difference == -1
    assert findings[0].over_reports is False
    assert findings[0].is_defect is False


def test_the_defects_sort_ahead_of_the_readings_that_have_merely_fallen_behind() -> None:
    """Defects first, then by size. A reader acts on the refusals and waits out
    the lags, so a list that interleaves them by magnitude buries the half that
    needs doing."""
    findings = channel_count_findings(
        [
            {"monitor": "MON-DEG-A", "device": "r1", "compared_against": "oms-a", "reported": 1, "observed": 40},
            {"monitor": "MON-DEG-B", "device": "r2", "compared_against": "oms-b", "reported": 71, "observed": 40},
        ]
    )
    assert [finding.monitor for finding in findings] == ["MON-DEG-B", "MON-DEG-A"]
    assert [finding.is_defect for finding in findings] == [True, False]
    assert [finding.difference for finding in findings] == [31, -39]


def test_a_subject_whose_reading_cannot_lag_is_a_defect_in_either_direction() -> None:
    """The coarse multiplexer, and it is why the asymmetry travels per comparison.

    `cwdm_channels` is the fixed set of wavelengths a filter passes, not a design
    a branch adds to, so no clock can account for a monitor sitting below it.
    Both directions are a record being wrong, and the caller says so by passing
    `reading_can_lag: False` rather than by subtracting the two numbers again.
    """
    findings = channel_count_findings(
        [
            {
                "monitor": "MON-mux-asp-01",
                "device": "mux-asp-01",
                "compared_against": "the coarse grid on mux-asp-01",
                "reported": 3,
                "observed": 4,
                "reading_can_lag": False,
            }
        ]
    )
    assert [finding.is_defect for finding in findings] == [True]
    assert findings[0].over_reports is False


def test_a_comparison_that_says_nothing_about_lag_is_read_as_a_reading_that_can() -> None:
    """The default is the degree monitor, which is 42 of the 44 comparisons. A
    caller that forgets the key gets the lenient direction, not a refusal it did
    not ask for."""
    findings = channel_count_findings(
        [{"monitor": "MON-DEG-A", "device": "r1", "compared_against": "oms-a", "reported": 0, "observed": 1}]
    )
    assert findings[0].reading_can_lag is True


def test_a_comparison_with_a_missing_side_raises_rather_than_reading_as_zero() -> None:
    """A monitor that could not be compared is not a comparison. Letting `None`
    through would report the difference between a real reading and a number
    nobody took, with the same confidence as a real finding."""
    with pytest.raises(ValueError, match="nothing to compare"):
        channel_count_findings(
            [{"monitor": "MON-DEG-MIL", "device": "roadm-fra-01", "compared_against": "oms-fra-mil", "reported": 71}]
        )


# ---------------------------------------------------------------------------
# Invariant 6: absence is reported, presence is silent, for all five pairings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "monitor_kind"), sorted(MONITOR_BY_DEVICE_KIND.items()))
def test_each_pairing_reports_the_subject_that_carries_no_monitor(kind: str, monitor_kind: str) -> None:
    """The question the schema cannot ask. `ports` peers a generic, and Infrahub
    cannot filter a relationship to a generic by kind, so absence is invisible to
    every constraint form. Parametrized over the table so a sixth pairing is
    covered by adding a row and not by remembering to add a test."""
    assert missing_monitors([{"name": "subject-1", "kind": kind, "monitors": []}]) == [
        MissingMonitor(name="subject-1", kind=kind, monitor_kind=monitor_kind)
    ]


@pytest.mark.parametrize(("kind", "monitor_kind"), sorted(MONITOR_BY_DEVICE_KIND.items()))
def test_each_pairing_stays_quiet_on_the_subject_that_carries_its_monitor(kind: str, monitor_kind: str) -> None:
    """The other half, and it is what stops the check reporting the whole network
    the day a kind name is mistyped in the table."""
    assert missing_monitors([{"name": "subject-1", "kind": kind, "monitors": [monitor_kind]}]) == []


def test_a_monitor_of_the_wrong_kind_does_not_satisfy_the_pairing() -> None:
    """An amplifier carrying a receiver monitor carries no amplifier monitor, so
    nothing can compare its configured gain against what it delivers. The extra
    kinds are ignored rather than counted, so a caller may pass every port on the
    device."""
    subjects = [
        {"name": "amp-fra-mil-04", "kind": "OtnAmplifier", "monitors": ["OtnReceiverMonitor", "OtnLinePort"]},
    ]
    findings = missing_monitors(subjects)
    assert [(finding.name, finding.monitor_kind) for finding in findings] == [("amp-fra-mil-04", "OtnAmplifierMonitor")]


def test_two_subjects_of_one_name_on_different_devices_are_two_findings_that_can_be_told_apart() -> None:
    """A port name is unique on its device and nowhere else.

    `(device, name)` is the uniqueness constraint on `OtnGenericPort`, and six of
    the fifteen ROADMs ship a degree called `DEG-FRA`. A finding that carried the
    port name alone could not say which of the six was uncovered, and a caller
    keying its owner lookup on that name would attribute every one of them to
    whichever ROADM it read last."""
    subjects = [
        {"name": "DEG-FRA", "device": "roadm-ams-01", "kind": "OtnRoadmDegreePort", "monitors": []},
        {"name": "DEG-FRA", "device": "roadm-ber-01", "kind": "OtnRoadmDegreePort", "monitors": []},
    ]
    findings = missing_monitors(subjects)
    assert [(finding.device, finding.name) for finding in findings] == [
        ("roadm-ams-01", "DEG-FRA"),
        ("roadm-ber-01", "DEG-FRA"),
    ]


def test_a_subject_that_names_no_device_carries_an_empty_one() -> None:
    """The four device rows are their own subject and have no owning device, so
    the name is the identity on its own and `device` stays empty."""
    findings = missing_monitors([{"name": "amp-01", "kind": "OtnAmplifier", "monitors": []}])
    assert findings == [MissingMonitor(name="amp-01", kind="OtnAmplifier", monitor_kind="OtnAmplifierMonitor")]
    assert findings[0].device == ""


def test_a_kind_outside_the_table_produces_nothing() -> None:
    """Routers, patch panels and ODU switches carry no monitor and are not judged
    here. The silence is deliberate, which is why `KINDS_NOT_JUDGED` names them
    for the check's summary instead of leaving a reader to infer the boundary."""
    subjects = [{"name": f"device-{kind}", "kind": kind, "monitors": []} for kind in KINDS_NOT_JUDGED]
    assert missing_monitors(subjects) == []


def test_findings_are_grouped_by_kind_and_then_by_name() -> None:
    """A run over a whole network puts every missing amplifier monitor together,
    which is the difference between a list an operator can act on and one they
    have to sort themselves."""
    subjects = [
        {"name": "tp-02", "kind": "OtnTransponder", "monitors": []},
        {"name": "amp-09", "kind": "OtnAmplifier", "monitors": []},
        {"name": "amp-01", "kind": "OtnAmplifier", "monitors": []},
    ]
    assert [finding.name for finding in missing_monitors(subjects)] == ["amp-01", "amp-09", "tp-02"]


# ---------------------------------------------------------------------------
# Invariant 7: the table names kinds the schema actually declares
# ---------------------------------------------------------------------------


def test_every_kind_in_the_pairing_table_is_a_kind_the_schema_declares() -> None:
    """A mistyped kind here is silent in both directions: the device never
    matches, so it is never judged, and the check passes over it. Reading the
    schema is what turns that into a failing test."""
    declared = _declared_kinds()
    for device_kind, monitor_kind in MONITOR_BY_DEVICE_KIND.items():
        assert device_kind in declared, device_kind
        assert monitor_kind in declared, monitor_kind
    for kind in KINDS_NOT_JUDGED:
        assert kind in declared, kind


def test_the_per_port_row_is_a_row_of_the_table() -> None:
    """A ROADM carries one monitor per degree rather than one per device, so that
    row is applied by a different loop. It stays in the same table because a
    reader asking what carries a monitor must find one answer in one place, and
    this is what keeps the two statements about it consistent."""
    assert PER_PORT_KINDS == {"OtnRoadmDegreePort"}
    assert PER_PORT_KINDS <= set(MONITOR_BY_DEVICE_KIND)
    assert MONITOR_BY_DEVICE_KIND["OtnRoadmDegreePort"] == "OtnRoadmDegreeMonitor"
