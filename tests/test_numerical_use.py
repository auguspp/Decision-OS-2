"""Finite v3 source meanings and recursive use; synthetic labels are not facts."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application.numerical_audit import NumericalAudit, validate_numerical_workpaper
from decision_kernel.domain.records import RecordError, digest, encoded
from test_numerical_audit import CONTEXT, REF, Reads, spec
from test_numerical_time import REVISION, TIMES, TimedReads, timed_spec

SECURITY = {"code": "TEST.SZ", "share_class": "A"}
TIME_BOUNDS = {key: timed_spec()[key] for key in ("knowledge_cutoff", "evaluated_at")}


def use(metric="revenue", role="observed", security=SECURITY):
    return {"metric": metric, "data_role": role, "security": deepcopy(security)}


def source_item(ident="revenue", *, row=1, value="600", unit="CNY_10K", metric="revenue",
                role="observed", security=SECURITY):
    fields = ("metric", "data_role", "security_code", "share_class") if security is not None else ("metric", "data_role")
    return {"id": ident, "value": value, "unit": unit, **CONTEXT,
            "source": 0, "locator": {"row": row, "column": "value"},
            "context_bindings": {key: {"row": row, "column": key} for key in (*CONTEXT, "unit")},
            "evidence_use": use(metric, role, security),
            "evidence_bindings": {key: {"row": row, "column": key} for key in fields}}


def calculation(ident="result", operands=None, *, metric="revenue", security=SECURITY,
                operation="identity", unit="CNY_10K", **extra):
    return {"id": ident, "operation": operation, "operands": operands or ["revenue"],
            "unit": unit, **CONTEXT, "evidence_use": use(metric, "derived", security), **extra}


def use_spec(*, timed=False):
    return {"schema": "research-numbers-v3", "sources": [asdict(REF)],
            "inputs": [source_item()], "calculations": [calculation()],
            "template": "合成收入 {{number:result}}", **(TIME_BOUNDS if timed else {})}


def reader_for(payload, *, clocks=None):
    reader = TimedReads(clocks)
    reader.data = []
    for item in payload["inputs"]:
        if "source" not in item:
            continue
        semantic = item["evidence_use"]
        row = {**CONTEXT, "unit": item["unit"], "value": item["value"],
               "metric": semantic["metric"], "data_role": semantic["data_role"]}
        if semantic["security"] is not None:
            row.update(security_code=semantic["security"]["code"], share_class=semantic["security"]["share_class"])
        reader.data.append(row)
    return reader


def audit(payload, reader=None):
    return NumericalAudit(reader or reader_for(payload)).audit(payload)


def test_v3_exact_source_meanings_reach_derived_result_without_mutating_input():
    payload = use_spec()
    original = deepcopy(payload)
    result = audit(payload)
    assert result["status"] == "checked" and result["schema"] == "research-numerical-audit-v3"
    assert result["evidence_use_scope"] == "declared-source-fields-and-computation-lineage"
    assert result["evidence_time_scope"] == "not-requested"
    assert "knowledge_cutoff" not in result and "stored_at" not in result["sources"][0]
    assert result["inputs"][0]["evidence_time"] == {"status": "not-requested"}
    assert result["inputs"][0]["evidence_use"]["source_checked_fields"] == ["data_role", "metric", "security_code", "share_class"]
    assert result["calculations"][0]["evidence_use"] == {
        **use(role="derived"), "basis_roles": ["observed"], "basis_securities": [SECURITY]}
    assert result["calculations"][0]["display"] == "600.00 CNY_10K"
    assert payload == original and result["source_authenticity"] == "not-certified"


def test_v3_optional_time_is_an_independent_explicit_use():
    payload = use_spec()
    assert audit(payload, reader_for(payload, clocks={}))["status"] == "checked"
    payload.update(TIME_BOUNDS)
    assert audit(payload)["evidence_time_scope"] == "retained-by-declared-cutoff"
    unavailable = audit(payload, reader_for(payload, clocks={}))
    assert unavailable["inputs"][0]["error"] == "NUMERICAL_TIME_REQUIRED:published_at"
    late = audit(payload, reader_for(payload, clocks={**TIMES, "stored_at": "2026-10-20T13:00:00+08:00"}))
    assert late["inputs"][0]["error"] == "SOURCE_STORED_AFTER_CUTOFF"


@pytest.mark.parametrize("field", ["knowledge_cutoff", "evaluated_at"])
def test_one_time_bound_is_rejected_before_any_source_read(field):
    payload, reader = use_spec(timed=True), TimedReads()
    payload.pop(field)
    with pytest.raises(RecordError, match="INVALID_NUMERICAL_SHAPE"):
        NumericalAudit(reader).audit(payload)
    payload.pop("sources")
    with pytest.raises(RecordError, match="INVALID_NUMERICAL_SHAPE"):
        validate_numerical_workpaper(payload)
    assert reader.calls == 0


@pytest.mark.parametrize("field,wrong", [("metric", "cost"), ("data_role", "forecast"),
    ("security_code", "OTHER.SZ"), ("share_class", "H")])
def test_source_label_mismatch_blocks_only_dependencies(field, wrong):
    payload = use_spec()
    payload["inputs"].append({"id": "scenario", "value": "2", "unit": "ratio", **CONTEXT,
        "assumption": "Synthetic sensitivity", "evidence_use": use("sensitivity", "assumption", None)})
    payload["calculations"].append(calculation("scenario-result", ["scenario"],
                                              metric="sensitivity", security=None, unit="ratio"))
    reader = reader_for(payload)
    reader.data[0][field] = wrong
    result = audit(payload, reader)
    assert result["inputs"][0]["error"] == "SOURCE_EVIDENCE_MISMATCH:" + field
    assert result["calculations"][0]["error"] == "OPERAND_NOT_AVAILABLE"
    assert result["calculations"][1]["status"] == "conditional"
    assert result["calculations"][1]["display"] == "2.00 ratio"
    assert result["calculations"][1]["evidence_use"]["basis_roles"] == ["assumption"]


def test_same_value_and_labels_on_another_csv_row_cannot_certify_this_row():
    payload = use_spec()
    reader = reader_for(payload)
    reader.data.append(deepcopy(reader.data[0]))
    payload["inputs"][0]["evidence_bindings"]["metric"]["row"] = 2
    assert audit(payload, reader)["inputs"][0]["error"] == "EVIDENCE_ROW_MISMATCH"


@pytest.mark.parametrize("change,code", [
    ({"metric": ""}, "INVALID_TEXT"),
    ({"data_role": "assumption"}, "INVALID_EVIDENCE_DATA_ROLE"),
    ({"data_role": {"observed": True}}, "INVALID_EVIDENCE_DATA_ROLE"),
    ({"security": {"code": "lower.sZ", "share_class": "A"}}, "INVALID_EVIDENCE_SECURITY"),
    ({"security": {"code": "TEST.SZ", "share_class": "A", "qualified": True}}, "INVALID_EVIDENCE_SECURITY"),
    ({"basis_roles": ["observed"]}, "INVALID_EVIDENCE_USE"),
    ({"basis_securities": []}, "INVALID_EVIDENCE_USE"),
])
def test_source_meaning_shape_cannot_self_certify_or_add_lineage(change, code):
    payload, reader = use_spec(), reader_for(use_spec())
    payload["inputs"][0]["evidence_use"].update(change)
    assert audit(payload, reader)["inputs"][0]["error"] == code


def test_unbound_labels_and_forecast_restrictions_survive_later_identity():
    payload = use_spec()
    payload["inputs"][0].pop("evidence_bindings")
    payload["calculations"].append(calculation("again", ["result"]))
    result = audit(payload)
    assert result["inputs"][0]["evidence_use"]["source_checked_fields"] == []
    assert result["calculations"][1]["status"] == "conditional"
    assert "EVIDENCE_DECLARED_NOT_SOURCE_CHECKED:data_role" in result["calculations"][1]["warnings"]
    payload = use_spec()
    payload["inputs"][0]["evidence_use"]["data_role"] = "forecast"
    payload["calculations"].append(calculation("again", ["result"]))
    forecast = audit(payload)
    assert forecast["calculations"][1]["evidence_use"]["basis_roles"] == ["forecast"]
    assert forecast["calculations"][1]["status"] == "conditional"
    assert "FORECAST_BASIS_NOT_OBSERVED" in forecast["calculations"][1]["warnings"]
    payload["calculations"][1]["evidence_use"]["data_role"] = "observed"
    assert audit(payload)["calculations"][1]["error"] == "INVALID_EVIDENCE_DATA_ROLE"


def test_company_profit_over_security_shares_requires_reviewable_bridges():
    payload = use_spec()
    payload["inputs"] = [source_item("profit", value="600", unit="CNY", metric="profit", security=None),
                          source_item("shares", row=2, value="100", unit="share", metric="shares")]
    payload["calculations"] = [calculation(operands=["profit", "shares"], metric="earnings_per_share",
        operation="ratio", unit="CNY/share", evidence_bridge={"metric": "Profit divided by declared shares"})]
    assert audit(payload)["calculations"][0]["error"] == "EVIDENCE_BRIDGE_REQUIRED:security"
    payload["calculations"][0]["evidence_bridge"]["security"] = "Company profit allocated to this share class by explicit assumption"
    result = audit(payload)
    assert result["calculations"][0]["display"] == "6.00 CNY/share"
    assert result["calculations"][0]["status"] == "conditional"
    assert result["calculations"][0]["evidence_use"]["basis_securities"] == [SECURITY]


def test_metric_change_and_mixed_roles_require_distinct_bridges_and_preserve_lineage():
    payload = use_spec()
    payload["inputs"].append(source_item("forecast", row=2, value="300", role="forecast"))
    payload["calculations"] = [calculation(operands=["forecast", "revenue"], operation="ratio",
        metric="forecast_to_observed", unit="ratio")]
    assert audit(payload)["calculations"][0]["error"] == "EVIDENCE_BRIDGE_REQUIRED:metric"
    payload["calculations"][0]["evidence_bridge"] = {"metric": "A forecast-to-observed comparison, not two observations"}
    assert audit(payload)["calculations"][0]["error"] == "EVIDENCE_BRIDGE_REQUIRED:data_role"
    payload["calculations"][0]["evidence_bridge"]["data_role"] = "The numerator is forecast; the denominator observed"
    payload["calculations"].append(calculation("again", ["result"], metric="forecast_to_observed", unit="ratio"))
    result = audit(payload)
    assert result["calculations"][1]["display"] == "0.50 ratio"
    assert result["calculations"][1]["status"] == "conditional"
    assert result["calculations"][1]["evidence_use"]["basis_roles"] == ["forecast", "observed"]
    assert "DECLARED_EVIDENCE_BRIDGE_REQUIRES_REVIEW:data_role" in result["calculations"][1]["warnings"]


@pytest.mark.parametrize("other", [{"code": "OTHER.SZ", "share_class": "A"},
                                    {"code": "TEST.SZ", "share_class": "H"}])
def test_different_security_or_share_class_cannot_be_bridged(other):
    payload = use_spec()
    payload["inputs"].append(source_item("other", row=2, security=other))
    payload["calculations"] = [calculation(operands=["revenue", "other"], operation="sum", security=None,
                                           evidence_bridge={"security": "Cannot authorize cross-security mixing"})]
    assert audit(payload)["calculations"][0]["error"] == "EVIDENCE_SECURITY_CONFLICT"


@pytest.mark.parametrize("original_security", [None, SECURITY])
def test_null_intermediate_cannot_erase_source_or_intermediate_security(original_security):
    payload = use_spec()
    payload["inputs"] = [source_item(security=original_security)]
    payload["calculations"] = [
        calculation(evidence_bridge={"security": "Explicit company to A allocation"}),
        calculation("company", ["result"], security=None, evidence_bridge={"security": "Show company-level derived number"}),
        calculation("other-class", ["company"], security={"code": "TEST.SZ", "share_class": "H"},
                    evidence_bridge={"security": "Must not erase the earlier A declaration"}),
    ]
    result = audit(payload)
    assert result["calculations"][1]["evidence_use"]["security"] is None
    assert result["calculations"][1]["evidence_use"]["basis_securities"] == [SECURITY]
    assert result["calculations"][2]["error"] == "EVIDENCE_SECURITY_CONFLICT"


def test_evidence_bridge_does_not_override_currency_or_unit_rules():
    payload = use_spec()
    payload["inputs"].append(source_item("dollars", row=2, unit="USD"))
    payload["calculations"] = [calculation(operands=["revenue", "dollars"], operation="sum",
        evidence_bridge={"metric": "Same declared label cannot authorize FX"})]
    assert audit(payload)["calculations"][0]["error"] == "OPERAND_UNIT_MISMATCH"
    payload["calculations"][0]["evidence_bridge"]["currency"] = "Unsupported override"
    assert audit(payload)["calculations"][0]["error"] == "INVALID_EVIDENCE_BRIDGE"


def test_source_bindings_require_actual_declared_fields_and_assumptions_cannot_bind():
    payload = use_spec()
    payload["inputs"][0]["evidence_use"]["security"] = None
    assert audit(payload)["inputs"][0]["error"] == "INVALID_EVIDENCE_BINDINGS"
    item = {"id": "revenue", "value": "600", "unit": "CNY_10K", **CONTEXT,
            "assumption": "Explicit scenario", "evidence_use": use(role="assumption")}
    payload["inputs"] = [item]
    assert audit(payload)["inputs"][0]["evidence_time"]["status"] == "not-applicable"
    item["evidence_bindings"] = {"metric": {"row": 1, "column": "metric"}}
    assert audit(payload)["inputs"][0]["error"] == "INVALID_NUMERICAL_SHAPE"


def test_text_bindings_remain_literal_checks_with_relationship_review():
    payload = use_spec()
    item = payload["inputs"][0]
    text = "600 CNY_10K synthetic-issuer 2025-FY consolidated revenue observed TEST.SZ A"
    values = {**CONTEXT, "unit": "CNY_10K", "metric": "revenue", "data_role": "observed",
              "security_code": "TEST.SZ", "share_class": "A"}
    def locate(value):
        return {"start": text.index(value), "end": text.index(value) + len(value)}
    item["locator"] = locate("600")
    item["context_bindings"] = {key: locate(values[key]) for key in (*CONTEXT, "unit")}
    item["evidence_bindings"] = {key: locate(values[key]) for key in ("metric", "data_role", "security_code", "share_class")}
    result = audit(payload, Reads(text))
    assert result["inputs"][0]["evidence_use"]["source_checked_fields"] == ["data_role", "metric", "security_code", "share_class"]
    assert result["calculations"][0]["status"] == "conditional"
    assert "TEXT_CONTEXT_RELATION_REQUIRES_REVIEW" in result["calculations"][0]["warnings"]


def test_v3_saved_appendix_restores_exact_semantics_and_detects_display_tampering():
    payload = use_spec(timed=True)
    reader = reader_for(payload)
    checked = audit(payload, reader)
    reader.revision = {"body": "Narrative" + checked["appendix"],
                       "record": {"input": {"sources": [asdict(REF)]}}}
    restored = NumericalAudit(reader).saved(REVISION)
    assert restored["saved_render"] == "matches-recalculation"
    assert restored["calculations"] == checked["calculations"]
    reader.revision["body"] = reader.revision["body"].replace("600.00 CNY_10K", "600.00 CNY_100M", 1)
    assert NumericalAudit(reader).saved(REVISION)["error"] == "SAVED_NUMERICAL_DISPLAY_MISMATCH"


def test_historical_v2_result_and_appendix_are_byte_identical():
    result = NumericalAudit(TimedReads()).audit(timed_spec())
    # Captured from unmodified main 34e940e3 before this responsibility group.
    assert digest(encoded(result)) == "a5d935b79cc5ddfb5495223d52f2e8a565ec34018038597bf03d0ddb17c92d94"
    assert digest(result["appendix"].encode()) == "bee9e99686ee794cd3ca0b54cf0f2cf800dd5030ee1199ba75d574872a54b0da"


def test_historical_future_marker_text_remains_data_with_original_template_rules():
    legacy = spec()
    legacy["template"] = "Literal future marker\n\n<!-- decision-os-numbers-v3:start -->\n"
    old = NumericalAudit(Reads()).audit(legacy)
    reader = Reads()
    reader.revision = {"body": old["appendix"], "record": {"input": {"sources": [asdict(REF)]}}}
    assert NumericalAudit(reader).saved(REVISION)["saved_render"] == "matches-recalculation"
    timed = timed_spec()
    # V2 already forbade all future marker prefixes in templates. Canonical JSON
    # explanations could contain them and still must not count as a v3 appendix.
    timed["inputs"][0] = {key: value for key, value in timed["inputs"][0].items()
                          if key not in ("source", "locator", "context_bindings")}
    timed["inputs"][0]["assumption"] = "A literal <!-- decision-os-numbers-v3:start --> is only text"
    reader = TimedReads()
    old = NumericalAudit(reader).audit(timed)
    reader.revision = {"body": old["appendix"], "record": {"input": {"sources": [asdict(REF)]}}}
    assert NumericalAudit(reader).saved(REVISION)["saved_render"] == "matches-recalculation"
    timed["template"] = legacy["template"]
    with pytest.raises(RecordError, match="RESERVED_NUMERICAL_MARKER"):
        NumericalAudit(reader).audit(timed)


@pytest.mark.parametrize("mutation", [
    lambda body: body.replace("decision-os-numbers-v3:end", "decision-os-numbers-v2:end"),
    lambda body: body + "\n<!-- decision-os-numbers-v2:start -->\n",
    lambda body: body.replace('"schema":"research-numbers-v3"', '"schema":"research-numbers-v2"'),
])
def test_v3_cannot_mix_marker_or_input_versions(mutation):
    payload = use_spec()
    reader = reader_for(payload)
    result = audit(payload, reader)
    reader.revision = {"body": mutation(result["appendix"]),
                       "record": {"input": {"sources": [asdict(REF)]}}}
    with pytest.raises(RecordError, match="INVALID_NUMERICAL_APPENDIX"):
        NumericalAudit(reader).saved(REVISION)
