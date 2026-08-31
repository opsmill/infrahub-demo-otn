"""`containers.py`, which is one table and five short functions over it.

A table of constants does not need a test that reads the constants back. What
earns a place here is the handful of places where the obvious reading of the
table is the wrong one, because each of those is a change somebody will make in
good faith:

- ODU2e occupies nine slots and offers eight. Every other sized row has one
  number twice, so the natural tidy-up is to make this row agree with itself.
- Unknown is not zero. Substituting zero for an unsized child turns a container
  that might be overfull into one that reports room.
- `largest_fit(0)` and `largest_fit(None)` are different questions. Nothing fits
  is a verdict; nobody knows is not.
- `section_headroom(())` is `None`, and `None` must not read as roomy.
- An overfilled parent returns a negative number rather than a clamped zero.

The schema bound is checked here too. `tributary_slots` and
`tributary_slot_capacity` both cap at 640 in the YAML, which is ODUC8 and the
largest row in the table. Two files holding the same number is drift waiting to
happen, so the test recomputes one from the other.

The line-rate map earns a test for the same reason, and it earns it the hard way.
It covered 100 and 400 while the mode catalog carried six rates, so a service on
a 200G mode was refused for having no container type. The refusal read as a
physics verdict and was a missing table row, so the catalog is now the thing the
test reads.
"""

from typing import Any

import pytest
import yaml

from infrahub_demo_otn.containers import (
    LINE_CONTAINER_BY_LINE_RATE_GBPS,
    SLOT_RATE_KBPS,
    SLOT_TABLE,
    UNKNOWN_TYPES,
    free_slots,
    largest_fit,
    section_headroom,
    section_tightest,
    slot_capacity,
    slots_for_client,
    slots_occupied,
)
from infrahub_demo_otn.units import KBPS_PER_GBPS
from tests.unit.conftest import objects_of_kind, schema_files

SLOT_ATTRIBUTES = ("tributary_slots", "tributary_slot_capacity")


def _schema_attributes(name: str) -> list[dict[str, Any]]:
    """Every declaration of one attribute name, across every schema file.

    Collected from all files rather than the first match, so a second kind
    declaring the same attribute with a different bound cannot hide behind the
    first one.
    """
    found: list[dict[str, Any]] = []
    for path in schema_files():
        document = yaml.safe_load(path.read_text())
        for section in ("generics", "nodes"):
            for entry in document.get(section) or []:
                for attribute in entry.get("attributes") or []:
                    if attribute.get("name") == name:
                        found.append(attribute)
    return found


# ---------------------------------------------------------------------------
# The table is the enum
# ---------------------------------------------------------------------------


def test_the_table_holds_exactly_the_container_types_the_schema_allows() -> None:
    """A type in the enum and not in the table raises on the first client that
    uses it, which is a runtime failure for something a test can see."""
    declared = _schema_attributes("odu_type")
    assert declared, "odu_type is not declared in any schema file"
    for attribute in declared:
        assert set(attribute["enum"]) == set(SLOT_TABLE), attribute["enum"]


# ---------------------------------------------------------------------------
# Invariant 1: the ODU2e asymmetry
# ---------------------------------------------------------------------------


def test_odu2e_occupies_nine_slots_and_offers_eight() -> None:
    """A 10.3125 Gbit/s client does not fit in eight slots of 1.25 Gbit/s, so
    G.709 gives the container a ninth slot in its parent and leaves its payload
    at eight. Both figures, explicitly, so the row cannot be made to agree with
    itself."""
    assert slots_occupied("ODU2e") == 9
    assert slot_capacity("ODU2e") == 8
    assert slots_occupied("ODU2e") != slot_capacity("ODU2e")


def test_an_odu2e_in_an_odu4_leaves_seventy_one_slots_not_seventy_two() -> None:
    """The asymmetry as a caller sees it. Off by one the other way and the
    generator packs a client into a slot that does not exist."""
    assert free_slots(slot_capacity("ODU4"), [slots_occupied("ODU2e")]) == 71


# ---------------------------------------------------------------------------
# Invariant 2: ten ODU2 fill an ODU4 exactly
# ---------------------------------------------------------------------------


