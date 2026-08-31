"""The generator's client-signal selection, offline and against the real catalog.

`_client_signal` had no test at all, which is how it came to hand an InfiniBand
framing to any Ethernet service between 104 and 212 Gbps. Nothing failed, because
both shipped demo services are 400 Gbps and sit outside the gap. A rule with no
test and no service in its range is a rule nobody finds out about.

The catalog here is the catalog that ships. A synthetic one would assert that
`min` returns the smallest element, which is arithmetic; this asserts that the
rows in `objects/04_client_signals.yml` select the way the documentation says
they do.

`generators/` is not on the import path and is not a package, deliberately:
Infrahub loads the file by path and this module reaches it the same way, which is
what `tests/unit/test_checks.py` already does for a check.
"""

import importlib.util
from functools import cache
from typing import Any

import pytest

from infrahub_demo_otn.routing import REASON_NO_SLOTS
from infrahub_demo_otn.units import GRID_CHANNEL_COUNT, KBPS_PER_GBPS, channel_to_frequency_mhz
from tests.unit.conftest import REPO_ROOT, objects_of_kind
from tests.unit.test_plant import amplifier_node, roadm_node, span_node

GENERATOR_PATH = REPO_ROOT / "generators" / "optical_service.py"

CATALOG_FIELDS = ("name", "layer", "auto_selectable", "bit_rate_kbps", "default_container_type", "default_mapping")
"""Exactly what `queries/optical_service.gql` selects on `OtnClientSignal`.

Listed rather than "every key in the object file", so a payload built here can
never carry a field the real query does not return. That is the difference
between testing the generator and testing the object file.
"""

MAX_SWEEP_GBPS = 1600
"""`OtnOpticalMode.line_rate_gbps` is bounded at 1600, so no service can ask for
more. The sweep covers the whole range a service could name."""


@cache
def _module() -> Any:
    """The generator module, loaded once. The sweep below calls this 1600 times."""
    spec = importlib.util.spec_from_file_location(GENERATOR_PATH.stem, GENERATOR_PATH)
    assert spec and spec.loader, f"{GENERATOR_PATH} could not be loaded"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _select(rate_gbps: int, stated: str | None = None, *, drop_flag: bool = False) -> dict[str, Any]:
    """Run `_client_signal` over the shipped catalog at one rate."""
    module = _module()
    rows = [_wrapped(entry, drop_flag=drop_flag) for entry in objects_of_kind("OtnClientSignal")]
    payload = {"OtnClientSignal": {"edges": [{"node": row} for row in rows]}}
    service: dict[str, Any] = {"name": "svc-under-test", "rate_gbps": rate_gbps}
    if stated is not None:
        service["client_signal"] = {"node": _wrapped(_row(stated))}
    return module.OpticalServiceGenerator._client_signal(payload, service, "svc-under-test")  # noqa: SLF001


def _row(name: str) -> dict[str, Any]:
    matches = [entry for entry in objects_of_kind("OtnClientSignal") if entry["name"] == name]
    assert len(matches) == 1, f"{name} is not in the shipped catalog"
    return matches[0]


def _wrapped(entry: dict[str, Any], *, drop_flag: bool = False) -> dict[str, Any]:
    """One catalog row in the shape the GraphQL response has it."""
    fields = [field for field in CATALOG_FIELDS if not (drop_flag and field == "auto_selectable")]
    return {field: {"value": entry[field]} for field in fields}


def test_a_200_gbps_service_takes_400gbase_fr4() -> None:
    """The mistake the layer allow-list exists to prevent.

    `IB-HDR-4X` at 212,500,000 kbps is smaller than `400GBASE-FR4` at
    412,500,000, so a rate rule blind to layer picks the InfiniBand row and
    provisions an ODUflex for an Ethernet service. Both rows can carry 200 Gbps;
    only one of them is the right answer.
    """
    assert _select(200)["name"] == "400GBASE-FR4"


def test_a_100_gbps_service_takes_100gbase_lr4() -> None:
    """100,000,000 kbps wanted, 103,100,000 offered. The smallest row above it."""
    signal = _select(100)
    assert signal["name"] == "100GBASE-LR4"
    assert signal["default_container_type"] == "ODU4"


def test_a_stated_signal_is_honoured_and_names_its_own_container() -> None:
    """The only path to an InfiniBand row, and the reason the relationship exists."""
    signal = _select(200, stated="IB-HDR-4X")
    assert signal["name"] == "IB-HDR-4X"
    assert signal["default_container_type"] == "ODUflex"
    assert signal["default_mapping"] == "GMP"


def test_a_stated_signal_slower_than_the_rate_is_refused_by_name() -> None:
    """Refused, not substituted.

    `IB-EDR-4X` carries 103,125,000 kbps and the service asks for 200 Gbps.
    Substituting a faster row would make the relationship advisory: the service
    would provision, name a signal nobody asked for, and report success.
    """
    with pytest.raises(ValueError, match="states client signal IB-EDR-4X"):
        _select(200, stated="IB-EDR-4X")


def test_a_candidate_with_no_auto_selectable_flag_raises_naming_the_query() -> None:
    """The message has to blame the query, not the catalog.

    With `auto_selectable` absent every candidate is unplaced and the refusal
    that follows reads "no client signal in the catalog carries 400 Gbps", which
    is true of nothing and sends the reader to `objects/04_client_signals.yml`,
    where nothing is wrong. Both shipped 400 Gbps services would hit it.
    """
    with pytest.raises(ValueError, match="carries no auto_selectable flag"):
        _select(400, drop_flag=True)
    with pytest.raises(ValueError, match=r"queries/optical_service\.gql"):
        _select(400, drop_flag=True)


def test_no_rate_in_the_whole_range_selects_an_infiniband_row() -> None:
    """The sweep, and the four spot checks above are instances of it.

    A specialised layer must be unreachable at every rate, not only at the rates
    the demo happens to use. This survives a future row landing in a gap the spot
    checks do not cover, which is exactly how the defect it replaces was opened.

    Asserted over `layer` rather than over `auto_selectable`, which the generator
    now filters on: reading back the field the filter used would only restate the
    filter. The layer is the thing a reader cares about.

    `IB-EDR-4X` at 103,125,000 kbps could never have been selected even with the
    flag set: `100GBASE-LR4` at 103,100,000 is a candidate wherever it is and is
    25,000 kbps smaller. `IB-HDR-4X` is the one that was reachable, over 104
    through 212 Gbps inclusive, 109 integer rates.
    """
    offenders = []
    for rate in range(1, MAX_SWEEP_GBPS + 1):
        try:
            signal = _select(rate)
        except ValueError:
            continue
        if signal["layer"] == "infiniband":
            offenders.append(f"{rate} Gbps selects {signal['name']}")
    assert not offenders, "; ".join(offenders[:5])


def test_the_catalog_runs_out_above_412_gbps() -> None:
    """412,500,000 kbps is the largest row, so 412 is the last rate that selects.

    Hand-computed: 412 Gbps wants 412,000,000 kbps and `400GBASE-FR4` offers
    412,500,000. 413 Gbps wants 413,000,000 and nothing offers it.
    """
    assert _select(412)["name"] == "400GBASE-FR4"
    assert _row("400GBASE-FR4")["bit_rate_kbps"] == 412 * KBPS_PER_GBPS + 500_000
    with pytest.raises(ValueError, match="no client signal in the catalog carries 413 Gbps"):
        _select(413)


