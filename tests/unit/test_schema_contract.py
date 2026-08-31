"""Guard the schema YAML and the reference objects against the rules a human
reviewer stops enforcing.

Infrahub validates that a schema file is well formed. It does not validate that
this project's own conventions hold: scaled integers only, the unit in the
attribute name, both bounds on every number, and a display divisor that agrees
with `units.py`. Those are the rules that decay silently, because breaking one
still loads.

The divisor assertion is the important one. Every scale factor in this project
lives in `units.py`, with one exception: a Jinja2 `_display` template writes its
divisor inline, because the server-side template engine cannot import Python.
An exception nobody checks is a hole, so this module reads every divisor back
out of every template and asserts it is legal *for the unit of the attribute
being rendered*, not merely that it equals some constant in `units.py`.
`MDB_PER_DB`, `M_PER_KM` and `FS_PER_PS` are all 1000, so plain value membership
would pass on coincidence.

`objects/` is guarded here too. The readers live in `conftest.py` because
`test_geant_dataset.py` needs the same ones. The 96-row frequency grid is
generated from `units.py`, so the guard recomputes all 96 on every run and the
file cannot drift from `channel_to_frequency_mhz`.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from infrahub_demo_otn.routing import (
    CHANNEL_REASONS,
    REJECTION_CODES,
)
from infrahub_demo_otn.units import (
    CWDM_CHANNEL_COUNT,
    CWDM_FIRST_WAVELENGTH_NM,
    FS_PER_PS,
    GRID_CHANNEL_COUNT,
    GRID_FIRST_CHANNEL_MHZ,
    GROUP_INDEX_G652_MILLI,
    KBPS_PER_GBPS,
    KBPS_PER_MBPS,
    M_PER_KM,
    MDB_PER_DB,
    MHZ_PER_THZ,
    NS_PER_US,
    channel_to_frequency_mhz,
    cwdm_index_to_wavelength_nm,
    wavelength_nm_to_band,
)
from tests.unit.conftest import SCHEMA_DIR, objects_of_kind, schema_files

# `_ohm` stays here and is deliberately absent from SUFFIX_DIVISORS below.
# Impedance is a plain integer in ohms, not a scaled quantity, so "must be a
# Number" is the check that applies to it, and "must render through a divisor"
# is not.
UNIT_SUFFIXES = (
    "_mdb",
    "_mdbm",
    "_mhz",
    "_kbps",
    "_ohm",
    # `_fs_per_nm` was in SUFFIX_DIVISORS from the start and missing here, so
    # `cd_tolerance_fs_per_nm` was the one scaled attribute in the repository
    # that no test required to be a Number. Confirmed by breaking it: with the
    # suffix absent, changing that attribute to `kind: Text` passes this module.
    # `_fs_per_nm_km` does not end with `_fs_per_nm`, so both are needed.
    "_fs_per_nm",
    "_fs_per_nm_km",
    "_mbaud",
    "_gbps",
    "_ns",
    "_milli",
    "_m",
    # `_nm` holds the position `_ohm` holds: here, and deliberately not in
    # SUFFIX_DIVISORS. A nanometre is a natural unit, the value is a whole
    # number, and there is no scale factor for a divisor to use.
    #
    # Adding it here does not break `cd_tolerance_fs_per_nm`, and the reason is
    # worth stating so nobody "fixes the inconsistency" by adding `_nm` to the
    # divisor map. This tuple is consumed by a bare `str.endswith(tuple)` in
    # `test_unit_suffixed_attributes_are_numbers`, which resolves nothing and
    # asks only "is it a Number"; `cd_tolerance_fs_per_nm` is one, so it passes
    # under either suffix. The longest-first resolution that decides which
    # divisors a display may use runs over SUFFIX_DIVISORS, which `_nm` never
    # joins.
    #
    # Without this entry, `center_wavelength_nm` declared as `kind: Text` passes
    # every test in this module. Probed.
    "_nm",
)
"""Attribute-name suffixes that declare a scaled integer quantity."""

BANNED_KINDS = frozenset({"Float", "JSON", "Any"})
"""Infrahub has no Float; JSON and Any are not filterable."""

EXPECTED_DISPLAY_ATTRIBUTES = frozenset(
    {
        # Devices and ports.
        "input_power_display",
        "output_power_display",
        "measured_gain_display",
        "measured_osnr_display",
        "insertion_loss_display",
        "center_frequency_display",
        "tx_power_display",
        "rx_sensitivity_display",
        # Plant and the optics catalog.
        "attenuation_display",
        "dispersion_display",
        "length_display",
        "required_osnr_display",
        "nominal_reach_display",
        "bit_rate_display",
        # Amplifiers. Both are _mdb, which SUFFIX_DIVISORS already maps to
        # MDB_PER_DB, so the pin map below needs no new entry: only these two
        # names. `oms_sequence` gets no display, matching the span's.
        "noise_figure_display",
        "gain_display",
        # The Raman pump. Also _mdb, so the pin map below covers it too.
        "on_off_gain_display",
        # Services, paths and hops: six on the service and the path, four on the
        # hop. Three of the ten render nanoseconds, which is the only use of the
        # `_ns` entry in the pin map below.
        "max_latency_display",
        "total_length_display",
        "total_loss_display",
        "osnr_total_display",
        "osnr_margin_display",
        "latency_display",
        "cumulative_length_display",
        "cumulative_loss_display",
        "cumulative_osnr_display",
        "cumulative_delay_display",
    }
)
"""Twenty-four declarations, twenty-three names. `center_frequency_display` is
declared on both `OtnOpticalPort` and `OtnFrequencyGrid`; the set is over names,
and check 13 below is what keeps the two renderings identical."""

SUFFIX_DIVISORS: dict[str, dict[str, int]] = {
    "_mdb": {"MDB_PER_DB": MDB_PER_DB},
    "_mdbm": {"MDB_PER_DB": MDB_PER_DB},
    "_mdb_per_km": {"MDB_PER_DB": MDB_PER_DB},
    "_mhz": {"MHZ_PER_THZ": MHZ_PER_THZ},
    "_m": {"M_PER_KM": M_PER_KM},
    "_fs_per_nm": {"FS_PER_PS": FS_PER_PS},
    "_fs_per_nm_km": {"FS_PER_PS": FS_PER_PS},
    "_kbps": {"KBPS_PER_MBPS": KBPS_PER_MBPS, "KBPS_PER_GBPS": KBPS_PER_GBPS},
    # `NS_PER_US` is its own constant in `units.py` rather than a reuse of one of
    # the other thousands, precisely so a latency display cannot pass this guard
    # by dividing by `M_PER_KM`. This entry is what makes that pin real.
    "_ns": {"NS_PER_US": NS_PER_US},
}
"""The unit suffix of the *source* attribute decides which constants its
display may divide by. Matched longest-suffix-first, so `_mdb_per_km` wins over
`_mdb` and `_fs_per_nm_km` over `_fs_per_nm`."""

DIVISOR_PATTERN = re.compile(r"/\s*(\d+)")


def _load_documents() -> list[tuple[Path, dict[str, Any]]]:
    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in schema_files():
        parsed = yaml.safe_load(path.read_text())
        assert isinstance(parsed, dict), f"{path.name} does not parse to a mapping"
        documents.append((path, parsed))
    return documents


def _all_attributes() -> list[tuple[str, str, dict[str, Any]]]:
    """Every attribute in every schema file as (file name, kind name, attribute)."""
    collected: list[tuple[str, str, dict[str, Any]]] = []
    for path, document in _load_documents():
        for section in ("generics", "nodes"):
            for entry in document.get(section) or []:
                kind = f"{entry.get('namespace', '')}{entry.get('name', '')}"
                for attribute in entry.get("attributes") or []:
                    collected.append((path.name, kind, attribute))
    return collected


def _attributes_by_kind() -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for _, kind, attribute in _all_attributes():
        grouped.setdefault(kind, []).append(attribute)
    return grouped


# ---------------------------------------------------------------------------
# The five monitor kinds.
# ---------------------------------------------------------------------------
MONITOR_KINDS = (
    "OtnAmplifierMonitor",
    "OtnRoadmDegreeMonitor",
    "OtnMuxDemuxMonitor",
    "OtnRamanMonitor",
    "OtnReceiverMonitor",
)

CHANNEL_MONITOR_KINDS = ("OtnRoadmDegreeMonitor", "OtnMuxDemuxMonitor")

SHIPPED_READINGS = frozenset(
    {
        "input_power_mdbm",
        "output_power_mdbm",
        "measured_gain_mdb",
        "tilt_mdb",
        "total_power_mdbm",
        "channel_count",
        "pump_power_mdbm",
        "back_reflection_mdb",
        "measured_osnr_mdb",
        "rx_power_mdbm",
        "pre_fec_ber_ppb",
        "q_factor_mdb",
        "cd_fs_per_nm",
        "dgd_fs",
    }
)
"""The fourteen readings the one shipped monitor kind held. The five kinds that
replaced it must union to exactly this: nothing added and nothing dropped."""


def _readings_of(kind: str, attributes: dict[str, list[dict[str, Any]]]) -> set[str]:
    """One kind's declared readings, following the channel generic.

    A `_display` is a rendering of a reading and not a reading, so it is
    excluded. Everything else a monitor kind declares is one.
    """
    own = {str(item["name"]) for item in attributes.get(kind) or []}
    if kind in CHANNEL_MONITOR_KINDS:
        own |= {str(item["name"]) for item in attributes.get("OtnChannelMonitor") or []}
    return {name for name in own if not name.endswith("_display")}


def test_the_five_monitor_kinds_union_to_the_fourteen_readings_the_old_kind_held() -> None:
    """Computed, not listed. The whole argument for five kinds is that the
    readings partition almost cleanly across families, so the union is the one
    number that says nothing was lost in the split.

    `measured_gain_mdb` is declared twice, on the amplifier monitor and on the
    Raman monitor, and the union counts it once. That is the point: it is the one
    reading two families genuinely share.
    """
    attributes = _attributes_by_kind()
    union: set[str] = set()
    for kind in MONITOR_KINDS:
        union |= _readings_of(kind, attributes)
    assert union == set(SHIPPED_READINGS), (
        f"added: {sorted(union - SHIPPED_READINGS)}, dropped: {sorted(SHIPPED_READINGS - union)}"
    )


@pytest.mark.parametrize("kind", MONITOR_KINDS)
def test_every_monitor_reading_is_mandatory(kind: str) -> None:
    """This is what replaced the completeness check.

    With every reading `optional: false`, the server refuses a monitor that is
    missing one, and a kind has no field at all for a reading its hardware
    cannot take. Both halves of the old check, enforced by the schema, on every
    write path rather than only inside a proposed change.
    """
    attributes = _attributes_by_kind()
    sources = [kind, "OtnChannelMonitor"] if kind in CHANNEL_MONITOR_KINDS else [kind]
    optional = [
        f"{source}.{item['name']}"
        for source in sources
        for item in attributes.get(source) or []
        if not str(item["name"]).endswith("_display") and item.get("optional") is not False
    ]
    assert not optional, "monitor readings that a write may omit: " + "; ".join(optional)


@pytest.mark.parametrize("kind", MONITOR_KINDS)
def test_every_monitor_kind_takes_the_two_flat_generics_it_needs(kind: str) -> None:
    """Generics do not inherit generics, so the concrete kind takes both.

    `OtnMonitor` carries `measured_at` and `OtnGenericPort` carries the port
    identity, the device relationship, the `human_friendly_id` and the
    uniqueness constraint. The two channel kinds take `OtnChannelMonitor` as
    well, which is where their two shared readings are declared once.
    """
    entry = next(
        node
        for _, document in _load_documents()
        for node in document.get("nodes") or []
        if f"{node.get('namespace', '')}{node.get('name', '')}" == kind
    )
    inherits = list(entry.get("inherit_from") or [])
    assert "OtnGenericPort" in inherits, kind
    assert "OtnMonitor" in inherits, kind
    assert ("OtnChannelMonitor" in inherits) is (kind in CHANNEL_MONITOR_KINDS), kind
    assert entry.get("include_in_menu") is False, kind
    # Neither is redeclared on a concrete kind. Both come from OtnGenericPort,
    # and a constraint declared on a generic binds across every kind that
    # inherits it, so the five share one (device, name) namespace.
    assert "human_friendly_id" not in entry, kind
    assert "uniqueness_constraints" not in entry, kind


def test_neither_monitor_generic_inherits_the_other() -> None:
    """Generics cannot inherit generics in Infrahub, so both are flat and the
    concrete kinds compose them. `OtnChannelMonitor` taking `OtnMonitor` would be
    the shape the schema loader refuses."""
    generics = {
        f"{entry.get('namespace', '')}{entry.get('name', '')}": entry
        for _, document in _load_documents()
        for entry in document.get("generics") or []
    }
    for name in ("OtnMonitor", "OtnChannelMonitor"):
        assert name in generics, name
        assert not generics[name].get("inherit_from"), f"{name} inherits, and a generic may not"


def test_no_monitor_kind_carries_a_discriminator() -> None:
    """The kind is the type. A `monitor_type` beside a kind that already
    discriminates is the same fact stored twice, which is the failure the split
    removed."""
    attributes = _attributes_by_kind()
    offenders = [
        f"{kind}.{item['name']}"
        for kind in (*MONITOR_KINDS, "OtnMonitor", "OtnChannelMonitor")
        for item in attributes.get(kind) or []
        if str(item["name"]) == "monitor_type"
    ]
    assert not offenders, "monitor kinds carrying a discriminator: " + "; ".join(offenders)


def test_the_schema_ships_the_forty_four_kinds_the_installation_page_promises() -> None:
    """`installation-setup.mdx` tells a reader the schema load gives them 44
    empty kinds, and that is the first number the demo puts on screen. Eight
    generics and 36 nodes. A kind added without the page being updated fails
    here rather than on the reader's screen.

    `provisioning-scenarios.mdx`, `developer-guide.mdx`, `schema-reference.mdx`
    and `README.md` print the same total, so all five move together. It was 41 until
    `OtnOduSwitch` made the O-E-O device a kind of its own, 42 until
    `OtnDiversityGroup` made a diversity requirement an object instead of a
    string on the service, and 43 until `OtnFacility` made a EuroHPC facility an
    edge instead of the text after a prefix in a tag name.
    """
    generics = [entry for _, document in _load_documents() for entry in document.get("generics") or []]
    nodes = [entry for _, document in _load_documents() for entry in document.get("nodes") or []]
    assert (len(generics), len(nodes)) == (8, 36), (
        f"{len(generics)} generics and {len(nodes)} nodes, the page says 8 and 36"
    )


def test_no_banned_attribute_kinds() -> None:
    """Scaled integers, not Float; no untyped JSON or Any."""
    offenders = [
        f"{kind}.{attribute['name']} in {file_name} uses kind {attribute.get('kind')}"
        for file_name, kind, attribute in _all_attributes()
        if attribute.get("kind") in BANNED_KINDS
    ]
    assert not offenders, "banned attribute kinds: " + "; ".join(offenders)


def test_every_number_attribute_declares_both_bounds() -> None:
    """An unbounded integer is caught at budget time instead of at write time,
    which is far too late to be useful."""
    offenders: list[str] = []
    for file_name, kind, attribute in _all_attributes():
        if attribute.get("kind") != "Number":
            continue
        parameters = attribute.get("parameters") or {}
        missing = [bound for bound in ("min_value", "max_value") if bound not in parameters]
        if missing:
            offenders.append(f"{kind}.{attribute['name']} in {file_name} is missing {', '.join(missing)}")
    assert not offenders, "Number attributes without both bounds: " + "; ".join(offenders)


def test_unit_suffixed_attributes_are_numbers() -> None:
    """A unit suffix promises a scaled integer, so the kind must be Number."""
    offenders = [
        f"{kind}.{attribute['name']} in {file_name} is kind {attribute.get('kind')}"
        for file_name, kind, attribute in _all_attributes()
        if str(attribute.get("name", "")).endswith(UNIT_SUFFIXES) and attribute.get("kind") != "Number"
    ]
    assert not offenders, "unit-suffixed attributes that are not Number: " + "; ".join(offenders)


def test_the_display_attributes_are_exactly_the_expected_set() -> None:
    """A paired `_display` is added only where an operator reads the number.
    Dropping one loses the only readable rendering of that quantity; adding an
    unexpected one means a scaled integer was given a display nobody needs."""
    found = {attribute["name"] for _, _, attribute in _all_attributes() if str(attribute["name"]).endswith("_display")}
    assert found == EXPECTED_DISPLAY_ATTRIBUTES, (
        f"expected {sorted(EXPECTED_DISPLAY_ATTRIBUTES)}, found {sorted(found)}"
    )


def test_display_attributes_are_computed_and_read_only() -> None:
    """Infrahub rejects a computed attribute that is not read_only."""
    offenders: list[str] = []
    for file_name, kind, attribute in _all_attributes():
        if not str(attribute.get("name", "")).endswith("_display"):
            continue
        computed = attribute.get("computed_attribute") or {}
        if computed.get("kind") != "Jinja2":
            offenders.append(f"{kind}.{attribute['name']} in {file_name} has no Jinja2 computed_attribute")
        if attribute.get("read_only") is not True:
            offenders.append(f"{kind}.{attribute['name']} in {file_name} is not read_only")
        if attribute.get("optional") is not True:
            offenders.append(f"{kind}.{attribute['name']} in {file_name} is not optional")
    assert not offenders, "; ".join(offenders)


def _source_attribute_for(display_name: str, siblings: list[dict[str, Any]]) -> tuple[str, str | None]:
    """Resolve a `_display` attribute to the attribute it renders, on its own kind.

    The stem is the name with `_display` stripped. The source is the sibling
    Number attribute whose name is the stem, or the stem plus a unit suffix.
    Returns (source name, error) with exactly one of the two set.
    """
    stem = display_name[: -len("_display")]
    candidates = [
        str(sibling.get("name"))
        for sibling in siblings
        if sibling.get("kind") == "Number"
        and (str(sibling.get("name")) == stem or str(sibling.get("name")).startswith(f"{stem}_"))
    ]
    if not candidates:
        return "", f"no Number attribute on the same kind matches the stem {stem!r}"
    if len(candidates) > 1:
        return "", f"stem {stem!r} is ambiguous, matching {sorted(candidates)}"
    return candidates[0], None


def _allowed_divisors(source_name: str) -> dict[str, int] | None:
    for suffix in sorted(SUFFIX_DIVISORS, key=len, reverse=True):
        if source_name.endswith(suffix):
            return SUFFIX_DIVISORS[suffix]
    return None


def test_display_divisors_are_legal_for_the_unit_they_render() -> None:
    """Bound the inline-divisor exception per unit, not per value.

    Matching on VALUE alone is not enough. MDB_PER_DB, M_PER_KM and FS_PER_PS
    are all 1000, so a length display dividing by 1000 would pass while
    silently claiming the decibel constant. Each display is therefore resolved
    to the attribute it renders, and that attribute's unit suffix decides which
    constants the template may divide by.

    A `_display` whose source attribute cannot be found is a failure, not a
    skip, so a typo cannot quietly disable the check. There is no assertion on
    how many divisors were seen, because `bit_rate_display` carries two.
    """
    by_kind = _attributes_by_kind()
    offenders: list[str] = []
    for file_name, kind, attribute in _all_attributes():
        name = str(attribute.get("name", ""))
        if not name.endswith("_display"):
            continue
        source, error = _source_attribute_for(name, by_kind[kind])
        if error:
            offenders.append(f"{kind}.{name} in {file_name}: {error}")
            continue
        allowed = _allowed_divisors(source)
        if allowed is None:
            offenders.append(f"{kind}.{name} in {file_name}: source {source} has no unit suffix in SUFFIX_DIVISORS")
            continue
        template = str((attribute.get("computed_attribute") or {}).get("jinja2_template", ""))
        divisors = {int(match) for match in DIVISOR_PATTERN.findall(template)}
        if not divisors:
            offenders.append(f"{kind}.{name} in {file_name} has no divisor to check")
            continue
        legal = set(allowed.values())
        for divisor in sorted(divisors):
            if divisor not in legal:
                names = ", ".join(f"{const} ({value})" for const, value in sorted(allowed.items()))
                offenders.append(
                    f"{kind}.{name} in {file_name} divides {source} by {divisor}, but must use one of {names}"
                )
    assert not offenders, "; ".join(offenders)


def test_the_declared_divisor_pin_map_agrees_with_the_schema() -> None:
    """The map above derives the constant; this states it, so both must agree.

    Deriving alone would silently accept a display whose source attribute was
    renamed into a different unit. Stating alone is the pin-by-name map that
    has to be edited by hand for every new `_display`. Holding both and
    asserting they match is what makes either one load-bearing.
    """
    required: dict[str, str] = {
        "input_power_display": "MDB_PER_DB",
        "output_power_display": "MDB_PER_DB",
        "measured_gain_display": "MDB_PER_DB",
        "measured_osnr_display": "MDB_PER_DB",
        "insertion_loss_display": "MDB_PER_DB",
        "tx_power_display": "MDB_PER_DB",
        "rx_sensitivity_display": "MDB_PER_DB",
        "center_frequency_display": "MHZ_PER_THZ",
        "attenuation_display": "MDB_PER_DB",
        "dispersion_display": "FS_PER_PS",
        "length_display": "M_PER_KM",
        "required_osnr_display": "MDB_PER_DB",
        "nominal_reach_display": "M_PER_KM",
        "bit_rate_display": "KBPS_PER_MBPS",
        "noise_figure_display": "MDB_PER_DB",
        "gain_display": "MDB_PER_DB",
        "on_off_gain_display": "MDB_PER_DB",
        "max_latency_display": "NS_PER_US",
        "total_length_display": "M_PER_KM",
        "total_loss_display": "MDB_PER_DB",
        "osnr_total_display": "MDB_PER_DB",
        "osnr_margin_display": "MDB_PER_DB",
        "latency_display": "NS_PER_US",
        "cumulative_length_display": "M_PER_KM",
        "cumulative_loss_display": "MDB_PER_DB",
        "cumulative_osnr_display": "MDB_PER_DB",
        "cumulative_delay_display": "NS_PER_US",
    }
    assert set(required) == EXPECTED_DISPLAY_ATTRIBUTES, (
        "the divisor pin-map and EXPECTED_DISPLAY_ATTRIBUTES disagree: "
        f"{sorted(set(required) ^ EXPECTED_DISPLAY_ATTRIBUTES)}"
    )

    by_kind = _attributes_by_kind()
    offenders: list[str] = []
    for file_name, kind, attribute in _all_attributes():
        name = str(attribute.get("name", ""))
        if not name.endswith("_display"):
            continue
        source, error = _source_attribute_for(name, by_kind[kind])
        if error:
            offenders.append(f"{kind}.{name} in {file_name}: {error}")
            continue
        allowed = _allowed_divisors(source) or {}
        if required[name] not in allowed:
            offenders.append(
                f"{kind}.{name} in {file_name} is pinned to {required[name]}, "
                f"but its source {source} allows only {sorted(allowed)}"
            )
    assert not offenders, "; ".join(offenders)


def test_same_named_displays_carry_identical_templates() -> None:
    """Check 13. `center_frequency_display` is declared on `OtnOpticalPort` and on
    `OtnFrequencyGrid`, and nothing else keeps the two renderings in step."""
    templates: dict[str, set[str]] = {}
    for _, _, attribute in _all_attributes():
        name = str(attribute.get("name", ""))
        if not name.endswith("_display"):
            continue
        template = str((attribute.get("computed_attribute") or {}).get("jinja2_template", ""))
        templates.setdefault(name, set()).add(template)
    offenders = [
        f"{name} has {len(variants)} distinct templates" for name, variants in templates.items() if len(variants) > 1
    ]
    assert not offenders, "same-named _display attributes disagree: " + "; ".join(offenders)


def test_display_templates_guard_their_optional_inputs() -> None:
    """An unguarded divide on a null input raises inside the Jinja2 engine,
    where the error surfaces as a server-side template failure."""
    offenders = [
        f"{kind}.{attribute['name']} in {file_name}"
        for file_name, kind, attribute in _all_attributes()
        if str(attribute.get("name", "")).endswith("_display")
        and "is not none" not in str((attribute.get("computed_attribute") or {}).get("jinja2_template", ""))
        and "is none" not in str((attribute.get("computed_attribute") or {}).get("jinja2_template", ""))
    ]
    assert not offenders, "unguarded _display templates: " + "; ".join(offenders)


def _find_attributes(name: str) -> list[tuple[str, dict[str, Any]]]:
    matches = [(kind, attribute) for _, kind, attribute in _all_attributes() if attribute.get("name") == name]
    if not matches:
        pytest.fail(f"attribute {name} is not defined in any file under {SCHEMA_DIR}")
    return matches


def test_center_frequency_bounds_are_the_grid_endpoints() -> None:
    """The bounds duplicate units.py, so drift has to fail here, not later.

    Checked on every kind declaring the attribute. Two do, and taking only the
    first match would let the second drift unnoticed.
    """
    matches = _find_attributes("center_frequency_mhz")
    assert len(matches) >= 2, f"expected center_frequency_mhz on at least two kinds, found {[k for k, _ in matches]}"
    for kind, attribute in matches:
        parameters = attribute.get("parameters") or {}
        assert parameters.get("min_value") == GRID_FIRST_CHANNEL_MHZ, kind
        assert parameters.get("max_value") == channel_to_frequency_mhz(GRID_CHANNEL_COUNT), kind


def test_channel_number_bounds_are_the_grid_extent() -> None:
    """The grid is 96 channels because units.py says so, not the YAML."""
    for kind, attribute in _find_attributes("channel_number"):
        parameters = attribute.get("parameters") or {}
        assert parameters.get("min_value") == 1, kind
        assert parameters.get("max_value") == GRID_CHANNEL_COUNT, kind


def test_center_wavelength_bounds_are_the_cwdm_plan_extent() -> None:
    """The coarse plan runs 1271 to 1611 nm because units.py says so, not the YAML."""
    for kind, attribute in _find_attributes("center_wavelength_nm"):
        parameters = attribute.get("parameters") or {}
        assert parameters.get("min_value") == CWDM_FIRST_WAVELENGTH_NM, kind
        assert parameters.get("max_value") == cwdm_index_to_wavelength_nm(CWDM_CHANNEL_COUNT), kind


def test_no_generic_inherits_from_another_generic() -> None:
    """Infrahub's generic schema has no inherit_from key, so the taxonomy is
    composed by listing several generics on a concrete node and stays flat."""
    offenders: list[str] = []
    for path, document in _load_documents():
        for entry in document.get("generics") or []:
            if entry.get("inherit_from"):
                offenders.append(f"{entry.get('namespace', '')}{entry.get('name', '')} in {path.name}")
    assert not offenders, "generics declaring inherit_from: " + "; ".join(offenders)


def test_the_frequency_grid_objects_are_generated_from_units() -> None:
    """The 96 rows are output, not input, so recompute every one.

    The file was generated by running `channel_to_frequency_mhz` over the range
    and committing the result. Nothing stops a hand edit, so the guard redoes
    the arithmetic instead of trusting it.
    """
    channels = objects_of_kind("OtnFrequencyGrid")
    assert len(channels) == GRID_CHANNEL_COUNT, f"expected {GRID_CHANNEL_COUNT} channels, found {len(channels)}"

    numbers = [entry["channel_number"] for entry in channels]
    assert set(numbers) == set(range(1, GRID_CHANNEL_COUNT + 1)), "channel numbers are not exactly 1 to 96"
    assert len(numbers) == len(set(numbers)), "duplicate channel numbers"

    offenders = [
        f"channel {entry['channel_number']} stores {entry['center_frequency_mhz']}, "
        f"units.py says {channel_to_frequency_mhz(entry['channel_number'])}"
        for entry in channels
        if entry["center_frequency_mhz"] != channel_to_frequency_mhz(entry["channel_number"])
    ]
    assert not offenders, "; ".join(offenders)


def test_the_cwdm_channel_objects_are_generated_from_units() -> None:
    """The eighteen rows are output, not input, so recompute every one.

    Both fields, not only the wavelength. `band` is derivable and is stored so
    that "which coarse wavelengths could an erbium amplifier reach" is a GraphQL
    filter, and this is the guard that buys the redundancy back. Without it a
    hand edit to `05_cwdm_channels.yml` fails nothing.
    """
    channels = objects_of_kind("OtnCwdmChannel")
    assert len(channels) == CWDM_CHANNEL_COUNT, f"expected {CWDM_CHANNEL_COUNT} wavelengths, found {len(channels)}"

    expected = [cwdm_index_to_wavelength_nm(index) for index in range(1, CWDM_CHANNEL_COUNT + 1)]
    stored = [entry["center_wavelength_nm"] for entry in channels]
    assert sorted(stored) == expected, f"wavelengths are not the plan: {sorted(stored)}"
    assert len(stored) == len(set(stored)), "duplicate wavelengths"

    offenders = [
        f"{entry['center_wavelength_nm']} nm stores band {entry['band']}, "
        f"units.py says {wavelength_nm_to_band(int(entry['center_wavelength_nm']))}"
        for entry in channels
        if entry["band"] != wavelength_nm_to_band(int(entry["center_wavelength_nm"]))
    ]
    assert not offenders, "; ".join(offenders)


def test_exactly_two_cwdm_wavelengths_are_in_the_c_band() -> None:
    """Computed from the band edges, not listed.

    The whole placement argument for the coarse tail rests on this count. Two of
    eighteen reach the erbium window, so sixteen could not be amplified even if
    an amplifier were fitted, which is why the tail carries none and why the
    repository computes no coarse link budget. A listed pair would assert a
    transcription instead of the arithmetic.
    """
    c_band = [
        entry["center_wavelength_nm"]
        for entry in objects_of_kind("OtnCwdmChannel")
        if wavelength_nm_to_band(int(entry["center_wavelength_nm"])) == "c"
    ]
    assert len(c_band) == 2, f"expected two C-band wavelengths, found {sorted(c_band)}"


def _layer_choices() -> list[str]:
    """The declared `layer` vocabulary, in file order.

    Read from `schemas/otn_logical.yml` rather than from a running server. The
    server hands the choices back as `['sdh', 'pdh', 'fibre_channel',
    'ethernet']`, so an in-order pin against the live schema pins nothing.
    """
    for kind, attribute in _find_attributes("layer"):
        assert kind == "OtnClientSignal", f"layer is declared on {kind} as well, which this pin does not cover"
        return [str(choice["name"]) for choice in attribute.get("choices") or []]
    raise AssertionError("unreachable: _find_attributes fails when nothing matches")


def test_the_client_signal_layers_are_the_five_declared_in_order() -> None:
    """A literal pin, and it is deliberately only half of the guard.

    The two `auto_selectable` tests below are the other half, and they are
    separate on purpose: in one test this assertion fires first, the rest never
    runs, and the failure an implementer sees is the one with the easy wrong fix.
    """
    assert _layer_choices() == ["ethernet", "sdh", "pdh", "fibre_channel", "infiniband"]


def test_the_auto_selectable_flag_fails_closed() -> None:
    """The polarity is the whole point, so it is pinned rather than assumed.

    `default_value: false` with `optional: false` means a signal added without a
    decision is unreachable by the generator's automatic path until somebody
    writes `true` in a diff. A default of true would fail open and hand the next
    specialised signal to a service that never asked for it, which is the defect
    the flag replaced an allow-list to prevent.
    """
    matches = _find_attributes("auto_selectable")
    assert [kind for kind, _ in matches] == ["OtnClientSignal"], (
        f"auto_selectable is declared on {sorted(kind for kind, _ in matches)}, which this pin does not cover"
    )
    _, attribute = matches[0]
    assert attribute["kind"] == "Boolean"
    assert attribute["default_value"] is False, "a default of true fails open"
    assert attribute["optional"] is False, "an optional flag is an unplaced signal by another name"


def test_every_client_signal_places_itself_on_one_side_of_the_flag() -> None:
    """The vocabulary and the decision over it now live in the same place.

    Every catalog row states `auto_selectable` explicitly, so an omission reads
    as an oversight rather than as a silent default, and the layers that are
    automatically selectable are exactly the four the documentation names.

    The last assertion closes a separate gap: probed, an `OtnClientSignal`
    object naming a layer the schema does not declare passed the entire unit
    suite.
    """
    rows = objects_of_kind("OtnClientSignal")
    unplaced = [str(entry["name"]) for entry in rows if "auto_selectable" not in entry]
    assert not unplaced, f"client signals not stating auto_selectable: {sorted(unplaced)}"

    selectable = {str(entry["layer"]) for entry in rows if entry["auto_selectable"]}
    assert selectable == {"ethernet", "sdh", "pdh", "fibre_channel"}, (
        f"the automatically selectable layers moved: {sorted(selectable)}"
    )

    declared = set(_layer_choices())
    named = {str(entry["layer"]) for entry in rows}
    assert named <= declared, f"client signals naming an undeclared layer: {sorted(named - declared)}"


def test_the_g652_fiber_type_matches_the_units_default() -> None:
    """Check 12. units.py publishes GROUP_INDEX_G652_MILLI as the default and
    nothing else checks the catalog agrees with it."""
    fiber_types = {entry["name"]: entry for entry in objects_of_kind("OtnFiberType")}
    assert "G.652.D" in fiber_types, f"G.652.D is missing from the fiber-type catalog: {sorted(fiber_types)}"
    assert fiber_types["G.652.D"]["group_index_milli"] == GROUP_INDEX_G652_MILLI


def test_every_element_class_override_restates_the_generic_choices() -> None:
    """The one duplication in `schemas/`, held to its source, and the only guard.

    `element_class` is declared on `OtnOpticalElement` and overridden on each of
    the seven concrete kinds that inherit it, so each can carry a
    `default_value` matching its own class. Overriding an inherited Dropdown
    requires the full `choices` list, so the ten choices are written nine
    times.

    The server catches only half of getting that wrong, which is why this test
    is load-bearing rather than belt and braces. Omitting the `choices` key
    from an override is rejected at load with "The property 'choices' is
    required for kind=Dropdown". An override that supplies a `choices` key
    holding nine of the ten loads in silence: the generic then reports ten
    options and the overriding kinds report nine, and nothing complains until
    an object tries to write the value the override left out. Measured, not
    assumed.

    So a choice added to the generic and forgotten on one node produces a
    dropdown that offers different options depending on which kind you are
    looking at, and this test is the only thing that will say so. Weakening it
    removes the guard entirely.

    The default is checked too. It has to be one of the choices, and it has to
    be the class the kind actually is, which is the whole reason the override
    exists.
    """
    attributes = _attributes_by_kind()
    generic = next(
        (item for item in attributes.get("OtnOpticalElement") or [] if item.get("name") == "element_class"),
        None,
    )
    assert generic is not None, "OtnOpticalElement no longer declares element_class"
    expected = generic.get("choices")
    assert expected, "OtnOpticalElement.element_class declares no choices"
    # Pinned. The comparison below reads the generic at runtime, so a choice
    # deleted from the generic and from every override would pass it silently.
    assert [str(choice.get("name")) for choice in expected] == [
        "transponder",
        "roadm",
        "amplifier",
        "mux_demux",
        "patch_panel",
        "fiber_span",
        "splitter",
        "attenuator",
        "raman_pump",
        "odu_switch",
    ], "the generic's choice list changed; every override has to change with it"

    expected_defaults = {
        "OtnTransponder": "transponder",
        "OtnRoadm": "roadm",
        "OtnAmplifier": "amplifier",
        "OtnMuxDemux": "mux_demux",
        "OtnPatchPanel": "patch_panel",
        "OtnFiberSpan": "fiber_span",
        "OtnRamanPump": "raman_pump",
        "OtnOduSwitch": "odu_switch",
    }

    for kind, default in sorted(expected_defaults.items()):
        override = next(
            (item for item in attributes.get(kind) or [] if item.get("name") == "element_class"),
            None,
        )
        assert override is not None, f"{kind} no longer overrides element_class, so it has no default"
        assert override.get("choices") == expected, (
            f"{kind}.element_class restates a different choice list from OtnOpticalElement. "
            "A short list loads without complaint and fails only when an object writes the "
            "missing value, so the full list has to be repeated and kept in step here."
        )
        assert override.get("default_value") == default, (
            f"{kind}.element_class defaults to {override.get('default_value')!r}, expected {default!r}"
        )

    unexpected = [
        kind
        for kind, items in attributes.items()
        if kind not in expected_defaults
        and kind != "OtnOpticalElement"
        and any(item.get("name") == "element_class" for item in items)
    ]
    assert not unexpected, f"kinds overriding element_class without a recorded default: {sorted(unexpected)}"


# A hop's `cumulative_*` attribute is the running total of the path's `total_*`
# attribute, so the last hop always carries the path's own figure. Pairs of caps
# that have to agree, and the one that did not:
RUNNING_TOTAL_PAIRS = (
    ("cumulative_length_m", "total_length_m"),
    ("cumulative_loss_mdb", "total_loss_mdb"),
)


@pytest.mark.parametrize(("hop_name", "path_name"), RUNNING_TOTAL_PAIRS)
def test_a_running_total_is_capped_no_lower_than_the_total_it_accumulates(hop_name: str, path_name: str) -> None:
    """A hop cap below its path's cap refuses a write the path would accept.

    The last hop's cumulative figure IS the route total, so the two bounds are
    one bound written twice. When they drifted, the write failed on the hop
    rather than on the path, which is both the wrong place to read the error and
    silent about the route being too lossy.

    Measured when this was found: `cumulative_loss_mdb` at 500000 against
    `total_loss_mdb` at 1000000 refused 224 of the 830 route-directions the
    traversal can build on the shipped plant, over 122 distinct routes, the
    worst being Madrid to Vienna at 854295.
    """
    hop_caps = {
        kind: (attribute.get("parameters") or {}).get("max_value") for kind, attribute in _find_attributes(hop_name)
    }
    path_caps = {
        kind: (attribute.get("parameters") or {}).get("max_value") for kind, attribute in _find_attributes(path_name)
    }

    for hop_kind, hop_cap in sorted(hop_caps.items()):
        for path_kind, path_cap in sorted(path_caps.items()):
            assert hop_cap is not None and path_cap is not None, f"{hop_kind}/{path_kind} lost a bound"
            assert hop_cap >= path_cap, (
                f"{hop_kind}.{hop_name} caps at {hop_cap} while {path_kind}.{path_name} caps at {path_cap}. "
                f"The hop accumulates the path's own figure, so a lower cap refuses the last hop of any "
                f"route the path itself would accept."
            )


# Every kind inheriting OtnOpticalElement. OtnRouter is deliberately absent:
# light terminates at a router, so a router contributes no insertion loss and a
# query against the generic must not return one.
OPTICAL_ELEMENT_KINDS = (
    "OtnAmplifier",
    "OtnFiberSpan",
    "OtnMuxDemux",
    "OtnOduSwitch",
    "OtnPatchPanel",
    "OtnRamanPump",
    "OtnRoadm",
    "OtnTransponder",
)


def test_the_optical_element_generic_has_exactly_the_pinned_implementers() -> None:
    """The inheritance boundary, pinned offline.

    `tests/integration/test_infrahub.py` asserts the same set against a live
    graph, and that is the version which caught feature 017 adding
    `OtnOduSwitch` and not updating the list. It cost a six-minute run with a
    container stack behind it to say so.

    This is the same assertion for the price of reading eight YAML files. It is
    not a duplicate of the integration test: that one proves the *server* returns
    these kinds for the generic, which is a claim about Infrahub. This one proves
    the *schema* declares them, which is a claim about this repository, and it is
    the half a contributor can break.

    A kind added here without a reason is a kind the optical budget will start
    summing. Adding `OtnRouter` would make the budget charge a loss that does not
    exist, which is the failure the two-generic split exists to prevent.
    """
    declared = set()
    for _, document in _load_documents():
        for node in document.get("nodes") or []:
            if "OtnOpticalElement" in (node.get("inherit_from") or []):
                declared.add(f"{node['namespace']}{node['name']}")

    assert declared == set(OPTICAL_ELEMENT_KINDS), (
        "the set of kinds inheriting OtnOpticalElement moved. Update the tuple here, the "
        "matching one in tests/integration/test_infrahub.py, and the count the schema "
        "reference and concepts pages state, or the docs go stale and the budget sums a "
        f"kind nobody decided on. Added: {sorted(declared - set(OPTICAL_ELEMENT_KINDS))}. "
        f"Removed: {sorted(set(OPTICAL_ELEMENT_KINDS) - declared)}."
    )
    assert "OtnRouter" not in declared, "light terminates at a router, so it contributes no insertion loss"


def test_no_kind_gives_two_fields_the_same_order_weight() -> None:
    """Two fields at one weight order arbitrarily, and the order can change.

    `order_weight` decides what a reader sees first on an object page. When two
    fields share a weight, which one comes first is left to whatever the server
    does with a tie, and it may differ between one schema load and the next. The
    page moves for no reason anybody changed.

    Found by an audit on `OtnFiberSpan`, where `element_class` and
    `aging_margin_mdb` were both 1800. `element_class` is 1800 on all nine kinds
    that carry it, so the one that moved was the other.

    Attributes and relationships are weighted in one sequence, because they are
    rendered in one sequence.
    """
    for path, document in _load_documents():
        for group in ("generics", "nodes"):
            for node in document.get(group) or []:
                kind = f"{node['namespace']}{node['name']}"
                weights: dict[int, list[str]] = {}
                for field in (node.get("attributes") or []) + (node.get("relationships") or []):
                    weight = field.get("order_weight")
                    if weight is None:
                        continue
                    weights.setdefault(int(weight), []).append(str(field["name"]))
                clashes = {weight: names for weight, names in weights.items() if len(names) > 1}
                assert not clashes, (
                    f"{kind} in {path.name} gives one order_weight to more than one field: "
                    + "; ".join(f"{weight} -> {sorted(names)}" for weight, names in sorted(clashes.items()))
                )


# ---------------------------------------------------------------------------
# The six reason codes.
# ---------------------------------------------------------------------------
def _service_attribute(name: str) -> dict[str, Any]:
    matches = [attribute for attribute in _attributes_by_kind()["OtnService"] if attribute["name"] == name]
    assert len(matches) == 1, f"OtnService declares {len(matches)} attributes named {name}, expected exactly one"
    return matches[0]


def test_the_rejection_code_choices_are_exactly_the_six_python_constants() -> None:
    """The Dropdown and the constants are one list written twice, so pin them together.

    The generator writes `rejection_code` from a Python constant and the server
    accepts only a declared choice. Add a seventh code to one side and the
    failure arrives at write time, on a live branch, inside a pipeline run,
    saying only that a value is not valid for the attribute. This says it here,
    in two seconds, naming which side moved.

    Read off `routing.REJECTION_CODES`, which is the whole vocabulary and not
    `REASON_PRECEDENCE`, which orders only the five this module reports itself. A
    test tempted to build the set from precedence would be five-sixths right and
    would silently drop the code the demo refuses on most often.
    """
    declared = {choice["name"] for choice in _service_attribute("rejection_code")["choices"]}
    constants = set(REJECTION_CODES)

    assert len(constants) == len(REJECTION_CODES) == 6, (
        "two reason constants hold the same string, so one of them is unreachable"
    )
    assert declared == constants, (
        "the rejection_code choices and the Python reason constants disagree. In the schema and not in "
        f"Python: {sorted(declared - constants)}. In Python and not in the schema: {sorted(constants - declared)}."
    )


def test_the_channel_reasons_are_not_rejection_codes() -> None:
    """`CHANNEL_NO_SPECTRUM` and `CHANNEL_NO_BLOCK` are detail text, not codes.

    They say why a channel is `None` and they are folded into the prose half of
    a refusal, never into the code half. They sit twelve lines from
    `REASON_BUDGET` in `routing.py` and the word "reason" is in both names, so
    the mistake this pins is a genuinely easy one: promote them to choices and
    the Dropdown starts offering an operator two filters that no generator ever
    writes.
    """
    declared = {choice["name"] for choice in _service_attribute("rejection_code")["choices"]}
    assert not declared & set(CHANNEL_REASONS), (
        "a channel reason became a rejection_code choice. These are Selection.channel_reason values, "
        f"not refusal codes: {sorted(declared & set(CHANNEL_REASONS))}."
    )


def test_the_acceptance_flag_is_mandatory_and_defaults_to_not_accepted() -> None:
    """Mandatory with a default is what makes this safe to add to a loaded kind.

    Mandatory with no default fails validation against every existing
    `OtnService` and blocks the whole schema update. Optional would let a
    service carry no answer at all, and `checks/provisionable.py` would then
    have to invent one, which is where a gate quietly starts failing open.

    `false` is also the correct reading of every service written before this
    feature: nobody signed for those refusals, because there was nothing to sign.
    """
    accepted = _service_attribute("refusal_accepted")
    assert accepted["kind"] == "Boolean"
    assert accepted["optional"] is False
    assert accepted["default_value"] is False, "the default has to be false, or a new service is born accepting"


def test_the_old_rejection_reason_attribute_is_gone() -> None:
    """Deleted outright, not retired with `state: absent`.

    The dataset here is generated and reloads in one command, so the graph
    catches up with `uv run invoke init` and a retired declaration would be a
    paragraph of reading cost that changes nothing. `specs/022-provisionable-gate/research.md`
    R-003 records the decision and the case where the other answer is right.
    """
    names = {attribute["name"] for attribute in _attributes_by_kind()["OtnService"]}
    assert "rejection_reason" not in names, "rejection_reason was replaced by rejection_code and rejection_detail"
    assert {"rejection_code", "rejection_detail"} <= names


# ---------------------------------------------------------------------------
# The carrier to line port edge.
# ---------------------------------------------------------------------------
def _relationship(kind: str, name: str) -> dict[str, Any]:
    """One named relationship on one kind, or fail saying which half is missing."""
    for _, document in _load_documents():
        for group in ("generics", "nodes"):
            for node in document.get(group) or []:
                if f"{node['namespace']}{node['name']}" != kind:
                    continue
                matches = [item for item in node.get("relationships") or [] if item["name"] == name]
                assert len(matches) == 1, f"{kind} declares {len(matches)} relationships named {name}, expected one"
                return matches[0]
    pytest.fail(f"no kind named {kind} in schemas/")


def test_the_carrier_and_line_port_sides_share_one_written_identifier() -> None:
    """Both halves write `otn_carrier__line_ports`, and the string is literal here.

    Left off, Infrahub derives an identifier per side from that side's own kind
    and its peer. Both peers are concrete and mirror each other, so the two
    derived strings would agree and the edge would form, which is exactly what
    makes this worth pinning: the failure mode is not a broken edge, it is an
    edge keyed on `otnlineport__otnopticalcarrier`, matching neither the four
    identifiers already on `OtnOpticalCarrier` nor anything a reader would grep
    for.

    The literal is repeated here rather than read off one side and compared to
    the other. Reading it off would pass on a pair that agreed on the wrong
    string, which is the one thing that cannot be corrected later: an identifier
    is frozen the moment a load succeeds and changing it costs a remove-and-re-add
    on both sides.
    """
    port_side = _relationship("OtnLinePort", "carrier")
    carrier_side = _relationship("OtnOpticalCarrier", "line_ports")

    assert port_side["identifier"] == "otn_carrier__line_ports"
    assert carrier_side["identifier"] == "otn_carrier__line_ports"
    assert port_side["peer"] == "OtnOpticalCarrier"
    assert carrier_side["peer"] == "OtnLinePort"


def test_neither_side_of_the_carrier_edge_cascades_on_delete() -> None:
    """`no-action` is a decision, and a decision with no test is a comment.

    The physical reading argues for cascade: light exists because a transponder
    generates it. It is wrong here because a wavelength is terminated at both of
    its ends. Cascading from the port side would delete the carrier out from
    under the line port at the far site, and would reach the carrier's containers
    and the services groomed into them, so pulling one transponder would destroy
    customer service records. Cascading from the carrier side would delete the
    physical ports that terminated a wavelength, so retiring a service would
    remove hardware from the inventory.

    Deleting a transponder still deletes its line ports. That happens through
    `otn_device__ports`, which is `kind: Component` with `on_delete: cascade`,
    and it is a different edge from this one. The deletion stops at the port.

    Full reasoning in `specs/025-transponder-carrier-binding/research.md`, R-004a.
    """
    for kind, name in (("OtnLinePort", "carrier"), ("OtnOpticalCarrier", "line_ports")):
        relationship = _relationship(kind, name)
        assert relationship["on_delete"] == "no-action", (
            f"{kind}.{name} cascades. Deleting one end of a wavelength must not delete the other."
        )


def test_the_carrier_edge_is_one_to_many_and_optional_on_both_sides() -> None:
    """One colour per line side, two ends per wavelength, and both may be empty.

    Cardinality one on the port because a coherent line side emits one wavelength
    at a time. Many on the carrier because a wavelength is terminated at each of
    its two ends, which are two ports on two devices at two sites.

    Optional on both, and both states are reachable rather than theoretical. Six
    sites terminate an odd number of wavelengths, so each carries a line port with
    no colour; and `generators/optical_service.py` provisions a wavelength without
    binding a line port, so a fresh carrier has an empty list.

    `kind: Attribute` on both, because neither object owns the other. A carrier is
    not a component of a port and a port is not a component of a carrier.
    """
    port_side = _relationship("OtnLinePort", "carrier")
    carrier_side = _relationship("OtnOpticalCarrier", "line_ports")

    assert port_side["cardinality"] == "one"
    assert carrier_side["cardinality"] == "many"
    assert port_side["optional"] is True
    assert carrier_side["optional"] is True
    assert port_side["kind"] == "Attribute"
    assert carrier_side["kind"] == "Attribute"


def test_no_port_kind_but_the_line_port_carries_a_carrier() -> None:
    """Not on `OtnOpticalPort`, which six kinds inherit, and not on any sibling.

    A ROADM degree port, an amplifier port and a router port terminate nothing. A
    field they can never fill is a field that invites a null check, and it would
    let a write bind a wavelength to a port that cannot emit one.

    Scoped to the port family, because `OtnContainer.carrier` exists and is a
    different edge on a different identifier: what an ODU container rides. Widening
    this to every kind fails on that one, and dropping it to `OtnLinePort` alone
    would let the field reappear on the generic it was kept off.
    """
    port_generics = ("OtnGenericPort", "OtnOpticalPort")
    for _, document in _load_documents():
        for group in ("generics", "nodes"):
            for node in document.get(group) or []:
                kind = f"{node['namespace']}{node['name']}"
                if kind == "OtnLinePort":
                    continue
                inherited = set(node.get("inherit_from") or [])
                if kind not in port_generics and not inherited & set(port_generics):
                    continue
                names = {item["name"] for item in node.get("relationships") or []}
                assert "carrier" not in names, f"{kind} declares a carrier relationship, only OtnLinePort may"
