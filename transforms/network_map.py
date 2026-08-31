"""Render the network topology map for one site, as SVG.

A shim, deliberately. It reads the GraphQL response, fills in `MapSite` and
`MapSection`, and hands both to `render_map`. Every pixel decision lives in
`mapdraw.py` and every optical decision in `budget.py`, so this file holds
neither a colour nor a formula.

**Two directional margins per section, each from `evaluate_path`.** A section
carries an amplifier chain per direction and the two chains do not have to
agree, so the section is walked once from `roadm_a` and once from `roadm_b`,
with `start_node` passed explicitly both times. Pinning the start is the point:
left to itself `order_sections` picks the lexicographically smaller endpoint,
and which ROADM that is has nothing to do with which end the map calls A.

**A one-section path is a whole path, and that is the only reason this is
allowed.** `budget.py` warns that the OSNR cascade runs over a path and that
summing per-section figures would be wrong and would look right. Nothing here
sums or averages a margin across sections. Each section is evaluated as a
complete path of its own length, which is exactly what the map is a statement
about.

**A section that will not evaluate keeps its route and loses its colour.** The
`ValueError` is caught narrowly, the margin becomes `None`, and `mapdraw` paints
the explicit unknown band. A route that quietly disappears, or one defaulted
into a passing colour, is the worse failure and it is silent.

**The return is a `str`.** The artifact declares `image/svg+xml`, and a
transform returning a dict against a non-JSON content type writes a stringified
Python dict into the artifact body.
"""

from typing import Any, Mapping

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.budget import ModeInput, SectionInput, evaluate_path, span_loss_mdb
from infrahub_demo_otn.mapchrome import MapSite
from infrahub_demo_otn.mapdraw import REFERENCE_MODE_NAME, MapSection, render_map
from infrahub_demo_otn.plant import (
    modes_from_graphql,
    nodes_of,
    occupancy_from_graphql,
    peer,
    sections_from_graphql,
    unwrap,
)
from infrahub_demo_otn.units import CBAND_EXTENT_MHZ, free_blocks


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


def _reference_mode(data: Mapping[str, Any]) -> ModeInput:
    """The mode every route colour is a verdict about.

    An OSNR margin means nothing without the mode it is measured against, so a
    catalog missing this one is an error and not a map full of grey routes.
    """
    for candidate in modes_from_graphql(data):
        if candidate.name == REFERENCE_MODE_NAME:
            return candidate.budget_input
    raise ValueError(
        f"the optical mode catalog on this branch has no {REFERENCE_MODE_NAME!r}, "
        "which is the mode every route colour on the map is a margin against"
    )


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
    """Section name to the two site shortnames it runs between.

    `sections_from_graphql` keeps the ROADMs and drops the sites they stand in,
    because the budget engine has no use for a site. The map has nothing else,
    so the same records are walked once more for that one field.
    """
    return {
        str(record["name"]): (_endpoint_site(record, "roadm_a"), _endpoint_site(record, "roadm_b"))
        for record in nodes_of(data, "OtnOpticalMultiplexSection")
    }


def _occupancy(data: Mapping[str, Any]) -> dict[str, int]:
    """Section name to the spectrum the carriers crossing it hold, in MHz.

    Not a count of carriers, which is what this returned until the map started
    drawing widths. Two carriers are not twice one carrier: a 32 GBd wavelength
    holds 44,400 MHz and a 128 GBd one holds 150,000, and a bar drawn from the
    count paints those the same.

    The band minus the free blocks, so overlapping spectrum is counted once. A
    branch that has not passed `channel_collision` can hold two carriers on the
    same megahertz, and adding their widths would draw a section as fuller than
    the band it sits in.

    A carrier naming a section the query did not return still counts against that
    name and raises nothing. A carrier with no anchor, no mode or no section does
    raise, because `plant.occupancy_from_graphql` is the one derivation of what
    is taken and a map that quietly drew a lit section as dark would be read as
    free capacity.
    """
    return {
        section: CBAND_EXTENT_MHZ - sum(block.width_mhz for block in free_blocks(intervals))
        for section, intervals in occupancy_from_graphql(data).items()
    }