def test_ten_odu2_children_fill_an_eighty_slot_parent_to_exactly_zero_free() -> None:
    """The demo's headline arithmetic: ten 10G services on one 100G wavelength.
    Zero free is a real figure, and `largest_fit` reads it as nothing fits."""
    assert slot_capacity("ODU4") == 80
    assert free_slots(80, [slots_occupied("ODU2")] * 10) == 0
    assert largest_fit(0) is None


def test_largest_fit_names_the_biggest_type_that_still_goes_in() -> None:
    """Eighty is a tie between ODU4 and ODUC1 and the table's order breaks it."""
    assert largest_fit(80) == "ODU4"
    assert largest_fit(79) == "ODU3"
    assert largest_fit(8) == "ODU2"
    assert largest_fit(1) == "ODU0"
    assert largest_fit(320) == "ODUC4"
    # The two rows added for the 600G and 800G modes. One slot short of each and
    # the answer steps down to the row below, which is what proves the new rows
    # are ordered by occupancy rather than appended at the end.
    assert largest_fit(479) == "ODUC4"
    assert largest_fit(480) == "ODUC6"
    assert largest_fit(639) == "ODUC6"
    assert largest_fit(640) == "ODUC8"


def test_largest_fit_refuses_an_unknown_free_count_rather_than_calling_it_full() -> None:
    """The two `None` meanings that must not be conflated. `largest_fit(0)` is
    `None` for nothing fits; an unknown count has no answer at all."""
    with pytest.raises(ValueError, match="unknown free-slot count"):
        largest_fit(None)


# ---------------------------------------------------------------------------
# Invariant 3: the table's maximum is the schema's bound
# ---------------------------------------------------------------------------


def test_the_largest_occupancy_in_the_table_is_the_bound_the_schema_sets() -> None:
    """640 slots is ODUC8. The YAML says it twice and this module says it once,
    so the recomputation is what keeps all three the same number.

    A bound below the widest row rejects a container the arithmetic here calls
    legal, which is the failure that raising the table without raising the schema
    would produce, and it would show up only on the first 800G wavelength."""
    largest = max(sizes.occupies for sizes in SLOT_TABLE.values() if sizes.occupies is not None)
    assert largest == 640
    for name in SLOT_ATTRIBUTES:
        declared = _schema_attributes(name)
        assert declared, f"{name} is not declared in any schema file"
        for attribute in declared:
            parameters = attribute.get("parameters") or {}
            assert parameters.get("max_value") == largest, name
            assert parameters.get("min_value") == 0, name


# ---------------------------------------------------------------------------
# Invariant 4: unknown is not zero, and it propagates
# ---------------------------------------------------------------------------


def test_the_four_unsized_types_report_no_occupancy() -> None:
    """ODUflex is sized by its client rate; the three SDH virtual containers are
    not OTN constructs. None of the four has a figure to give."""
    assert UNKNOWN_TYPES == {"ODUflex", "VC-12", "VC-4", "STM-N"}
    for name in sorted(UNKNOWN_TYPES):
        assert slots_occupied(name) is None, name


def test_an_odu_flex_offers_a_real_zero_while_the_sdh_types_offer_nothing_known() -> None:
    """Zero and unknown in the same column, on purpose. An ODUflex carries a
    client and nests nothing; a VC-4's capacity is simply not defined here."""
    assert slot_capacity("ODUflex") == 0
    assert slot_capacity("ODU0") == 0
    for name in ("VC-12", "VC-4", "STM-N"):
        assert slot_capacity(name) is None, name


def test_one_unsized_child_makes_the_whole_free_figure_unknown() -> None:
    """Not the figure its sized siblings would give. Reading unknown as zero
    would report a parent that might be overfull as having room."""
    assert free_slots(80, [slots_occupied("ODU2"), slots_occupied("ODUflex")]) is None
    assert free_slots(None, [slots_occupied("ODU2")]) is None
    assert free_slots(None, []) is None
    # And the sized case for contrast, so the test above is not passing because
    # every call returns None.
    assert free_slots(80, [slots_occupied("ODU2")]) == 72
    assert free_slots(80, []) == 80


# ---------------------------------------------------------------------------
# Invariant 5: an overfilled parent is negative, not zero
# ---------------------------------------------------------------------------


def test_an_overfilled_parent_reports_a_negative_figure() -> None:
    """Clamping at zero would delete the finding the capacity check exists for.
    Nine ODU2e is 81 slots in an 80-slot ODU4, over by one."""
    assert free_slots(80, [slots_occupied("ODU2e")] * 9) == -1
    assert free_slots(8, [slots_occupied("ODU4")]) == -72