# ---------------------------------------------------------------------------
# The plan: which route, and groom or light
# ---------------------------------------------------------------------------
#
# `_plan` is the decision the channel requirement moved into. It is a pure read
# over the GraphQL payload plus a rank-ordered candidate list, so it is asserted
# here rather than against a stack: no client is constructed and nothing is
# written. What it must get right is that a route with no free channel is still
# usable by grooming, which is the ordering gap `routing.choose_route` used to
# close before grooming was tried.


def _budget() -> Any:
    """A budget that closes, with the fields `_plan` and the ranking read.

    Every other field is filled with a figure that cannot be mistaken for a
    computed one. `_plan` reads the mode and the route off the selection and the
    margin off the budget, and nothing here re-derives the physics: `budget.py`
    has its own suite for that.
    """
    from infrahub_demo_otn.budget import PathBudget

    return PathBudget(
        hops=(),
        total_length_m=240_000,
        total_loss_mdb=45_000,
        osnr_total_mdb=27_784,
        required_osnr_mdb=24_500,
        system_margin_mdb=1000,
        osnr_margin_mdb=2284,
        cd_total_fs_per_nm=4_080_000,
        cd_tolerance_fs_per_nm=50_000_000,
        cd_margin_fs_per_nm=45_920_000,
        latency_ns=1_176_000,
        node_count=2,
        amplifier_count=4,
        span_count=3,
        gain_shortfalls=(),
    )


def _selection(
    route_key: str,
    sections: tuple[str, ...],
    channel: int | None,
    rate_gbps: int = 400,
    channel_reason: str | None = None,
    widest_free_mhz: int = 0,
) -> Any:
    """One selection, with the reason a `None` channel is required to carry.

    `Selection` refuses a `None` channel with no reason, because a refusal an
    operator cannot act on is the failure FR-024a exists to close. The default
    below is "no spectrum at all", which is the saturated corridor most of these
    fixtures model; a test about the other reading passes `CHANNEL_NO_BLOCK` and
    the widest free block that goes with it.
    """
    from infrahub_demo_otn.budget import ModeInput
    from infrahub_demo_otn.routing import CHANNEL_NO_SPECTRUM, ModeCandidate, RouteCandidate, Selection

    reason = None if channel is not None else (channel_reason or CHANNEL_NO_SPECTRUM)

    mode = ModeCandidate(
        name=f"DP-16QAM 64GBd {rate_gbps}G",
        mode_class="transponder",
        line_rate_gbps=rate_gbps,
        baud_mbaud=64_000,
        budget_input=ModeInput(
            name=f"DP-16QAM 64GBd {rate_gbps}G",
            required_osnr_mdb=24_500,
            cd_tolerance_fs_per_nm=50_000_000,
            fec_latency_ns=4000,
        ),
    )
    return Selection(
        route=RouteCandidate(key=route_key, section_names=sections, start_node="roadm-fra"),
        mode=mode,
        channel=channel,
        budget=_budget(),
        channel_reason=reason,
        widest_free_mhz=widest_free_mhz,
    )


def _line(
    name: str,
    odu_type: str = "ODU4",
    capacity: int = 80,
    children: tuple[int, ...] = (),
    extra_children: tuple[tuple[str, int], ...] = (),
) -> dict[str, Any]:
    """One line container in the shape `queries/optical_service.gql` returns it.

    `children` names its own children after itself, which is all most assertions
    need. `extra_children` names them explicitly, which the idempotence test needs
    because the name is what decides whether a child is this service's own.
    """
    named = [(f"{name}-child-{index}", slots) for index, slots in enumerate(children)]
    return {
        "name": {"value": name},
        "odu_type": {"value": odu_type},
        "tributary_slots": {"value": 0},
        "tributary_slot_capacity": {"value": capacity},
        "child_containers": {
            "edges": [
                {
                    "node": {
                        "name": {"value": child},
                        "odu_type": {"value": "ODU2e"},
                        "tributary_slots": {"value": slots},
                    }
                }
                for child, slots in [*named, *extra_children]
            ]
        },
    }


def _payload(*carriers: tuple[str, tuple[str, ...], tuple[dict[str, Any], ...]]) -> dict[str, Any]:
    return {
        "OtnOpticalCarrier": {
            "edges": [
                {
                    "node": {
                        "id": name,
                        "__typename": "OtnOpticalCarrier",
                        "name": {"value": name},
                        "sections": {"edges": [{"node": {"name": {"value": item}}} for item in sections]},
                        "containers": {"edges": [{"node": line} for line in lines]},
                    }
                }
                for name, sections, lines in carriers
            ]
        }
    }


def _planner() -> Any:
    """The generator, with no client and no server.

    `_plan` touches `self.logger` and the four pure helpers under it, and
    `InfrahubGenerator.__init__` clones a client, which needs a stack. Building
    the instance this way is the same trick `tests/unit/test_transforms.py` uses
    on the transforms.
    """
    import logging

    cls = _module().OpticalServiceGenerator
    instance = cls.__new__(cls)
    instance.logger = logging.getLogger("test-planner")
    return instance


def test_a_route_with_no_free_channel_still_grooms_into_a_line_container_with_room() -> None:
    """T015a, at the level the decision is actually made.

    `oms-fra-mil` at 96 of 96 with a pre-provisioned wavelength that has 80 free
    slots on it. Nesting a client under that line container consumes no channel,
    so the route is usable and the plan grooms. Before the ordering fix
    `choose_route` refused this route for `capacity` and the plan was never built.
    """
    payload = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-oc-ch003-fra-mil"),)))
    plan = _planner()._plan(payload, (_selection("oms-fra-mil", ("oms-fra-mil",), None),), 8, "svc-x")  # noqa: SLF001
    assert plan.usable
    assert plan.selection.channel is None
    assert plan.line is not None
    assert plan.line.name == "odu-line-oc-ch003-fra-mil"
    assert plan.line.free == 80


def test_a_full_route_with_no_room_falls_back_to_a_longer_route_with_spectrum() -> None:
    """The regression the ordering fix could have introduced, asserted directly.

    The top-ranked route is full of spectrum *and* full of slots, so it can
    neither groom nor light. Reading `result.selection` alone would refuse the
    service there, while the second candidate has a channel free and lights a
    wavelength of its own. `_plan` walks the list, so the service provisions.
    """
    payload = _payload(
        ("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-full", children=(80,)),)),
        ("oc-detour", ("oms-fra-gen", "oms-gen-mil"), ()),
    )
    candidates = (
        _selection("oms-fra-mil", ("oms-fra-mil",), None),
        _selection("oms-fra-gen|oms-gen-mil", ("oms-fra-gen", "oms-gen-mil"), 41),
    )
    plan = _planner()._plan(payload, candidates, 8, "svc-x")  # noqa: SLF001
    assert plan.usable
    assert plan.line is None, "nothing on the detour has a container, so this is a lighting plan"
    assert plan.selection.route.key == "oms-fra-gen|oms-gen-mil"
    assert plan.selection.channel == 41


