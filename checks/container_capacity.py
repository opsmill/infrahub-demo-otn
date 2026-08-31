"""No container may hold children that occupy more slots than it offers.

The slot capacity rule was stated in the documentation and enforced nowhere.
Provisioning refuses to overfill a wavelength now, but provisioning is one of
four ways container data arrives: a data file, the API and the UI are the others,
and none of them runs the generator. This is what closes that, on the branch,
before the change can merge.

**The rule has exactly one implementation and it is not here.**
`containers.free_slots(capacity, child_occupancies)` is the subtraction, and this
check calls it. Nothing in this file sums a child occupancy or compares a total
to a capacity, deliberately, because the two-implementations failure is worse
than either one failing alone: a proposed change that passes the check and then
has its service refused by the generator, or the reverse, sends the reader
looking for a bug in whichever of the two they happen to trust less. The
committed total in the failure message is recovered as `capacity - free` rather
than re-summed, so even the number the message quotes comes out of the shared
function.

**Global, not targeted.** Overfilling is a property of a parent plus all of its
children, and a proposed change may add one child while the parent and its
siblings sit outside the change entirely. A targeted check bound to the objects a
change touched would look at the new child, find nothing wrong with it on its
own, and report green on a wavelength that now holds 160 slots of clients in 80
slots of space. `channel_collision.py` is global for the same reason.

**Unknown is reported as unknown.** Four of the sixteen container types have no
tributary slot size G.709 defines, so `containers.slot_capacity` answers `None`
for their capacity and `free_slots` propagates that `None` through the whole
parent. Such a parent is reported and neither passed nor failed. Reading the
missing figure as zero is the specific silent failure this distinction exists to
prevent: a parent whose capacity nobody knows would be reported as full, or, the
other way round, an unmeasurable child would be counted as taking nothing and an
unlimited number of them could be nested while the check stayed green.

There is no third log level. `InfrahubCheck` offers `log_error`, which blocks the
merge, and `log_info`, which does not, and no `log_warning` between them. Unknown
is therefore an INFO that names the container and says which figure is missing,
and the summary counts it apart from the containers that fit. That separation is
the whole of it: a summary reading "every container fits" over a branch holding
one unmeasurable parent would be the pass this check is not allowed to give.

**It fails closed on a container it cannot read at all.** An `odu_type` outside
the sixteen makes `containers.slot_capacity` raise, and that is an error against
the container rather than a skip. The schema declares the same sixteen values as
an enum, so this is drift between the table and the schema rather than bad user
input, and one such container must not take the other seventy down with it.

What this check does not do: it does not audit a stored `tributary_slots` against
the table's figure for the child's type. A sized child carrying a stored zero
would be counted as occupying nothing and could hide an overfill, and nothing
here catches that. It is left uncovered rather than half covered, because the
generator writes that figure from `containers.slots_for_client` on every path and
the case is reachable only by hand editing the same file this check reads.
"""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.containers import free_slots, slot_capacity, slots_occupied
from infrahub_demo_otn.plant import nodes_of, peers


