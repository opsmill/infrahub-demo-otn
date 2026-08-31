"""Tributary slot arithmetic for the ODU layer: the G.709 table and what follows.

The one implementation of the capacity rule. The service generator calls it to
decide where a client fits, the capacity check calls it to decide whether the
data still adds up, and the ODU map calls it to decide what colour a section is.
Three callers, one table, so the three cannot disagree about whether a
wavelength is full.

Pure functions over plain data. Nothing here imports `infrahub_sdk`, touches the
network or reads a file, so its tests need no server and no Docker.

A tributary slot is 1.25 Gbit/s of an ODU's payload. Two figures per container
type, not one: what a container of that type takes up in its parent, and what it
offers to its own children. An ODU4 offers 80 slots and takes 80 in whatever
carries it, which makes the two look like the same number and invites somebody to
collapse them. Two of the sixteen types say otherwise, and both asymmetries are
load bearing.

**ODU2e occupies nine slots and offers eight.** A 10 Gigabit Ethernet client with
its preamble and inter-packet gap intact runs at 10.3125 Gbit/s, which does not
fit in eight slots of 1.25. G.709 gives the container a ninth slot in its parent
for the overhead and leaves its payload at the eight an ODU2 offers. So an ODU2e
in an ODU4 leaves 71 slots free, not 72, and an ODU2e still only takes one ODU2's
worth of clients.

**Unknown is not zero, and it propagates.** Four of the sixteen types have no
defined slot size here: ODUflex is sized by its client's rate rather than by the
table, and VC-12, VC-4 and STM-N are SDH virtual containers that are not OTN
constructs at all. They are in the enum because an E1 maps into a VC-12 into an
STM-N into an ODU1, and without them that chain cannot be written down. Their
size is `None`, and a parent holding one has an unknown free-slot figure rather
than a figure computed from its sized siblings. Reading unknown as zero is the
failure this module exists to make impossible: it would report a container that
might be overfull as having room.

**The table stays unknown; a real container of an unsized type does not.** The
four rows above have no figure because G.709 gives them none, and no default
belongs in the table. A container that actually exists has a client, and a client
has a bit rate, so `slots_for_client` sizes it from that instead: the rate over
the 1.25 Gbit/s slot, rounded up, never fewer than one. The two statements are
not in tension. `SLOT_TABLE` answers "what does this type take", which for those
four is nothing anybody can say; `slots_for_client` answers "what does this
container take", which is answerable as soon as a client is known. An E1 service
is therefore groomed and counted rather than refused, and the table still holds
no number G.709 does not define.

The negative result is stated rather than hidden in both directions.
`free_slots` returns a negative number for an overfilled parent instead of
clamping at zero, because an overfilled parent is exactly what the check is
looking for. `section_headroom` over no lit carrier returns `None`, which means
"not known", and a caller that paints that as roomy has misread it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping


@dataclass(frozen=True)
class SlotSizes:
    """The two slot figures for one container type.

    `occupies` is what a container of this type takes in its parent. `offers` is
    what it gives its own children. `None` on either means the figure is not
    defined, which is never the same statement as zero: `offers=0` says nothing
    nests inside this container, and `offers=None` says nobody here knows.
    """

    occupies: int | None
    offers: int | None


SLOT_TABLE: Mapping[str, SlotSizes] = {
    "ODU0": SlotSizes(occupies=1, offers=0),
    "ODU1": SlotSizes(occupies=2, offers=2),
    "ODU2": SlotSizes(occupies=8, offers=8),
    # The asymmetry. Nine in the parent for the 10.3125 Gbit/s client, eight to
    # its own children. Written out rather than derived, so a later pass that
    # tidies the table into one number per row has to delete a comment first.
    "ODU2e": SlotSizes(occupies=9, offers=8),
    "ODU3": SlotSizes(occupies=32, offers=32),
    "ODU4": SlotSizes(occupies=80, offers=80),
    "ODUC1": SlotSizes(occupies=80, offers=80),
    "ODUC2": SlotSizes(occupies=160, offers=160),
    "ODUC3": SlotSizes(occupies=240, offers=240),
    "ODUC4": SlotSizes(occupies=320, offers=320),
    # ODUCn is n times 100G, so these two are the line containers for the 600G
    # and 800G modes in `objects/03_optical_modes.yml`. They exist for that and
    # nothing else: no client signal in the catalog maps into one, and no mode
    # runs faster than 800G, so ODUC8's 640 slots is the widest row the table
    # needs and the bound the schema sets on both slot attributes.
    "ODUC6": SlotSizes(occupies=480, offers=480),
    "ODUC8": SlotSizes(occupies=640, offers=640),
    # Sized by its client rate, which this module does not read, so its own size
    # is unknown. Its capacity is a real zero: an ODUflex carries a client, not
    # a tree of smaller containers.
    "ODUflex": SlotSizes(occupies=None, offers=0),
    # Not OTN constructs. Both figures unknown, and no default that would let
    # one of these silently participate in an arithmetic it has no place in.
    "VC-12": SlotSizes(occupies=None, offers=None),
    "VC-4": SlotSizes(occupies=None, offers=None),
    "STM-N": SlotSizes(occupies=None, offers=None),
}
"""Container type to its two slot figures, in units of 1.25 Gbit/s.

