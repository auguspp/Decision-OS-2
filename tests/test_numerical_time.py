"""Versioned numerical time uses; synthetic metadata is not economic evidence."""
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from decision_kernel.application.numerical_audit import (
    END, INPUT, START, NUMERICAL_SCHEMAS, NumericalAudit, has_numerical_appendix,
    validate_numerical_workpaper,
)
from decision_kernel.domain.records import BlobRef, RecordError, SourceRef, digest, encoded
from test_numerical_audit import CONTEXT, REF, Reads, spec

TIMES = {"published_at": "2026-10-20T08:00:00+08:00",
         "acquired_at": "2026-10-20T09:00:00+08:00",
         "stored_at": "2026-10-20T10:00:00+08:00"}
OTHER_REF = SourceRef(replace(REF.manifest, commit="f" * 40))
REVISION = BlobRef("synthetic-store", "d" * 40,
                   "research/cases/synthetic/revisions/r1/record.json", "e" * 64)


def timed_spec():
    return {**spec(), "schema": "research-numbers-v2",
            "knowledge_cutoff": "2026-10-20T12:00:00+08:00",
            "evaluated_at": "2026-10-21T12:00:00+08:00"}


class TimedReads(Reads):
    def __init__(self, clocks=None, other_clocks=None):
        super().__init__()
        self.clocks = deepcopy(TIMES if clocks is None else clocks)
        self.other_clocks = deepcopy(TIMES if other_clocks is None else other_clocks)

    def read_source(self, reference):
        assert reference in (REF, OTHER_REF)
        view = super().read_source(REF)
        clocks = self.clocks if reference == REF else self.other_clocks
        for field in ("published_at", "acquired_at"):
            if field in clocks:
                view["manifest"]["input"][field] = clocks[field]
        if "stored_at" in clocks:
            view["manifest"]["stored_at"] = clocks["stored_at"]
        return view


def saved_reader(body, clocks=None):
    reader = TimedReads(clocks)
    reader.revision = {"body": body, "record": {"input": {"sources": [asdict(REF)]}}}
    return reader


def test_historical_v1_result_and_appendix_are_byte_identical():
    result = NumericalAudit(Reads()).audit(spec())
    # Captured from unmodified d94e02cd before this responsibility group.
    assert digest(encoded(result)) == "cfc6665f8c67e5a355ba7a82adf0fa1280708721ffffe6ad1df03fcee8940854"
    assert digest(result["appendix"].encode()) == "1ba621bc6a294eeb840ae16230cb23df107d443a314b2ad92cd6b2d94494aefe"
    assert result["schema"] == "research-numerical-audit-v1"
    assert all(key not in result for key in ("knowledge_cutoff", "evaluated_at", "evidence_time_scope"))
    assert all("evidence_time" not in row for row in result["inputs"])
    assert result["appendix"].startswith(START) and INPUT in result["appendix"] and result["appendix"].endswith(END)
    assert "research-numbers-v1" in NUMERICAL_SCHEMAS


def test_v2_uses_original_arithmetic_and_reports_three_separate_clocks():
    reader, payload = TimedReads(), timed_spec()
    original = deepcopy(payload)
    result = NumericalAudit(reader).audit(payload)
    assert result["schema"] == "research-numerical-audit-v2" and result["status"] == "checked"
    assert result["knowledge_cutoff"] == payload["knowledge_cutoff"]
    assert result["evaluated_at"] == payload["evaluated_at"]
    assert result["evidence_time_scope"] == "retained-by-declared-cutoff"
    assert result["calculations"][0]["display"] == "150.00 CNY_10K" and reader.calls == 1
    time = result["inputs"][0]["evidence_time"]
    assert time["status"] == "checked" and {k: time[k] for k in TIMES} == TIMES
    assert time["precision"] == {key: "instant" for key in TIMES}
    assert time["provenance"] == {"published_at": "source-input-declaration",
        "acquired_at": "source-input-declaration", "stored_at": "retained-manifest-metadata"}
    assert result["sources"][0]["stored_at"] == TIMES["stored_at"]
    assert result["source_authenticity"] == "not-certified" and result["research_assessment"] == "not-performed"
    assert "decision-os-numbers-v2:start" in result["appendix"] and START not in result["appendix"]
    assert payload == original