def _span_boundaries(section: SectionInput) -> tuple[float, ...]:
    """Interior span boundaries as fractions of route length.

    Fractions rather than span counts, so a route made of unequal spans puts
    its dots where the amplifier huts are.
    """
    total_m = sum(span.length_m for span in section.spans)
    if total_m <= 0:
        return ()
    running = 0
    boundaries: list[float] = []
    for span in section.spans[:-1]:
        running += span.length_m
        boundaries.append(running / total_m)
    return tuple(boundaries)


def _margin_mdb(section: SectionInput, mode: ModeInput, start_node: str) -> int | None:
    """One direction's OSNR margin, or `None` when the walk will not run.

    The catch is on `ValueError` alone, which is what `order_sections`,
    `flatten_path` and `SectionInput.validate` raise for a chain that is short
    an amplifier or a section that starts and ends at the same ROADM. Anything
    else is a defect in this repository and has to reach the log.
    """
    try:
        return evaluate_path([section], mode, start_node).osnr_margin_mdb
    except ValueError:
        return None


def _facility_name(site: Mapping[str, Any]) -> str | None:
    """The facility caption for a site, or `None` when it hosts none.

    Read off the `facility` edge, not off a name. `plant.peer` is not used
    here: it raises when the peer is absent, and eight of the fourteen PoPs
    host no facility, which is an answer and not an error.

    Still reads no device. Nothing about an amplifier, a ROADM or a router is
    recovered from a name anywhere in this file. What changed is the other
    half: the facility used to be the text after `eurohpc-` in a tag name, and
    that failed silently in both directions. `schemas/location.yml` records
    what was measured.
    """
    node = (site.get("facility") or {}).get("node")
    if not isinstance(node, dict):
        return None
    return str(unwrap(node)["name"])


def _coordinate(site: Mapping[str, Any], attribute: str) -> int:
    """One stored coordinate, or a `ValueError` naming the site that lacks it.

    Both coordinates are optional in the schema, so the query can return null
    for either. The map draws the whole network onto every site, so one PoP
    without a position fails all fourteen artifacts rather than its own, and a
    bare `TypeError: int() argument must be...` does not say which PoP.
    """
    value = site.get(attribute)
    if value is None:
        raise ValueError(f"site {site.get('name')} has no {attribute}, so it cannot be placed on the map")
    return int(value)


class NetworkMapTransform(InfrahubTransform):
    query = "network_map"

    async def transform(self, data: dict[str, Any]) -> str:
        focus = _focus_shortname(data)
        mode = _reference_mode(data)
        section_inputs = sections_from_graphql(data)
        endpoints = _section_endpoints(data)
        occupancy = _occupancy(data)

        degree: dict[str, int] = {}
        sections: list[MapSection] = []
        for name in sorted(section_inputs):
            built = section_inputs[name]
            site_a, site_b = endpoints[name]
            degree[site_a] = degree.get(site_a, 0) + 1
            degree[site_b] = degree.get(site_b, 0) + 1
            sections.append(
                MapSection(
                    name=name,
                    site_a=site_a,
                    site_b=site_b,
                    length_m=sum(span.length_m for span in built.spans),
                    loss_mdb=sum(span_loss_mdb(span) for span in built.spans),
                    margin_a_to_b_mdb=_margin_mdb(built, mode, built.head_node.name),
                    margin_b_to_a_mdb=_margin_mdb(built, mode, built.tail_node.name),
                    span_boundaries=_span_boundaries(built),
                    raman_pumped=any(span.raman_gain_mdb or span.raman_gain_reverse_mdb for span in built.spans),
                    occupied_mhz=occupancy.get(name, 0),
                    band_extent_mhz=CBAND_EXTENT_MHZ,
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
        # The branch goes on the map for the same reason the other eight reports
        # carry it in a `branch` key: a margin and an occupied width are true of
        # the branch they were read from and of nothing else.
        return render_map(sites, sections, focus, self.branch_name)
