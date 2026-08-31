"""The scenario files under `demo/`, held to the claims their headers make.

**What earns these tests their place.** `tests/unit/test_budget_claims.py`
already recomputes every margin the Madrid to Warsaw scenario quotes, from
`objects/` through the shipped budget engine, so nothing here re-derives a
figure. What nothing asserted was that the YAML in `demo/` still describes the
splits those figures were computed for. A wavelength whose `sections` list gains
one section stops covering its half of the route, `chains.find_chains` stops
returning a junction, and the scenario refuses on `no-route` while its header
still promises three OSNR refusals and one acceptance. That is a silent failure:
the demo runs, the check is green, and the thing being demonstrated is gone.
Every test here can be broken by editing a file in this repository and by
nothing Infrahub does.

The diversity half is the same shape. `demo/09_diversity_fra_feeds.yml` only
fails the check because two sections happen to share `cd-fra-north`, and
`demo/08_diversity_mil_feeds.yml` only passes it because two others do not. Both
facts live in the generated dataset, where a seed change can move them, so both
are recomputed here from the spans rather than trusted.

`demo/10_amplifier_without_monitor.yml` is the one scenario whose content is an
absence, so it is the one that a well-meaning edit repairs into nothing. The two
tests over it are written against what the file must **not** hold.
"""

from typing import Any

import pytest
import yaml

from infrahub_demo_otn.chains import CarrierSpan, JunctionDevice, RouteSection, find_chains
from infrahub_demo_otn.monitors import MONITOR_BY_DEVICE_KIND, missing_monitors
from tests.unit.conftest import DEMO_DIR, demo_objects_of_kind, objects_of_kind

SIXTEEN_QAM = "06_mad_waw_16qam.yml"
QPSK = "07_mad_waw_qpsk.yml"
MIL_FEEDS = "08_diversity_mil_feeds.yml"
FRA_FEEDS = "09_diversity_fra_feeds.yml"

QAM16_400G = "DP-16QAM 64GBd 400G"
QPSK_400G = "DP-QPSK 128GBd 400G"

MAD_WAW_ROUTE = ("oms-par-mad", "oms-par-fra", "oms-prg-fra", "oms-prg-waw")
"""The only route Madrid to Warsaw has inside the four-section cap.

`test_routing_claims.py` enumerates the section graph; this is the route the
scenario files are written against, and the splits below are cuts of it.
"""

MAD_WAW_SPLITS = (
    # junction site, junction device, first carrier, second carrier, sections of the first half
    ("par", "oeo-par-01", "oc-ch070-mad-par", "oc-ch070-par-waw", 1),
    ("fra", "oeo-fra-02", "oc-ch071-mad-fra", "oc-ch071-fra-waw", 2),
    ("prg", "oeo-prg-01", "oc-ch072-mad-prg", "oc-ch072-prg-waw", 3),
)
"""The three cuts `demo/06_mad_waw_16qam.yml` places, one per interior site.

The last column is where the cut falls, counted in sections from Madrid, which is
what makes the three rows three different splits rather than three copies.
"""


def _name(record: dict[str, Any], key: str = "name") -> str:
    return str(record[key])


def _carriers(file_name: str) -> dict[str, dict[str, Any]]:
    return {_name(record): record for record in demo_objects_of_kind(file_name, "OtnOpticalCarrier")}


def _switches(file_name: str) -> dict[str, dict[str, Any]]:
    return {_name(record): record for record in demo_objects_of_kind(file_name, "OtnOduSwitch")}


def _section_conduits() -> dict[str, frozenset[str]]:
    """The ducts each section's spans lie in, recomputed from the dataset.

    The same walk `impact.service_exposure` performs on a live route, done here
    over the committed YAML because these tests have no server. What is under
    test is the plant, not the walk: `tests/unit/test_impact.py` owns the walk.
    """
    ducts = {_name(span): span.get("conduit") for span in objects_of_kind("OtnFiberSpan")}
    computed: dict[str, frozenset[str]] = {}
    for section in objects_of_kind("OtnOpticalMultiplexSection"):
        found = {str(ducts[str(span)]) for span in section["spans"] if ducts.get(str(span))}
        computed[_name(section)] = frozenset(found)
    return computed


