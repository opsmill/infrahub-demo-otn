"""A service the model refused does not merge, unless somebody signed for it.

The generator proves a service unprovisionable and records the verdict on the
node: `status: rejected`, a reason code and a detail. Until this check existed,
nothing in the pipeline read either field, so a service that cannot be built
merged into the intended state and the refusal survived as something an operator
had to open an artifact to find. This is what turns that verdict into a red
proposed change naming the service, the code and the detail.

The invariant it buys is one sentence: **the default branch holds only services
that can be provisioned, or refusals somebody signed for.**

**Which half the schema already took.** `rejection_code` is a `Dropdown` of
exactly six choices, so a seventh reason is refused at write time and no code
here validates one. `rejection_detail` is `Text` with `max_length: 512`, so the
prose cannot outgrow the field and no code here truncates it.
`refusal_accepted` is `Boolean`, `optional: false`, `default_value: false`, so
every service ever written is born not accepting refusals and there is no
missing value to interpret. `status` is a `Dropdown` of five choices, so
`rejected` is a value the server knows rather than a string convention. All four
are enforced on every write path: an object file, the API, the UI, the
generator, a hand edit during a demo. What is left for Python is the remainder,
below, and nothing that overlaps this list.

**Which half the check carries, and why the schema cannot.** Three rules: a
refused and unaccepted service must not merge, a refusal must be readable, and
acceptance without a refusal is a mistake. None of the three is a property of a
single write, which is the only thing a schema constraint sees.

The first is structural rather than incidental. The tempting shape is a schema
constraint saying `status` may not be `rejected`, and it cannot work: a schema
constraint runs on **every** write, including the generator's own write midway
through this same pipeline, and the generator's job is to record refusals. Such
a constraint would refuse the generator. The rule is not "this value may never be
written", it is "this value may not be **merged** unaccepted", and merge is not
a write path the schema sees at all. The proposed change is the only place the
question is even well posed.

The second and third are about **absence**, which is the other thing a schema
cannot notice: a constraint governs what is written and is blind to a gap. A
`rejected` status beside an empty code, and a set acceptance flag beside no
refusal, are both pairs of fields disagreeing, and each field on its own is
valid.

**Freshness is what makes gating on a derived verdict safe.** An earlier reading
of this problem objected that capacity verdicts go stale, so a three-week-old
refusal would block an unrelated change. That objection is correct about a
refusal at rest and does not apply here. The generator is registered under
`generator_definitions` with `targets: optical_services`, so it runs inside the
proposed change pipeline **ahead of the checks**, and the verdict read below was
written seconds earlier against this branch's own data. This check never reads a
refusal that has been sitting on the default branch between pipeline runs.

**The silence about an accepted refusal is the feature, and widening it would
undo the reason the flag exists.** `checks/diversity.py` argues the same shape at
length and this copies it. Some refusals are the answer rather than a failure:
Madrid to Warsaw at 400G refused for optical budget is a demo scenario whose
entire point is the recorded refusal, and the network cannot be improved into
provisioning it. An operator who reads the code and the detail and decides the
record is worth keeping sets `refusal_accepted`, and this check goes quiet about
that service. A reviewer who "improves" this check by also flagging accepted
refusals has deleted the capability the flag exists to preserve and blocks
merges on decisions somebody already made. Do not widen it. The unconditional
question, "which services are refused, signed for or not", already has an
answer: it is the service trace report, and a report is the right instrument for
it because a report blocks nothing.

**Fail closed on an unreadable refusal.** A service marked `rejected` with no
code is an error and never a skip. The generator sets both fields together or
neither, so reaching that state means a hand edit or a write path nobody
modelled, which is exactly what a check is for. Skipping it would report green
for a refusal nobody can read, which is worse than reporting the refusal.

**Fail closed on an unreadable payload, which is the same rule one level up.**
Two reads here are strict rather than forgiving, and both were fail-open once.

`status` is read as `service["status"]` and a null one raises, because a status
that did not come back is not a status of `""`: read loosely it routes a refused
service into the not-rejected branch and the gate reports green over the one
record it exists to stop. The service `name`, `id` and `__typename` are read the
same strict way beside it, and the reason code already failed closed, so this is
the field the gate turns on catching up with the fields around it.

The `OtnService` collection is checked for **presence** before anything is
counted. `plant.nodes_of` reads `payload.get(kind) or {}`, so an absent or null
collection and an empty one both arrive as no services, and the summary below
would then say the check looked and found none, which asserts something it never
verified. A renamed query, a partial GraphQL error and a permission filter all
land in exactly that state. So the two are separated here rather than in
`nodes_of`, which four other checks share and which is right to be forgiving
about a relationship nobody selected.

**`status` is the field the gate turns on.** A service is refused when its status
is `rejected`, and the code and the detail only decide whether that refusal is
readable. A leftover code or detail on a service that is no longer rejected is
therefore not a blocked merge; it is said out loud as an INFO and nothing more,
the way `diversity.py` reports a member with no route. It must not count as a
silent pass, and it must not stop a branch on which the network improved. Both
fields are watched, not only the code: the generator writes the pair together or
writes neither, so a detail left on its own is the same broken pair seen from
the other side.

**Global, not targeted.** A proposed change may touch one service while a
different service on the branch is the refused one: adding a span, retiring a
mode or filling a corridor can make a service that nobody edited unprovisionable
on the generator's next run. A targeted check bound to the objects the change
touched would look at the edited objects, find nothing wrong with them, and
report green over the service that can no longer be built.
`channel_collision.py`, `container_capacity.py` and `diversity.py` are global for
the same shape of reason.

**Two log levels, because there are only two.** `log_error` blocks the merge and
`log_info` annotates it. There is no `log_warning` in the SDK, and calling one
would raise `AttributeError` and fail the check with a traceback instead of the
verdict it was written to report. `_say` is the one place that picks between the
two, so the four reporters below differ in their sentence and in nothing else.
"""

