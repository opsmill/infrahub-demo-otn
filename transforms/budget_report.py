"""Render the per-hop link budget table for every wavelength.

One total tells a planner nothing actionable. This says where the budget went:
which span cost the most, where the OSNR fell fastest, how much of the
dispersion tolerance is left, and what the running one-way delay is at each
point on the path.

**Python rather than Jinja2**, which is the exception rather than the habit.
The body is a cascade in the linear domain and a running total over an ordered
chain, which is computation. It also returns a dict rather than text, so the
capacity view can consume it without re-parsing a table.

**Every scaled integer is rendered next to its raw value.** A report that makes
the reader divide by a thousand in their head is a report they will misread
once and stop trusting.

**One margin per carrier, and it is the worse of the two directions.** The two
walks stopped agreeing the moment a section grew two amplifier chains and a span
could be pumped one way only. The worse direction is the one that limits the
service, so that is the headline, and both directions are listed underneath so
the choice is visible rather than assumed.

**Raman columns appear only on pumped spans.** Nine spans of a hundred and
thirty-two carry a pump, and a zero on the rest would read as a broken column.
"""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.budget import (
    LAUNCH_POWER_PER_CHANNEL_MDBM,
    OSNR_REFERENCE_MDB,
    ROADM_FILTERING_PENALTY_MDB,
    SPAN,
    SYSTEM_MARGIN_MDB,
    Hop,
    PathBudget,
    evaluate_both_directions,
    worse_direction,
)
from infrahub_demo_otn.plant import carriers_from_graphql, sections_from_graphql
from infrahub_demo_otn.units import fs_per_nm_to_ps_per_nm, m_to_km, mdb_to_db, ns_to_us

RAMAN_NOTE = (
    "Raman is credited as reduced effective span loss: the on-off gain comes off the fibre loss for the "
    "direction the pump serves, and the combiner's insertion loss is charged whichever way the light goes. "
    "The gain is credited without charging the pump's own noise contribution, so the improvement is "
    "slightly optimistic."
)


def _decibels(value_mdb: int) -> str:
    return f"{mdb_to_db(value_mdb):.3f} dB"


def _kilometres(value_m: int) -> str:
    return f"{m_to_km(value_m):.3f} km"


def _microseconds(value_ns: int) -> str:
    return f"{ns_to_us(value_ns):.1f} us"


def _picoseconds_per_nm(value_fs_per_nm: int) -> str:
    return f"{fs_per_nm_to_ps_per_nm(value_fs_per_nm):.1f} ps/nm"


def _raman_columns(hop: Hop) -> dict[str, Any]:
    """The Raman arithmetic on one span, and nothing at all on an unpumped one.

    Raman ships on nine spans of a hundred and thirty-two. Emitting a zero on
    the other hundred and twenty-three would read as a broken column rather
    than a correct one, so the keys are absent unless a pump is on the span.

    The condition is "a pump is on the span", not "this direction is credited
    gain". A pump serving the other direction still charges its combiner loss
    to this walk, so the row would otherwise show a span that costs half a
    decibel more than its neighbours with nothing on it to say why. That row is
    the one an operator queries, and an unexplained half decibel reads as a
    modelling error.

    `loss_display` on the same row is already the effective loss, gain
    subtracted and combiner charged. What is added here is the comparison that
    makes the number mean something: what the span would have cost unpumped.
    """
    if not (hop.raman_gain_mdb or hop.pump_loss_mdb):
        return {}
    credited = (
        f"Raman credits {_decibels(hop.raman_gain_mdb)} of on-off gain to this direction"
        if hop.raman_gain_mdb
        else "The pump on this span serves the other direction, so this walk is credited no gain"
    )
    return {
        "raman_gain_mdb": hop.raman_gain_mdb,
        "raman_gain_display": _decibels(hop.raman_gain_mdb),
        "pump_loss_mdb": hop.pump_loss_mdb,
        "pump_loss_display": _decibels(hop.pump_loss_mdb),
        "unpumped_loss_mdb": hop.unpumped_loss_mdb,
        "unpumped_loss_display": _decibels(hop.unpumped_loss_mdb),
        "raman_note": (
            f"{credited} and the combiner charges {_decibels(hop.pump_loss_mdb)}, so the span costs "
            f"{_decibels(hop.loss_mdb)} instead of {_decibels(hop.unpumped_loss_mdb)}. The gain is credited "
            "without charging the pump's own noise contribution, so the improvement is slightly optimistic."
        ),
    }