# --------------------------------------------------------------------------
# Madrid to Warsaw: the wavelengths the chain rides
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("site", "device", "first", "second", "cut_after"), MAD_WAW_SPLITS)
def test_each_16qam_split_covers_the_whole_route_and_cuts_it_where_it_says(
    site: str, device: str, first: str, second: str, cut_after: int
) -> None:
    """A cover, not an overlap and not a gap.

    `chains.CarrierSpan` compares a carrier's sections against a run of the route
    by set **equality**, because an ODU is added and dropped where its wavelength
    terminates and nowhere in between. One extra section on either carrier and
    the pair stops being a cover of this route at all.
    """
    carriers = _carriers(SIXTEEN_QAM)
    head = tuple(carriers[first]["sections"])
    tail = tuple(carriers[second]["sections"])

    assert head == MAD_WAW_ROUTE[:cut_after], f"{first} does not reach {site} from Madrid"
    assert tail == MAD_WAW_ROUTE[cut_after:], f"{second} does not reach Warsaw from {site}"
    assert not set(head) & set(tail), "a cover may not repeat a section"
    assert carriers[first]["optical_mode"] == carriers[second]["optical_mode"] == QAM16_400G


@pytest.mark.parametrize(("site", "device", "first", "second", "cut_after"), MAD_WAW_SPLITS)
def test_each_split_is_joined_by_one_regenerator_at_the_site_it_cuts_at(
    site: str, device: str, first: str, second: str, cut_after: int
) -> None:
    """`chains.joins` needs one device holding **both** wavelengths, at the site
    where they meet. A device holding one of the two joins nothing: the light
    arriving has to be terminated and the light leaving originated by the same
    device."""
    switch = _switches(SIXTEEN_QAM)[device]
    assert switch["site"] == site
    assert switch["switching_mode"] == "regenerator"
    assert tuple(switch["carriers"]) == (first, second)


def test_the_qpsk_pair_is_the_frankfurt_split_on_a_regenerator_of_its_own() -> None:
    """Why there are two O-E-O devices at Frankfurt across the two files.

    One device holding all four Frankfurt wavelengths is a valid plant and it
    changes the answer: the generator then takes the mixed cover
    `oc-ch073-mad-fra` to `oc-ch071-fra-waw` and closes at +2.745 dB and
    +0.240 dB. Both halves close, so it provisions, and the scenario stops being
    about the modulation. Keeping the QPSK pair on `oeo-fra-03` makes the mixed
    cover unrepresentable rather than merely unlikely.
    """
    carriers = _carriers(QPSK)
    assert set(carriers) == {"oc-ch073-mad-fra", "oc-ch073-fra-waw"}
    assert tuple(carriers["oc-ch073-mad-fra"]["sections"]) == MAD_WAW_ROUTE[:2]
    assert tuple(carriers["oc-ch073-fra-waw"]["sections"]) == MAD_WAW_ROUTE[2:]
    assert all(record["optical_mode"] == QPSK_400G for record in carriers.values())

    switches = _switches(QPSK)
    assert set(switches) == {"oeo-fra-03"}
    assert switches["oeo-fra-03"]["site"] == "fra"
    assert tuple(switches["oeo-fra-03"]["carriers"]) == ("oc-ch073-mad-fra", "oc-ch073-fra-waw")
    assert not set(switches) & set(_switches(SIXTEEN_QAM)), "the fix must not rewrite a device the refusal placed"


