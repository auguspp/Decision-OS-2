"""Content hashes alone do not certify financial results or current-source substitution."""
from dataclasses import asdict
import pytest
from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.domain.records import RecordError, SourceInput
from test_conditional_odds import authorized, context, price_payload, spec


def test_valid_artifact_hash_cannot_hide_tampered_calculation(context, spec):
    from decision_kernel.application.conditional_odds import RESULT_RECORD
    store, _ = context
    service = ConditionalOdds(store)
    frozen = service.freeze(authorized("spec", spec), submission_id="freeze")
    result = service.compute({"spec_ref": frozen["reference"], "price": price_payload()})
    result["world_results"][0]["total_payoff_per_share"] = "999"
    # Custody bytes/hash are internally valid; semantic reconstruction must still reject.
    forged = service._put("forged", RESULT_RECORD, result, "Synthetic tampering regression")
    with pytest.raises(RecordError, match="RECALCULATION_MISMATCH"):
        service.read(asdict(forged))


def test_old_source_is_not_silently_replaced_after_new_version(context, spec):
    store, _ = context
    service = ConditionalOdds(store, allow_declared_only=True)
    frozen = service.freeze(authorized("spec", spec), submission_id="freeze")
    saved = service.save(authorized("calculation", {"spec_ref": frozen["reference"], "price": price_payload()}), submission_id="result")
    original = service.read(saved["reference"])["result"]
    store.put_source("new-source", SourceInput("s", "content", "Synthetic source", "synthetic:test", "Changed new source"))
    assert service.read(saved["reference"])["result"] == original
    assert service.read_spec(frozen["reference"])["spec_sha256"] == frozen["spec_sha256"]