The authoritative table, and the same sixteen keys as the `odu_type` enum on
`OtnContainer`. Insertion order is the enum's order, which is also increasing
occupancy, and `largest_fit` relies on that for its tie break.
"""

UNKNOWN_TYPES: frozenset[str] = frozenset(name for name, sizes in SLOT_TABLE.items() if sizes.occupies is None)
"""The four types with no defined tributary slot size: ODUflex, VC-12, VC-4 and
STM-N. Derived from the table rather than typed a second time, so the two cannot
drift apart."""

SLOT_RATE_KBPS = 1_250_000
"""One tributary slot, in kilobits per second. 1.25 Gbit/s, from G.709.

An integer in kbps, not 1.25 with a Gbit/s label, and the choice is the project
rule rather than taste. Every client rate on this model is stored in kbps, so the
division that turns a rate into a slot count is integer over integer and rounds
up in one expression. A float here would be the one place a slot count could come
back as 164.99999999 and then floor to 164, which is a container that fits where
it does not.

`units.py` is where a scale factor lives when it converts between two ways of
writing the same quantity. This one is not that: it is the size of an OTN
construct, which happens to be expressible as a rate, and it divides into the
table two lines below rather than into anything else. `test_containers.py`
recomputes it from `units.KBPS_PER_GBPS` so the two files cannot drift apart even
though the number is written here.
"""

LINE_CONTAINER_BY_LINE_RATE_GBPS: Mapping[int, str] = {
    100: "ODU4",
    200: "ODUC2",
    300: "ODUC3",
    400: "ODUC4",
    600: "ODUC6",
    800: "ODUC8",
}
"""Carrier line rate to the container type that rides it.

The line container is what holds the carrier and offers the slots every client
container is packed into. Six rates, which is every rate
`objects/03_optical_modes.yml` defines: 100, 200, 300, 400, 600 and 800 Gbit/s.
A mode at any other rate has no line container here and provisioning has to
refuse it rather than guess.