def test_the_two_files_together_offer_exactly_four_chains_and_no_mixed_one() -> None:
    """The traversal's own module, run over the scenario as it loads.

    Four covers, one per regenerator, each of two segments. The count is the
    thing: a fifth cover would mean two wavelength pairs share a device, and the
    mixed cover it produces closes on 0.240 dB and would be chosen ahead of the
    pair this scenario is about.

    The order is asserted too, because it is the order the generator evaluates
    them in and therefore the order the demo prints. The QPSK cover is last,
    which is why one run shows three refusals and then an acceptance rather than
    stopping at the first thing that works.
    """
    steps = _route_steps()
    carriers = {**_carriers(SIXTEEN_QAM), **_carriers(QPSK)}
    spans = [CarrierSpan(name=name, section_names=frozenset(record["sections"])) for name, record in carriers.items()]
    devices = _junction_devices({**_switches(SIXTEEN_QAM), **_switches(QPSK)})

    found = list(find_chains(steps, spans, devices))
    assert [chain.key for chain in found] == [
        "oc-ch070-mad-par|oeo-par-01|oc-ch070-par-waw",
        "oc-ch071-mad-fra|oeo-fra-02|oc-ch071-fra-waw",
        "oc-ch072-mad-prg|oeo-prg-01|oc-ch072-prg-waw",
        "oc-ch073-mad-fra|oeo-fra-03|oc-ch073-fra-waw",
    ]
    assert {chain.segment_count for chain in found} == {2}
    assert {chain.section_names for chain in found} == {MAD_WAW_ROUTE}


def _route_steps() -> list[RouteSection]:
    """Madrid to Warsaw as an ordered list of sections with the ROADM at each end."""
    ends = {
        _name(record): (str(record["roadm_a"]), str(record["roadm_b"]))
        for record in objects_of_kind("OtnOpticalMultiplexSection")
    }
    node = "roadm-mad-01"
    steps: list[RouteSection] = []
    for name in MAD_WAW_ROUTE:
        side_a, side_b = ends[name]
        far = side_b if side_a == node else side_a
        steps.append(RouteSection(name=name, node_a=node, node_z=far))
        node = far
    assert node == "roadm-waw-01", "the route does not end at Warsaw"
    return steps


def _junction_devices(switches: dict[str, dict[str, Any]]) -> list[JunctionDevice]:
    """The scenario's O-E-O devices, each with the optical nodes at its site.

    `site_nodes` is what makes "these two wavelengths meet where this device is"
    answerable. Only ROADMs are collected because a section terminates on a
    ROADM, so a section boundary is never any other kind of node.
    """
    at_site: dict[str, set[str]] = {}
    for roadm in objects_of_kind("OtnRoadm"):
        at_site.setdefault(str(roadm["site"]), set()).add(_name(roadm))
    return [
        JunctionDevice(
            name=name,
            site=str(record["site"]),
            site_nodes=frozenset(at_site[str(record["site"])]),
            carrier_names=frozenset(record["carriers"]),
        )
        for name, record in switches.items()
    ]


def test_no_two_scenario_wavelengths_hold_one_channel_in_one_section() -> None:
    """The reservation `checks/channel_collision.py` enforces, applied to the
    file rather than to the branch after it loads.

    All eight wavelengths cross `oms-par-mad` or `oms-prg-waw`, so the four pairs
    cannot reuse a channel between them. Loading a colliding pair succeeds and
    the collision surfaces later, as a failed proposed change on a scenario
    branch, which is the wrong place to find out.
    """
    holders: dict[tuple[str, str], list[str]] = {}
    for file_name in (SIXTEEN_QAM, QPSK):
        for name, record in _carriers(file_name).items():
            for section in record["sections"]:
                holders.setdefault((str(section), str(record["channel"])), []).append(name)
    clashes = {key: names for key, names in holders.items() if len(names) > 1}
    assert not clashes, f"two wavelengths on one channel in one section: {clashes}"


def test_every_scenario_wavelength_arrives_lit_with_an_empty_oduc4() -> None:
    """A chain grooms, it never lights.

    A wavelength with no line container is discarded before its budget is read,
    with "carries no line container, so there is nothing for the ODUC4 to be
    groomed into", and the scenario then reports a refusal that has nothing to
    do with OSNR.
    """
    for file_name in (SIXTEEN_QAM, QPSK):
        carriers = _carriers(file_name)
        containers = {_name(record): record for record in demo_objects_of_kind(file_name, "OtnContainer")}
        assert {str(record["carrier"]) for record in containers.values()} == set(carriers)
        assert len(containers) == len(carriers)
        for record in containers.values():
            assert record["odu_type"] == "ODUC4"
            assert record["tributary_slots"] == 0
            assert record["tributary_slot_capacity"] == 320


