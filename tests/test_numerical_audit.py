"""Declared number/source/display protection; fixtures are not economic evidence."""
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal, Inexact, ROUND_UP, localcontext

import pytest

from decision_kernel.application.numerical_audit import END, INPUT, START, NumericalAudit
from decision_kernel.domain.records import (
    BlobRef, IntegrityError, MissingObject, RecordError, Selection, SourceRef, StoreUnavailable,
    encoded,
)
from decision_kernel.domain.research_numbers import Quantity, calculate, number

CONTEXT = dict(subject="synthetic-issuer", period="2025-FY", scope="consolidated")
REF = SourceRef(BlobRef("synthetic-store", "a" * 40,
                       "source-views/synthetic/" + "b" * 64 + "/manifest.json", "c" * 64))
ROW = {**CONTEXT, "unit": "CNY_10K", "revenue": "600", "cost": "450"}


class Reads:
    """Only models reader data. Separate integration tests use real Git and VersionedStore."""
    def __init__(self, data=None):
        self.data = [dict(ROW)] if data is None else data
        self.calls = 0
        self.failure = None
        self.custody = "provided"
        self.limitations = []
        self.revision = None

    def read_source(self, reference):
        self.calls += 1
        assert reference == REF
        if self.failure:
            raise self.failure
        return dict(data=deepcopy(self.data), manifest={"input": dict(custody=self.custody,
                    origin="synthetic:numeric-input", report_period="2025-FY")},
                    limitations=self.limitations, original_verified=False)

    def read_revision(self, reference):
        return self.revision


def source_input(ident, column, value):
    return {"id": ident, "value": value, "unit": "CNY_10K", **CONTEXT,
            "source": 0, "locator": {"row": 1, "column": column},
            "context_bindings": {name: {"row": 1, "column": name} for name in (*CONTEXT, "unit")}}


def spec():
    return {"schema": "research-numbers-v1", "sources": [asdict(REF)],
            "inputs": [source_input("sales", "revenue", "600"), source_input("cost", "cost", "450")],
            "calculations": [{"id": "gross", "operation": "difference", "operands": ["sales", "cost"],
                              "unit": "CNY_10K", **CONTEXT, "claimed": "150.00"},
                             {"id": "margin", "operation": "ratio", "operands": ["gross", "sales"],
                              "unit": "percent", **CONTEXT, "claimed": "25.00"}],
            "template": "合成毛利 {{number:gross}}，毛利率 {{number:margin}}；此处未标记的数字999没有被审核。"}


def q(value, unit="CNY"):
    return Quantity(Decimal(value), unit, tuple(CONTEXT.values()))


@pytest.mark.parametrize("operation,values,result", [
    ("identity", ["1.50"], "1.50"), ("sum", ["0.1", "0.2"], "0.30"),
    ("difference", ["4", "6"], "-2.00"), ("product", ["2", "3"], "6.00"),
    ("ratio", ["3", "2"], "1.50"),
])
def test_five_legacy_operations(operation, values, result):
    value, displayed, rounded = calculate(operation, [q(x, "ratio") for x in values], "ratio", 2)
    assert value == Decimal(result) and displayed == result and rounded is False


@pytest.mark.parametrize("value", [1, 1.25, True, None, "", "NaN", "sNaN", "Infinity", "1_000", "12,34",
                                   "1e129", "1e-129", "9" * 81, "1e10000", "1%", "１", "0x10"])
def test_non_decimal_or_unbounded_inputs_rejected(value):
    with pytest.raises(RecordError):
        number(value)


@pytest.mark.parametrize("text,value", [("1,234.50", "1234.5"), ("-1,234", "-1234"),
                                        ("1e3", "1000"), (" 0 ", "0")])
def test_exact_supported_source_decimal_forms(text, value):
    assert number(text) == Decimal(value)


