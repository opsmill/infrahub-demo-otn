"""Which services share a duct, and are therefore not diverse.

Two services on two different routes look diverse on a map. If their spans share
a conduit, one backhoe takes both. The conduit is modelled as a shared-risk
group so this question has an answer, and a pair of AI-profile services sharing
one is flagged.

**Diversity is reported, not provisioned.** Protection switching is out of
scope, and this is a report rather than a check because an operator may have
accepted the exposure deliberately, and a check would block a merge on a
decision somebody already made.

**Grouped by conduit first.** That is one pass over the services, it is the
question an operator asks first, and the pairs fall out of the groups without
ever scanning every pair of services.

**Read off the spans, not the sections.** A section can cross several conduits
and a conduit can hold spans from several sections, so intersecting section
lists misses pairs that share a duct across two corridors and invents pairs that
share a corridor and no duct.

**And read off every segment, unioned.** A regenerated circuit is one optical
path per wavelength, so the same reasoning goes one level up: reading the first
segment's spans answered with the ducts of the first half of the circuit and
none of the ducts of the second. That answer was narrow rather than wrong, which
is the dangerous kind. A two-segment circuit came back looking more diverse than
it is, and a pair exposed by a duct under its second half was simply not in the
report. Nothing else changes: the union is what `impact.service_exposures`
returns and every pair still falls out of the conduit groups.

**Every shared conduit, declared diversity group or not.** That is FR-018 and it
is unconditional here. `checks/diversity.py` is the half that fails a merge, and
it is silent about services that declared no group, because an operator may have
accepted an exposure deliberately and a check has no way to tell that from an
oversight. This report tells them anyway, which is what a report is for. Adding
a group filter here would delete the one place accepted exposure is still
visible.
"""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.impact import (
    ServiceExposure,
    conduit_groups,
    kilometres,
    non_diverse_pairs,
    service_exposures,
)
from infrahub_demo_otn.plant import nodes_of, peers, unwrap


def _conduit_catalog(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every conduit, with the sections buried in it.

    Selected even for conduits no service crosses, because "this duct is empty
    today" is the answer to the question that follows the report: where can a
    second route go.
    """
    catalog: dict[str, dict[str, Any]] = {}
    for record in nodes_of(data, "OtnConduit"):
        sections: set[str] = set()
        length = 0
        spans = 0
        for span in peers(record, "spans"):
            spans += 1
            length += int(span.get("length_m") or 0)
            oms = (span.get("oms") or {}).get("node")
            if isinstance(oms, dict):
                sections.add(str(unwrap(oms)["name"]))
        catalog[str(record["name"])] = {
            "conduit": str(record["name"]),
            "owner": record.get("owner"),
            "description": record.get("description"),
            "span_count": spans,
            "length_m": length,
            "length_display": kilometres(length),
            "sections": sorted(sections),
        }
    return catalog


def _exposure_row(exposure: ServiceExposure) -> dict[str, Any]:
    return {
        "service": exposure.service,
        "customer": exposure.customer,
        "service_profile": exposure.profile,
        "latency_sensitive": exposure.is_ai,
        "conduits": list(exposure.conduits),
        "conduit_count": len(exposure.conduits),
        "span_count": exposure.span_count,
        "unducted_span_count": exposure.unducted_span_count,
        # The segment count is on the row because the conduit set of a chained
        # circuit is a union over its wavelengths, and a reader who cannot see
        # that has no way to tell it from one wavelength's ducts. `segment_note`
        # is null for the unregenerated circuits, which is all of them until a
        # chain is provisioned.
        "segment_count": exposure.segment_count,
        "segment_note": (
            f"This circuit is regenerated and rides {exposure.segment_count} wavelengths. The conduits "
            "above are the union across its segments: a backhoe through any one of them takes the circuit, "
            "because a segment lost is the service lost in a model with no protection switching."
            if exposure.is_regenerated
            else None
        ),
        "note": (
            f"{exposure.unducted_span_count} of {exposure.span_count} spans on this route are outside any "
            "recorded conduit. Those spans are not shared risk in this model; they are unrecorded risk."
            if exposure.unducted_span_count
            else None
        ),
    }


class SrlgExposureTransform(InfrahubTransform):
    query = "srlg_exposure"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        exposures = service_exposures(data)
        groups = conduit_groups(exposures)
        pairs = non_diverse_pairs(exposures)
        catalog = _conduit_catalog(data)

        high = [pair for pair in pairs if pair.severity == "high"]
        exposed = sorted({name for pair in pairs for name in (pair.service_a, pair.service_b)})
        diverse = sorted({exposure.service for exposure in exposures} - set(exposed))

        return {
            "branch": self.branch_name,
            "headline": (
                f"{len(pairs)} non-diverse pair(s) among {len(exposures)} provisioned service(s) on "
                f"{self.branch_name}, {len(high)} of them between two latency-sensitive services."
                if pairs
                else f"No two of the {len(exposures)} provisioned service(s) share a conduit."
            ),
            "service_count": len(exposures),
            # How many of those circuits are regenerated, so a reader can tell
            # whether any conduit set on this report is a union over segments.
            # Zero is the answer on every branch with no chain provisioned, and
            # it is worth printing rather than omitting: the report having walked
            # the segments is not visible from a route that has one.
            "regenerated_service_count": len([item for item in exposures if item.is_regenerated]),
            "pair_count": len(pairs),
            "high_severity_count": len(high),
            "scope_note": (
                "Diversity is reported, not provisioned. Protection switching and restoration are out of "
                "scope; the exposure is what this model can tell you cheaply."
            ),
            "services": [_exposure_row(exposure) for exposure in exposures],
            "conduits": [
                {
                    **catalog.get(conduit, {"conduit": conduit}),
                    "services": list(services),
                    "service_count": len(services),
                }
                for conduit, services in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
            ],
            "empty_conduits": sorted(set(catalog) - set(groups)),
            "pairs": [
                {
                    "service_a": pair.service_a,
                    "service_b": pair.service_b,
                    "shared_conduits": list(pair.shared),
                    "severity": pair.severity,
                    "finding": (
                        f"{pair.service_a} and {pair.service_b} share "
                        f"{', '.join(pair.shared)}. One cut in "
                        f"{'any of those ducts' if len(pair.shared) > 1 else 'that duct'} takes both."
                    ),
                }
                for pair in pairs
            ],
            "not_exposed": diverse,
            "not_exposed_note": (
                "These services share no conduit with any other. That is the control on the report: it is "
                "not pairing everything."
                if diverse
                else None
            ),
        }