# --------------------------------------------------------------------------
# The two diversity declarations, one kept and one broken
# --------------------------------------------------------------------------

DIVERSITY_SCENARIOS: tuple[tuple[str, str, str, str, str, frozenset[str]], ...] = (
    # file, group, hub both members end at, route of the first member, route of the second, ducts shared
    (MIL_FEEDS, "dg-milan-feeds", "rtr-mil-01", "oms-vie-mil", "oms-gva-mil", frozenset()),
    (FRA_FEEDS, "dg-frankfurt-feeds", "rtr-fra-01", "oms-ams-fra", "oms-par-fra", frozenset({"cd-fra-north"})),
)


@pytest.mark.parametrize(("file_name", "group", "hub", "first", "second", "shared"), DIVERSITY_SCENARIOS)
def test_each_diversity_scenario_shares_the_ducts_its_header_says_it_does(
    file_name: str, group: str, hub: str, first: str, second: str, shared: frozenset[str]
) -> None:
    """The premise of both halves of the scenario, recomputed from the spans.

    The Milan pair passes the check because `cd-mil-northeast` and
    `cd-mil-northwest` are different trenches, and the Frankfurt pair fails it
    because both of its routes enter through `cd-fra-north`. Neither is a fact
    about the check. Both are facts about which conduit each span records, which
    the dataset generator decides and a seed change can move, and a move would
    turn one scenario into the other without editing either file.

    The empty intersection is asserted alongside two non-empty duct sets on
    purpose: two routes with no recorded conduit at all also intersect in
    nothing, and that would be a pass on absence rather than on diversity.
    """
    conduits = _section_conduits()
    assert conduits[first], f"{first} lies in no recorded conduit, so diversity over it is undetermined"
    assert conduits[second], f"{second} lies in no recorded conduit, so diversity over it is undetermined"
    assert conduits[first] & conduits[second] == shared


@pytest.mark.parametrize(("file_name", "group", "hub", "first", "second", "shared"), DIVERSITY_SCENARIOS)
def test_each_diversity_scenario_declares_one_group_over_exactly_two_services(
    file_name: str, group: str, hub: str, first: str, second: str, shared: frozenset[str]
) -> None:
    """A group of one declares nothing that can be broken, and the check says
    nothing about its member. Two is the smallest size at which the requirement
    exists, and both members have to name the group or the pair is not a pair."""
    groups = demo_objects_of_kind(file_name, "OtnDiversityGroup")
    assert [_name(record) for record in groups] == [group]

    services = demo_objects_of_kind(file_name, "OtnService")
    assert len(services) == 2
    assert {str(record["diversity_group"]) for record in services} == {group}
    assert {str(record["endpoint_z"]) for record in services} == {hub}


# --------------------------------------------------------------------------- #
# Which scenario signs for its refusal, and which one does not
# --------------------------------------------------------------------------- #

SERVICES = "00_services.yml"
ODU_TEN_IN_ONE = "04_odu_ten_in_one.yml"
SATURATED = "90_fra_mil_saturated.yml"

FRA_MIL_AI = "svc-fra-mil-ai-400g"
"""Defined in `00_services.yml`, refused by `90_fra_mil_saturated.yml`."""

ACCEPTED = (
    (ODU_TEN_IN_ONE, "svc-lon-mil-sdh-11"),
    (SATURATED, FRA_MIL_AI),
)
"""The two services a scenario file signs for, and the file that signs.

Both are refusals whose point is the record rather than a blocked merge, so
`checks/provisionable.py` reads the signature and stays quiet and the branch
merges. Everything not listed here must carry no flag: the check treats
`refusal_accepted` on a service that was never refused as an error of its own,
so a flag added to a service that provisions fails the branch it was meant to
help.
"""

UNSIGNED_REFUSAL = (SIXTEEN_QAM, "svc-mad-waw-400g")
"""The one scenario that ends in a blocked merge, which is the whole of FR-018a.

If this ever gains `refusal_accepted` the feature passes every test and is never
seen doing anything, which is the failure this assertion exists to catch.
"""