# ---------------------------------------------------------------------------
# Invariant 6: an empty section has no headroom figure
# ---------------------------------------------------------------------------


def test_a_section_with_no_lit_carrier_has_no_headroom_figure() -> None:
    """`None` means not known. It is the map's unknown band, and a caller that
    paints it as roomy has misread it."""
    assert section_headroom(()) is None
    assert section_tightest(()) is None


def test_headroom_is_the_best_free_figure_and_tightest_is_the_worst() -> None:
    assert section_headroom((72, 0, 8)) == 72
    assert section_tightest((72, 0, 8)) == 0
    assert section_headroom((0,)) == 0
    assert section_tightest((-1, 40)) == -1


def test_one_unknown_carrier_makes_the_section_figures_unknown() -> None:
    """Skipping the unknown carrier would report the section's headroom as the
    best of the carriers somebody happened to be able to measure."""
    assert section_headroom((72, None, 8)) is None
    assert section_tightest((72, None, 8)) is None


# ---------------------------------------------------------------------------
# Invariant 7: a type outside the enum raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("odu_type", ["ODU5", "odu2", "ODU-2", "", "ODUC5"])
def test_a_type_outside_the_enum_raises_rather_than_returning_none(odu_type: str) -> None:
    """A typo in `odu_type` is a data error. Returning `None` would file it
    alongside ODUflex as a quantity nobody knows, and the check would pass."""
    with pytest.raises(ValueError, match="not one of the 16 container types"):
        slots_occupied(odu_type)
    with pytest.raises(ValueError, match="not one of the 16 container types"):
        slot_capacity(odu_type)


# ---------------------------------------------------------------------------
# Invariant 8: an unsized container is sized from its client
# ---------------------------------------------------------------------------


def test_the_slot_rate_is_the_one_point_two_five_gigabit_slot_written_in_kbps() -> None:
    """Two files hold the kbps convention and one of them holds this number, so
    the number is recomputed from the other. 1.25 Gbit/s is five quarters of a
    gigabit, in integers, because a float would put a rounding decision into a
    slot count."""
    assert SLOT_RATE_KBPS == 5 * KBPS_PER_GBPS // 4
    assert SLOT_RATE_KBPS == 1_250_000


def test_an_infiniband_client_sizes_its_flex_container_from_its_bit_rate() -> None:
    """The contract's worked example. 206.25 Gbit/s over a 1.25 Gbit/s slot is
    165 slots, which goes into an ODUC4 and does not go into an ODU4, so the
    figure decides which wavelength the service can ride."""
    assert slots_for_client("ODUflex", 206_250_000) == 165
    capacity_400g = slot_capacity(LINE_CONTAINER_BY_LINE_RATE_GBPS[400])
    capacity_100g = slot_capacity(LINE_CONTAINER_BY_LINE_RATE_GBPS[100])
    assert capacity_400g is not None and capacity_100g is not None
    assert free_slots(capacity_400g, [165]) == 155
    assert free_slots(capacity_100g, [165]) == -85
    # The catalog row itself runs at 212.5 Gbit/s, not the 206.25 the contract
    # quotes, so both figures are checked. Neither fits a 100G wavelength and
    # both fit a 400G one, which is why the discrepancy changes no verdict, but
    # asserting only the contract's number would leave the shipped signal
    # untested.
    assert slots_for_client("ODUflex", 212_500_000) == 170


def test_the_smallest_client_in_the_catalog_takes_a_slot_and_not_none() -> None:
    """An E1 at 2048 kbps is 0.0016 slots. Zero is the arithmetically correct
    answer and the operationally wrong one: a container occupying no slots is
    invisible to every capacity figure downstream."""
    assert slots_for_client("VC-12", 2_048) == 1


def test_the_division_rounds_up_rather_than_to_nearest() -> None:
    """One kilobit over a slot boundary takes the next whole slot. Rounding to
    nearest would pack a client into a slot that does not exist."""
    assert slots_for_client("ODUflex", SLOT_RATE_KBPS) == 1
    assert slots_for_client("ODUflex", SLOT_RATE_KBPS + 1) == 2
    assert slots_for_client("ODUflex", 2 * SLOT_RATE_KBPS - 1) == 2


