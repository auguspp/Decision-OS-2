"""Shared dispatch for local and restored market reading; no storage or source I/O."""
from decision_kernel.application.global_index_inputs import (
    US_INDEX_READING_SCHEMA, US_INDEX_REQUIRED, read_us_index_context,
)
from decision_kernel.application.market_group_inputs import (
    GROUP_REQUIRED, read_ths_group_surface, read_three_market_surfaces,
)
from decision_kernel.application.market_relay_inputs import read_relay_stock_surface
from decision_kernel.application.market_security_inputs import read_security_stock_surface
from decision_kernel.application.shibor_inputs import (
    SHIBOR_READING_SCHEMA, SHIBOR_REQUIRED, read_shibor_context,
)
from decision_kernel.observations.market_daily import MarketInputError


def _route(payload):
    if not isinstance(payload, dict):
        raise MarketInputError("MARKET_RELAY_PAYLOAD_INVALID")
    schema = payload.get("schema")
    required = {"schema", "calendar_result", "stock_results", "exchange", "start", "end"}
    optional = {"horizons"}
    if schema == SHIBOR_READING_SCHEMA:
        required, optional = SHIBOR_REQUIRED | {"schema"}, set()
        reader = read_shibor_context
    elif schema == "relay-stock-reading-v1":
        required |= {"declared_entities", "coverage"}
        reader = read_relay_stock_surface
    elif schema == "relay-security-stock-reading-v1":
        required |= {"master_results", "factor_results"}
        optional |= {"reference_policy"}
        reader = read_security_stock_surface
    elif schema == "relay-ths-group-reading-v1":
        required = GROUP_REQUIRED | {"schema", "kind"}
        optional |= {"benchmark", "reference_policy"}
        reader = read_ths_group_surface
    elif schema == US_INDEX_READING_SCHEMA:
        required = US_INDEX_REQUIRED | {"schema"}
        reader = read_us_index_context
    elif schema == "relay-three-market-reading-v1":
        required, optional = {"schema", "surfaces"}, set()
        reader = read_three_market_surfaces
    else:
        raise MarketInputError("MARKET_RELAY_PAYLOAD_INVALID")
    if not required <= payload.keys() <= required | optional:
        raise MarketInputError("MARKET_RELAY_PAYLOAD_INVALID")
    return reader


def read_market_payload(payload, registry, *, offset=0, limit=50, entity_id=None):
    """One dispatcher for existing local and custody readers; no input mutation."""
    reader = _route(payload)
    args = {k: v for k, v in payload.items() if k != "schema"}
    if "horizons" in args:
        if not isinstance(args["horizons"], (list, tuple)):
            raise MarketInputError("HORIZON_INVALID")
        args["horizons"] = tuple(args["horizons"])
    return reader(**args, registry=registry, offset=offset, limit=limit, entity_id=entity_id)

