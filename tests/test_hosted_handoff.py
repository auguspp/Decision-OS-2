"""Real local synthetic Git saves, exact reopens and content/identity rejection."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from decision_kernel.application import hosted_handoff as handoff
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, RecordError, RevisionInput, digest
from decision_kernel.infrastructure.git_store import GitStore

AT = "2026-10-19T19:00:00+08:00"


@pytest.fixture
def saved(tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path/"records.git"))
    ref = store.submit_revision("synthetic-quick", RevisionInput(case_id="test-quick", revision_id="r1",
        kind="quick", body="合成研究：增长不等于现金；下一披露才能检验。", mandate="synthetic test",
        method="synthetic", unresolved=("No real external evidence",)))
    quick = {"schema": handoff.SCHEMA, "kind": "quick", "execution_date": "2026-10-19",
        "cutoff": AT, "research": asdict(ref), "scope": "Synthetic no-ticker discovery test", "gaps": ["Not real financial research"]}
    return store, ref, quick


def brief(store, ref):
    return {"schema": handoff.SCHEMA, "kind": "brief", "execution_date": "2026-10-19", "cutoff": AT,
        "scope": "Synthetic actual research adoption", "gaps": [], "baseline": None,
        "body": "这是合成Brief，实际采用已保存Quick的增长与现金分歧，而非复制标题。",
        "adoptions": [{"reference": asdict(ref), "body_sha256": digest(store.read_revision(ref)["body"].encode()),
                       "used_change": "Keep the specific cash-flow challenge"}]}


def test_saved_quick_reference_restores_only_canonical_research(saved):
    store, ref, doc = saved
    result = handoff.save(store, doc, "quick-handoff")
    fresh = VersionedStore(GitStore(store.backend.root))
    opened = handoff.open_handoff(fresh, result["reference"])
    assert opened["research"][0]["reference"] == asdict(ref)
    assert "现金" in opened["research"][0]["body"]
    assert "body" not in opened["document"]
    assert opened["economic_quality"] == "not-certified-by-reader"


def test_actual_brief_adoption_and_independent_reopen(saved):
    store, ref, doc = saved
    handoff.save(store, doc, "quick-handoff")
    b = brief(store, ref)
    result = handoff.save(store, b, "brief-handoff")
    fresh = VersionedStore(GitStore(store.backend.root))
    opened = handoff.open_handoff(fresh, result["reference"])
    assert opened["document"]["body"] == b["body"]
    assert opened["research"][0]["body"] == store.read_revision(ref)["body"]
    assert opened["human_acceptance"] == "not-confirmed"


def test_replay_same_intent_no_new_commit(saved):
    store, _, doc = saved
    first = handoff.save(store, doc, "same")
    head = store.backend.head()
    assert handoff.save(store, doc, "same") == first
    assert store.backend.head() == head


def test_different_text_same_day_cannot_overwrite_or_rename(saved):
    store, ref, _ = saved
    doc = brief(store, ref)
    handoff.save(store, doc, "first")
    head = store.backend.head()
    doc["body"] += "Changed."
    with pytest.raises(Conflict, match="HOSTED_DAY_ALREADY_SAVED"):
        handoff.save(store, doc, "second")
    assert store.backend.head() == head


def test_wrong_adopted_body_rejected_before_commit(saved):
    store, ref, _ = saved
    doc = brief(store, ref); doc["adoptions"][0]["body_sha256"] = "f"*64
    head = store.backend.head()
    with pytest.raises(RecordError, match="HOSTED_ADOPTED_BODY_MISMATCH"):
        handoff.save(store, doc, "bad")
    assert store.backend.head() == head


def test_future_baseline_rejected(saved):
    store, ref, _ = saved
    prior = brief(store, ref)
    p = handoff.save(store, prior, "prior")
    doc = brief(store, ref); doc["execution_date"] = "2026-10-18"
    doc["cutoff"] = "2026-10-18T19:15:00+08:00"; doc["baseline"] = p["reference"]
    with pytest.raises(RecordError, match="HOSTED_BASELINE_FROM_FUTURE"):
        handoff.save(store, doc, "bad")


def test_no_handoff_is_not_task_failed_and_late_quick_remains_visible(saved):
    store, _, doc = saved
    assert handoff.latest(store, kind="quick", as_of=AT)["status"] == "no-saved-handoff"
    doc["cutoff"] = "2026-10-19T19:30:00+08:00"
    handoff.save(store, doc, "late")
    early = handoff.latest(store, kind="quick", as_of=AT)
    assert early["status"] == "partial" and early["gaps"] and not early["items"]
    next_day = handoff.latest(store, kind="quick", as_of="2026-10-20T19:15:00+08:00")
    assert len(next_day["items"]) == 1 and next_day["items"][0]["document"]["cutoff"] == doc["cutoff"]


def test_wrong_kind_baseline_rejected(saved):
    store, ref, quick = saved
    q = handoff.save(store, quick, "q")
    b = brief(store, ref); b["baseline"] = q["reference"]
    with pytest.raises(RecordError, match="WRONG_KIND"):
        handoff.save(store, b, "b")


@pytest.mark.parametrize("field,value", [("schema", "wrong"), ("kind", "trade"), ("execution_date", "bad"),
    ("cutoff", "2026-10-19"), ("scope", ""), ("gaps", "not a list")])
def test_invalid_header(saved, field, value):
    _, _, doc = saved; doc[field] = value
    with pytest.raises((RecordError, ValueError)):
        handoff.validate(doc)


def test_extra_investment_authority_not_accepted(saved):
    _, _, doc = saved; doc["human_acceptance"] = True
    with pytest.raises(RecordError):
        handoff.validate(doc)


def test_unrelated_artifacts_are_not_quick(saved):
    store, ref, _ = saved
    b = brief(store, ref)
    handoff.save(store, b, "b")
    assert not handoff.latest(store, kind="quick", as_of=AT)["items"]


def test_workflow_routes_new_save_only_to_writer():
    # This guards permission routing, not runtime business behavior or real credentials.
    text = (Path(__file__).parents[1]/".github/workflows/research-runtime.yml").read_text()
    assert "inputs.operation != 'save-hosted-handoff'" in text
    assert "inputs.operation == 'save-hosted-handoff'" in text
    assert "reconcile-hosted-handoff" in text
