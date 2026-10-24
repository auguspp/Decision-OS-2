"""Finite numerical v3 evidence meanings; no source or financial certification.

Labels can be compared to retained fields. Explicit bridges remain declarations,
and recursive role/security bases prevent intermediate calculations from erasing
their restrictions. Arithmetic, custody and source time checks stay with owners.
"""
from __future__ import annotations

import re

from decision_kernel.domain.records import checked_text, require

VERSION = "research-numbers-v3"
EVIDENCE_USE_SCOPE = "declared-source-fields-and-computation-lineage"
_FIELDS = {"metric", "data_role", "security"}
_BRIDGES = {"metric", "data_role", "security"}


def _use(value: dict, roles: set[str]) -> dict:
    require(isinstance(value, dict) and set(value) == _FIELDS, "INVALID_EVIDENCE_USE")
    metric = checked_text(value["metric"], limit=256)
    role = value["data_role"]
    require(isinstance(role, str) and role in roles, "INVALID_EVIDENCE_DATA_ROLE")
    security = value["security"]
    if security is not None:
        require(isinstance(security, dict) and set(security) == {"code", "share_class"},
                "INVALID_EVIDENCE_SECURITY")
        code = security["code"]
        require(isinstance(code, str) and re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", code) is not None,
                "INVALID_EVIDENCE_SECURITY")
        security = {"code": code, "share_class": checked_text(security["share_class"], limit=64)}
    return {"metric": metric, "data_role": role, "security": security}


def input_use(item: dict, view: dict | None, extract) -> tuple[dict, list[str]]:
    """Bind declared labels to the same retained row as the value when supplied."""
    assumption = "assumption" in item
    use = _use(item["evidence_use"], {"assumption"} if assumption else {"observed", "forecast"})
    result = {**use, "basis_roles": [use["data_role"]],
              "basis_securities": [use["security"]] if use["security"] is not None else []}
    if assumption:
        require("evidence_bindings" not in item, "ASSUMPTION_HAS_NO_EVIDENCE_BINDINGS")
        return result, []
    expected = {key: use[key] for key in ("metric", "data_role")}
    if use["security"] is not None:
        expected.update(security_code=use["security"]["code"], share_class=use["security"]["share_class"])
    bindings = item.get("evidence_bindings", {})
    require(isinstance(bindings, dict) and set(bindings) <= set(expected), "INVALID_EVIDENCE_BINDINGS")
    warnings, checked = [], []
    for key, value in expected.items():
        if key not in bindings:
            warnings.append("EVIDENCE_DECLARED_NOT_SOURCE_CHECKED:" + key)
            continue
        locator = bindings[key]
        if isinstance(view["data"], list):
            require(isinstance(locator, dict) and locator.get("row") == item["locator"]["row"],
                    "EVIDENCE_ROW_MISMATCH")
        require(extract(view["data"], locator) == value, "SOURCE_EVIDENCE_MISMATCH:" + key)
        checked.append(key)
    if use["data_role"] == "forecast":
        warnings.append("FORECAST_BASIS_NOT_OBSERVED")
    result["source_checked_fields"] = sorted(checked)
    return result, warnings


def calculation_use(item: dict, operands: list[dict]) -> tuple[dict, list[str]]:
    """Keep source and intermediate declarations through ordered calculations."""
    use = _use(item["evidence_use"], {"derived"})
    bridges = item.get("evidence_bridge", {})
    require(isinstance(bridges, dict) and set(bridges) <= _BRIDGES, "INVALID_EVIDENCE_BRIDGE")
    for reason in bridges.values():
        checked_text(reason, limit=4096)
    # Include intermediate declared securities, not just original source labels:
    # null -> A -> null -> H must not erase the first explicit A declaration.
    securities = {(s["code"], s["share_class"])
                  for operand in operands for s in operand["basis_securities"]}
    if use["security"] is not None:
        securities.add((use["security"]["code"], use["security"]["share_class"]))
    require(len(securities) <= 1, "EVIDENCE_SECURITY_CONFLICT")
    needs = {
        "metric": any(operand["metric"] != use["metric"] for operand in operands),
        "security": any(operand["security"] != use["security"] for operand in operands),
        "data_role": len({tuple(operand["basis_roles"]) for operand in operands}) > 1,
    }
    for field, needed in needs.items():
        require(not needed or field in bridges, "EVIDENCE_BRIDGE_REQUIRED:" + field)
    result = {**use,
              "basis_roles": sorted({role for operand in operands for role in operand["basis_roles"]}),
              "basis_securities": [{"code": code, "share_class": share_class}
                                   for code, share_class in sorted(securities)]}
    warnings = ["DECLARED_EVIDENCE_BRIDGE_REQUIRES_REVIEW:" + field for field in sorted(bridges)]
    return result, warnings
