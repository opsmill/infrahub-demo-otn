"""No two wavelengths may occupy the same spectrum on the same section.

This check **is** the reservation. Free capacity is derived, not stored, so
nothing tracks occupancy as state and nothing needs releasing when a service is
torn down. What stops two services lighting the same megahertz through Frankfurt
is that a proposed change carrying both cannot merge.

Because checks run against branched data, that reservation is branch-aware with
no extra work. Two engineers provisioning on two branches each see the same free
spectrum, and they collide at the proposed change, which is the only place where
both intentions exist at once.

**A wavelength occupies a width, not a channel number.** The width comes from the
symbol rate of the mode the carrier runs, one relationship hop away, through
`units.occupied_width_mhz`. Two carriers on different channels collide whenever
their intervals overlap, which is why the rule that compared channel numbers for
equality passed a 128 GBd carrier on channel 40 next to a 100G on channel 41 and
called the pair clean.

**Half of the rule is in the schema and this check does not repeat it.**

The schema carries the anchor and its bounds, enforced on every write path:

- `OtnOpticalCarrier.channel` is `cardinality: one, optional: false` peering
  `OtnFrequencyGrid`, so a carrier always has an anchor and it is always on the
  fine grid.
- `OtnFrequencyGrid.channel_number` declares `min_value: 1, max_value: 96`, so an
  anchor off the grid is refused before any check runs.
- `OtnOpticalMode.baud_mbaud` is mandatory, so a mode always has a symbol rate.

The check covers the remainder, which is the part no schema constraint in 1.11.0
can express: the **width** each carrier occupies, the **overlap** between two of
them on a section they share, and the **band edge** a wide carrier on a low or
high anchor runs past. A range is not a value, and interval disjointness is not a
uniqueness constraint. Anything a later reader is tempted to add here about
whether a channel exists or is in range belongs in the schema instead, where it
runs on writes this check never sees.

Spectrum is scarce *within a section*, not globally. Two carriers whose intervals
overlap but which share no section are not in conflict, or the network would have
one wavelength plan in total rather than one per section.

Two intervals that meet at exactly one frequency are neighbours. The edges are
half-open, `[lower, upper)`, set in `units.carrier_interval_mhz` and acted on by
`plant.intervals_overlap`. A densely packed plan touches its own boundaries
everywhere, and reporting those as collisions would make the check useless
exactly where it matters most.

**It fails closed.** A carrier with no channel, no section, no mode, or a mode
with no symbol rate is an error and not a skip. A check that silently passes over
the carrier it could not read reports green for spectrum it never looked at, and
the next generator run allocates on top of it. `plant.occupancy_from_graphql`
raises on all four, and the first one stops the scan rather than being collected
alongside the overlaps: an unreadable carrier means the occupancy map is
incomplete, and an overlap verdict over an incomplete map is the green result this
paragraph exists to prevent.
"""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.plant import CarrierInterval, nodes_of, occupancy_from_graphql, overlap_range
from infrahub_demo_otn.units import CBAND_EXTENT_MHZ, CBAND_LOWER_EDGE_MHZ, CBAND_UPPER_EDGE_MHZ


def _describe(interval: CarrierInterval) -> str:
    """One carrier as anchor, mode and the megahertz it holds.

    The channel number stays in every message. It is what an operator typed, what
    the UI shows and what the object files hold, so a message written only in
    megahertz would be correct and unusable.
    """
    return (
        f"{interval.carrier} on channel {interval.channel} running {interval.mode} "
        f"({interval.lower_mhz:,} to {interval.upper_mhz:,} MHz, {interval.width_mhz:,} MHz wide)"
    )


