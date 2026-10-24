"""Exact saved external evidence adoption; synthetic I/O never accepts research quality."""
from dataclasses import asdict, replace

import pytest

from decision_kernel.application.research_external_use import external_source_guidance
from decision_kernel.application.research_source_use import (
    SourceUseAudit, acceptance_pending, audit_demonstrated, execution_contract,
)
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    BlobRef, IntegrityError, MissingObject, RecordError, RevisionInput,
    Selection, SourceInput, StoreUnavailable, encoded,
)
from decision_kernel.domain.research_work import (
    ResearchProgress, ResearchQuestion, ResearchRequest, render_workpaper,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.methods.research import EXTERNAL_METHOD_ID, research_method


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "external-use.git"))


def request(kind="full"):
    return ResearchRequest("external-use", kind, "Synthetic external intake, no investment mandate",
                           (ResearchQuestion("cash", "Explain the synthetic cash evidence"),))


def external_method():
    return research_method("full", method_id=EXTERNAL_METHOD_ID)


def add_source(store, ident="one", custody="provided"):
    return store.put_source("source-" + ident, SourceInput(
        ident, "content", "Synthetic external representation", "synthetic:external-use",
        None if custody == "external" else "Synthetic retained first line\nSynthetic selected second line\n",
        custody=custody,
        limitations=("Only the authorized excerpt or locator is retained",) if custody != "provided" else (),
    ))


def progress(*refs, evidence=(0,), challenge_evidence=()):
    return ResearchProgress.from_dict({
        "body": "Synthetic partial analysis, not completed research",
        "findings": [{"question_id": "cash", "status": "open", "finding": "Economic work remains",
                      "remaining_kind": "acquired-unanalysed", "next_action": "Analyze the evidence",
                      "evidence": list(evidence)}],
        "challenges": ([{"question_id": "cash", "argument": "A synthetic contrary reading remains",
                         "disposition": "unresolved", "response": "Needs further economic assessment",
                         "evidence": list(challenge_evidence)}] if challenge_evidence else []),
        "sources": [asdict(ref) for ref in refs], "stop": "continue", "stop_reason": "Work remains",
        "limitations": ["Synthetic material does not establish a real study"],
    })


def save(store, paper, *, req=None, method=None):
    req = req or request()
    method = method or external_method()
    return store.submit_revision("external-checkpoint", RevisionInput(
        req.case_id, "v1", req.kind, render_workpaper(req, paper, method), req.mandate,
        method["id"] + "@sha256:" + method["sha256"], paper.sources,
        unresolved=paper.unresolved(req),
    ))


class ReadProbe:
    """Only existing read capabilities; record exact reads and simulate a lost source."""
    def __init__(self, store, *, unavailable=None, error=StoreUnavailable):
        self.store, self.unavailable, self.error = store, unavailable, error
        self.revisions, self.sources = [], []

    def read_revision(self, ref):
        self.revisions.append(ref)
        return self.store.read_revision(ref)

    def read_source(self, ref):
        self.sources.append(ref)
        if ref == self.unavailable:
            raise self.error("synthetic private diagnostic must not be returned")
        return self.store.read_source(ref)


@pytest.mark.parametrize("custody,availability,demonstrated", [
    ("provided", "stored-representation", True),
    ("excerpt", "stored-representation", True),
    ("external", "external-only", False),
])
def test_saved_representations_are_read_once_without_native_source_work(
        store, monkeypatch, custody, availability, demonstrated):
    ref = add_source(store, custody=custody)
    if custody != "external":
        ref = replace(ref, selection=Selection("lines", 2, 2))
    saved = save(store, progress(ref))
    probe = ReadProbe(store)
    auditor = SourceUseAudit(probe)
    monkeypatch.setattr(auditor, "_pool_recovery", lambda *args: pytest.fail("default source path was used"))
    before = store.backend.head()
    result = auditor.audit({"reference": asdict(saved)})
    assert probe.revisions == [saved] and probe.sources == [ref]
    assert store.backend.head() == before
    assert result["schema"] == "research-external-source-use-audit-v1"
    assert audit_demonstrated(result) is demonstrated
    assert result["status"] == ("external-evidence-use-demonstrated" if demonstrated else "needs-action")
    assert result["gaps"] == ([] if demonstrated else ["NO_READABLE_ADOPTED_SOURCE"])
    row = result["sources"][0]
    assert row["reference"] == asdict(ref) and row["custody"] == custody
    assert row["status"] == availability and row["adopted_by_author"] and not row["original_verified"]
    assert row["limitation_count"] == (0 if custody == "provided" else 1)
    assert row["limitations_operation"] == "read-source"
    assert b"Synthetic selected second line" not in encoded(result)
    assert "pool" not in result and "relay" not in result
    assert result["publication"] == "not-authorized-by-audit"
    assert result["source_rights"] == "not-granted-by-audit" and result["persistence"] == "not-written"
    assert result["default_full_acceptance"] == "not-assessed"
    assert result["full_product_acceptance"] == result["research_assessment"] == "not-performed"
    assert result["human_acceptance"] == "not-confirmed"