def test_equal_instants_in_different_offsets_and_microseconds_are_comparable():
    clocks = {"published_at": "2026-10-20T00:00:00.123456Z",
              "acquired_at": "2026-10-20T08:00:00.123456+08:00",
              "stored_at": "2026-10-19T20:00:00.123456-04:00"}
    payload = timed_spec()
    payload.update(knowledge_cutoff="2026-10-20T01:00:00.123456+01:00",
                   evaluated_at="2026-10-20T00:00:00.123456+00:00")
    result = NumericalAudit(TimedReads(clocks)).audit(payload)
    assert result["status"] == "checked"
    assert result["inputs"][0]["evidence_time"]["stored_at"] == clocks["stored_at"]


@pytest.mark.parametrize("field", ["published_at", "acquired_at", "stored_at"])
@pytest.mark.parametrize("value,prefix,precision", [
    (None, "NUMERICAL_TIME_REQUIRED", "missing"),
    ("2026-10-20", "NUMERICAL_TIME_PRECISION_REQUIRED", "date-only"),
    ("2026-10-20T08:00Z", "NUMERICAL_TIME_PRECISION_REQUIRED", "minute-only"),
    ("2026-10-20T08:00:00", "NUMERICAL_TIMEZONE_REQUIRED", "timezone-unavailable"),
    ("2026-02-30T08:00:00Z", "NUMERICAL_TIME_INVALID", "invalid"),
    ("2026-10-20T08:00:00+00:60", "NUMERICAL_TIME_INVALID", "invalid"),
    ({"qualified": True}, "NUMERICAL_TIME_INVALID", "invalid"),
])
def test_unusable_source_clock_blocks_its_inputs_without_inventing_precision(field, value, prefix, precision):
    clocks = {**TIMES, field: value}
    result = NumericalAudit(TimedReads(clocks)).audit(timed_spec())
    assert result["status"] == "blocked"
    assert result["inputs"][0]["error"] == prefix + ":" + field
    assert result["inputs"][0]["evidence_time"]["precision"][field] == precision
    assert result["calculations"][0]["error"] == "OPERAND_NOT_AVAILABLE"
    if value is None or not isinstance(value, str):
        assert result["inputs"][0]["evidence_time"][field] is None
    else:
        assert result["inputs"][0]["evidence_time"][field] == value


@pytest.mark.parametrize("field", ["published_at", "acquired_at", "stored_at"])
def test_absent_source_clock_is_explicit(field):
    clocks = {key: value for key, value in TIMES.items() if key != field}
    result = NumericalAudit(TimedReads(clocks)).audit(timed_spec())
    assert result["inputs"][0]["error"] == "NUMERICAL_TIME_REQUIRED:" + field


@pytest.mark.parametrize("clocks,code", [
    ({**TIMES, "published_at": "2026-10-20T09:00:01+08:00"}, "SOURCE_PUBLISHED_AFTER_ACQUISITION"),
    ({**TIMES, "acquired_at": "2026-10-20T10:00:01+08:00"}, "SOURCE_ACQUIRED_AFTER_STORAGE"),
    ({**TIMES, "stored_at": "2026-10-21T12:00:01+08:00"}, "SOURCE_STORED_AFTER_EVALUATION"),
    ({"published_at": "2026-10-20T13:00:00+08:00", "acquired_at": "2026-10-20T14:00:00+08:00",
      "stored_at": "2026-10-20T15:00:00+08:00"}, "SOURCE_PUBLISHED_AFTER_CUTOFF"),
    ({**TIMES, "acquired_at": "2026-10-20T13:00:00+08:00", "stored_at": "2026-10-20T14:00:00+08:00"},
      "SOURCE_ACQUIRED_AFTER_CUTOFF"),
    ({**TIMES, "stored_at": "2026-10-20T12:00:00.000001+08:00"}, "SOURCE_STORED_AFTER_CUTOFF"),
])
def test_source_time_order_and_actual_retention_cutoff_are_distinct(clocks, code):
    result = NumericalAudit(TimedReads(clocks)).audit(timed_spec())
    assert result["inputs"][0]["error"] == code
    assert result["inputs"][0]["evidence_time"]["error"] == code
    assert result["sources"][0]["evidence_time"]["status"] == "blocked"