The map used to cover 100 and 400 alone, which are the only two rates the 71
pre-provisioned carriers ride. `invoke demo` found the gap: a service on a 200G
mode was refused for having no line container type, which reads as a physics
verdict and is nothing of the kind. Every rate in the catalog now has a type, so
a refusal from here means the rate is genuinely undefined.
"""


def slots_occupied(odu_type: str) -> int | None:
    """What a container of this type takes in its parent.

    `None` for one of the four unsized types. Raises `ValueError` for a type that
    is not in the enum at all, because an unrecognised type is a data error, and
    reporting it as an unknown quantity would let a typo in `odu_type` read as a
    container whose size nobody happens to know.
    """
    return _sizes(odu_type).occupies


def slot_capacity(odu_type: str) -> int | None:
    """What a container of this type offers its children.

    `0` is a real answer and means nothing nests inside it, which is true of
    ODU0 and of ODUflex. `None` means the figure is not defined. Raises for a
    type outside the enum, for the same reason `slots_occupied` does.
    """
    return _sizes(odu_type).offers


def slots_for_client(odu_type: str, bit_rate_kbps: int) -> int:
    """What a container of this type takes in its parent, given the client inside it.

    One entry point covering both halves of the table, and that is the whole
    reason it exists. The four unsized types get `ceil(bit_rate_kbps /
    SLOT_RATE_KBPS)` with a floor of one. The twelve sized types get the table's
    figure and the bit rate is ignored, so a caller never has to know which half
    it is holding.

    The alternative was a caller that branches on `slots_occupied(...) is None`.
    That branch gets written correctly the first time and forgotten the second,
    and the forgotten one writes the schema default of zero into
    `tributary_slots`. A container occupying zero slots is invisible to every
    capacity figure downstream: the wavelength reads empty however many clients
    are in it, the map paints it roomy and the check passes. One function with no
    branch for the caller to forget is cheaper than finding that later.

    **The floor of one is deliberate and it is not rounding.** An E1 at 2048 kbps
    works out to 0.0016 slots, and zero is the arithmetically correct answer to a
    question nobody asked. A 2 Mbit/s tributary still takes a slot in the
    multiplex, and reporting it as taking none puts it back in the invisible case
    above. The floor is what stops the smallest client in the catalog from being
    the one that breaks the count.

    Never returns `None`. An unsized type is unsized in the table, not in a
    provisioned network, and the negative result this module states elsewhere is
    about the table rather than about a container somebody can point at.

    Raises `ValueError` for a type outside the enum, on the same terms as
    `slots_occupied`. A typo in `odu_type` must not fall through to the bit-rate
    arithmetic as though it had been an ODUflex all along, because that path
    returns a plausible number for a type that does not exist.
    """
    sized = _sizes(odu_type).occupies
    if sized is not None:
        return sized
    # Ceiling division on integers. `-(-a // b)` rather than `math.ceil(a / b)`,
    # which would go through a float and put a rounding decision back in.
    return max(1, -(-bit_rate_kbps // SLOT_RATE_KBPS))


def _sizes(odu_type: str) -> SlotSizes:
    try:
        return SLOT_TABLE[odu_type]
    except KeyError:
        raise ValueError(f"{odu_type} is not one of the {len(SLOT_TABLE)} container types") from None


def free_slots(capacity: int | None, child_occupancies: Iterable[int | None]) -> int | None:
    """A parent's capacity less the sum of what its children occupy.

    `None` when the capacity is `None` or when any one child's occupancy is,
    because a parent holding an unsized child has an unknown free figure rather
    than one computed from the children that are sized.

    May return a negative number. An overfilled container is what the capacity
    check reports, so clamping at zero here would delete the finding before
    anything could act on it.
    """
    occupancies = list(child_occupancies)
    if capacity is None or any(occupancy is None for occupancy in occupancies):
        return None
    return capacity - sum(occupancy for occupancy in occupancies if occupancy is not None)


def largest_fit(free: int | None) -> str | None:
    """The type with the greatest occupancy that is at most `free`.

    `None` when nothing fits, which is a verdict and not an absence: a parent
    with zero free slots has an answer, and the answer is that no client can go
    in. An unknown free count has no answer at all, so `largest_fit(None)`
    raises. A caller that wants both cases has to tell them apart itself, and the
    two must not be conflated: returning `None` for an unknown count would report
    a container nobody has measured as one that is definitively full.

    ODU4 and ODUC1 both occupy 80. The tie goes to whichever the table lists
    first, which is ODU4, the 100G line container and the one a reader of a
    100G-or-400G network expects to see named.
    """
    if free is None:
        raise ValueError("largest_fit has no answer for an unknown free-slot count")
    best: str | None = None
    best_occupies = 0
    for name, sizes in SLOT_TABLE.items():
        occupies = sizes.occupies
        if occupies is None or occupies > free:
            continue
        if best is None or occupies > best_occupies:
            best, best_occupies = name, occupies
    return best


def section_headroom(carrier_free_slots: Iterable[int | None]) -> int | None:
    """The greatest free-slot figure across a section's lit carriers.

    The caller filters the dark carriers out first: a carrier with no container
    on it has no slot capacity to report, and counting it as zero free would make
    an empty section look full. An empty input therefore returns `None`, which
    the map draws as its own explicit unknown and never as roomy.

    One `None` among known figures makes the whole result `None` rather than
    being skipped, because a section holding an unsized container is not a
    section whose headroom is known.
    """
    return _extreme(carrier_free_slots, max)


def section_tightest(carrier_free_slots: Iterable[int | None]) -> int | None:
    """The least free-slot figure across the same set, on the same terms.

    The pair is what separates a section with one roomy carrier and one full one
    from a section where every carrier is half used. Both read the same at the
    headroom figure alone.
    """
    return _extreme(carrier_free_slots, min)


def _extreme(
    carrier_free_slots: Iterable[int | None],
    pick: Callable[[Iterable[int]], int],
) -> int | None:
    """`max` or `min` over the figures, with the two unknown cases handled once.

    Both public callers have the same two ways of having no answer, and writing
    them out twice is how one of them starts skipping the `None` instead of
    propagating it.
    """
    figures = list(carrier_free_slots)
    if not figures or any(figure is None for figure in figures):
        return None
    return pick([figure for figure in figures if figure is not None])