@pytest.mark.parametrize(("file_name", "service"), ACCEPTED)
def test_the_signed_refusals_carry_the_flag_and_nothing_else_in_the_file_does(file_name: str, service: str) -> None:
    """One signature per file, on the one service the file refuses."""
    signed = {
        _name(record) for record in demo_objects_of_kind(file_name, "OtnService") if record.get("refusal_accepted")
    }
    assert signed == {service}


def test_madrid_to_warsaw_signs_for_nothing_so_one_scenario_still_blocks() -> None:
    """The gate has to be seen firing somewhere or it may as well not exist."""
    file_name, service = UNSIGNED_REFUSAL
    services = demo_objects_of_kind(file_name, "OtnService")
    assert [_name(record) for record in services] == [service]
    assert "refusal_accepted" not in services[0]


def test_the_qpsk_fix_accepts_nothing_because_it_refuses_nothing() -> None:
    """`07` ends `active` with two segments and logs three discarded chains.

    The discards are in the generator's log and not on the node, so there is no
    refusal on this branch to sign for, and a flag here would be an acceptance
    with no refusal under it: an error in its own right.
    """
    for record in demo_objects_of_kind(QPSK, "OtnService"):
        assert "refusal_accepted" not in record


def test_the_saturated_scenario_restates_the_service_it_signs_for_unchanged() -> None:
    """`90` signs for a service `00_services.yml` defines, so it restates it.

    The loader rejects a name-and-flag update with "customer is mandatory", so
    the whole definition is repeated, and a repeated definition can drift. Every
    field the two files share must agree; the flag is the only thing `90` adds.
    """
    defined = next(record for record in demo_objects_of_kind(SERVICES, "OtnService") if _name(record) == FRA_MIL_AI)
    restated = next(record for record in demo_objects_of_kind(SATURATED, "OtnService") if _name(record) == FRA_MIL_AI)
    assert restated.keys() - defined.keys() == {"refusal_accepted"}
    assert {key: restated[key] for key in defined} == defined
    assert "refusal_accepted" not in defined


# --------------------------------------------------------------------------- #
# The scenario whose whole content is something that is not there
# --------------------------------------------------------------------------- #

NO_MONITOR = "10_amplifier_without_monitor.yml"

UNWATCHED = "amp-ham-ber-11"
"""The one amplifier `demo/10_amplifier_without_monitor.yml` adds."""

WATCHED_SECTION = "oms-ham-ber"
"""The section the new hut is racked on, and the one it is deliberately not in."""


def _demo_kinds(file_name: str) -> set[str]:
    """Every `spec.kind` a file under `demo/` declares.

    `demo_objects_of_kind` answers about a kind the caller already named, which
    cannot say that a file holds nothing else. This scenario is defined by what
    it leaves out, so the set is what is under test.
    """
    kinds = set()
    for parsed in yaml.safe_load_all((DEMO_DIR / file_name).read_text()):
        if isinstance(parsed, dict) and (parsed.get("spec") or {}).get("kind"):
            kinds.add(str(parsed["spec"]["kind"]))
    return kinds


def test_the_unwatched_amplifier_scenario_adds_one_amplifier_and_no_monitor_for_it() -> None:
    """The file is one device and one absence, and the absence is the whole file.

    This is the test that survives somebody helpfully repairing the scenario. An
    `OtnAmplifierMonitor` added here would load cleanly, read as a tidier record,
    and leave `checks/monitor_completeness.py` with nothing on the branch to fire
    on. The demo would then ship a file that demonstrates nothing while every
    other test in the suite stayed green, which is exactly the silent failure
    this module exists for.

    `missing_monitors` is called rather than described, so the claim in the
    file's header is the shared module's own answer and not a sentence somebody
    typed. Every monitor family in `MONITOR_BY_DEVICE_KIND` is checked for, not
    only the amplifier one, so a sixth pairing added later is covered here
    without an edit.
    """
    assert _demo_kinds(NO_MONITOR) == {"OtnAmplifier"}, "the scenario declares one kind and adds nothing else"

    added = demo_objects_of_kind(NO_MONITOR, "OtnAmplifier")
    assert [_name(record) for record in added] == [UNWATCHED]
    assert UNWATCHED not in {_name(record) for record in objects_of_kind("OtnAmplifier")}, (
        "the scenario adds an amplifier, it does not restate a shipped one"
    )

    for monitor_kind in MONITOR_BY_DEVICE_KIND.values():
        assert demo_objects_of_kind(NO_MONITOR, monitor_kind) == (), f"the scenario must carry no {monitor_kind}"

    findings = missing_monitors([{"name": UNWATCHED, "kind": "OtnAmplifier", "monitors": set()}])
    assert [(finding.name, finding.kind, finding.monitor_kind) for finding in findings] == [
        (UNWATCHED, "OtnAmplifier", "OtnAmplifierMonitor")
    ]