class ContainerCapacityCheck(InfrahubCheck):
    query = "container_capacity"

    def validate(self, data: dict[str, Any]) -> None:
        containers = 0
        parents = 0
        overfilled = 0
        unknown = 0

        for record in nodes_of(data, "OtnContainer"):
            containers += 1
            children = list(peers(record, "child_containers"))
            if not children:
                # A container with no child cannot be overfilled. Its own
                # occupancy is measured when its parent comes round as a record
                # of its own, so nothing is skipped by leaving here.
                continue

            parents += 1
            name = str(record["name"])
            try:
                capacity = self._offered(record)
                occupancies = [self._occupancy(child) for child in children]
            except ValueError as error:
                self._against(record, f"{name} cannot be measured: {error}")
                continue

            free = free_slots(capacity, occupancies)
            if free is None:
                unknown += 1
                self.log_info(
                    message=(
                        f"{name} has no known free-slot figure, so whether it is overfilled is unknown: "
                        f"{self._why_unknown(record, children, capacity, occupancies)}"
                    )
                )
                continue

            if free < 0:
                overfilled += 1
                # `capacity - free` is the committed total, recovered from the
                # one implementation rather than summed a second time here.
                self._against(
                    record,
                    f"{name} is overfilled: its {len(children)} children commit "
                    f"{capacity - free} tributary slots and it offers {capacity}, so it is over by "
                    f"{-free}. The children are {', '.join(sorted(str(child['name']) for child in children))}",
                )

        self._summarise(containers, parents, overfilled, unknown)

    def _against(self, record: dict[str, Any], message: str) -> None:
        """One error, named against the container it is about.

        `object_id` and `object_type` are what lets the proposed change link the
        failure to the object it failed on, instead of leaving the reader to find
        a name in a message. The query selects `id` and `__typename` on its
        top-level nodes, so both values are free.
        """
        self.log_error(message=message, object_id=str(record["id"]), object_type=str(record["__typename"]))

    def _summarise(self, containers: int, parents: int, overfilled: int, unknown: int) -> None:
        """One INFO line saying what was looked at, including when that is nothing.

        The three counts are reported separately because they are three different
        statements and a single "checked N containers" would let two of them hide
        inside the third. A branch where every parent is unmeasurable is not a
        branch where every parent fits.
        """
        if not containers:
            self.log_info(message="No containers on this branch, so no parent has children to overfill it")
            return
        if not parents:
            self.log_info(
                message=(
                    f"Checked {containers} containers and none of them holds a child, so no committed total can "
                    "exceed a capacity. Every wavelength here is lit and empty"
                )
            )
            return
        fitting = parents - overfilled - unknown
        self.log_info(
            message=(
                f"Checked {containers} containers, {parents} of which hold children. {fitting} are within their "
                f"capacity, {unknown} have no known figure and {overfilled} are overfilled"
            )
        )

    @staticmethod
    def _why_unknown(
        record: dict[str, Any],
        children: list[dict[str, Any]],
        capacity: int | None,
        occupancies: list[int | None],
    ) -> str:
        """Which figure is missing, named rather than left to the reader.

        Two ways to arrive here and they send the reader to two different places.
        An unsized parent is a fact about its type, and the answer is that the
        container should not be a parent at all. An unreadable child is a fact
        about one child in a parent that is otherwise fine, so the child is named:
        the alternative message, "this container cannot be measured", would have
        the reader open a parent with nineteen perfectly readable children in it.
        """
        if capacity is None:
            return f"its own type {record['odu_type']} has no tributary slot capacity G.709 defines"
        named = sorted(
            str(child["name"]) for child, occupancy in zip(children, occupancies, strict=True) if occupancy is None
        )
        return f"the occupancy of {', '.join(named)} cannot be read"

    @staticmethod
    def _offered(record: dict[str, Any]) -> int | None:
        """What one container offers its children, or `None` when that is undefined.

        Two sources answering two different questions, and this is the same pair
        the generator's `_offered` reads. The table in `containers.py` says
        whether the figure exists at all, because four of the sixteen types have
        none. The stored `tributary_slot_capacity` supplies the value, because
        that is the figure an operator set and the ODU map draws, and a check
        comparing against a number nobody stored would fail a wavelength the
        generator packs into happily.

        Written here rather than shared with the generator because a check must
        not import a generator, and the agreement is asserted instead: the
        SC-006 test in `tests/unit/test_checks.py` runs this verdict and the
        generator's grooming decision over one container tree and fails if the
        two ever part company.
        """
        if slot_capacity(str(record["odu_type"])) is None:
            return None
        return int(record["tributary_slot_capacity"])

    @staticmethod
    def _occupancy(child: dict[str, Any]) -> int | None:
        """What one child takes in its parent, read from the child.

        The stored `tributary_slots` is the figure for a sized type and an unsized
        one alike, because that is the number the map draws and the generator
        packs against. The one guard is where the unknown still lives: a
        provisioned container of one of the four unsized types never legitimately
        occupies zero slots, since `containers.slots_for_client` floors its figure
        at one, so a stored zero on such a child means nobody wrote a real count.
        Counting it as zero would report a parent that might be overfull as having
        room, which is exactly the reading `containers.py` exists to make
        impossible, so it is unknown and `free_slots` propagates it.

        This is the generator's `_child_occupancy` rule, on the same terms and for
        the same reason.
        """
        stored = int(child["tributary_slots"])
        if slots_occupied(str(child["odu_type"])) is None and stored == 0:
            return None
        return stored
