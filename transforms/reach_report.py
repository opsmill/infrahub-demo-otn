"""Where the cheap pluggables reach on this network. Nowhere.

400ZR and 800ZR are 120 km parts. The shortest optical multiplex section in the
modelled topology is Amsterdam to Brussels at 220 km. Zero of twenty-one, with a
100 km shortfall against the easiest section in the network.

That is the whole report and it leads with it. Modelling reach as data is what
lets the model tell you no.

**Reach is not the budget.** A mode whose catalog reach covers a section can
still fail the OSNR margin over that section's amplifier chain, and
`budget.evaluate_path` is the thing that decides that. Every row says so. This
report answers the procurement question, whether it is worth ordering these
parts, and points at the budget report for the engineering one.

**Reach figures are illustrative**, and every row says so: they are
representative of the part classes, not vendor-sourced. The 100 km shortfall is
a fact about the topology at any plausible ZR reach, so the finding survives the
caveat.
"""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.impact import (
    ModeReach,
    decibels,
    kilometres,
    reach_table,
    section_lengths_m,
)

BUDGET_CAVEAT = (
    "Nominal reach is a catalog figure, not a verdict. A mode in reach of a section can still miss the "
    "OSNR margin over that section's amplifier chain; `budget_report` is what computes that. This report "
    "answers whether the part is worth ordering, not whether the wavelength closes."
)

ILLUSTRATIVE_CAVEAT = (
    "Reach and required OSNR are representative of the part class, not vendor-sourced. The finding below "
    "does not depend on the exact figure: the shortest section in this topology is 220 km, so no 120 km "
    "part reaches anything at any plausible value."
)


def _mode_row(mode: ModeReach) -> dict[str, Any]:
    return {
        "mode": mode.name,
        "mode_class": mode.mode_class,
        "line_rate_gbps": mode.line_rate_gbps,
        "nominal_reach_m": mode.nominal_reach_m,
        "nominal_reach_display": kilometres(mode.nominal_reach_m),
        "required_osnr_display": decibels(mode.required_osnr_mdb),
        "sections_in_reach": len(mode.in_reach),
        "sections_total": mode.section_count,
        "in_reach": list(mode.in_reach),
        "out_of_reach": list(mode.out_of_reach),
        "shortfall_to_shortest_m": mode.shortfall_to_shortest_m,
        "verdict": (
            f"Reaches nothing. The shortest section, {mode.shortest_section}, is "
            f"{kilometres(mode.shortest_section_length_m)} and this part reaches "
            f"{kilometres(mode.nominal_reach_m)}: {kilometres(mode.shortfall_to_shortest_m)} short."
            if mode.reaches_nothing
            else (
                f"In reach of all {mode.section_count} sections."
                if mode.reaches_everything
                else (
                    f"In reach of {len(mode.in_reach)} of {mode.section_count} sections. "
                    f"Out of reach of {', '.join(mode.out_of_reach)}."
                )
            )
        ),
        "budget_caveat": BUDGET_CAVEAT,
    }


class ReachReportTransform(InfrahubTransform):
    query = "mode_reach"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        modes = reach_table(data)
        if not modes:
            raise ValueError("this branch carries no optical mode, so no reach can be judged")
        lengths = section_lengths_m(data)
        if not lengths:
            raise ValueError("this branch carries no optical multiplex section, so no reach can be measured")
        shortest = min(lengths.items(), key=lambda item: (item[1], item[0]))
        longest = max(lengths.items(), key=lambda item: (item[1], item[0]))
        unusable = [mode for mode in modes if mode.reaches_nothing]

        headline = (
            f"{', '.join(mode.name for mode in unusable)} reach nothing on this network. The shortest "
            f"optical multiplex section is {shortest[0]} at {kilometres(shortest[1])}, and every one of "
            f"these parts is rated {kilometres(unusable[0].nominal_reach_m)}."
            if unusable
            else "Every mode in the catalog reaches at least one section on this network."
        )

        return {
            "branch": self.branch_name,
            "headline": headline,
            "unusable_modes": [mode.name for mode in unusable],
            "plant": {
                "section_count": len(lengths),
                "shortest_section": shortest[0],
                "shortest_section_display": kilometres(shortest[1]),
                "longest_section": longest[0],
                "longest_section_display": kilometres(longest[1]),
            },
            "illustrative_caveat": ILLUSTRATIVE_CAVEAT,
            "budget_caveat": BUDGET_CAVEAT,
            "modes": [_mode_row(mode) for mode in modes],
            "pluggables": [_mode_row(mode) for mode in modes if mode.mode_class == "pluggable"],
            "procurement_note": (
                "Only transponder modes are provisionable in this model. A ZR wavelength originates in the "
                "router's own pluggable, and every router port in the dataset is grey: no centre frequency, "
                "-2 dBm, LC. The pluggable rows exist to be reported on, not routed onto."
            ),
        }
