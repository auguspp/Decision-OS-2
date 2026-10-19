"""One bounded industry panel over the existing exact readers; no acquisition."""
from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application.industry_operations import industry_source_scope, read_industry_operations
from decision_kernel.application.industry_reading import read_industry
from decision_kernel.application.vehicle_flows import read_vehicle_flows
from decision_kernel.application.livestock_weekly import read_livestock_weekly
from decision_kernel.application.industry_uses import read_industry_uses
from decision_kernel.domain.records import Selection, SourceRef, encoded, identifier, require

VIEWS = ("industry", "operating-cycle", "vehicle-flows", "livestock-weekly", "industry-uses")
MAX_INDUSTRY_BYTES = 2 * 1024 * 1024


def industry_plan(value):
    """An exact retained packet and one optional query, not a URL or acquisition plan."""
    require(isinstance(value, dict) and {"reference", "view"} <= set(value)
            <= {"reference", "view", "subject_id", "company_id"}, "ROOM_INDUSTRY_PLAN")
    require(isinstance(value["view"], str) and value["view"] in VIEWS, "ROOM_INDUSTRY_VIEW")
    ref = SourceRef.from_dict(value["reference"])
    require(ref.selection == Selection(), "INDUSTRY_WHOLE_PACKET_REQUIRED")
    subject, company = value.get("subject_id"), value.get("company_id")
    require(subject is None or company is None, "INDUSTRY_ONE_QUERY_DIRECTION")
    for selector in (subject, company):
        if selector is not None:
            identifier(selector)
    require(value["view"] != "operating-cycle" or company is None,
            "OPERATING_COMPANY_QUERY_USE_ORIGINAL_INDUSTRY_READER")
    return {"reference": asdict(ref), "view": value["view"],
            "subject_id": subject, "company_id": company}


def open_industry(store, value):
    """Keep the original result, scope and interpretation, including partial results."""
    plan = industry_plan(value)
    ref = SourceRef.from_dict(plan["reference"])
    query = {key: plan[key] for key in ("subject_id", "company_id")}
    if plan["view"] == "industry-uses":
        result = read_industry_uses(ref, store, **query)
    elif plan["view"] == "livestock-weekly":
        result = read_livestock_weekly(ref, store, **query)
    elif plan["view"] == "vehicle-flows":
        result = read_vehicle_flows(ref, store, **query)
    elif plan["view"] == "operating-cycle":
        result = read_industry_operations(ref, store, **query)
    else:
        with industry_source_scope(store) as reader:
            result = read_industry(ref, reader, **query)
    require(result["reference"] == plan["reference"], "ROOM_INDUSTRY_REFERENCE_MISMATCH")
    panel = {"reference": deepcopy(plan["reference"]), "plan": plan,
             "open_request": {"operation": "industry-read", "payload": deepcopy(plan)}}
    # Source-output consent is deliberately absent from exported requests.
    if len(encoded(result)) > MAX_INDUSTRY_BYTES:
        return {**panel, "status": "not-included", "error": "ROOM_INDUSTRY_DISPLAY_BOUND"}
    return {**panel, "status": "readable", "view": result}