def test_units_shares_and_global_context_are_not_silent_inputs():
    before = calculate("product", [q("9.7", "CNY/share"), q("50000", "share_10K")], "CNY_100M", 2)
    assert before == (Decimal("48.5"), "48.50", False)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert calculate("product", [q("9.7", "CNY/share"), q("50000", "share_10K")], "CNY_100M", 2) == before
        _, shown, rounded = calculate("ratio", [q("1"), q("3")], "percent", 2)
        assert shown == "33.33" and rounded
    assert calculate("sum", [q("2", "CNY_10K"), q("30000")], "CNY_10K", 2)[1] == "5.00"
    assert calculate("identity", [q("-0.00001")], "CNY", 2)[1] == "0.00"


@pytest.mark.parametrize("op,values,target,places,code", [
    ("sum", [q("1"), q("1", "USD")], "CNY", 2, "OPERAND_UNIT_MISMATCH"),
    ("product", [q("1"), q("2")], "CNY", 2, "RESULT_UNIT_MISMATCH"),
    ("ratio", [q("1"), q("0")], "ratio", 2, "ZERO_DENOMINATOR"),
    ("ratio", [q("1")], "ratio", 2, "NUMERIC_OPERATION_ARITY"),
    ("identity", [q("1"), q("2")], "CNY", 2, "NUMERIC_OPERATION_ARITY"),
    ("exec", [q("1")], "CNY", 2, "UNSUPPORTED_NUMERIC_OPERATION"),
    ("sum", [], "CNY", 2, "NUMERIC_OPERAND_LIMIT"),
    ("identity", [q("1")], "unknown", 2, "UNSUPPORTED_UNIT"),
    ("identity", [q("1")], "CNY", True, "INVALID_DECIMAL_PLACES"),
    ("identity", [q("1")], "CNY", 13, "INVALID_DECIMAL_PLACES"),
])
def test_operation_failures_have_bounded_codes(op, values, target, places, code):
    with pytest.raises(RecordError, match=code):
        calculate(op, values, target, places)


def test_source_computation_render_and_canonical_input_do_not_mutate():
    reads, payload = Reads(), spec()
    original = deepcopy(payload)
    result = NumericalAudit(reads).audit(payload)
    assert result["status"] == "checked" and reads.calls == 1
    assert result["calculations"][0]["value"] == "150"
    assert "150.00 CNY_10K" in result["table_markdown"]
    assert "25.00 percent" in result["rendered_excerpt"]
    assert "{{number:" not in result["rendered_excerpt"]
    assert result["unmarked_prose"] == "not-audited" and "999" in result["rendered_excerpt"]
    assert result["sources"][0]["reference"] == asdict(REF)
    assert result["sources"][0]["original_verified"] is False
    assert result["research_assessment"] == "not-performed" and result["persistence"] == "not-written"
    assert payload == original
    assert NumericalAudit(Reads()).audit(payload)["appendix"] == result["appendix"]


@pytest.mark.parametrize("field,value", [("unit", "CNY"), ("subject", "other-issuer"),
                                        ("period", "2025-H1"), ("scope", "parent-only")])
def test_source_unit_subject_period_scope_mismatch_is_not_certified(field, value):
    payload = spec()
    payload["inputs"][0][field] = value
    result = NumericalAudit(Reads()).audit(payload)
    assert result["status"] == "blocked"
    assert result["inputs"][0]["error"] == "SOURCE_CONTEXT_MISMATCH:" + field
    assert result["inputs"][1]["status"] == "checked"
    assert "UNAVAILABLE:gross" in result["rendered_excerpt"]


def test_wrong_value_wrong_display_and_forward_dependency():
    payload = spec()
    payload["inputs"][0]["value"] = "60"
    result = NumericalAudit(Reads()).audit(payload)
    assert result["inputs"][0]["error"] == "SOURCE_VALUE_MISMATCH"
    payload = spec()
    payload["calculations"][0]["claimed"] = "1500000"
    result = NumericalAudit(Reads()).audit(payload)
    assert result["calculations"][0]["error"] == "CLAIMED_DISPLAY_MISMATCH"
    assert result["calculations"][0]["display_number"] == "150.00"
    assert result["calculations"][1]["error"] == "OPERAND_NOT_AVAILABLE"
    payload = spec()
    payload["calculations"].reverse()
    result = NumericalAudit(Reads()).audit(payload)
    assert result["calculations"][0]["error"] == "OPERAND_NOT_AVAILABLE"
    assert result["calculations"][1]["status"] == "checked"