def test_every_unsized_type_still_reports_no_figure_from_the_table() -> None:
    """`slots_for_client` sizing a real container does not put a number into the
    table. The two answer different questions and the table keeps holding none of
    the figures G.709 does not define."""
    for name in sorted(UNKNOWN_TYPES):
        assert slots_occupied(name) is None, name
        assert slots_for_client(name, 10_000_000) == 8, name


# ---------------------------------------------------------------------------
# Invariant 9: a sized type ignores the bit rate it is handed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bit_rate_kbps", [0, 2_048, 10_312_500, 206_250_000])
def test_a_sized_type_returns_the_table_figure_whatever_rate_it_is_handed(bit_rate_kbps: int) -> None:
    """The two paths cannot disagree, because the sized path never looks at the
    rate. A caller that has stopped branching on which half of the table it holds
    is relying on exactly this."""
    for name, sizes in SLOT_TABLE.items():
        if sizes.occupies is None:
            continue
        assert slots_for_client(name, bit_rate_kbps) == sizes.occupies, name


def test_a_sized_type_takes_the_table_figure_where_the_arithmetic_would_differ() -> None:
    """The discriminating case, so the test above is not passing by coincidence.
    A 10.3125 Gbit/s client is 8.25 slots, which rounds up to nine, and G.709
    gives an ODU2 eight. The table wins; an ODU2e is the row that takes nine, and
    it takes nine from the table rather than from this arithmetic."""
    assert slots_for_client("ODU2", 10_312_500) == 8
    assert -(-10_312_500 // SLOT_RATE_KBPS) == 9
    assert slots_for_client("ODU2e", 10_312_500) == 9


@pytest.mark.parametrize("odu_type", ["ODU5", "oduflex", "VC-3", ""])
def test_a_type_outside_the_enum_is_not_sized_from_a_bit_rate(odu_type: str) -> None:
    """A typo must not fall through to the bit-rate arithmetic. That path returns
    a plausible number for a container type that does not exist, and the caller
    has no way to tell it from a real one."""
    with pytest.raises(ValueError, match="not one of the 16 container types"):
        slots_for_client(odu_type, 10_000_000)


# ---------------------------------------------------------------------------
# The line container map
# ---------------------------------------------------------------------------


def test_each_line_rate_maps_to_a_container_that_offers_the_whole_wavelength() -> None:
    """A line container has to offer every slot it occupies, or provisioning
    loses capacity at the top of the tree before a client is written."""
    assert LINE_CONTAINER_BY_LINE_RATE_GBPS == {
        100: "ODU4",
        200: "ODUC2",
        300: "ODUC3",
        400: "ODUC4",
        600: "ODUC6",
        800: "ODUC8",
    }
    for name in LINE_CONTAINER_BY_LINE_RATE_GBPS.values():
        assert slot_capacity(name) == slots_occupied(name), name
    # The faster wavelength is the wider container, and the 800G one is the
    # widest row the table has.
    assert LINE_CONTAINER_BY_LINE_RATE_GBPS[800] == max(SLOT_TABLE, key=lambda name: SLOT_TABLE[name].occupies or 0)


def test_a_line_container_offers_eighty_slots_per_hundred_gigabits() -> None:
    """ODUCn is n times 100G, and 100G is eighty 1.25 Gbit/s slots.

    Written as arithmetic rather than as six asserted pairs, so a seventh rate
    added with the wrong container type is caught by the rule and not by whether
    somebody remembered to extend a list."""
    for rate, name in LINE_CONTAINER_BY_LINE_RATE_GBPS.items():
        assert slot_capacity(name) == rate * 80 // 100, name


def test_every_line_rate_in_the_mode_catalog_has_a_container_type() -> None:
    """The defect `invoke demo` found, as a test.

    The map covered 100 and 400 while `objects/03_optical_modes.yml` carried six
    rates, so a service on a 200G mode was refused for having no container type.
    That refusal reads as a physics verdict on the wavelength and is nothing of
    the kind, which is worse than a crash. Reading the catalog rather than
    restating its rates is what makes a seventh mode fail here instead of in the
    generator."""
    rates = {int(mode["line_rate_gbps"]) for mode in objects_of_kind("OtnOpticalMode")}
    assert rates == {100, 200, 300, 400, 600, 800}
    missing = sorted(rates - set(LINE_CONTAINER_BY_LINE_RATE_GBPS))
    assert not missing, f"the mode catalog runs at {missing} Gbit/s with no line container type"