def test_a_refusal_names_the_full_section_rather_than_the_missing_container_type() -> None:
    """The refusal message, when spectrum is what blocks lighting.

    One candidate, full of slots and out of channels, at a line rate that does
    have a container type. The old message asserted the container type was the
    blocker, which sent the reader to `containers.py` over a full section.
    """
    payload = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-full", children=(80,)),)))
    module = _module()
    plan = _planner()._plan(payload, (_selection("oms-fra-mil", ("oms-fra-mil",), None),), 8, "svc-x")  # noqa: SLF001
    assert not plan.usable
    message = module.OpticalServiceGenerator._no_room(plan, "ODU2e", 8)  # noqa: SLF001
    assert "odu-line-full is the tightest of 1 line containers" in message
    assert "offers 80 slots with 0 free" in message
    assert "on oms-fra-mil no spectrum at all is free on all 1 of its sections: oms-fra-mil" in message
    assert "a line container type is defined only for" not in message


def test_a_refusal_separates_a_full_corridor_from_one_with_no_block_wide_enough() -> None:
    """FR-024a, at the message an operator reads.

    Both selections below carry `channel=None` and they are different answers.
    The first corridor is full and the answer is somebody else's wavelength to
    turn down. The second has 102,800 MHz free and no anchor that puts a 79.6 GHz
    carrier inside it, and the answer is a narrower transponder or another route.
    A message that said "no channel is free" for both would send the operator to
    the wrong place half the time, and on a nearly full section the second case is
    the common one.

    The log line and the refusal are asserted together because they are the two
    places an operator meets the sentence, and they read it from one function.
    """
    from infrahub_demo_otn.routing import CHANNEL_NO_BLOCK

    module = _module()
    payload = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-full", children=(80,)),)))
    narrow = _selection("oms-fra-mil", ("oms-fra-mil",), None, channel_reason=CHANNEL_NO_BLOCK, widest_free_mhz=102_800)
    plan = _planner()._plan(payload, (narrow,), 8, "svc-x")  # noqa: SLF001
    assert not plan.usable

    message = module.OpticalServiceGenerator._no_room(plan, "ODU2e", 8)  # noqa: SLF001
    assert "no anchor puts a DP-16QAM 64GBd 400G carrier, which occupies 79,600 MHz, inside spectrum free" in message
    assert "the widest free block is 102,800 MHz" in message
    assert "no spectrum at all" not in message

    line = module.OpticalServiceGenerator._spectrum(narrow)  # noqa: SLF001
    assert line.endswith(", groom only")
    assert "the widest free block is 102,800 MHz" in line
    full = module.OpticalServiceGenerator._spectrum(_selection("oms-fra-mil", ("oms-fra-mil",), None))  # noqa: SLF001
    assert full == "no spectrum at all is free on all 1 of its sections: oms-fra-mil, groom only"


def test_best_fit_takes_the_tightest_container_that_still_fits() -> None:
    """Acceptance scenarios 2, 3 and 4 of User Story 1, in one payload.

    Four line containers on one route and one 10G client that occupies nine
    slots. `odu-line-d` is tighter than the winner and does not fit, `odu-line-c`
    is full and is not a candidate at all, and `odu-line-a` fits with room to
    spare. The winner is `odu-line-b`, the fewest free slots that still takes
    nine, which is what makes ten SDH services land in one wavelength instead of
    spreading over four.
    """
    payload = _payload(
        (
            "oc-ch003-fra-mil",
            ("oms-fra-mil",),
            (
                _line("odu-line-a"),
                _line("odu-line-b", children=(9,)),
                _line("odu-line-c", children=(80,)),
                _line("odu-line-d", children=(75,)),
            ),
        )
    )
    plan = _planner()._plan(payload, (_selection("oms-fra-mil", ("oms-fra-mil",), 41),), 9, "svc-x")  # noqa: SLF001
    assert plan.line is not None
    assert plan.line.name == "odu-line-b"
    assert plan.line.free == 71
    assert [option.free for option in plan.options] == [80, 71, 0, 5]


def test_a_line_container_of_an_unsized_type_is_not_a_candidate() -> None:
    """FR-003 and FR-004 reaching the packing decision.

    `VC-4` has no defined tributary slot size, so nobody knows how full a `VC-4`
    line container is. The stored `tributary_slot_capacity` says 80, and reading
    it would pack a client into a container that might already be overfull.
    `_offered` returns `None` for the type and `_best_fit` drops the option, so
    the sized container beside it wins even though it is the roomier of the two.
    """
    payload = _payload(
        (
            "oc-ch003-fra-mil",
            ("oms-fra-mil",),
            (_line("odu-line-unsized", odu_type="VC-4"), _line("odu-line-sized", children=(9,))),
        )
    )
    plan = _planner()._plan(payload, (_selection("oms-fra-mil", ("oms-fra-mil",), 41),), 9, "svc-x")  # noqa: SLF001
    assert [(option.name, option.free) for option in plan.options] == [
        ("odu-line-sized", 71),
        ("odu-line-unsized", None),
    ]
    assert plan.line is not None
    assert plan.line.name == "odu-line-sized"


# ---------------------------------------------------------------------------
# Provisioning: what a run writes, and what it must never write
# ---------------------------------------------------------------------------
#
# `_provision` is the one place the write set is decided, and the write set is
# what R-011 measured a destructive failure in. Everything below drives it
# against a client that records rather than saves, so the assertions are about
# the calls the generator makes rather than about the state a stack ends in.
# A live stack cannot make these assertions at all: `update_group_context` is a
# keyword on a `save()` and leaves no trace in the graph.


class _Attribute:
    """One attribute on a fetched node, so `node.status.value = x` works."""

    def __init__(self) -> None:
        self.value: Any = None


class _Write:
    """One `save()` the generator made, with the keywords it passed."""

    def __init__(self, kind: str, name: str, data: dict[str, Any], keywords: dict[str, Any]) -> None:
        self.kind = kind
        self.name = name
        self.data = data
        self.keywords = keywords

    def __repr__(self) -> str:  # pragma: no cover - failure output only
        return f"{self.kind}:{self.name} {self.keywords}"


class _RecordingNode:
    def __init__(self, kind: str, data: dict[str, Any], writes: list[_Write]) -> None:
        self.kind = kind
        self.data = data
        self.id = f"id-{data.get('name', kind)}"
        self.status = _Attribute()
        self.rejection_code = _Attribute()
        self.rejection_detail = _Attribute()
        # Seeded to `False` rather than `None` because the schema gives
        # `refusal_accepted` `default_value: false` and `optional: false`, so a
        # fetched service always carries one of the two booleans and never an
        # empty. A fake that started it at `None` would let a test pass on a
        # generator that wrote nothing where it had to write `False`.
        self.refusal_accepted = _Attribute()
        self.refusal_accepted.value = False
        self.optical_path: Any = None
        self._writes = writes

    async def save(self, **keywords: Any) -> None:
        recorded = dict(self.data)
        recorded["status_value"] = self.status.value
        recorded["rejection_code_value"] = self.rejection_code.value
        recorded["rejection_detail_value"] = self.rejection_detail.value
        recorded["refusal_accepted_value"] = self.refusal_accepted.value
        self._writes.append(_Write(self.kind, str(self.data.get("name", "")), recorded, keywords))


