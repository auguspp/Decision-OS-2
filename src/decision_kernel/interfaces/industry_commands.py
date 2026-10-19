"""Exact retained industry reads on the existing host; no acquisition or writer."""
from decision_kernel.application.industry_operations import industry_source_scope, read_industry_operations
from decision_kernel.application.industry_reading import read_industry
from decision_kernel.application.vehicle_flows import read_vehicle_flows
from decision_kernel.application.livestock_weekly import read_livestock_weekly
from decision_kernel.application.industry_uses import read_industry_uses
from decision_kernel.domain.records import Selection, SourceRef, identifier, require

OPERATION = "industry-read"
VIEWS = ("industry", "operating-cycle", "vehicle-flows", "livestock-weekly", "industry-uses")


def checked_payload(payload):
    """Fail before store access or public artifact creation; no caller URLs or code."""
    require(isinstance(payload, dict) and {"reference", "view", "public_sources_custody_confirmed"}
            <= set(payload) <= {"reference", "view", "public_sources_custody_confirmed",
                               "subject_id", "company_id"}, "INDUSTRY_HOST_PAYLOAD")
    require(payload["public_sources_custody_confirmed"] is True, "INDUSTRY_HOST_PUBLIC_SOURCES_REQUIRED")
    require(isinstance(payload["view"], str) and payload["view"] in VIEWS, "INDUSTRY_HOST_VIEW")
    reference = SourceRef.from_dict(payload["reference"])
    require(reference.selection == Selection(), "INDUSTRY_WHOLE_PACKET_REQUIRED")
    subject, company = payload.get("subject_id"), payload.get("company_id")
    require(subject is None or company is None, "INDUSTRY_ONE_QUERY_DIRECTION")
    for value in (subject, company):
        if value is not None:
            identifier(value)
    require(payload["view"] != "operating-cycle" or company is None,
            "OPERATING_COMPANY_QUERY_USE_ORIGINAL_INDUSTRY_READER")
    return reference, payload["view"], {"subject_id": subject, "company_id": company}


def preflight(operation, payload):
    if operation == OPERATION:
        checked_payload(payload)


def perform(store, payload):
    """Return the original consumer result unchanged, after every scope exit check."""
    reference, view, query = checked_payload(payload)
    if view == "industry-uses":
        return read_industry_uses(reference, store, **query)
    if view == "livestock-weekly":
        return read_livestock_weekly(reference, store, **query)
    if view == "vehicle-flows":
        return read_vehicle_flows(reference, store, **query)
    if view == "operating-cycle":
        return read_industry_operations(reference, store, **query)
    with industry_source_scope(store) as reader:
        result = read_industry(reference, reader, **query)
    return result