@pytest.mark.parametrize("error,code", [(MissingObject("secret"), "SOURCE_MISSING"),
    (StoreUnavailable("secret"), "SOURCE_UNAVAILABLE"), (IntegrityError("secret"), "SOURCE_INTEGRITY_FAILED"),
    (RecordError("secret"), "SOURCE_VALIDATION_FAILED")])
def test_missing_denied_corrupt_source_is_not_empty_or_zero(error, code):
    reads = Reads(); reads.failure = error
    payload = spec()
    payload["inputs"].append({"id": "assumed", "value": "2", "unit": "ratio", **CONTEXT,
                              "assumption": "Explicit synthetic illustration, not evidence"})
    payload["calculations"].append({"id": "independent", "operation": "identity", "operands": ["assumed"],
                                    "unit": "ratio", **CONTEXT})
    result = NumericalAudit(reads).audit(payload)
    assert result["inputs"][0]["error"] == code and reads.calls == 1
    assert "secret" not in str(result)
    assert result["calculations"][-1]["status"] == "conditional"
    assert result["calculations"][-1]["display_number"] == "2.00"


def test_missing_context_is_a_declaration_not_a_verified_source_field():
    payload = spec()
    payload["inputs"][0].pop("context_bindings")
    result = NumericalAudit(Reads()).audit(payload)
    assert result["status"] == "conditional"
    assert "CONTEXT_DECLARED_NOT_SOURCE_CHECKED:unit" in result["calculations"][1]["warnings"]


def test_context_from_another_row_cannot_authenticate_a_number():
    payload = spec()
    payload["inputs"][0]["context_bindings"]["subject"]["row"] = 2
    result = NumericalAudit(Reads([ROW, ROW])).audit(payload)
    assert result["inputs"][0]["error"] == "CONTEXT_ROW_MISMATCH"


@pytest.mark.parametrize("field", ["subject", "period", "scope"])
def test_context_bridge_is_explicit_and_stays_conditional(field):
    payload = spec()
    payload["calculations"][0][field] = "different-declared-context"
    result = NumericalAudit(Reads()).audit(payload)
    assert result["calculations"][0]["error"] == "CALCULATION_CONTEXT_MISMATCH:" + field
    payload["calculations"][0]["context_bridge"] = {field: "Explicit mapping requiring economic review"}
    payload["calculations"][1]["context_bridge"] = {field: "Explicit return to original context"}
    result = NumericalAudit(Reads()).audit(payload)
    assert result["status"] == "conditional"
    assert "DECLARED_CONTEXT_BRIDGE_REQUIRES_REVIEW" in result["calculations"][1]["warnings"]


def test_exact_text_span_and_custody_are_not_pdf_authentication():
    payload = spec(); payload.pop("template")
    payload["inputs"] = [{"id": "text-value", "value": "50", "unit": "CNY", **CONTEXT,
                           "source": 0, "locator": {"start": 3, "end": 5}}]
    payload["calculations"] = [{"id": "copied", "operation": "identity", "operands": ["text-value"],
                               "unit": "CNY", **CONTEXT}]
    reads = Reads("收入 50 元"); reads.custody = "excerpt"; reads.limitations = ["Not an original document"]
    result = NumericalAudit(reads).audit(payload)
    assert result["status"] == "conditional" and result["calculations"][0]["display"] == "50.00 CNY"
    assert "SOURCE_IS_EXCERPT_OR_LOCATOR" in result["calculations"][0]["warnings"]
    payload["inputs"][0]["value"] = "5"; payload["inputs"][0]["locator"]["end"] = 4
    assert NumericalAudit(reads).audit(payload)["inputs"][0]["error"] == "PARTIAL_NUMERIC_TOKEN"
    reads.data = None
    assert NumericalAudit(reads).audit(payload)["inputs"][0]["error"] == "SOURCE_VALUE_NOT_STORED"