class ChannelCollisionCheck(InfrahubCheck):
    query = "channel_collision"

    def validate(self, data: dict[str, Any]) -> None:
        identities = {
            str(record["name"]): (str(record["id"]), str(record["__typename"]))
            for record in nodes_of(data, "OtnOpticalCarrier")
        }
        if not identities:
            self.log_info(message="No optical carriers on this branch, so no spectrum can collide")
            return

        try:
            occupancy = occupancy_from_graphql(data)
        except ValueError as error:
            self.log_error(
                message=(
                    f"{error}. The occupancy map is incomplete, so no verdict is given on the carriers that "
                    "could be read either"
                )
            )
            return

        outside = self._report_band_edges(occupancy, identities)
        collisions = self._report_overlaps(occupancy, identities)
        if outside or collisions:
            return

        occupied = {
            section: sum(interval.width_mhz for interval in intervals) for section, intervals in occupancy.items()
        }
        busiest, held = max(occupied.items(), key=lambda item: (item[1], item[0]))
        self.log_info(
            message=(
                f"Checked {len(identities)} carriers over {len(occupancy)} sections and found no two sharing "
                f"spectrum. The busiest is {busiest}, holding {held:,} of {CBAND_EXTENT_MHZ:,} MHz"
            )
        )

    def _report_band_edges(
        self,
        occupancy: dict[str, tuple[CarrierInterval, ...]],
        identities: dict[str, tuple[str, str]],
    ) -> int:
        """Carriers whose interval runs off the end of the modelled C-band.

        A carrier is reported once however many sections it crosses, because the
        band edge is a property of the carrier and its mode alone. The occupancy
        map holds one interval per section, so the intervals are collapsed by
        carrier name first.

        This is the failure a wide mode on a low or high anchor produces. Channel 1
        centres 25 GHz above the lower edge and a 64 GBd carrier is 79.6 GHz wide,
        so it reaches below the band before it reaches its first neighbour.
        """
        unique = {interval.carrier: interval for intervals in occupancy.values() for interval in intervals}
        reported = 0
        for name, interval in sorted(unique.items()):
            edges = []
            if interval.lower_mhz < CBAND_LOWER_EDGE_MHZ:
                edges.append(f"below the lower edge at {CBAND_LOWER_EDGE_MHZ:,} MHz")
            if interval.upper_mhz > CBAND_UPPER_EDGE_MHZ:
                edges.append(f"above the upper edge at {CBAND_UPPER_EDGE_MHZ:,} MHz")
            if not edges:
                continue
            reported += 1
            object_id, object_type = identities[name]
            self.log_error(
                message=(
                    f"{_describe(interval)} reaches {' and '.join(edges)} of the modelled C-band, "
                    "so it is unprovisionable on any section at that anchor"
                ),
                object_id=object_id,
                object_type=object_type,
            )
        return reported

    def _report_overlaps(
        self,
        occupancy: dict[str, tuple[CarrierInterval, ...]],
        identities: dict[str, tuple[str, str]],
    ) -> int:
        """Every pair sharing spectrum on a section they both cross.

        One message per pair per shared section, because the section is what the
        operator has to free and a pair that overlaps on four sections is four
        pieces of work.

        The intervals arrive sorted by lower edge from `occupancy_from_graphql`, so
        the inner loop stops at the first interval that starts at or after the
        outer one ends. Nothing further along can reach back.
        """
        reported = 0
        for section, intervals in sorted(occupancy.items()):
            for index, left in enumerate(intervals):
                for right in intervals[index + 1 :]:
                    if right.lower_mhz >= left.upper_mhz:
                        break
                    shared = overlap_range(left, right)
                    if shared is None:
                        continue
                    lower, upper = shared
                    reported += 1
                    object_id, object_type = identities[left.carrier]
                    self.log_error(
                        message=(
                            f"{left.carrier} and {right.carrier} share {upper - lower:,} MHz of spectrum on "
                            f"{section}, from {lower:,} to {upper:,} MHz. {_describe(left)} against "
                            f"{_describe(right)}. A wavelength holds its width for the whole length of every "
                            "section it crosses, so only one of these can be provisioned"
                        ),
                        object_id=object_id,
                        object_type=object_type,
                    )
        return reported