from collections import Counter
from collections.abc import Mapping
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.plant import nodes_of, peers

SERVICE_KIND = "OtnService"
"""The one collection `queries/provisionable.gql` returns, named so the presence
check below and the walk below it cannot drift apart."""

REJECTED = "rejected"
"""The `OtnService.status` choice the generator writes when it refuses.

Spelled here because the shared package holds no constant for it: the generator
writes the literal in `_refuse`. The schema is the source of truth for the five
choices, and `tests/unit/test_checks.py` asserts this string is one of them, so a
status renamed in the schema fails there rather than turning this check silently
green.
"""


def _fetched(payload: Mapping[str, Any]) -> bool:
    """Whether the payload holds an `OtnService` collection at all.

    `True` for a collection with services in it and for one with none, which are
    the two states the check is allowed to judge. `False` for a key that is
    absent, for one whose value is null, and for a collection carrying no
    `edges` list, which are the three shapes a query that did not run arrives in.

    Kept here and not pushed into `plant.nodes_of`. That helper is shared by four
    other checks and both map transforms, and it is forgiving on purpose: most of
    its callers are reading a relationship the query may legitimately not have
    selected. Widening its contract to serve this one caller would make every
    other one raise on a payload it is happy with today.
    """
    collection = payload.get(SERVICE_KIND)
    return isinstance(collection, dict) and isinstance(collection.get("edges"), list)


def _status_of(service: Mapping[str, Any], name: str) -> str:
    """The gate field, read so an unreadable one cannot pass for a readable one.

    A `KeyError` on an absent key and a `ValueError` on a null value, and both
    are the point. `service.get("status") or ""` was the earlier reading, and it
    turned a status that never arrived into a value that is not `rejected`, which
    is the branch where the check says nothing and the merge goes through. The
    field the gate turns on is the last field that should be read generously.
    """
    value = service["status"]
    if value is None:
        raise ValueError(
            f"{name} came back with a null status, and status is the field this gate turns on. "
            "Reading it as an empty string would route a refused service into the not-refused branch "
            "and report the merge green"
        )
    return str(value)