@pytest.mark.parametrize("field", ["knowledge_cutoff", "evaluated_at"])
@pytest.mark.parametrize("value,prefix", [
    (None, "NUMERICAL_TIME_REQUIRED"),
    ("2026-10-20", "NUMERICAL_TIME_PRECISION_REQUIRED"),
    ("2026-10-20T08:00:00", "NUMERICAL_TIMEZONE_REQUIRED"),
    ("2026-10-20T08:00:00+24:00", "NUMERICAL_TIME_INVALID"),
    ("2026-10-20T08:00:00+12:99", "NUMERICAL_TIME_INVALID"),
    (1, "NUMERICAL_TIME_INVALID"),
])
def test_invalid_declared_bounds_are_rejected_before_source_reads(field, value, prefix):
    payload, reader = timed_spec(), TimedReads()
    payload[field] = value
    with pytest.raises(RecordError, match=prefix + ":" + field):
        NumericalAudit(reader).audit(payload)
    assert reader.calls == 0
    payload.pop("sources")
    with pytest.raises(RecordError, match=prefix + ":" + field):
        validate_numerical_workpaper(payload)


def test_delivery_envelope_validation_is_exact_and_source_free():
    payload = timed_spec()
    payload.pop("sources")
    assert validate_numerical_workpaper(payload) is None
    with pytest.raises(RecordError, match="INVALID_NUMERICAL_SHAPE"):
        validate_numerical_workpaper({**payload, "sources": []})
    with pytest.raises(RecordError, match="INVALID_NUMERICAL_SHAPE"):
        validate_numerical_workpaper({key: value for key, value in payload.items() if key != "knowledge_cutoff"})
    with pytest.raises(RecordError, match="INVALID_NUMERICAL_SHAPE"):
        validate_numerical_workpaper({**payload, "schema": "research-numbers-v1"})
    with pytest.raises(RecordError, match="NUMERICAL_CUTOFF_AFTER_EVALUATION"):
        validate_numerical_workpaper({**payload, "evaluated_at": "2026-10-20T11:59:59+08:00"})
    legacy = spec()
    legacy.pop("sources")
    assert validate_numerical_workpaper(legacy) is None


def test_bad_source_time_does_not_erase_other_source_or_explicit_assumption():
    payload = timed_spec()
    payload["sources"].append(asdict(OTHER_REF))
    payload["inputs"][1]["source"] = 1
    payload["inputs"].append({"id": "assumed", "value": "2", "unit": "ratio", **CONTEXT,
                              "assumption": "Synthetic assumption is not historical evidence"})
    payload["calculations"].extend([
        {"id": "other-source", "operation": "identity", "operands": ["cost"], "unit": "CNY_10K", **CONTEXT},
        {"id": "illustration", "operation": "identity", "operands": ["assumed"], "unit": "ratio", **CONTEXT},
    ])
    reader = TimedReads({**TIMES, "stored_at": "2026-10-20T13:00:00+08:00"})
    result = NumericalAudit(reader).audit(payload)
    assert reader.calls == 2 and result["status"] == "blocked"
    assert result["inputs"][0]["error"] == "SOURCE_STORED_AFTER_CUTOFF"
    assert result["inputs"][1]["status"] == "checked"
    assert result["inputs"][2]["evidence_time"]["status"] == "not-applicable"
    assert result["calculations"][0]["error"] == "OPERAND_NOT_AVAILABLE"
    assert result["calculations"][2]["display"] == "450.00 CNY_10K"
    assert result["calculations"][3]["status"] == "conditional"
    assert result["calculations"][3]["display"] == "2.00 ratio"


def test_same_values_from_distinct_retained_references_have_same_calculation():
    first, second = timed_spec(), timed_spec()
    second["sources"] = [asdict(OTHER_REF)]
    a, b = NumericalAudit(TimedReads()).audit(first), NumericalAudit(TimedReads()).audit(second)
    assert a["calculations"] == b["calculations"] and a["input_sha256"] != b["input_sha256"]
    assert a["sources"][0]["reference"] != b["sources"][0]["reference"]
    assert a["source_authenticity"] == b["source_authenticity"] == "not-certified"


