"""Re-validate every wavelength's optical margin on a proposed change.

This is what makes `budget.py` more than a calculator. Lengthen a span on a
branch, swap a fiber family, retune an amplifier, and every carrier crossing
that plant is re-evaluated before the change can merge.

Three independent gates, all reported:

- **OSNR**: `osnr_total - required_osnr - system_margin >= 0`.
- **Chromatic dispersion**: `accumulated <= mode.cd_tolerance`.
- **Gain**: every amplifier can recover the loss ahead of its input, ageing
  allowance included.

A carrier that cannot be evaluated is an error, not a skip. A check that
silently passes over the object it could not read is worse than no check: it
reports green for a network it never looked at.

**Two loops.** The carrier loop budgets the traffic that exists, which on the
loaded dataset reaches five of the twenty-one sections, because only five are
crossed by any carrier at all.

The section sweep covers the rest. It budgets every section standalone in both
directions against one named reference mode, so 21 sections give 42 evaluations
and every chain in the plant is walked on every run. That matters beyond the
margin figures: the server enforces no cardinality on either of the section's
two amplifier relationships, so `SectionInput.validate()` is the only guard
there is, and the sweep is what reaches it for the whole network rather than a
quarter of it.

The reference mode is named here and not in the query, and the query returns the
mode catalog unconditionally rather than only the modes some carrier happens to
use. A sweep that reached its reference mode through a carrier would have
nothing to budget against on exactly the branches where a carrier was removed.

Main is expected to fail this check. That is deliberate: Paris to Madrid is
short of OSNR in both directions, and a branch that adds Raman pumps to it is
what turns the check green.
"""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from infrahub_demo_otn.budget import ModeInput, SectionInput, evaluate_path
from infrahub_demo_otn.plant import (
    DIRECTION_A_TO_B,
    DIRECTION_B_TO_A,
    build_mode,
    carriers_from_graphql,
    nodes_of,
    sections_from_graphql,
)
from infrahub_demo_otn.units import m_to_km, mdb_to_db

REFERENCE_MODE = "DP-16QAM 64GBd 400G"
"""The mode the section sweep budgets against.

One named mode rather than every mode in the catalog. The sweep answers "can
this section carry the workhorse wavelength", and running all nine modes over 21
sections would report the same fact nine times.
"""