def test_the_unwatched_amplifier_is_a_shipped_amplifier_minus_its_monitor() -> None:
    """Valid on its own, in the same fields the dataset writes, and in no chain.

    Two claims, and they are one argument. The first is that the load succeeds
    and the record is unremarkable: it carries the same attributes as an inline
    amplifier in `objects/13_geant_devices.yml`, so nothing the schema can refuse
    is what makes it wrong.

    The second is why it joins neither `amplifiers_a2b` nor `amplifiers_b2a`.
    `budget.SectionInput.validate` requires exactly one more amplifier than spans
    per direction, so a sixth amplifier on a four-span section raises before a
    decibel is computed and `osnr_margin` goes red on a section this scenario has
    nothing to say about. The N+1 arithmetic is recomputed here from the dataset
    rather than asserted from the header, because a generator change that respans
    the section moves it.
    """
    record = demo_objects_of_kind(NO_MONITOR, "OtnAmplifier")[0]
    assert "oms_a2b" not in record and "oms_b2a" not in record, "an amplifier in a chain breaks that chain's N+1 rule"

    inline = next(shipped for shipped in objects_of_kind("OtnAmplifier") if "site" not in shipped)
    assert set(record) == set(inline), "the scenario amplifier is written in the fields the dataset writes"

    section = next(entry for entry in objects_of_kind("OtnOpticalMultiplexSection") if _name(entry) == WATCHED_SECTION)
    expected = len(section["spans"]) + 1
    for direction in ("amplifiers_a2b", "amplifiers_b2a"):
        assert len(section[direction]) == expected, f"{WATCHED_SECTION} {direction} is already at N+1 without this file"
        assert UNWATCHED not in section[direction]


def test_every_scenario_but_the_one_about_absence_carries_a_monitor_for_each_device_it_adds() -> None:
    """A scenario that adds a monitored device and no monitor blocks its own merge.

    `demo/02_par_mad_raman.yml` used to. It loaded six Raman pumps and no
    monitors, so `checks/monitor_completeness.py` reported `9/15 Raman pumps` and
    refused the proposed change the Raman page invites the reader to open. The
    check was right and the scenario was incomplete: nothing could measure six
    pumps the file had just installed.

    That was found by running the check against a live stack, not here, which is
    the gap this test closes. Every scenario is now held to the rule the shipped
    dataset already meets, so the next file to add an amplifier, a pump, a
    transponder or a multiplexer without its monitor fails in two seconds rather
    than at the end of a walkthrough.

    `demo/10_amplifier_without_monitor.yml` is the deliberate exception and is
    excluded by name. It exists to be the absence, and the two tests above hold
    it to that.
    """
    for path in sorted(DEMO_DIR.glob("*.yml")):
        if path.name == NO_MONITOR:
            continue
        subjects = []
        for kind, monitor_kind in MONITOR_BY_DEVICE_KIND.items():
            for record in demo_objects_of_kind(path.name, kind):
                held = {
                    str(port.get("device")): monitor_kind
                    for other in (monitor_kind,)
                    for port in demo_objects_of_kind(path.name, other)
                }
                subjects.append(
                    {
                        "name": _name(record),
                        "kind": kind,
                        "device": "",
                        "monitors": {held[_name(record)]} if _name(record) in held else set(),
                    }
                )
        gaps = [finding.name for finding in missing_monitors(subjects)]
        assert not gaps, (
            f"{path.name} adds {len(gaps)} device(s) with no monitor: {', '.join(gaps)}. "
            "monitor_completeness refuses the proposed change that scenario asks the reader to open"
        )