class _RecordingClient:
    """A client that records every save instead of making one.

    `get` returns a node whether or not anything of that name exists, which is
    what the real client does for the two lookups the generator makes: a channel
    row it knows is there and the service it was handed. Neither lookup is a
    membership test, so nothing here has to model absence.

    `refusal_accepted` is what the fetched service already carries, which is the
    only state a rerun of this generator can read and the only way to drive the
    two halves of FR-007. The generator writes over a node it fetched, so a flag
    a person set on an earlier run arrives here and not in any payload.
    """

    def __init__(self, refusal_accepted: bool = False) -> None:
        self.writes: list[_Write] = []
        self.refusal_accepted = refusal_accepted

    async def create(self, kind: str, data: dict[str, Any], **_: Any) -> _RecordingNode:
        return _RecordingNode(kind, data, self.writes)

    async def get(self, kind: str, **keywords: Any) -> _RecordingNode:
        name = keywords.get("id") or f"{kind}-{keywords.get('channel_number__value')}"
        node = _RecordingNode(kind, {"name": name}, self.writes)
        node.refusal_accepted.value = self.refusal_accepted
        return node


def _provisioner(refusal_accepted: bool = False) -> Any:
    """The generator with a recording client, and no stack behind it."""
    import logging

    instance = _planner()
    instance.client = _RecordingClient(refusal_accepted=refusal_accepted)
    # `branch_name` is a read-only property over `branch`, which is what the SDK
    # sets from the `--branch` flag.
    instance.branch = "test-branch"
    instance.logger = logging.getLogger("test-provisioner")
    return instance


def _service(name: str = "svc-x", signal: str = "10GBASE-LR", rate_gbps: int = 10) -> dict[str, Any]:
    """One service in the shape the query returns it, stating its client signal.

    Stated rather than selected, so the payload needs no `OtnClientSignal`
    collection and the container type under test is the one named here.
    """
    return {
        "id": name,
        "name": name,
        "rate_gbps": rate_gbps,
        "client_signal": {"node": _wrapped(_row(signal))},
    }


async def _provision(instance: Any, payload: dict[str, Any], candidates: tuple[Any, ...], **kwargs: Any) -> None:
    """The decision `generate` makes, without the traversal or a stack behind it.

    Feature 017 moved the groom-or-refuse decision out of `_provision` and into
    `generate`, because FR-009 needs a direct wavelength to be tried before a
    chain and `_provision` now writes whichever one won. This mirrors that flow
    so the write-set assertions below still exercise the real decision: resolve
    the signal, plan the direct wavelength, and on failure look for a chain
    before refusing.

    The payloads here carry no `OtnOduSwitch`, so the chain attempt short-circuits
    on the absence of a device and the refusal says so. That is the sentence
    FR-010 asks for, and `test_a_refused_service_is_marked_rejected_and_creates_nothing`
    asserts it.
    """
    from infrahub_demo_otn.containers import slots_for_client

    module = _module()
    service = _service(**kwargs)
    name = str(service["name"])
    signal = module.OpticalServiceGenerator._client_signal(payload, service, name)  # noqa: SLF001
    odu_type = str(signal["default_container_type"])
    occupies = slots_for_client(odu_type, int(signal["bit_rate_kbps"]))
    plan = instance._plan(payload, candidates, occupies, name)  # noqa: SLF001
    if plan.usable:
        await instance._provision(payload, service, plan, signal, odu_type, occupies)  # noqa: SLF001
        return
    attempt = instance._chain(  # noqa: SLF001
        payload, [candidate.route for candidate in candidates], {}, service, name, occupies, odu_type
    )
    detail = instance._neither(instance._no_room(plan, odu_type, occupies), attempt.detail)  # noqa: SLF001
    await instance._refuse(service, REASON_NO_SLOTS, detail)  # noqa: SLF001


def _kinds(writes: list[_Write]) -> list[str]:
    return [write.kind for write in writes]


@pytest.mark.asyncio
async def test_grooming_writes_the_client_container_and_nothing_else_on_the_wavelength() -> None:
    """FR-009, and the write set is the assertion.

    One carrier on the route with an empty line container on it, and no channel
    free anywhere. The run writes the path, its hops, the client container and
    the service row, and it writes no carrier and no line container: the
    wavelength already exists and belongs to nobody. The client container carries
    `parent_container` and no `carrier` of its own, because the line container
    above it holds the wavelength and a second claim could only disagree.
    """
    instance = _provisioner()
    payload = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-oc-ch003-fra-mil"),)))
    await _provision(instance, payload, (_selection("oms-fra-mil", ("oms-fra-mil",), None),))

    writes = instance.client.writes
    assert _kinds(writes) == ["OtnOpticalPath", "OtnContainer", "OtnService"]
    container = writes[1]
    assert container.name == "odu-svc-x"
    assert container.data["odu_type"] == "ODU2e"
    assert container.data["tributary_slots"] == 9
    assert container.data["parent_container"] == {"hfid": ["odu-line-oc-ch003-fra-mil"]}
    assert "carrier" not in container.data
    assert writes[2].data["status_value"] == "active"


@pytest.mark.asyncio
async def test_a_refused_service_is_marked_rejected_and_creates_nothing() -> None:
    """FR-012, both halves, and FR-006's two fields.

    Every line container on the only candidate route is full and the route has no
    channel free, so there is nothing to groom into and nothing can be lit. The
    run writes exactly one thing, the service row, and the detail on it names the
    tightest container and both of its slot figures.

    The code and the detail are asserted separately because they are separate
    fields since feature 022. The detail no longer begins with the code, and that
    absence is the assertion worth having: a detail still carrying `"no-slots: "`
    would mean the string was written into both halves and the `Dropdown` bought
    nothing.
    """
    instance = _provisioner()
    payload = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-full", children=(80,)),)))
    await _provision(instance, payload, (_selection("oms-fra-mil", ("oms-fra-mil",), None),))

    writes = instance.client.writes
    assert _kinds(writes) == ["OtnService"]
    assert writes[0].data["status_value"] == "rejected"
    assert writes[0].data["rejection_code_value"] == REASON_NO_SLOTS
    reason = str(writes[0].data["rejection_detail_value"])
    assert not reason.startswith("no-slots: ")
    assert "odu-line-full is the tightest of 1 line containers on oms-fra-mil" in reason
    assert "offers 80 slots with 0 free" in reason
    assert "none of the 1 has room for the 9 slots ODU2e takes" in reason
    assert "on oms-fra-mil no spectrum at all is free on all 1 of its sections: oms-fra-mil" in reason
    # FR-010: the refusal names which of the two was missing rather than
    # implying the route is unreachable. There is no O-E-O device on this
    # payload, so the chain half is the absence of one.
    assert reason.startswith("neither a direct wavelength nor a chain serves this route.")
    assert "no O-E-O device exists on this branch" in reason