def test_saved_v2_uses_its_fixed_times_and_detects_display_mismatch():
    original = NumericalAudit(TimedReads()).audit(timed_spec())
    reader = saved_reader("Unmarked prose." + original["appendix"])
    later = NumericalAudit(reader).saved(REVISION)
    assert later["saved_render"] == "matches-recalculation"
    assert later["appendix"] == original["appendix"] and later["evaluated_at"] == timed_spec()["evaluated_at"]
    reader.revision["body"] = reader.revision["body"].replace("150.00 CNY_10K", "15.00 CNY_10K", 1)
    mismatch = NumericalAudit(reader).saved(REVISION)
    assert mismatch["status"] == "blocked" and mismatch["error"] == "SAVED_NUMERICAL_DISPLAY_MISMATCH"


def test_historical_v1_can_still_quote_future_markers_in_ordinary_template_text():
    payload = spec()
    payload["template"] = ("Quoted future marker: <!--decision-os-numbers-v2:input -->\n"
                           "\n<!-- decision-os-numbers-v2:start -->\n"
                           "Original calculation {{number:gross}}")
    original = NumericalAudit(Reads()).audit(payload)
    reader = saved_reader(original["appendix"])
    restored = NumericalAudit(reader).saved(REVISION)
    assert restored["saved_render"] == "matches-recalculation"
    assert restored["appendix"] == original["appendix"]


def test_v2_canonical_json_string_values_are_data_not_appendix_markers():
    payload = timed_spec()
    payload["calculations"][0]["context_bridge"] = {
        "period": "Literal <!-- decision-os-numbers-v2:input --> is ordinary explanation text"}
    original = NumericalAudit(TimedReads()).audit(payload)
    restored = NumericalAudit(saved_reader(original["appendix"])).saved(REVISION)
    assert restored["saved_render"] == "matches-recalculation" and restored["status"] == "conditional"


@pytest.mark.parametrize("damage", ["partial", "input-only", "mixed", "duplicate", "wrong-input",
                                    "wrong-schema", "extra-partial", "damaged-end"])
def test_damaged_mixed_or_wrong_version_markers_are_never_not_declared(damage):
    body = NumericalAudit(TimedReads()).audit(timed_spec())["appendix"]
    if damage == "partial":
        body = "A report\n<!-- decision-os-numbers-v2:start"
    elif damage == "input-only":
        body = "A report\n<!-- decision-os-numbers-v2:input -->\n```json\n{}"
    elif damage == "mixed":
        body += NumericalAudit(Reads()).audit(spec())["appendix"]
    elif damage == "duplicate":
        body += body
    elif damage == "wrong-input":
        body = body.replace("decision-os-numbers-v2:input", "decision-os-numbers-v1:input")
    elif damage == "wrong-schema":
        body = body.replace('"schema":"research-numbers-v2"', '"schema":"research-numbers-v1"')
    elif damage == "extra-partial":
        body += "\n<!--decision-os-numbers-v2:"
    elif damage == "damaged-end":
        body = body.replace("decision-os-numbers-v2:end -->", "decision-os-numbers-v2:end")
    assert has_numerical_appendix(body)
    with pytest.raises(RecordError, match="INVALID_NUMERICAL_APPENDIX"):
        NumericalAudit(saved_reader(body)).saved(REVISION)


def test_v2_template_cannot_smuggle_a_second_partial_appendix():
    payload = timed_spec()
    payload["template"] = "<!--decision-os-numbers-v2:start"
    with pytest.raises(RecordError, match="RESERVED_NUMERICAL_MARKER"):
        NumericalAudit(TimedReads()).audit(payload)


def test_saved_bad_time_remains_use_blocked_and_not_display_tampering():
    clocks = {**TIMES, "stored_at": "2026-10-20T13:00:00+08:00"}
    result = NumericalAudit(TimedReads(clocks)).audit(timed_spec())
    restored = NumericalAudit(saved_reader(result["appendix"], clocks)).saved(REVISION)
    assert restored["status"] == "blocked"
    assert restored["saved_render"] == "not-comparable-until-inputs-and-calculations-resolved"
    assert "error" not in restored
