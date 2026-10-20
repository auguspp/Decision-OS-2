"""Optional Room panel; the existing outcome consumer owns qualification."""
from copy import deepcopy

from decision_kernel.application.outcome_review import open_outcome_review, outcome_plan


def open_outcome(store, value, *, registry=None):
    plan = outcome_plan(value)
    result = open_outcome_review(store, plan, registry=registry)
    next_plan = None
    market = result["market"]
    if market is not None and market.get("page") is not None and market["page"]["remaining"]:
        next_plan = deepcopy(plan)
        next_plan["market"]["offset"] = market["page"]["offset"] + market["page"]["limit"]
    partial = any(status != "compared-literal-basis" and count for status, count in
                  result["economic"]["scope"]["outcomes"].items())
    if market is not None:
        # The whole retained scope owns completeness, including gaps off-page.
        partial = partial or market.get("status") == "unavailable" or any(
            (scope["original_scope_count"] is None and scope["status"] != "not-supplied") or any(
                status != "observed" and count for counts in scope["horizons"].values()
                for status, count in counts.items()) for scope in (market.get("scope") or {}).values())
    return {"plan": plan, "status": "readable", "partial": partial, "view": result, "next_plan": next_plan,
        "reading_scope": "explicit-original-forecasts-and-saved-discovery-generations"}