@pytest.mark.asyncio
async def test_an_accepted_refusal_survives_a_rerun_that_refuses_again() -> None:
    """FR-007, the first of the two directions the acceptance flag has.

    The same full corridor as above, but a person has already read this refusal
    and signed for it. The rerun re-refuses, and `refusal_accepted` comes out the
    other side still true.

    Clearing it here is the failure this test exists to catch, and it would be a
    quiet one: the service still refuses for the same reason, the detail still
    reads the same, and the only visible change is that a proposed change that
    merged last week now fails with nothing on the node saying why the signature
    went away.
    """
    instance = _provisioner(refusal_accepted=True)
    payload = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-full", children=(80,)),)))
    await _provision(instance, payload, (_selection("oms-fra-mil", ("oms-fra-mil",), None),))

    writes = instance.client.writes
    assert _kinds(writes) == ["OtnService"]
    assert writes[0].data["status_value"] == "rejected"
    assert writes[0].data["rejection_code_value"] == REASON_NO_SLOTS
    assert writes[0].data["refusal_accepted_value"] is True


@pytest.mark.asyncio
async def test_an_accepted_refusal_is_cleared_by_a_rerun_that_provisions() -> None:
    """FR-007a, the other direction, and the one the first draft got wrong.

    Same accepted service, but the corridor now has a wavelength to groom into,
    so this rerun provisions instead of refusing. All three fields are cleared:
    the code and the detail because the refusal is gone, and the acceptance
    because the thing that was accepted is gone with it.

    Leaving the flag set would put an acceptance on a provisioned service, which
    the provisionable check reports as an error under FR-011, and the merge would
    be blocked because the network improved. Critique finding P1. The two rules
    look like opposites and the pair of tests is the only place that shows they
    are not: refusing leaves the flag alone, provisioning clears it.
    """
    instance = _provisioner(refusal_accepted=True)
    payload = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-oc-ch003-fra-mil"),)))
    await _provision(instance, payload, (_selection("oms-fra-mil", ("oms-fra-mil",), None),))

    writes = instance.client.writes
    assert _kinds(writes) == ["OtnOpticalPath", "OtnContainer", "OtnService"]
    service = writes[2]
    assert service.data["status_value"] == "active"
    assert service.data["rejection_code_value"] is None
    assert service.data["rejection_detail_value"] is None
    assert service.data["refusal_accepted_value"] is False


@pytest.mark.asyncio
async def test_a_chain_writes_one_path_and_one_client_container_per_segment() -> None:
    """T024, and the write set is the whole assertion.

    A two-segment chain over two wavelengths that already exist, joined at
    `oeo-fra-01`. What the run must write is one `OtnOpticalPath` and one
    `OtnContainer` per segment, both carrying the same `segment_sequence`, and the
    service row. What it must **not** write is any carrier and any line container:
    both segments groom into wavelengths this run did not light, so feature 016's
    R-011 and R-012 are satisfied by there being nothing shared to write at all.

    The budget is a fixture. `budget.py` has its own suite for the physics and
    `evaluate_route` is tested in `test_budget.py`; what is under test here is the
    shape of the writes.
    """
    from infrahub_demo_otn.budget import RegeneratorInput, RouteBudget, SegmentBudget
    from infrahub_demo_otn.chains import Chain, ChainSegment
    from infrahub_demo_otn.plant import nodes_of
    from infrahub_demo_otn.routing import RouteCandidate

    module = _module()
    payload = _payload(
        ("oc-seg-par-fra", ("oms-par-fra",), (_line("odu-line-oc-seg-par-fra"),)),
        ("oc-seg-fra-ber", ("oms-ber-fra",), (_line("odu-line-oc-seg-fra-ber"),)),
    )
    segments = (
        ChainSegment(
            carrier_name="oc-seg-par-fra",
            section_names=("oms-par-fra",),
            start_node="roadm-par-01",
            junction_node="roadm-fra-01",
            junction_site="Frankfurt",
            junction_device="oeo-fra-01",
        ),
        ChainSegment(
            carrier_name="oc-seg-fra-ber",
            section_names=("oms-ber-fra",),
            start_node="roadm-fra-01",
            junction_node=None,
            junction_site=None,
            junction_device=None,
        ),
    )
    chain = Chain(segments=segments)
    instance = _provisioner()
    records = {str(record["name"]): record for record in nodes_of(payload, "OtnOpticalCarrier")}
    lines = {
        "oc-seg-par-fra": instance._carrier_options(records["oc-seg-par-fra"], own_child="odu-svc-x")[0],  # noqa: SLF001
        "oc-seg-fra-ber": instance._carrier_options(records["oc-seg-fra-ber"], own_child="odu-svc-x-s2")[0],  # noqa: SLF001
    }
    plan = module.ChainPlan(
        route=RouteCandidate(
            key="oms-par-fra|oms-ber-fra", section_names=("oms-par-fra", "oms-ber-fra"), start_node="roadm-par-01"
        ),
        chain=chain,
        segments=tuple(
            module.ChainSegmentPlan(
                sequence=sequence,
                segment=segment,
                carrier_id=segment.carrier_name,
                line=lines[segment.carrier_name],
                path_name="path-svc-x" if sequence == 1 else f"path-svc-x-s{sequence}",
                container_name="odu-svc-x" if sequence == 1 else f"odu-svc-x-s{sequence}",
            )
            for sequence, segment in enumerate(segments, start=1)
        ),
        budget=RouteBudget(
            segments=(
                SegmentBudget(sequence=1, budget=_budget(), regenerator=RegeneratorInput("oeo-fra-01", 500)),
                SegmentBudget(sequence=2, budget=_budget()),
            )
        ),
    )

    service = _service(name="svc-x")
    signal = module.OpticalServiceGenerator._client_signal(payload, service, "svc-x")  # noqa: SLF001
    await instance._provision_chain(payload, service, plan, signal, "ODU2e", 9)  # noqa: SLF001

    writes = instance.client.writes
    assert _kinds(writes) == ["OtnOpticalPath", "OtnContainer", "OtnOpticalPath", "OtnContainer", "OtnService"]
    assert [write.data["segment_sequence"] for write in writes[:4]] == [1, 1, 2, 2]
    assert [write.name for write in writes if write.kind == "OtnContainer"] == ["odu-svc-x", "odu-svc-x-s2"]
    assert [write.name for write in writes if write.kind == "OtnOpticalPath"] == ["path-svc-x", "path-svc-x-s2"]
    for container in [write for write in writes if write.kind == "OtnContainer"]:
        assert container.data["service"] == "svc-x"
        assert "carrier" not in container.data
    assert writes[1].data["parent_container"] == {"hfid": ["odu-line-oc-seg-par-fra"]}
    assert writes[3].data["parent_container"] == {"hfid": ["odu-line-oc-seg-fra-ber"]}
    assert writes[4].data["status_value"] == "active"