@pytest.mark.parametrize("change", ["duplicate", "float", "source-limit", "template", "script", "reserved"])
def test_invalid_envelopes_or_scripts_never_become_execution(change):
    payload = spec()
    if change == "duplicate": payload["inputs"].append(deepcopy(payload["inputs"][0]))
    if change == "float": payload["unexpected"] = 1.2
    if change == "source-limit": payload["sources"] *= 65
    if change == "template": payload["template"] = "{{number:unknown}}"
    if change == "script": payload["script"] = "os.system('echo not executed')"
    if change == "reserved": payload["template"] = START
    reads = Reads()
    with pytest.raises(RecordError):
        NumericalAudit(reads).audit(payload)
    assert reads.calls == 0


def test_saved_recalculation_checks_retained_refs_and_generated_display():
    reads, payload = Reads(), spec()
    original = NumericalAudit(reads).audit(payload)
    revision = BlobRef("synthetic-store", "d" * 40, "research/cases/synthetic/revisions/r1/record.json", "e" * 64)
    reads.revision = {"body": "Original prose, outside declared numerical audit." + original["appendix"],
                      "record": {"input": {"sources": [asdict(REF)]}}}
    assert NumericalAudit(reads).saved(revision)["saved_render"] == "matches-recalculation"
    reads.revision["body"] = reads.revision["body"].replace("150.00 CNY_10K", "150.00 CNY_100M", 1)
    result = NumericalAudit(reads).saved(revision)
    assert result["status"] == "blocked" and result["error"] == "SAVED_NUMERICAL_DISPLAY_MISMATCH"
    reads.revision["record"]["input"]["sources"] = []
    with pytest.raises(RecordError, match="NUMERICAL_SOURCE_NOT_RETAINED"):
        NumericalAudit(reads).saved(revision)
    reads.revision["body"] = "No numerical appendix. Not a pass."
    assert NumericalAudit(reads).saved(revision)["status"] == "not-declared"


def test_saved_input_failure_does_not_accuse_unavailable_source_of_display_tampering():
    reads = Reads()
    appendix = NumericalAudit(reads).audit(spec())["appendix"]
    reads.revision = {"body": appendix, "record": {"input": {"sources": [asdict(REF)]}}}
    reads.failure = StoreUnavailable("denied")
    result = NumericalAudit(reads).saved(REF.manifest)
    assert result["status"] == "blocked" and result["saved_render"].startswith("not-comparable")


def test_real_git_saved_revision_uses_old_source_not_latest(tmp_path):
    from decision_kernel.infrastructure.git_store import GitStore
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.domain.records import SourceInput, RevisionInput
    backend = GitStore.init_synthetic(tmp_path / "records.git")
    store = VersionedStore(backend)
    csv = "subject,period,scope,unit,revenue,cost\nsynthetic-issuer,2025-FY,consolidated,CNY_10K,600,450\n"
    source = SourceInput("numbers", "dataset", "Synthetic numeric evidence", "synthetic:numeric", csv)
    ref = store.put_source("s1", source)
    payload = spec(); payload["sources"] = [asdict(ref)]
    audited = NumericalAudit(store).audit(payload)
    saved = store.submit_revision("r1", RevisionInput("synthetic", "r1", "full", "Synthetic only" + audited["appendix"],
                                  "synthetic mandate", "synthetic method", (ref,)))
    before = backend.head()
    assert NumericalAudit(store).saved(saved)["saved_render"] == "matches-recalculation"
    assert backend.head() == before
    store.put_source("s2", replace(source, text=csv.replace(",600,450", ",900,450")))
    old = NumericalAudit(store).saved(saved)
    assert old["calculations"][0]["display_number"] == "150.00"
    assert old["sources"][0]["reference"] == asdict(ref)
    changed = store.submit_revision("r2", RevisionInput("synthetic", "r2", "full",
        audited["appendix"].replace("25.00 percent", "2500.00 percent", 1),
        "synthetic mandate", "synthetic method", (ref,), saved))
    assert NumericalAudit(store).saved(changed)["error"] == "SAVED_NUMERICAL_DISPLAY_MISMATCH"
    assert NumericalAudit(store).saved(saved)["saved_render"] == "matches-recalculation"
