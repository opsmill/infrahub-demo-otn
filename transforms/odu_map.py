"""Render the ODU capacity and grooming map for one site, as SVG.

A shim, deliberately, and the sibling of `network_map.py`. It reads the GraphQL
response, fills in `MapSite` and `OduSection`, and hands both to
`render_odu_map`. Every pixel decision and every band edge lives in
`odudraw.py`, and every slot figure in `containers.py`, so this file holds
neither a colour nor a slot table. A transform that worked out for itself how
many slots an ODU2e takes would be the second implementation of the capacity
rule, and the map would be free to disagree with the check about whether a
wavelength is full.

**The graph's figures, sized against the table.** A child's occupancy is the
`tributary_slots` the graph stores, and a line container's offering is its stored
`tributary_slot_capacity`, which is what the quickstart tells an operator to
check by hand. `containers.py` is asked one question about each of them: whether
the type has a defined size at all. Four of the sixteen types do not, and for
one of those the figure becomes unknown and propagates, rather than being read as
the schema default of zero. Zero committed and unknown committed are different
pictures and only one of them is safe to paint as roomy.

**A carrier is lit when it holds a container, and a dark one is skipped
entirely.** It contributes to no numerator and no denominator. Slot capacity
exists once a container is written on the wavelength, so counting a dark carrier
as zero free would make an unprovisioned section look full, and counting it as
fully free would make it look available.

**Nothing here sums a figure across sections.** A wavelength runs end to end over
several sections and every one of them counts it, so a network total of offered
slots reports more capacity than the network has. The totals below are per
carrier and then per section, and they stop there.

**The return is a `str`.** The artifact declares `image/svg+xml`, and a transform
returning a dict against a non-JSON content type writes a stringified Python dict
into the artifact body.

The four small accessors at the top are the same four `network_map.py` has, and
the duplication is deliberate. `transforms/` is not a package and is not going to
be: Infrahub loads each of these files by path, so one transform cannot import
another. The alternative is a home in `src/`, which puts four dict accessors in
the worker image and makes both maps wait on a rebuild to change a field name.
"""

from typing import Any, Iterable, Mapping

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.containers import (
    free_slots,
    section_headroom,
    section_tightest,
    slot_capacity,
    slots_occupied,
)
from infrahub_demo_otn.mapchrome import MapSite
from infrahub_demo_otn.odudraw import OduSection, render_odu_map
from infrahub_demo_otn.plant import nodes_of, peer, peers, unwrap


def _focus_shortname(data: Mapping[str, Any]) -> str:
    """The site this copy of the map belongs to.

    The `focus_site` alias resolves the `$site` variable the artifact definition
    binds. No match means the parameter named a site that is not on this branch,
    and a map with nobody highlighted is a different picture from the one that
    was asked for, so this raises rather than falling back to `None`.
    """
    matched = list(nodes_of(data, "focus_site"))
    if len(matched) != 1:
        raise ValueError(
            f"{len(matched)} sites matched the `site` variable on this branch; a map is drawn for exactly one"
        )
    return str(matched[0]["shortname"])


def _endpoint_site(section: Mapping[str, Any], relationship: str) -> str:
    """The shortname of the site one of the section's ROADMs stands in.

    Read off the ROADM's `site` relationship. The ROADM's own name is an
    identifier and nothing here recovers a position, a role or a site from one.
    """
    roadm = peer(section, relationship)
    site = (roadm.get("site") or {}).get("node")
    if not isinstance(site, dict):
        raise ValueError(f"ROADM {roadm.get('name')} stands in no site, so the routes it ends have no end to draw")
    return str(unwrap(site)["shortname"])