@pytest.mark.asyncio
async def test_two_runs_of_one_service_leave_one_client_container_in_the_same_place() -> None:
    """FR-013, across the branch state the first run leaves behind.

    The second run reads a branch that already holds `odu-svc-x` under
    `odu-line-b`, and `_line_options` leaves this service's own child out of every
    child sum. Without that exclusion `odu-line-b` would read as nine slots
    fuller than it is, the best fit would move to `odu-line-a`, and a re-run would
    walk the client from wavelength to wavelength until it was refused. Both runs
    write the same one container with the same parent and the same occupancy, so
    the second save is an upsert of the first.
    """
    before = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-a"), _line("odu-line-b", children=(9,)))))
    first = _provisioner()
    await _provision(first, before, (_selection("oms-fra-mil", ("oms-fra-mil",), None),))

    # The branch as the first run left it: the client container is now a child of
    # the line container it groomed into.
    after = _payload(
        (
            "oc-ch003-fra-mil",
            ("oms-fra-mil",),
            (_line("odu-line-a"), _line("odu-line-b", children=(9,), extra_children=(("odu-svc-x", 9),))),
        )
    )
    second = _provisioner()
    await _provision(second, after, (_selection("oms-fra-mil", ("oms-fra-mil",), None),))

    containers = [write for write in second.client.writes if write.kind == "OtnContainer"]
    assert len(containers) == 1
    assert [write.data for write in first.client.writes if write.kind == "OtnContainer"] == [
        write.data for write in containers
    ]
    assert containers[0].data["parent_container"] == {"hfid": ["odu-line-b"]}


def _saturated_corridor() -> dict[str, Any]:
    """`oms-fra-mil` as `demo/90_fra_mil_saturated.yml` leaves it.

    Forty pre-provisioned 400G wavelengths, each carrying an `ODUC4` line
    container that offers 320 tributary slots and already holds one 80-slot
    tenant. Forty is the figure the file engineers and it is not decoration: a
    single empty `ODUC4` anywhere on the route offers 320 free slots and takes the
    400G client outright.
    """
    return _payload(
        *(
            (
                f"oc-ch{channel:03d}-fra-mil",
                ("oms-fra-mil",),
                (
                    _line(
                        f"odu-line-oc-ch{channel:03d}-fra-mil",
                        odu_type="ODUC4",
                        capacity=320,
                        extra_children=((f"odu-fill-ch{channel:03d}-fra-mil", 80),),
                    ),
                ),
            )
            for channel in range(1, 41)
        )
    )


@pytest.mark.parametrize(
    ("occupies", "odu_type", "groomed"),
    [(320, "ODUC4", False), (80, "ODU4", True)],
)
def test_the_saturated_corridor_scenario_refuses_a_400g_and_grooms_a_100g(
    occupies: int, odu_type: str, groomed: bool
) -> None:
    """The two figures `docs/docs/demo-otn/provisioning-scenarios.mdx` publishes for
    scenario two.

    Same route, same zero free channels, opposite answers, and the only difference
    is how many slots the client needs. A `400GBASE-FR4` maps into an `ODUC4` and
    wants all 320 slots of a wavelength, which none of the forty has. A
    `100GBASE-LR4` wants 80 and every one of the forty has 240 free, so it grooms
    into `odu-line-oc-ch001-fra-mil`, first of forty tied at 240 and therefore
    picked on name.

    This is the claim FR-024a is about. The scenario used to demonstrate a latency
    refusal and best-fit grooming took that away, so the figures are asserted here
    rather than being recomputed and left unguarded.
    """
    payload = _saturated_corridor()
    plan = _planner()._plan(payload, (_selection("oms-fra-mil", ("oms-fra-mil",), None),), occupies, "svc-x")  # noqa: SLF001
    assert len(plan.options) == 40
    assert {option.free for option in plan.options} == {240}
    assert plan.usable is groomed
    if groomed:
        assert plan.line is not None
        assert plan.line.name == "odu-line-oc-ch001-fra-mil"
        return
    message = _module().OpticalServiceGenerator._no_room(plan, odu_type, occupies)  # noqa: SLF001
    assert "odu-line-oc-ch001-fra-mil is the tightest of 40 line containers on oms-fra-mil" in message
    assert "offers 320 slots with 240 free" in message
    assert "none of the 40 has room for the 320 slots ODUC4 takes" in message
    assert "on oms-fra-mil no spectrum at all is free on all 1 of its sections: oms-fra-mil" in message


@pytest.mark.asyncio
async def test_no_line_container_save_ever_joins_the_run_tracking_group() -> None:
    """The regression guard for FR-013a, and for the deletion R-011 measured.

    **Why the obvious assertion is the wrong one.** "The generator writes no
    `OtnContainer` holding a `carrier`" was the guard while line containers lived
    only in the generated dataset. It is false now: a route with no wavelength on
    it is lit by the run, and the line container it creates legitimately holds
    that carrier. Asserting the absence would forbid FR-008a.

    What R-011 measured is narrower than that and is not visible in the graph at
    all. A line container that joins a run's tracking group is deleted by the next
    run of the same service that stops writing it, which a refusal is, and a
    sibling service grooming into the same wavelength is left with an orphaned
    child because `OtnContainer` carries `on_delete: no-action`. Two properties
    of the calls prevent it, so both are asserted here:

    1. Every line container save passes `update_group_context=False`.
       `node.py:1288` defaults the flag to true only when it is `None`, and
       `query_groups.py:131` skips the membership write when it is explicitly
       false, so `delete_unused` can never reach the object.
    2. No save targets a line container this run did not create. An upsert counts
       the same as a create for group membership, so reading somebody else's
       wavelength and saving it back is the same defect wearing an upsert.

    The carrier is asserted the other way round on purpose. It *is* tracked, and
    it has to be: a carrier this run stops writing must be reclaimed or it holds
    a channel claim nothing owns, which `checks/channel_collision.py` then fails
    against.
    """
    groomed = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-oc-ch003-fra-mil"),)))
    dark = _payload(("oc-ch003-fra-mil", ("oms-ber-fra",), (_line("odu-line-oc-ch003-fra-mil"),)))

    grooming = _provisioner()
    await _provision(grooming, groomed, (_selection("oms-fra-mil", ("oms-fra-mil",), None),))
    lighting = _provisioner()
    await _provision(lighting, dark, (_selection("oms-fra-mil", ("oms-fra-mil",), 41),))

    # Grooming touches the wavelength not at all, which is the strongest form of
    # rule 2: the pre-existing line container is in no write set.
    assert [write.name for write in grooming.client.writes] == ["path-svc-x", "odu-svc-x", "svc-x"]

    # Lighting creates a carrier and its line container. The line container is
    # untracked, the carrier is not.
    lit = {write.name: write for write in lighting.client.writes}
    assert set(lit) == {"oc-svc-x", "odu-line-oc-svc-x", "path-svc-x", "odu-svc-x", "svc-x"}
    line = lit["odu-line-oc-svc-x"]
    assert line.data["odu_type"] == "ODUC4"
    assert line.data["carrier"] == "id-oc-svc-x"
    assert line.keywords["update_group_context"] is False
    assert "update_group_context" not in lit["oc-svc-x"].keywords

    for run in (grooming, lighting):
        for write in run.client.writes:
            if write.kind != "OtnContainer" or not write.name.startswith("odu-line-"):
                continue
            assert write.keywords.get("update_group_context") is False, write
            assert write.name == "odu-line-oc-svc-x", f"{write.name} was not created by this run"


