"""Do not certify a positive source value after dropping its sign or token prefix.

Synthetic fixtures protect the existing number audit, not company economics.
"""
from dataclasses import asdict

import pytest

from decision_kernel.application import numerical_audit
from decision_kernel.application.numerical_audit import NumericalAudit, _extract
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceInput, require
from decision_kernel.domain.research_numbers import number
from decision_kernel.infrastructure.git_store import GitStore


@pytest.mark.parametrize("text", [
    "−50", "－50", "﹣50", "–50", "—50", "➖50", "- 50", "−\u00a050",
    "-\t50", "−\u200b50", "(50)", "（50）", "( 50 )", "（\u00a050 万元）",
    "50 −", "50－", "150", "５０50", "50２", "5.50", "50.1", "50ｅ3",
    "１50", "+ 50",
])
def test_unsigned_subspan_cannot_discard_surrounding_token(text):
    start = text.index("50")
    with pytest.raises(RecordError, match="PARTIAL_NUMERIC_TOKEN"):
        _extract(text, {"start": start, "end": start + 2}, numeric=True)


@pytest.mark.parametrize("text,start,end,expected", [
    ("50", 0, 2, "50"), ("-50", 0, 3, "-50"),
    ("value=-50 CNY", 6, 9, "-50"), ("50万元", 0, 2, "50"),
    ("value 50 CNY", 6, 8, "50"), ("−\n50", 2, 4, "50"),
    ("50\n-60", 0, 2, "50"), ("a  50  b", 1, 7, "50"),
    ("1e3", 0, 3, "1e3"), ("  -50  ", 0, 7, "-50"),
])
def test_complete_ascii_tokens_units_and_source_line_boundaries(text, start, end, expected):
    assert _extract(text, {"start": start, "end": end}, numeric=True) == expected


@pytest.mark.parametrize("text,start,end", [
    ("- 50 ", 1, 5), ("x－ 50 y", 2, 6), ("1 50.0", 1, 4),
])
def test_padding_inside_locator_does_not_hide_token_boundary(text, start, end):
    with pytest.raises(RecordError, match="PARTIAL_NUMERIC_TOKEN"):
        _extract(text, {"start": start, "end": end}, numeric=True)


@pytest.mark.parametrize("text", ["−50", "－50", "(50)", "（50）"])
def test_unsupported_whole_forms_are_not_silently_normalized(text):
    # Boundary inspection is not a new accounting-number conversion feature.
    assert _extract(text, {"start": 0, "end": len(text)}, numeric=True) == text
    with pytest.raises(RecordError, match="DECIMAL_STRING_REQUIRED"):
        number(text)
    assert _extract([{"amount": text}], {"row": 1, "column": "amount"}) == text


CONTEXT = {"subject": "synthetic-issuer", "period": "2025-FY", "scope": "consolidated"}
TEXT = "Loss = −50\nRevenue = 250\n"


def _payload(reference, text=TEXT, loss="50"):
    def source_value(ident, literal):
        start = text.index(literal)
        return {"id": ident, "source": 0, "locator": {"start": start, "end": start + len(literal)},
                "value": literal, "unit": "CNY", **CONTEXT}
    def identity(ident, operand):
        return {"id": ident, "operation": "identity", "operands": [operand], "unit": "CNY", **CONTEXT}
    return {"schema": "research-numbers-v1", "sources": [asdict(reference)],
            "inputs": [source_value("loss", loss), source_value("revenue", "250")],
            "calculations": [identity("loss-display", "loss"), identity("revenue-display", "revenue"),
                             {"id": "combined", "operation": "sum", "operands": ["loss", "revenue"],
                              "unit": "CNY", **CONTEXT}],
            "template": "Synthetic: {{number:loss-display}}; {{number:revenue-display}}."}


def _store(tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "store.git"))
    source = SourceInput("signed-amount", "content", "Synthetic signed amounts", "synthetic:sign-regression", TEXT)
    reference = store.put_source("source-one", source)
    return store, reference


def test_real_git_sign_failure_blocks_dependants_but_not_other_values(tmp_path):
    store, reference = _store(tmp_path)
    before = store.backend.head()
    report = NumericalAudit(store).audit(_payload(reference))
    assert report["status"] == "blocked"
    assert report["inputs"][0]["error"] == "PARTIAL_NUMERIC_TOKEN"
    assert report["calculations"][0]["error"] == "OPERAND_NOT_AVAILABLE"
    assert report["calculations"][2]["error"] == "OPERAND_NOT_AVAILABLE"
    assert report["calculations"][1]["display"] == "250.00 CNY"
    assert report["calculations"][1]["status"] == "conditional"
    assert "[UNAVAILABLE:loss-display:" in report["rendered_excerpt"]
    assert report["source_authenticity"] == "not-certified"
    assert store.read_source(reference)["data"] == TEXT
    assert store.backend.head() == before


def test_new_source_version_does_not_repair_an_old_invalid_locator(tmp_path):
    store, old_reference = _store(tmp_path)
    corrected = TEXT.replace("−50", "-50")
    new_reference = store.put_source("source-two", SourceInput(
        "signed-amount", "content", "Synthetic signed amounts", "synthetic:sign-regression", corrected))
    before = store.backend.head()
    old = NumericalAudit(store).audit(_payload(old_reference))
    new = NumericalAudit(store).audit(_payload(new_reference, corrected, "-50"))
    assert old["status"] == "blocked"
    assert new["status"] == "conditional"
    assert new["calculations"][0]["display"] == "-50.00 CNY"
    assert new["calculations"][2]["display"] == "200.00 CNY"
    assert store.read_source(old_reference)["data"] == TEXT
    assert store.backend.head() == before


def test_saved_pre_fix_synthetic_positive_is_rejected_without_rewriting(tmp_path, monkeypatch):
    store, reference = _store(tmp_path)

    def old_boundary(data, start, end):
        # Exact pre-fix predicate, only to construct a synthetic legacy appendix.
        require((start == 0 or data[start - 1] not in "0123456789.,+-eE")
                and (end == len(data) or data[end] not in "0123456789.,eE"), "PARTIAL_NUMERIC_TOKEN")

    with monkeypatch.context() as historical:
        historical.setattr(numerical_audit, "_check_numeric_span", old_boundary)
        old_audit = NumericalAudit(store).audit(_payload(reference))
    assert old_audit["status"] == "conditional"
    assert old_audit["calculations"][0]["display"] == "50.00 CNY"
    body = "Synthetic pre-fix numerical appendix." + old_audit["appendix"]
    saved = store.submit_revision("legacy-save", RevisionInput(
        "sign-regression", "legacy", "full", body, "Synthetic regression", "synthetic:legacy", (reference,)))
    before = store.backend.head()
    new_audit = NumericalAudit(store).saved(saved)
    assert new_audit["status"] == "blocked"
    assert new_audit["saved_render"] == "not-comparable-until-inputs-and-calculations-resolved"
    assert new_audit["inputs"][0]["error"] == "PARTIAL_NUMERIC_TOKEN"
    assert store.read_revision(saved)["body"] == body
    assert store.backend.head() == before