class ProvisionableCheck(InfrahubCheck):
    query = "provisionable"

    def validate(self, data: dict[str, Any]) -> None:
        if not _fetched(data):
            self._unfetched()
            return

        counts: Counter[str] = Counter()

        for service in nodes_of(data, SERVICE_KIND):
            counts["judged"] += 1
            name = str(service["name"])
            status = _status_of(service, name)
            code = str(service.get("rejection_code") or "")
            detail = str(service.get("rejection_detail") or "")
            accepted = bool(service.get("refusal_accepted"))

            if status == REJECTED:
                if not code:
                    counts["unreadable"] += 1
                    self._unreadable(service, detail, accepted)
                elif not accepted:
                    counts["refused"] += 1
                    self._refused(service, code, detail)
                else:
                    counts["signed"] += 1
                continue

            # Read below the refused branch and not above it. A refusal carries
            # no path, so counting one for a service that has just been reported
            # refused is work whose answer is always zero.
            segments = sum(1 for _ in peers(service, "optical_path"))

            if accepted:
                counts["misplaced"] += 1
                self._misplaced(service, status, segments)
            elif code or detail:
                self._stale(service, status, code, detail)

            if segments:
                counts["provisioned"] += 1

        self._summarise(counts)

    def _say(self, service: Mapping[str, Any], message: str, *, blocking: bool) -> None:
        """One place that turns a service and a sentence into a log line.

        The object identity is read here so the four reporters below differ only
        in the sentence they build, and so the `id` and `__typename` reads that
        attach a message to a node in the proposed change cannot drift apart
        between them. `blocking` picks the level and there are only two of those:
        `log_error` stops the merge, `log_info` annotates it.
        """
        log = self.log_error if blocking else self.log_info
        log(
            message=message,
            object_id=str(service["id"]),
            object_type=str(service["__typename"]),
        )

    def _refused(self, service: dict[str, Any], code: str, detail: str) -> None:
        """The gate firing, with all three parts of the answer in one message.

        The service, the code and the detail, because a proposed change shows the
        reader this message and nothing else. A failure naming only the service
        would send them querying for the reason, and one naming only the code
        would not say which circuit it was about. The escape hatch is named too:
        somebody reading a blocked merge needs to know that keeping the refusal
        is an option and where to say so.
        """
        name = str(service["name"])
        self._say(
            service,
            f"{name} cannot be provisioned and was refused for {code}: "
            f"{detail or 'no detail recorded'}. It has not been accepted, so this branch does not merge. "
            "Fix the network or the request, or set refusal_accepted on the service to keep the refusal "
            "on the record",
            blocking=True,
        )

    def _unreadable(self, service: dict[str, Any], detail: str, accepted: bool) -> None:
        """Fail closed on a refusal with no code, accepted or not.

        The acceptance flag is deliberately not consulted for this one. Accepting
        a refusal means having read it, and a refusal with no code cannot have
        been read, so honouring the flag here would let the one state the
        generator cannot produce merge on the strength of a signature nobody
        could have given. It is reported as unreadable rather than as refused,
        because the two need different fixes.
        """
        name = str(service["name"])
        self._say(
            service,
            f"{name} is marked {REJECTED} and carries no reason code, so why it was refused cannot be "
            f"read{f' beyond the detail {detail!r}' if detail else ''}. The generator writes the code and "
            "the detail together, so this came from a hand edit or another write path. It is an error and "
            f"not a skip{', and the acceptance flag on it does not clear it' if accepted else ''}",
            blocking=True,
        )

    def _misplaced(self, service: dict[str, Any], status: str, segments: int) -> None:
        """An acceptance flag on a service that was never refused.

        Blocking rather than annotating, because the flag does nothing where it
        sits and the operator who set it believes a refusal is signed for. The
        refusal they meant to sign for is on some other node, still unaccepted,
        and still blocking. The likelier cause is a service that has since been
        provisioned, which is why the message names the status and the path.
        """
        name = str(service["name"])
        carried = f"{segments} path segment(s)" if segments else "no optical path"
        self._say(
            service,
            f"{name} has refusal_accepted set and is not {REJECTED}: its status is {status} and "
            f"it carries {carried}. There is no refusal on it to accept, so the flag is on the wrong node "
            "or it outlived the refusal it was set for. Clear it, and check whether the refusal it was "
            "meant for is still blocking somewhere else",
            blocking=True,
        )

    def _stale(self, service: dict[str, Any], status: str, code: str, detail: str) -> None:
        """Either half of a refusal left behind on a service that is no longer refused.

        An INFO and not an error. The gate turns on `status`, this service is not
        refused, and blocking here would stop a branch on which the network
        improved. What it must not do is pass unremarked: a code nobody cleared
        makes every list of refusals wrong, and the reader should hear that it was
        seen and deliberately not acted on.

        Both fields are watched because FR-006 pairs them: the generator writes
        the code and the detail together or writes neither, so a detail on its own
        is the same broken pair as a code on its own and needs the same naming.
        Watching only the code left half the invariant with nothing looking at it.
        """
        if code and detail:
            leftover = f"the reason code {code} and its detail"
            consequence = "every filter by reason code counts this service"
        elif code:
            leftover = f"the reason code {code} and no detail beside it"
            consequence = "every filter by reason code counts this service, and half the pair is already gone"
        else:
            leftover = "a rejection detail and no reason code"
            consequence = "no filter by reason code finds it, so the prose sits where nobody reads it"
        name = str(service["name"])
        self._say(
            service,
            f"{name} is {status} rather than {REJECTED} and still carries {leftover}. Nothing "
            f"is blocked by it, because the gate reads the status, but the leftover is stale and "
            f"{consequence}",
            blocking=False,
        )

    def _unfetched(self) -> None:
        """The services were not read, which is not the same as there being none.

        An error, and the only one here that names no object, because the thing
        that went wrong is the payload rather than a record in it. The summary's
        no-services line would otherwise be reached and would state that the check
        looked and found none, which is a claim about a branch this run never saw.
        A green gate over services nobody read is the exact failure the whole file
        argues against, one level further out.
        """
        self.log_error(
            message=(
                f"The payload carries no {SERVICE_KIND} collection, so no service on this branch was judged. "
                "That is a query that did not run, not a branch with nothing on it: a renamed query, a "
                "partial GraphQL error and a permission filter all arrive this way and are indistinguishable "
                "from an empty result once the collection is gone. Nothing here is provable, so this is an "
                "error rather than a pass"
            )
        )

    def _summarise(self, counts: Counter[str]) -> None:
        """One INFO line saying what was judged, including when that is nothing.

        Taken as one mapping rather than six positional `int`s. Six same-typed
        parameters read in the right order by eye and in any order at all by the
        type checker, so a transposed pair would report one state's count under
        another's name and nothing would catch it. Keyed counts cannot transpose.

        The counts stay separate for the reason `diversity.py` keeps its separate:
        a single "judged N services" would let them hide inside each other, and a
        branch whose every refusal is signed for is not a branch with no refusals
        on it.

        The no-services line exists because a green check on an empty branch and a
        green check on a provisioned network look identical from the proposed
        change, and only one of them is evidence of anything. It is reached only
        after `_fetched` has said the collection was there, so it means an empty
        branch and never a query that failed to return one.
        """
        if not counts["judged"]:
            self.log_info(
                message=(
                    "No service on this branch, so nothing was refused and nothing was accepted. This is a "
                    "check that looked and found none, not a network that was proved provisionable"
                )
            )
            return
        self.log_info(
            message=(
                f"Judged {counts['judged']} service(s). {counts['refused']} refused and unaccepted, "
                f"{counts['unreadable']} refused with no readable code, {counts['misplaced']} accepting a "
                f"refusal that does not exist, {counts['signed']} refused and signed for, "
                f"{counts['provisioned']} carrying an optical path. Only the first three block this branch"
            )
        )