# ---------------------------------------------------------------------------
# The entry point: a direct wavelength before a chain
# ---------------------------------------------------------------------------
#
# Everything above this line drives one method. `_provision` above is a helper
# that *mirrors* what `generate` does with the plan it makes, and a mirror
# carries its own copy of the thing under test: swapping the direct block and
# the chain block in `generators/optical_service.py` left every one of those
# tests green. FR-009 is a property of control flow in `generate` and nothing
# reached `generate`.
#
# So the two tests below call `generate`. The only thing stubbed is `_discover`,
# which is the one round trip the method makes, and `_chain` is wrapped rather
# than replaced so the real cover still runs when it is reached.
#
# The plant records come from `tests/unit/test_plant.py` and the modes from
# `objects/03_optical_modes.yml`, so a payload built here cannot drift from what
# `plant.py` reads or from what the catalog ships.

MODE_FIELDS = (
    "name",
    "mode_class",
    "line_rate_gbps",
    "baud_mbaud",
    "required_osnr_mdb",
    "cd_tolerance_fs_per_nm",
    "fec_latency_ns",
)
"""Exactly what `queries/optical_service.gql` selects on `OtnOpticalMode`."""

CARRIER_MODE_FIELDS = ("name", "baud_mbaud", "required_osnr_mdb", "cd_tolerance_fs_per_nm", "fec_latency_ns")
"""What the same query selects on a carrier's own mode, which is five of the seven.

`baud_mbaud` is the fifth and it is not there for the budget. It is the symbol
rate the occupied width comes from, and `plant.occupancy_from_graphql` raises
without it rather than allocating against spectrum it could not measure.
"""

RIDDEN_MODE = "DP-QPSK 32GBd 100G"
"""The mode every wavelength in the fixture runs.

The narrowest baud in the catalog, so `eligible_modes` ranks it first for the
10 Gbps service and the direct route and the two chain segments are all budgeted
against the same requirement. Its 14 dB is the lowest in the catalog, which is
what lets a two-section route close on a fixture nobody has to tune.
"""


def _mode_row(name: str) -> dict[str, Any]:
    matches = [entry for entry in objects_of_kind("OtnOpticalMode") if entry["name"] == name]
    assert len(matches) == 1, f"{name} is not in the shipped mode catalog"
    return matches[0]


def _mode_node(fields: tuple[str, ...] = MODE_FIELDS) -> dict[str, Any]:
    row = _mode_row(RIDDEN_MODE)
    return {field: {"value": row[field]} for field in fields}


def _identified(record: dict[str, Any]) -> dict[str, Any]:
    """One plant record with the `id` the query selects and `test_plant` omits.

    `_element_ids` keys every hop on this, so a record without one turns a
    provisioning run into a `KeyError` from inside `_write_hop`.
    """
    return {"id": f"id-{record['name']['value']}", **record}


def _section_node(name: str, head: str, tail: str) -> dict[str, Any]:
    """One `OtnOpticalMultiplexSection`: two spans and three amplifiers each way.

    Three per direction because `SectionInput.validate` holds the N+1 rule, and
    40 km spans because the fixture only has to close, not to be realistic. The
    span, amplifier and ROADM records come from `tests/unit/test_plant.py`.
    """
    spans = [_identified(span_node(f"{name}-span-{index}", index, 40_000)) for index in (1, 2)]
    return {
        "id": f"id-{name}",
        "__typename": "OtnOpticalMultiplexSection",
        "name": {"value": name},
        "roadm_a": {"node": _identified(roadm_node(head))},
        "roadm_b": {"node": _identified(roadm_node(tail))},
        "spans": {"edges": [{"node": node} for node in spans]},
        # Two chains of distinct letters rather than a direction in the name, the
        # same convention `test_plant.payload` uses. Which chain an amplifier is
        # in is which relationship holds it, and
        # `test_repository_config.py::test_no_source_file_carries_a_direction_token_in_a_device_name`
        # is what stops a fixture reintroducing the second answer.
        "amplifiers_a2b": {
            "edges": [
                {"node": _identified(amplifier_node(f"{name}-amp-{letter}", index))}
                for index, letter in enumerate("abc", start=1)
            ]
        },
        "amplifiers_b2a": {
            "edges": [
                {"node": _identified(amplifier_node(f"{name}-amp-{letter}", index))}
                for index, letter in enumerate("xyz", start=1)
            ]
        },
    }