class OsnrMarginCheck(InfrahubCheck):
    query = "osnr_margin"

    def validate(self, data: dict[str, Any]) -> None:
        sections = sections_from_graphql(data)
        self.check_carriers(data, sections)
        self.sweep_sections(data, sections)

    def _against(self, carrier: dict[str, Any], message: str) -> None:
        """One error, named against the carrier it is about.

        `object_id` and `object_type` are what lets the proposed change link a
        failure to the object it failed on, instead of leaving the reader to
        find a name in a message. Every query here selects `id` and
        `__typename` on its top-level nodes, so the two values are free.
        """
        self.log_error(message=message, object_id=carrier["id"], object_type=carrier["kind"])

    def reference_mode(self, data: dict[str, Any]) -> ModeInput | None:
        """The sweep's reference mode, or an error naming what was returned.

        A branch whose catalog has been renamed or emptied is a data problem
        and the check says so. Returning silently would leave the sweep off and
        the check green over a network nothing budgeted.
        """
        modes = {str(record["name"]): record for record in nodes_of(data, "OtnOpticalMode")}
        record = modes.get(REFERENCE_MODE)
        if record is None:
            self.log_error(
                message=(
                    f"the section sweep budgets against {REFERENCE_MODE}, which this branch does not carry. "
                    f"The catalog holds {', '.join(sorted(modes)) or 'no mode at all'}"
                )
            )
            return None
        return build_mode(record)

    def sweep_sections(self, data: dict[str, Any], sections: dict[str, SectionInput]) -> None:
        """Every section standalone, both directions, against the reference mode.

        Two evaluations per section, and the direction is named in every
        message. A section is walked from `roadm_a` for the `a_to_b` figure and
        from `roadm_b` for the other, so each walk reads the amplifier chain
        that actually amplifies it. With unequal ROADM insertion losses and two
        independent chains the two numbers are genuinely different, and the
        worse one is what constrains a service over that section.
        """
        if not sections:
            self.log_info(message="No optical multiplex sections on this branch, so there is no plant to sweep")
            return
        mode = self.reference_mode(data)
        if mode is None:
            return

        worst_name = ""
        worst_margin: int | None = None
        evaluated = 0
        failing: set[str] = set()

        for name in sorted(sections):
            section = sections[name]
            roadm_a, roadm_b = section.endpoints
            for direction, start_node in ((DIRECTION_A_TO_B, roadm_a), (DIRECTION_B_TO_A, roadm_b)):
                label = f"{name} {direction}"
                try:
                    budget = evaluate_path([section], mode, start_node=start_node)
                except ValueError as error:
                    self.log_error(message=f"{label} cannot be budgeted: {error}")
                    continue

                evaluated += 1
                if worst_margin is None or budget.osnr_margin_mdb < worst_margin:
                    worst_margin, worst_name = budget.osnr_margin_mdb, label

                if not budget.osnr_ok:
                    failing.add(name)
                    self.log_error(
                        message=(
                            f"{label} is short of OSNR by {-mdb_to_db(budget.osnr_margin_mdb):.3f} dB on "
                            f"{mode.name}: the section delivers {mdb_to_db(budget.osnr_total_mdb):.3f} dB over "
                            f"{m_to_km(budget.total_length_m):.0f} km and {budget.amplifier_count} amplifiers, "
                            f"and the mode needs {mdb_to_db(budget.required_osnr_mdb):.3f} dB plus "
                            f"{mdb_to_db(budget.system_margin_mdb):.3f} dB of system margin"
                        )
                    )
                if not budget.cd_ok:
                    failing.add(name)
                    self.log_error(
                        message=(
                            f"{label} accumulates {budget.cd_total_fs_per_nm} fs/nm of chromatic dispersion on "
                            f"{mode.name}, against a tolerance of {budget.cd_tolerance_fs_per_nm} fs/nm"
                        )
                    )
                if not budget.gain_ok:
                    failing.add(name)
                    self.log_error(
                        message=(
                            f"{label} carries {', '.join(budget.gain_shortfalls)}, whose gain cannot recover "
                            "the loss ahead of its input at end of life"
                        )
                    )

        if worst_margin is not None:
            self.log_info(
                message=(
                    f"Swept {evaluated} section evaluations over {len(sections)} sections in two directions "
                    f"against {mode.name}. Worst standalone OSNR margin is {mdb_to_db(worst_margin):+.3f} dB, "
                    f"on {worst_name}. "
                    + (f"Short of OSNR: {', '.join(sorted(failing))}" if failing else "Every section closes")
                )
            )

    def check_carriers(self, data: dict[str, Any], sections: dict[str, SectionInput]) -> None:
        """Every carrier over the sections it crosses.

        This loop is unchanged in substance. It reports on the traffic that
        exists, which the sweep deliberately does not: a section the sweep
        closes standalone can still be the section a four-hop wavelength runs
        out of margin on.
        """
        carriers = carriers_from_graphql(data)
        if not carriers:
            self.log_info(message="No optical carriers on this branch, so there is no carrier margin to re-validate")
            return

        worst_name = ""
        worst_margin: int | None = None
        evaluated = 0

        for carrier in carriers:
            name = str(carrier["name"])
            mode = carrier["mode"]
            if mode is None:
                self._against(
                    carrier, f"{name} has no optical mode, so it has no OSNR requirement to be measured against"
                )
                continue

            missing = [section for section in carrier["section_names"] if section not in sections]
            if missing:
                self._against(carrier, f"{name} crosses {', '.join(sorted(missing))}, which the query did not return")
                continue
            if not carrier["section_names"]:
                self._against(carrier, f"{name} crosses no section, so it has no path to budget")
                continue

            # One error boundary per carrier. `order_sections` and the section
            # validators raise on data the engine cannot walk, and one such
            # carrier must not hide the other seventy.
            try:
                budget = evaluate_path([sections[section] for section in carrier["section_names"]], mode)
            except ValueError as error:
                self._against(carrier, f"{name} cannot be budgeted: {error}")
                continue

            evaluated += 1
            if worst_margin is None or budget.osnr_margin_mdb < worst_margin:
                worst_margin, worst_name = budget.osnr_margin_mdb, name

            if not budget.osnr_ok:
                self._against(
                    carrier,
                    f"{name} on {mode.name} is short of OSNR by {-mdb_to_db(budget.osnr_margin_mdb):.3f} dB: "
                    f"the path delivers {mdb_to_db(budget.osnr_total_mdb):.3f} dB over "
                    f"{m_to_km(budget.total_length_m):.0f} km and {budget.amplifier_count} amplifiers, "
                    f"and the mode needs {mdb_to_db(budget.required_osnr_mdb):.3f} dB plus "
                    f"{mdb_to_db(budget.system_margin_mdb):.3f} dB of system margin",
                )
            if not budget.cd_ok:
                self._against(
                    carrier,
                    f"{name} on {mode.name} accumulates {budget.cd_total_fs_per_nm} fs/nm of chromatic "
                    f"dispersion over {m_to_km(budget.total_length_m):.0f} km, against a tolerance of "
                    f"{budget.cd_tolerance_fs_per_nm} fs/nm",
                )
            if not budget.gain_ok:
                self._against(
                    carrier,
                    f"{name} crosses {', '.join(budget.gain_shortfalls)}, whose gain cannot recover the loss "
                    "ahead of its input at end of life",
                )

        if worst_margin is not None:
            self.log_info(
                message=(
                    f"Re-validated {evaluated} carriers over {len(sections)} sections. Worst OSNR margin is "
                    f"{mdb_to_db(worst_margin):+.3f} dB, on {worst_name}"
                )
            )