def _section_endpoints(data: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Section name to the two site shortnames it runs between."""
    return {
        str(record["name"]): (_endpoint_site(record, "roadm_a"), _endpoint_site(record, "roadm_b"))
        for record in nodes_of(data, "OtnOpticalMultiplexSection")
    }


def _facility_name(site: Mapping[str, Any]) -> str | None:
    """The facility caption for a site, or `None` when it hosts none.

    Read off the `facility` edge. Both maps draw the same node discs, so both
    read the same edge, and each keeps its own copy of the read because each
    has its own query. `network_map.py` carries the longer note on why this
    stopped being a tag name.
    """
    node = (site.get("facility") or {}).get("node")
    if not isinstance(node, dict):
        return None
    return str(unwrap(node)["name"])


def _coordinate(site: Mapping[str, Any], attribute: str) -> int:
    """One stored coordinate, or a `ValueError` naming the site that lacks it.

    Both coordinates are optional in the schema, so the query can return null for
    either. The map draws the whole network onto every site, so one PoP without a
    position fails all fourteen artifacts rather than its own, and a bare
    `TypeError: int() argument must be...` does not say which PoP.
    """
    value = site.get(attribute)
    if value is None:
        raise ValueError(f"site {site.get('name')} has no {attribute}, so it cannot be placed on the map")
    return int(value)


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


def _total(figures: Iterable[int | None]) -> int | None:
    """Sum with unknown propagating, and zero for nothing at all.

    One `None` among known figures makes the whole total unknown rather than
    being skipped, on the same terms `containers.section_headroom` states for the
    extremes: a section holding an unsized container is not a section whose
    committed total is known.

    Nothing in `containers.py` covers this, and that is not an omission there.
    Its functions answer questions about one container or about the extremes over
    a set; a sum across the carriers on a section is a figure only the panel
    wants, and it is a sum rather than an arithmetic on the slot table.

    Empty sums to zero, which is why every caller checks for a lit carrier first.
    An empty line container has committed nothing, and a section with no lit
    carrier has committed nothing anybody can see.
    """
    values = list(figures)
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _offers(container: Mapping[str, Any]) -> int | None:
    """What a line container offers its children, or `None` for an unsized type.

    The figure is the graph's own `tributary_slot_capacity`. The table is asked
    only whether the type has a defined offering, because a VC-4 has none and its
    stored zero would then read as a wavelength with no room rather than one
    nobody can size.
    """
    odu_type = str(container["odu_type"])
    if slot_capacity(odu_type) is None:
        return None
    return int(container["tributary_slot_capacity"])


def _occupies(container: Mapping[str, Any]) -> int | None:
    """What a child takes in its parent, or `None` for an unsized type.

    Same rule as `_offers`, at the other end of the relationship, and the same
    reason: the stored zero of an unsized container is invisible to every capacity
    figure downstream, and the wavelength would read empty however many clients
    are in it.
    """
    odu_type = str(container["odu_type"])
    if slots_occupied(odu_type) is None:
        return None
    return int(container["tributary_slots"])


def _carrier_figures(carrier: Mapping[str, Any]) -> tuple[int, int | None, int | None, int | None]:
    """One carrier's line container count, offered slots, committed and free.

    The count is what decides whether the carrier is lit; the other three are
    `None` together when the tree holds a type the slot table cannot size.

    All three come off one walk of the same tree. Free slots are
    `containers.free_slots` per line container and nothing else: a subtraction
    written here would be the second implementation of the rule that a parent
    holding an unsized child has no free figure at all.
    """
    line_containers = list(peers(carrier, "containers"))
    offered: list[int | None] = []
    committed: list[int | None] = []
    frees: list[int | None] = []
    for container in line_containers:
        capacity = _offers(container)
        occupancies = [_occupies(child) for child in peers(container, "child_containers")]
        offered.append(capacity)
        committed.append(_total(occupancies))
        frees.append(free_slots(capacity, occupancies))
    return len(line_containers), _total(offered), _total(committed), _total(frees)


def _carriers_by_section(data: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Section name to the carriers crossing it, ordered by carrier name.

    A carrier naming a section the query did not return still lands under that
    name and raises nothing: no section reads the entry, and the map draws what it
    was given. Sorted because the response order is not guaranteed and the
    section figures have to be the same on every render of an unchanged branch.
    """
    crossing: dict[str, list[dict[str, Any]]] = {}
    for carrier in sorted(nodes_of(data, "OtnOpticalCarrier"), key=lambda record: str(record["name"])):
        for section in peers(carrier, "sections"):
            crossing.setdefault(str(section["name"]), []).append(carrier)
    return crossing


class OduMapTransform(InfrahubTransform):
    query = "odu_map"

    async def transform(self, data: dict[str, Any]) -> str:
        focus = _focus_shortname(data)
        endpoints = _section_endpoints(data)
        crossing = _carriers_by_section(data)

        degree: dict[str, int] = {}
        sections: list[OduSection] = []
        for name in sorted(endpoints):
            site_a, site_b = endpoints[name]
            degree[site_a] = degree.get(site_a, 0) + 1
            degree[site_b] = degree.get(site_b, 0) + 1

            lit = 0
            offered: list[int | None] = []
            committed: list[int | None] = []
            frees: list[int | None] = []
            for carrier in crossing.get(name, []):
                count, carrier_offered, carrier_committed, carrier_free = _carrier_figures(carrier)
                if count == 0:
                    continue
                lit += 1
                offered.append(carrier_offered)
                committed.append(carrier_committed)
                frees.append(carrier_free)

            sections.append(
                OduSection(
                    name=name,
                    site_a=site_a,
                    site_b=site_b,
                    carriers_lit=lit,
                    # `None` rather than zero on a section with no lit carrier.
                    # `_total` sums an empty list to zero, and a route reporting
                    # 0 of 0 committed is a route somebody reads as full.
                    committed_slots=_total(committed) if lit else None,
                    offered_slots=_total(offered) if lit else None,
                    headroom_slots=section_headroom(frees),
                    tightest_free_slots=section_tightest(frees),
                )
            )

        # Every site the query returned is drawn, including one that no section
        # touches. It lands as an isolated node of degree zero, which is the
        # honest picture of a PoP whose plant is not modelled yet.
        sites = [
            MapSite(
                name=str(record["name"]),
                shortname=str(record["shortname"]),
                longitude_microdeg=_coordinate(record, "longitude_microdeg"),
                latitude_microdeg=_coordinate(record, "latitude_microdeg"),
                optical_degree=degree.get(str(record["shortname"]), 0),
                eurohpc_name=_facility_name(record),
            )
            for record in nodes_of(data, "OtnSite")
        ]
        # The branch goes on the map because a free-slot count is a property of
        # the branch it was read from and of nothing else. A capacity claim with
        # no branch on it is a capacity claim with no date on it.
        return render_odu_map(sites, sections, focus, self.branch_name)
