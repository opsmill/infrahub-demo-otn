"""Two services in one declared diversity group must not share a duct.

An operator who puts two circuits in one `OtnDiversityGroup` has written down a
promise that one backhoe cannot take both. Nothing enforced it. This is what
fails the proposed change when the routes on the branch break that promise,
naming the duct, both circuits and the group.

**The silence is the feature, and widening it would undo the reason this check
exists.** `transforms/srlg_exposure.py` argues in its own docstring that
diversity should be reported and not enforced, because "an operator may have
accepted the exposure deliberately, and a check would block a merge on a decision
somebody already made". That objection is correct, and it is correct against a
check that flags every shared conduit. This check answers it rather than
overriding it: it only ever speaks about a declaration somebody wrote down, so an
accepted exposure stays accepted and nothing here says a word about it. A
reviewer who "improves" this check by also flagging undeclared pairs has
reintroduced exactly the failure the report was written to avoid, and blocks
merges on decisions that were already made deliberately. Do not widen it. The
question "who shares a duct with whom, declared or not" already has an answer,
and it is the report, which is the right instrument for it because a report does
not block anything.

**Rooted on the group, so the undeclared circuits are not even fetched.**
`queries/diversity.gql` selects `OtnDiversityGroup.services`, so a service with
no group is absent from the payload rather than present and skipped. That is
deliberate: the silence is a property of the query shape and not of an `if` that
a later change could invert.

**Which half the schema already took.** `OtnService.diversity_group` is a
relationship, not the Text attribute it replaced, so a mistyped group name is
refused at write time instead of silently meaning "no requirement", and the
declaration itself needs no validating here. `OtnDiversityGroup.name` is unique,
so two groups cannot share an identity. What is left for Python is the part no
schema can carry: an intersection between the routes of two objects that have no
relationship to each other.

**The walk is not here.** `impact.service_exposure` is the one span-to-conduit
walk and `transforms/srlg_exposure.py` calls the same function, which is FR-021.
Nothing in this file reads a hop, a span or a conduit, and nothing in it
intersects two sets: `impact.non_diverse_pairs` derives the pairs from the
conduit groups. A rule implemented twice is free to drift, and here the drift is
a check that passes what the report flags, which sends the reader looking for a
bug in whichever of the two they trust less.

**Two segments of one circuit sharing a duct is not a violation.** FR-020. A
regenerated circuit crosses its own conduits by construction, and a pairwise
comparison over segments would report that as a diversity failure on a circuit
that is behaving normally. The comparison here is between two **services**, over
the union of each one's segments, which is what `service_exposure` returns.
There is no code path in this file that compares one segment to another.

**Global, not targeted.** Two services in one group can be touched by different
changes, and the pair is the unit of judgement: a change that reroutes one of
them onto its partner's duct touches neither the group nor the other service. A
targeted check bound to the objects a change touched would look at the rerouted
circuit, find nothing wrong with it on its own, and report green.
`channel_collision.py` and `container_capacity.py` are global for the same shape
of reason.

**Absent is not diverse.** A member with no route yet is reported as
undetermined, not as compliant. It is an INFO rather than an error because
declaring the group before provisioning the circuits is the normal order of work
and must not block the branch that does it; there is no `log_warning` between the
two. What it must not do is count as a pass, which is why it is said out loud.
"""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.impact import ServiceExposure, non_diverse_pairs, service_exposure
from infrahub_demo_otn.plant import nodes_of, peers


class DiversityCheck(InfrahubCheck):
    query = "diversity"

    def validate(self, data: dict[str, Any]) -> None:
        groups = 0
        compared = 0
        violated = 0
        undetermined = 0

        for group in nodes_of(data, "OtnDiversityGroup"):
            groups += 1
            name = str(group["name"])
            members = list(peers(group, "services"))

            if len(members) < 2:
                # A group of one declares nothing that can be broken. Nothing is
                # said about its member, including whether it has a route: there
                # is no pair for the answer to be about.
                continue

            routed: list[ServiceExposure] = []
            for member in members:
                exposure = service_exposure(member)
                if exposure is None:
                    undetermined += 1
                    self._undetermined(member, name, len(members))
                    continue
                routed.append(exposure)

            compared += len(routed)
            for pair in non_diverse_pairs(routed):
                violated += 1
                self._violation(group, name, pair.service_a, pair.service_b, pair.shared)

        self._summarise(groups, compared, violated, undetermined)

    def _violation(
        self,
        group: dict[str, Any],
        name: str,
        first: str,
        second: str,
        shared: tuple[str, ...],
    ) -> None:
        """One error per pair, logged against the group whose promise it breaks.

        The pair is the unit of judgement and a pair is not an object, so there is
        a choice of what to name. It is the group: the group is the thing that was
        declared, both circuits are within their own rights individually, and a
        reader who opens the group finds the requirement written next to the
        failure. Both service names and every shared duct are in the message, so
        nothing is left to be looked up.
        """
        ducts = ", ".join(shared)
        self.log_error(
            message=(
                f"{first} and {second} are both in diversity group {name} and their routes share {ducts}. "
                f"One cut in {'any of those ducts' if len(shared) > 1 else 'that duct'} takes both, so the "
                "diversity this group declares does not hold"
            ),
            object_id=str(group["id"]),
            object_type=str(group["__typename"]),
        )

    def _undetermined(self, member: dict[str, Any], name: str, size: int) -> None:
        """A member with no route, said out loud rather than counted as a pass."""
        self.log_info(
            message=(
                f"{member['name']} is in diversity group {name} and has no route yet, so whether it is "
                f"diverse from the other {size - 1} member(s) of that group is undetermined. An absent route "
                "is not a diverse one"
            ),
            object_id=str(member["id"]),
            object_type=str(member["__typename"]),
        )

    def _summarise(self, groups: int, compared: int, violated: int, undetermined: int) -> None:
        """One INFO line saying what was judged, including when that is nothing.

        The counts are separate statements and a single "checked N groups" would
        let them hide inside each other: a branch where every declared circuit is
        unrouted is not a branch where every declaration holds.

        The no-groups line says where the unconditional answer lives, because a
        reader who sees this check pass on a branch full of shared ducts should
        not conclude the network is diverse. It is not that nothing shares a duct;
        it is that nobody asked for anything else.
        """
        if not groups:
            self.log_info(
                message=(
                    "No diversity group on this branch, so no declaration can be broken. Shared conduits are "
                    "reported whether they were declared or not by the srlg_exposure report, which is the "
                    "unconditional half of this question"
                )
            )
            return
        self.log_info(
            message=(
                f"Checked {groups} diversity group(s) over {compared} routed service(s). {violated} pair(s) "
                f"share a conduit against a declaration and {undetermined} member(s) have no route yet. "
                "Services declaring no group are not judged here"
            )
        )