def test_challenge_adoption_does_not_require_every_auxiliary_source_to_be_adopted(store):
    refs = [add_source(store, "evidence"), add_source(store, "unused", "excerpt"),
            add_source(store, "locator", "external")]
    saved = save(store, progress(*refs, evidence=(), challenge_evidence=(0,)))
    probe = ReadProbe(store)
    result = SourceUseAudit(probe).audit({"reference": asdict(saved)})
    assert audit_demonstrated(result) and result["gaps"] == []
    assert [row["adopted_by_author"] for row in result["sources"]] == [True, False, False]
    assert result["sources"][2]["status"] == "external-only"
    assert result["sources"][2]["limitation_count"] == 1
    assert probe.sources == refs and probe.revisions == [saved]
    assert result["acceptance_scope"] == "retained-representation-and-author-adoption-only"


@pytest.mark.parametrize("has_source", [False, True])
def test_absent_or_unadopted_sources_leave_saved_partial_work_pending(store, has_source):
    refs = [add_source(store)] if has_source else []
    saved = save(store, progress(*refs, evidence=()))
    result = SourceUseAudit(store).audit({"reference": asdict(saved)})
    assert result["status"] == "needs-action" and not audit_demonstrated(result)
    assert "NO_SOURCE_ADOPTED" in result["gaps"] and "NO_READABLE_ADOPTED_SOURCE" in result["gaps"]
    assert ("NO_SOURCE_REFERENCES" in result["gaps"]) is not has_source
    assert store.current(request().case_id) == saved
    pending = acceptance_pending(asdict(saved), method=external_method())
    assert pending["status"] == "not-demonstrated" and pending["partial_research_save_allowed"]


@pytest.mark.parametrize("error", [MissingObject, StoreUnavailable, IntegrityError])
def test_unreadable_reference_is_safe_and_blocks_despite_other_adopted_evidence(store, error):
    good, unavailable = add_source(store, "good"), add_source(store, "unavailable", "excerpt")
    saved = save(store, progress(good, unavailable))
    before = store.backend.head()
    probe = ReadProbe(store, unavailable=unavailable, error=error)
    result = SourceUseAudit(probe).audit({"reference": asdict(saved)})
    assert result["status"] == "needs-action" and result["gaps"] == ["SOURCE_REFERENCE_UNAVAILABLE"]
    assert result["sources"][1]["status"] == "unavailable"
    assert result["sources"][1]["error"] == "SOURCE_USE_SOURCE_UNAVAILABLE"
    assert b"private diagnostic" not in encoded(result)
    assert probe.sources == [good, unavailable] and store.backend.head() == before


@pytest.mark.parametrize("extra,value", [
    ("receipts", []), ("exceptions", {}), ("reuse_run_ids", []), ("relay_recoveries", []),
])
def test_external_audit_rejects_receipt_and_exception_overrides_even_when_empty(store, extra, value):
    saved = save(store, progress(add_source(store)))
    probe = ReadProbe(store)
    with pytest.raises(RecordError, match="^EXTERNAL_SOURCE_USE_REFERENCE_ONLY$"):
        SourceUseAudit(probe).audit({"reference": asdict(saved), extra: value})
    assert probe.revisions == [saved] and probe.sources == []