def _hop_row(hop: Hop) -> dict[str, Any]:
    return {
        **_raman_columns(hop),
        "index": hop.index,
        "kind": hop.kind,
        "name": hop.name,
        "length_m": hop.length_m,
        "length_display": _kilometres(hop.length_m),
        "loss_mdb": hop.loss_mdb,
        "loss_display": _decibels(hop.loss_mdb),
        "gain_mdb": hop.gain_mdb,
        "gain_display": _decibels(hop.gain_mdb),
        "osnr_stage_mdb": hop.osnr_stage_mdb,
        "osnr_stage_display": None if hop.osnr_stage_mdb is None else _decibels(hop.osnr_stage_mdb),
        "cumulative_length_m": hop.cumulative_length_m,
        "cumulative_length_display": _kilometres(hop.cumulative_length_m),
        "cumulative_loss_mdb": hop.cumulative_loss_mdb,
        "cumulative_loss_display": _decibels(hop.cumulative_loss_mdb),
        "cumulative_osnr_mdb": hop.cumulative_osnr_mdb,
        "cumulative_osnr_display": None if hop.cumulative_osnr_mdb is None else _decibels(hop.cumulative_osnr_mdb),
        "cumulative_delay_ns": hop.cumulative_delay_ns,
        "cumulative_delay_display": _microseconds(hop.cumulative_delay_ns),
    }


def _direction_row(budget: PathBudget) -> dict[str, Any]:
    """Which way this walk went, and what it came out at.

    Named by its two endpoints rather than by `a_to_b`, because a carrier
    crosses several sections and the section-relative token means nothing at
    path level.
    """
    return {
        "from": budget.hops[0].name,
        "to": budget.hops[-1].name,
        "osnr_margin_mdb": budget.osnr_margin_mdb,
        "osnr_margin_display": _decibels(budget.osnr_margin_mdb),
        "osnr_ok": budget.osnr_ok,
        "ok": budget.ok,
    }


def _raman_spans(forward: PathBudget, reverse: PathBudget) -> list[dict[str, Any]]:
    """Every pumped span on this path, with what it costs each way.

    Both directions, and this is the one block in the report that has to carry
    both. The headline margin is the worse direction, and for a span pumped one
    way only the worse direction is the *unpumped* one, so a report that showed
    only the reported walk would show a Raman span with no Raman on it.

    Spans with no pump are absent rather than listed with zeros. Nine spans of a
    hundred and thirty-two are pumped, and a column of zeros over the other
    hundred and twenty-three reads as broken.
    """
    rows: dict[str, dict[str, Any]] = {}
    for budget in (forward, reverse):
        towards = budget.hops[-1].name
        for hop in budget.hops:
            if hop.kind != SPAN:
                continue
            row = rows.setdefault(hop.name, {"span": hop.name, "directions": []})
            row["directions"].append(
                {
                    "towards": towards,
                    "raman_gain_mdb": hop.raman_gain_mdb,
                    "raman_gain_display": _decibels(hop.raman_gain_mdb),
                    "pump_loss_mdb": hop.pump_loss_mdb,
                    "pump_loss_display": _decibels(hop.pump_loss_mdb),
                    "effective_loss_mdb": hop.loss_mdb,
                    "effective_loss_display": _decibels(hop.loss_mdb),
                    "unpumped_loss_mdb": hop.unpumped_loss_mdb,
                    "unpumped_loss_display": _decibels(hop.unpumped_loss_mdb),
                }
            )
    return [
        row for row in rows.values() if any(way["raman_gain_mdb"] or way["pump_loss_mdb"] for way in row["directions"])
    ]