def _carrier_node(
    name: str,
    sections: tuple[str, ...],
    channel: int,
    lines: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """One `OtnOpticalCarrier` with the anchor and mode `generate` reads.

    `_payload` above builds carriers for `_plan`, which reads neither, and
    `occupancy_from_graphql` refuses a carrier with no anchor, no centre
    frequency, no mode or no symbol rate. So this is the fuller shape rather than
    a second spelling of the same helper.

    The mode is no longer optional here. It was, while occupancy counted channel
    numbers and a carrier without a mode still occupied one; occupancy is
    spectrum now and a carrier without a symbol rate has no width at all.
    """
    node: dict[str, Any] = {
        "id": name,
        "__typename": "OtnOpticalCarrier",
        "name": {"value": name},
        "channel": {
            "node": {
                "channel_number": {"value": channel},
                "center_frequency_mhz": {"value": channel_to_frequency_mhz(channel)},
            }
        },
        "sections": {"edges": [{"node": {"name": {"value": item}}} for item in sections]},
        "containers": {"edges": [{"node": line} for line in lines]},
        "optical_mode": {"node": _mode_node(CARRIER_MODE_FIELDS)},
    }
    return node


def _endpoint(device: str, site: str, roadm: str) -> dict[str, Any]:
    """A service endpoint, and the site whose ROADM `_anchor` walks to."""
    return {
        "node": {
            "id": f"id-{device}",
            "name": {"value": device},
            "site": {
                "node": {
                    "name": {"value": site},
                    "devices": {
                        "edges": [
                            {"node": {"id": f"id-{device}", "__typename": "OtnTransponder", "name": {"value": device}}},
                            {"node": {"id": f"id-{roadm}", "__typename": "OtnRoadm", "name": {"value": roadm}}},
                        ]
                    },
                }
            },
        }
    }


def _direct_or_chain_payload(*, direct_usable: bool) -> dict[str, Any]:
    """Frankfurt-shaped in miniature: one route, two sections, both answers on it.

    `roadm-a` to `roadm-c` through `roadm-b`. `oc-direct` crosses both sections,
    which is the direct wavelength; `oc-seg-ab` and `oc-seg-bc` cross one each and
    both terminate on `oeo-b-01` at the middle site, which is the chain. So the
    route is served twice over and FR-009 is the only thing deciding which one is
    written.

    `direct_usable=False` takes the direct answer away and leaves the chain
    untouched, which is what makes the negative control a control: the two runs
    differ in the direct wavelength alone. Both halves of `WavelengthPlan.usable`
    have to go, because either one on its own still provisions. Grooming goes by
    filling the only line container on `oc-direct`, and lighting goes by claiming
    all 96 channels of `oms-a-b`, which leaves the route no spectrum for a
    wavelength of its own.
    """
    direct_line = _line("odu-line-oc-direct", children=(80,) if not direct_usable else ())
    carriers = [
        _carrier_node("oc-direct", ("oms-a-b", "oms-b-c"), 1, (direct_line,)),
        _carrier_node("oc-seg-ab", ("oms-a-b",), 2, (_line("odu-line-oc-seg-ab"),)),
        _carrier_node("oc-seg-bc", ("oms-b-c",), 3, (_line("odu-line-oc-seg-bc"),)),
    ]
    if not direct_usable:
        # No line container on these: they exist to hold spectrum. `oeo-b-01`
        # does not terminate them, so `find_chains` drops them before the
        # enumeration and the chain the run finds is still the two-segment one.
        #
        # They do carry a mode, because occupancy is spectrum and a carrier with
        # no symbol rate has no width. `occupancy_from_graphql` refuses one
        # rather than treating it as occupying nothing, which is the whole
        # argument for failing closed.
        carriers += [
            _carrier_node(f"oc-fill-{channel:03d}", ("oms-a-b",), channel)
            for channel in range(1, GRID_CHANNEL_COUNT + 1)
        ]
    return {
        "OtnService": {
            "edges": [
                {
                    "node": {
                        "id": "svc-x",
                        "__typename": "OtnService",
                        "name": {"value": "svc-x"},
                        "rate_gbps": {"value": 10},
                        "max_latency_ns": {"value": None},
                        "client_signal": {"node": _wrapped(_row("10GBASE-LR"))},
                        "endpoint_a": _endpoint("xpdr-a-01", "Site A", "roadm-a"),
                        "endpoint_z": _endpoint("xpdr-c-01", "Site C", "roadm-c"),
                    }
                }
            ]
        },
        "OtnOpticalMode": {"edges": [{"node": _mode_node()}]},
        "OtnOpticalCarrier": {"edges": [{"node": node} for node in carriers]},
        "OtnOduSwitch": {
            "edges": [
                {
                    "node": {
                        "id": "id-oeo-b-01",
                        "__typename": "OtnOduSwitch",
                        "name": {"value": "oeo-b-01"},
                        "framing_latency_ns": {"value": 500},
                        "site": {
                            "node": {
                                "name": {"value": "Site B"},
                                "devices": {
                                    "edges": [
                                        {"node": {"name": {"value": "roadm-b"}, "__typename": "OtnRoadm"}},
                                        {"node": {"name": {"value": "oeo-b-01"}, "__typename": "OtnOduSwitch"}},
                                    ]
                                },
                            }
                        },
                        "carriers": {
                            "edges": [
                                {"node": {"name": {"value": "oc-seg-ab"}}},
                                {"node": {"name": {"value": "oc-seg-bc"}}},
                            ]
                        },
                    }
                }
            ]
        },
        "OtnOpticalMultiplexSection": {
            "edges": [
                {"node": _section_node("oms-a-b", "roadm-a", "roadm-b")},
                {"node": _section_node("oms-b-c", "roadm-b", "roadm-c")},
            ]
        },
    }


def _routed(instance: Any) -> list[tuple[Any, ...]]:
    """Stub the one round trip `generate` makes, and spy on the chain lookup.

    `_chain` is wrapped rather than replaced. A stub that returned an empty
    attempt would make the negative control assert only that a call happened,
    and the run would then refuse instead of writing the chain it found. Wrapping
    keeps the real cover, so "the chain was reached" and "the chain provisions"
    are both observable.
    """
    from infrahub_demo_otn.routing import RouteCandidate

    async def discover(source: dict[str, Any], destination: dict[str, Any]) -> list[Any]:
        assert (source["name"], destination["name"]) == ("roadm-a", "roadm-c")
        return [RouteCandidate(key="oms-a-b|oms-b-c", section_names=("oms-a-b", "oms-b-c"), start_node="roadm-a")]

    calls: list[tuple[Any, ...]] = []
    looked_for = instance._chain  # noqa: SLF001

    def chain(*arguments: Any, **keywords: Any) -> Any:
        calls.append(arguments)
        return looked_for(*arguments, **keywords)

    instance._discover = discover  # noqa: SLF001
    instance._chain = chain  # noqa: SLF001
    return calls


@pytest.mark.asyncio
async def test_generate_takes_the_direct_wavelength_and_never_looks_for_a_chain() -> None:
    """FR-009 at the entry point, and the spy is the whole assertion.

    A chain is sitting in this payload, fully serviceable: two wavelengths that
    meet on an O-E-O at the middle site, each with an empty line container. The
    run must not see it. A chain costs a regeneration and latency, so a direct
    wavelength that works ends the decision before the chain lookup is reached.

    Asserting on the write set alone would not say this. Both answers groom, both
    write a path and a client container, and a generator that ranked the two
    instead of ordering them could still land on the direct one. What is under
    test is that `_chain` is never called at all.
    """
    instance = _provisioner()
    calls = _routed(instance)
    await instance.generate(_direct_or_chain_payload(direct_usable=True))

    assert calls == [], "a direct wavelength provisioned this service, so the chain lookup is dead code on this run"
    writes = instance.client.writes
    assert [write.kind for write in writes if write.kind != "OtnPathHop"] == [
        "OtnOpticalPath",
        "OtnContainer",
        "OtnService",
    ]
    container = next(write for write in writes if write.kind == "OtnContainer")
    assert container.name == "odu-svc-x"
    assert container.data["parent_container"] == {"hfid": ["odu-line-oc-direct"]}
    assert writes[-1].data["status_value"] == "active"


@pytest.mark.asyncio
async def test_generate_falls_through_to_the_chain_once_the_direct_wavelength_cannot_serve() -> None:
    """The negative control, and it is not optional.

    Without it the test above passes against a generator that never chains at
    all, which is how a fixture built in a hurry passes for the wrong reason.
    R-008 names that failure directly.

    Same payload, same route, same chain. The only difference is that the direct
    wavelength has no room to groom into and the corridor has no channel left to
    light one with, so `WavelengthPlan.usable` is false and the fall-through is
    taken. What the run writes is one path and one client container per segment.
    """
    instance = _provisioner()
    calls = _routed(instance)
    await instance.generate(_direct_or_chain_payload(direct_usable=False))

    assert len(calls) == 1, "the direct wavelength cannot serve this route, so the chain is the only answer left"
    writes = instance.client.writes
    assert [write.kind for write in writes if write.kind != "OtnPathHop"] == [
        "OtnOpticalPath",
        "OtnContainer",
        "OtnOpticalPath",
        "OtnContainer",
        "OtnService",
    ]
    containers = [write for write in writes if write.kind == "OtnContainer"]
    assert [write.name for write in containers] == ["odu-svc-x", "odu-svc-x-s2"]
    assert [write.data["parent_container"] for write in containers] == [
        {"hfid": ["odu-line-oc-seg-ab"]},
        {"hfid": ["odu-line-oc-seg-bc"]},
    ]
    assert writes[-1].data["status_value"] == "active"