@pytest.mark.parametrize("variant", ["wrong-id", "wrong-digest", "wrong-kind", "default-full"])
def test_untrusted_or_default_method_identity_does_not_route_to_external_audit(store, variant):
    method, req = external_method(), request()
    if variant == "wrong-id":
        method = {**method, "id": method["id"] + "-untrusted"}
    elif variant == "wrong-digest":
        method = {**method, "sha256": "0" * 64}
    elif variant == "wrong-kind":
        req = request("quick")
    else:
        method = research_method("full")
    saved = save(store, progress(add_source(store)), req=req, method=method)
    probe = ReadProbe(store)
    result = SourceUseAudit(probe).audit({"reference": asdict(saved)})
    assert result["schema"] == "research-source-use-audit-v1" and result["status"] == "needs-action"
    assert "POOL_USE_NOT_COMPLETE" in result["gaps"]
    assert "RELAY_CAPTURE_OR_REUSE_NOT_DEMONSTRATED" in result["gaps"]
    assert probe.revisions == [saved] and not audit_demonstrated(result)


def test_known_external_token_without_a_workpaper_cannot_shortcut_the_audit(store):
    method, req, ref = external_method(), request(), add_source(store)
    saved = store.submit_revision("plain-revision", RevisionInput(
        req.case_id, "plain", "full", "Ordinary free-form research remains saveable", req.mandate,
        method["id"] + "@sha256:" + method["sha256"], (ref,),
    ))
    probe = ReadProbe(store)
    with pytest.raises(RecordError, match="^SOURCE_USE_WORKPAPER_REQUIRED$"):
        SourceUseAudit(probe).audit({"reference": asdict(saved)})
    assert probe.revisions == [saved] and probe.sources == []


def test_method_aware_guidance_keeps_default_outputs_and_separate_external_boundaries():
    req, method = request(), external_method()
    reference = asdict(BlobRef("synthetic:research", "a" * 40,
                              "research/cases/external-use/revisions/v1/record.json", "b" * 64))
    default = execution_contract(req)
    assert default == execution_contract(req, method=research_method("full"))
    assert default["schema"] == "full-execution-contract-v1" and default["pool"] == "not-executed"
    assert acceptance_pending(reference) == {
        "status": "not-demonstrated", "operation": "audit-source-use", "reference": reference,
        "reason": "saved-research-or-inventory-is-not-proof-of-pool-and-relay-use",
        "partial_research_save_allowed": True,
    }
    fake = {**method, "sha256": "0" * 64}
    assert execution_contract(req, method=fake) == default
    assert acceptance_pending(reference, method=fake) == acceptance_pending(reference)
    contract = execution_contract(req, method=method)
    pending = acceptance_pending(reference, method=method)
    assert contract["schema"] == "external-full-execution-contract-v1"
    assert contract["method"] == pending["method"] == {"id": method["id"], "sha256": method["sha256"]}
    assert "pool" not in contract and "relay" not in contract
    assert contract["required_actions"][-1]["operation"] == "audit-source-use"
    assert contract["required_actions"][-1]["input"] == "reference"
    assert contract["workflow_acceptance"] == pending["status"] == "not-demonstrated"
    assert contract["partial_research_save_allowed"] and not contract["automatic_research"]
    assert contract["publication_permission"] == "not-granted"
    assert contract["default_full_acceptance"] == pending["default_full_acceptance"] == "not-assessed"
    assert contract["human_acceptance"] == pending["human_acceptance"] == "not-confirmed"
    guidance = external_source_guidance()
    assert "registry" not in guidance and "source_workflow" not in guidance
    assert guidance["publication_permission"] == "not-granted"


def test_audit_success_is_bound_to_its_exact_schema_and_status():
    default = {"schema": "research-source-use-audit-v1", "status": "usage-demonstrated"}
    external = {"schema": "research-external-source-use-audit-v1", "status": "external-evidence-use-demonstrated"}
    assert audit_demonstrated(default) and audit_demonstrated(external)
    for result in (
        {**default, "status": external["status"]}, {**external, "status": default["status"]},
        {**external, "status": "needs-action"}, {"status": external["status"]},
        {"schema": [], "status": external["status"]}, {}, None, [],
    ):
        assert not audit_demonstrated(result)