def _verdict(budget: PathBudget) -> dict[str, Any]:
    return {
        "osnr_total_mdb": budget.osnr_total_mdb,
        "osnr_total_display": _decibels(budget.osnr_total_mdb),
        "required_osnr_mdb": budget.required_osnr_mdb,
        "required_osnr_display": _decibels(budget.required_osnr_mdb),
        "system_margin_mdb": budget.system_margin_mdb,
        "system_margin_display": _decibels(budget.system_margin_mdb),
        "osnr_margin_mdb": budget.osnr_margin_mdb,
        "osnr_margin_display": _decibels(budget.osnr_margin_mdb),
        "osnr_ok": budget.osnr_ok,
        "cd_total_fs_per_nm": budget.cd_total_fs_per_nm,
        "cd_total_display": _picoseconds_per_nm(budget.cd_total_fs_per_nm),
        "cd_tolerance_fs_per_nm": budget.cd_tolerance_fs_per_nm,
        "cd_tolerance_display": _picoseconds_per_nm(budget.cd_tolerance_fs_per_nm),
        "cd_margin_fs_per_nm": budget.cd_margin_fs_per_nm,
        "cd_margin_display": _picoseconds_per_nm(budget.cd_margin_fs_per_nm),
        "cd_ok": budget.cd_ok,
        "gain_shortfalls": list(budget.gain_shortfalls),
        "gain_ok": budget.gain_ok,
        "ok": budget.ok,
    }


class BudgetReportTransform(InfrahubTransform):
    query = "budget_report"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        sections = sections_from_graphql(data)
        reports: list[dict[str, Any]] = []
        unbudgeted: list[dict[str, str]] = []

        for carrier in carriers_from_graphql(data):
            name = str(carrier["name"])
            mode = carrier["mode"]
            if mode is None or not carrier["section_names"]:
                unbudgeted.append({"carrier": name, "reason": "no optical mode, or no section to cross"})
                continue
            missing = [section for section in carrier["section_names"] if section not in sections]
            if missing:
                unbudgeted.append({"carrier": name, "reason": f"sections not returned: {', '.join(sorted(missing))}"})
                continue
            try:
                forward, reverse = evaluate_both_directions(
                    [sections[section] for section in carrier["section_names"]], mode
                )
            except ValueError as error:
                unbudgeted.append({"carrier": name, "reason": str(error)})
                continue

            # The wavelength runs both ways and the two ways no longer agree:
            # each reads its own amplifier chain and is credited only its own
            # Raman. One number per carrier is therefore the worse of the two,
            # because that is the direction the service is limited by. Both are
            # reported, so nobody has to take the choice on trust.
            budget = worse_direction(forward, reverse)

            reports.append(
                {
                    "carrier": name,
                    "mode": mode.name,
                    "sections": sorted(carrier["section_names"]),
                    "direction": _direction_row(budget),
                    "directions": [_direction_row(forward), _direction_row(reverse)],
                    "raman_spans": _raman_spans(forward, reverse),
                    "span_count": budget.span_count,
                    "amplifier_count": budget.amplifier_count,
                    "node_count": budget.node_count,
                    "total_length_m": budget.total_length_m,
                    "total_length_display": _kilometres(budget.total_length_m),
                    "total_loss_mdb": budget.total_loss_mdb,
                    "total_loss_display": _decibels(budget.total_loss_mdb),
                    "latency_ns": budget.latency_ns,
                    "latency_display": _microseconds(budget.latency_ns),
                    "verdict": _verdict(budget),
                    "hops": [_hop_row(hop) for hop in budget.hops],
                }
            )

        reports.sort(key=lambda report: report["verdict"]["osnr_margin_mdb"])
        pumped = sum(1 for report in reports for _ in report["raman_spans"])
        return {
            "raman_span_count": pumped,
            "raman_note": RAMAN_NOTE if pumped else None,
            "model": {
                "launch_power_per_channel_mdbm": LAUNCH_POWER_PER_CHANNEL_MDBM,
                "launch_power_per_channel_display": f"{mdb_to_db(LAUNCH_POWER_PER_CHANNEL_MDBM):+.1f} dBm",
                "osnr_reference_mdb": OSNR_REFERENCE_MDB,
                "osnr_reference_display": _decibels(OSNR_REFERENCE_MDB),
                "roadm_filtering_penalty_mdb": ROADM_FILTERING_PENALTY_MDB,
                "roadm_filtering_penalty_display": _decibels(ROADM_FILTERING_PENALTY_MDB),
                "system_margin_mdb": SYSTEM_MARGIN_MDB,
                "system_margin_display": _decibels(SYSTEM_MARGIN_MDB),
            },
            "carrier_count": len(reports),
            "failing_count": sum(1 for report in reports if not report["verdict"]["ok"]),
            "unbudgeted": unbudgeted,
            "carriers": reports,
        }
