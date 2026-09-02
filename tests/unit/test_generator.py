"""The generator's client-signal selection, offline and against the real catalog."""

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
"""Exactly what `queries/optical_service.gql` selects on `OtnClientSignal`."""

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
    """The mistake the layer allow-list exists to prevent."""
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
    """Refused, not substituted."""
    with pytest.raises(ValueError, match="states client signal IB-EDR-4X"):
        _select(200, stated="IB-EDR-4X")


def test_a_candidate_with_no_auto_selectable_flag_raises_naming_the_query() -> None:
    """The message has to blame the query, not the catalog."""
    with pytest.raises(ValueError, match="carries no auto_selectable flag"):
        _select(400, drop_flag=True)
    with pytest.raises(ValueError, match=r"queries/optical_service\.gql"):
        _select(400, drop_flag=True)


def test_no_rate_in_the_whole_range_selects_an_infiniband_row() -> None:
    """The sweep, and the four spot checks above are instances of it."""
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
    """A budget that closes, with the fields `_plan` and the ranking read."""
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
    """One selection, with the reason a `None` channel is required to carry."""
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
    """One line container in the shape `queries/optical_service.gql` returns it."""
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
    """The generator, with no client and no server."""
    import logging

    cls = _module().OpticalServiceGenerator
    instance = cls.__new__(cls)
    instance.logger = logging.getLogger("test-planner")
    return instance


def test_a_route_with_no_free_channel_still_grooms_into_a_line_container_with_room() -> None:
    """T015a, at the level the decision is actually made."""
    payload = _payload(("oc-ch003-fra-mil", ("oms-fra-mil",), (_line("odu-line-oc-ch003-fra-mil"),)))
    plan = _planner()._plan(payload, (_selection("oms-fra-mil", ("oms-fra-mil",), None),), 8, "svc-x")  # noqa: SLF001
    assert plan.usable
    assert plan.selection.channel is None
    assert plan.line is not None
    assert plan.line.name == "odu-line-oc-ch003-fra-mil"
    assert plan.line.free == 80


def test_a_full_route_with_no_room_falls_back_to_a_longer_route_with_spectrum() -> None:
    """The regression the ordering fix could have introduced, asserted directly."""
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
    """The refusal message, when spectrum is what blocks lighting."""
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
    """FR-024a, at the message an operator reads."""
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
    """Acceptance scenarios 2, 3 and 4 of User Story 1, in one payload."""
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
    """FR-003 and FR-004 reaching the packing decision."""
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
    """A client that records every save instead of making one."""

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
    """The decision `generate` makes, without the traversal or a stack behind it."""
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
    """FR-009, and the write set is the assertion."""
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
    """FR-012, both halves, and FR-006's two fields."""
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
    """FR-007, the first of the two directions the acceptance flag has."""
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
    """FR-007a, the other direction, and the one the first draft got wrong."""
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
    """T024, and the write set is the whole assertion."""
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
    """FR-013, across the branch state the first run leaves behind."""
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
    """`oms-fra-mil` as `demo/90_fra_mil_saturated.yml` leaves it."""
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
    """The two figures `docs/docs/provisioning-scenarios.mdx` publishes for."""
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
    """The regression guard for FR-013a, and for the deletion R-011 measured."""
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
"""What the same query selects on a carrier's own mode, which is five of the seven."""

RIDDEN_MODE = "DP-QPSK 32GBd 100G"
"""The mode every wavelength in the fixture runs."""


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
    """One `OtnOpticalMultiplexSection`: two spans and three amplifiers each way."""
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
    """One `OtnOpticalCarrier` with the anchor and mode `generate` reads."""
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
    """Frankfurt-shaped in miniature: one route, two sections, both answers on it."""
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
    """Stub the one round trip `generate` makes, and spy on the chain lookup."""
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
    """FR-009 at the entry point, and the spy is the whole assertion."""
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
    """The negative control, and it is not optional."""
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


@pytest.mark.asyncio
async def test_a_wavelength_the_generator_lights_arrives_planned_and_not_active() -> None:
    """The status a carrier is created with, and it is the whole of the argument."""
    instance = _provisioner()
    selection = _selection("oms-fra-mil", ("oms-fra-mil",), 12)

    carrier = await instance._carrier("svc-x", selection)  # noqa: SLF001

    written = next(write for write in instance.client.writes if write.kind == "OtnOpticalCarrier")
    assert written.data["status"] == "planned", (
        "a wavelength with no line port at either end is designed, not turned up, and "
        "carrier_termination reports an active one that terminates nowhere"
    )
    assert carrier is not None
