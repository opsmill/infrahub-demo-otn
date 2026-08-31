"""Every AI and HPC service against the latency budget it declared.

Distributed training stalls on round-trip time. All-reduce collectives wait for
the slowest peer, and fiber sets the floor at 4897 ns per kilometre in G.652.
Only the AI, inference and research profiles carry a `max_latency_ns`, so this
report is filtered to those three and says how many rows it dropped.

**The delay is read, not recomputed.** The service generator wrote
`OtnOpticalPath.latency_ns` from `budget.evaluate_path`. Re-deriving it here
would be a second source of truth for a number the branch already holds.

**The propagation share is computed, and that is the point of the row.** It is
summed per span at each span's own fiber type group index. On this network it
comes out at 99.9 percent of the total: the electronics contribute about five
microseconds of a three-thousand-eight-hundred-microsecond delay. So there is no
FEC-against-reach trade-off at continental distances, because propagation
dominates by three orders of magnitude, and the OSNR-optimal route and the
latency-optimal route are the same route: the shortest.

The tension that is real is capacity against latency. The shortest route fills
up, and the next one is longer, and distance is latency. That story is the
capacity view's and the demo guide's; this report supplies the numbers under it.
"""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from infrahub_demo_otn.impact import (
    AI_PROFILES,
    LatencyVerdict,
    is_ai_profile,
    kilometres,
    latency_rows,
    microseconds,
    service_path,
    signed_microseconds,
)
from infrahub_demo_otn.plant import nodes_of
from infrahub_demo_otn.units import GROUP_INDEX_G652_MILLI, M_PER_KM, propagation_delay_ns

PROPAGATION_NS_PER_KM = propagation_delay_ns(M_PER_KM, GROUP_INDEX_G652_MILLI)
"""4897 ns per kilometre in G.652 at 1550 nm, derived rather than quoted.

Through `units.py` rather than inline, so this figure and every per-span delay
in the model come out of one function and one rounding rule.
"""


def _row(verdict: LatencyVerdict) -> dict[str, Any]:
    margin = verdict.margin_ns
    return {
        "service": verdict.service,
        "customer": verdict.customer,
        "service_profile": verdict.profile,
        "sections": list(verdict.sections),
        "length_m": verdict.total_length_m,
        "length_display": kilometres(verdict.total_length_m),
        "latency_ns": verdict.latency_ns,
        "latency_display": microseconds(verdict.latency_ns),
        "propagation_ns": verdict.propagation_ns,
        "propagation_display": microseconds(verdict.propagation_ns),
        "electronics_ns": verdict.overhead_ns,
        "electronics_display": microseconds(verdict.overhead_ns),
        "electronics_share_percent": round(verdict.overhead_share_percent, 2),
        "budget_ns": verdict.budget_ns,
        "budget_display": None if verdict.budget_ns is None else microseconds(verdict.budget_ns),
        "margin_ns": margin,
        "margin_display": None if margin is None else signed_microseconds(margin),
        "ok": verdict.ok,
        "verdict": (
            "No budget recorded. This service declares no `max_latency_ns`, so nothing here is a pass."
            if verdict.ok is None
            else (
                f"Within budget by {signed_microseconds(margin or 0)}."
                if verdict.ok
                else f"Over budget by {microseconds(-(margin or 0))}."
            )
        ),
    }


class AiLatencyTransform(InfrahubTransform):
    query = "service_latency"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        every = latency_rows(data)
        rows = [verdict for verdict in every if is_ai_profile(verdict.profile)]

        services = list(nodes_of(data, "OtnService"))
        unprovisioned = [
            {
                "service": str(service["name"]),
                "service_profile": str(service.get("service_profile") or ""),
                "status": str(service.get("status") or ""),
                # The code and the detail as two keys, matching
                # `transforms/service_trace.py`. A latency report that handed
                # back `"latency: too slow by 853.605 us"` made the caller split
                # a string to answer "which of these were refused for latency",
                # which is the question this report exists to be asked.
                # `service_latency.gql` selects both and neither is ever parsed.
                "rejection_code": service.get("rejection_code"),
                "rejection_detail": service.get("rejection_detail"),
                # `refusal_accepted` is deliberately absent, as it is in
                # `transforms/service_trace.py`. `service_latency.gql` does not
                # select it, so reading it here would answer `None` on every
                # service and render every refusal as unsigned. This report
                # cannot say whether a refusal was signed, and does not try.
                # `checks/provisionable.py` owns that question and queries for
                # it itself.
            }
            for service in services
            if is_ai_profile(str(service.get("service_profile") or ""))
            # Through `service_path`, not the raw relationship. `optical_path`
            # is cardinality many, so the bare `node` this used to read is
            # absent from the response and every service would read as
            # unprovisioned.
            and service_path(service) is None
        ]

        failing = [verdict for verdict in rows if verdict.ok is False]
        unbudgeted = [verdict for verdict in rows if verdict.ok is None]
        tightest = rows[0] if rows else None

        return {
            "branch": self.branch_name,
            "profiles_reported": sorted(AI_PROFILES),
            "excluded_service_count": len(every) - len(rows),
            "excluded_note": (
                f"{len(every) - len(rows)} provisioned service(s) were excluded because their profile carries "
                "no latency budget. Only the AI and HPC profiles declare one."
            ),
            "propagation_ns_per_km": PROPAGATION_NS_PER_KM,
            "physics_note": (
                f"Fiber propagation is {PROPAGATION_NS_PER_KM} ns per kilometre in G.652 at 1550 nm, and at "
                "continental distances it dominates every other latency term by three orders of magnitude. "
                "FEC and node latency are the `electronics` column below. There is no FEC-against-reach "
                "trade-off to exploit here, and the demo does not pretend otherwise."
            ),
            "reported_count": len(rows),
            "failing_count": len(failing),
            "unbudgeted_count": len(unbudgeted),
            "headline": (
                f"{len(rows)} latency-sensitive service(s) on {self.branch_name}. "
                + (f"{len(failing)} over budget. " if failing else "None over budget. ")
                + (
                    f"The tightest is {tightest.service} at {signed_microseconds(tightest.margin_ns)}."
                    if tightest is not None and tightest.margin_ns is not None
                    else "No service declares a budget."
                )
            ),
            "services": [_row(verdict) for verdict in rows],
            "unprovisioned": unprovisioned,
        }
