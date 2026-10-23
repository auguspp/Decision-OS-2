"""A Full Watch check reaches the author Brief and an independent retained reader."""
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from decision_kernel.application import hosted_handoff as handoff
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.watch_activity import WatchActivity
from decision_kernel.application.watch_evidence import export_required
from decision_kernel.domain.records import BlobRef, Conflict, RecordError, RevisionInput, digest, encoded
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure.git_store import GitStore
from test_conditional_odds import authorized, price_payload
from test_full_watch import full_watch, question_link, saved_odds, structured_full
from test_odds_watch import case, request, save
from test_watch_activity import apply, change, query
from test_watch_conditions import evidence_condition, evidence_entry, numeric, v2_input


def document(research, check_ref, result, *, question="open", kind="check"):
    change = "The unresolved cash bridge still cannot establish the shareholder cash assumption."
    return {"schema": handoff.BOUND_SCHEMA, "kind": "brief", "execution_date": "2026-01-03",
        "cutoff": "2026-01-03T05:00:00Z", "scope": "Synthetic Full Watch adoption",
        "gaps": ["Synthetic engineering evidence, not investment research"], "baseline": None,
        "body": "Author Brief. " + change, "adoptions": [], "sources": [], "watch_adoptions": [{
            "reference": research, "question_id": question, "kind": kind, "check_ref": check_ref,
            "result_sha256": digest(encoded(result)), "used_change": change}]}


def legacy_baseline(api, doc, reference, submission, *, version="hosted-ai-handoff-v1",
                    execution_date="2026-01-04", cutoff="2026-01-04T05:00:00Z"):
    """Materialise a pre-v3 Brief as an explicit replay.

    A v1/v2 Brief can no longer be written by a normal save, but read-path tests
    still need a baseline that really exists in that version instead of a
    dictionary that only looks like one.
    """
    value = {**doc, "schema": version, "execution_date": execution_date, "cutoff": cutoff,
             "baseline": reference}
    for field in ("watch_adoptions", "sources"):
        value.pop(field, None)
    legacy = VersionedStore(GitHubStore(replace(api.binding, write_enabled=True), api))
    return handoff.save(legacy, value, submission, replay=True)["reference"]


@pytest.fixture
def retained(case):
    store, _, _, service, _ = case
    research, _, source = structured_full(store)
    value = full_watch(research, kind="research", conditions=[evidence_condition()], links=[question_link()])
    watch = service.freeze(authorized("watch", value), submission_id="full-evidence-watch")["reference"]
    check, result = save(service, request(store, watch,
        price=v2_input(evidence=[evidence_entry(source, "cannot-determine")])), "full-evidence-check")
    return store, service, watch, research, check, result, document(research, check, result)


def test_full_question_unknown_is_adopted_in_same_body_and_independently_restored(retained):
    store, _, _, research, check, result, doc = retained
    quick = store.submit_revision("quick", RevisionInput("discovery", "r1", "quick",
        "A separate discovery remains readable by Brief.", "Synthetic", "synthetic", unresolved=("Synthetic only",)))
    doc["adoptions"] = [{"reference": asdict(quick), "body_sha256": digest(store.read_revision(quick)["body"].encode()),
                         "used_change": "Brief may also adopt normal Quick research."}]
    receipt = handoff.save(store, doc, "brief")
    head = store.backend.head()
    fresh = VersionedStore(GitStore(store.backend.root))
    opened = handoff.open_handoff(fresh, receipt["reference"])
    adopted = opened["watch_evidence_adoption"][0]
    assert opened["document"] == doc and opened["research"][0]["reference"] == asdict(quick)
    assert adopted["result"] == result and adopted["result"]["status"] == "cannot-determine"
    assert adopted["selection"]["question_id"] == "open" and adopted["selection"]["check_ref"] == check
    assert adopted["definition"]["tracking"]["question_links"][0]["affected_inputs"] == ["cash", "world-value"]
    assert handoff.latest(fresh, kind="brief", as_of=doc["cutoff"])["items"][0] == opened
    assert fresh.backend.head() == head and store.current("questions") == BlobRef(**research)
    assert handoff.save(store, doc, "brief") == receipt and store.backend.head() == head


@pytest.mark.parametrize("fault", ["hash", "future", "question", "missing-question", "other-research", "body", "missing-check"])
def test_inexact_or_unadopted_watch_is_refused_without_commit(retained, fault):
    store, _, _, research, _, _, doc = retained
    adoption = doc["watch_adoptions"][0]
    if fault == "hash": adoption["result_sha256"] = "0" * 64
    elif fault == "future": doc["cutoff"] = "2026-01-03T03:55:00Z"
    elif fault == "question": adoption["question_id"] = "closed"
    elif fault == "missing-question": adoption["question_id"] = None
    elif fault == "other-research": adoption["reference"] = asdict(store.current("case"))
    elif fault == "body": doc["body"] = "This author did not actually use the retained finding."
    else: adoption["check_ref"]["path"] = adoption["check_ref"]["path"].replace("odds-watch-check-", "missing-")
    head = store.backend.head()
    with pytest.raises(RecordError): handoff.save(store, doc, "invalid")
    assert store.backend.head() == head


def test_price_adoption_uses_full_odds_without_inventing_an_evidence_question(case):
    store, research, spec, service, _ = case
    value = full_watch(spec, saved_odds(store, spec), conditions=[numeric("price")])
    watch = service.freeze(authorized("watch", value), submission_id="full-price")["reference"]
    check, result = save(service, request(store, watch, price=v2_input(price_payload())), "price-check")
    doc = document(asdict(research), check, result, question=None)
    doc["body"] = "The actual retained price condition was reached; review the unchanged conditional Odds."
    doc["watch_adoptions"][0]["used_change"] = doc["body"]
    receipt = handoff.save(store, doc, "price-brief")
    opened = handoff.open_handoff(VersionedStore(GitStore(store.backend.root)), receipt["reference"])
    assert opened["watch_evidence_adoption"][0]["result"]["status"] == "first-trigger"


def test_legacy_freeform_watch_does_not_claim_new_full_question_adoption(case):
    store, research, _, service, legacy = case
    check, result = save(service, request(store, legacy, price=price_payload()), "legacy")
    doc = document(asdict(research), check, result, question=None)
    with pytest.raises(RecordError, match="HOSTED_FULL_WATCH_REQUIRED"):
        handoff.save(store, doc, "legacy-brief")


def test_administrative_event_is_not_an_actual_check_but_managed_review_is(retained):
    store, _, watch, research, _, _, doc = retained
    activity = WatchActivity(store)
    registered = apply(activity, change(watch), "register")
    doc["watch_adoptions"][0].update(kind="activity", check_ref=registered, result_sha256=digest(encoded(None)))
    with pytest.raises(RecordError, match="HOSTED_WATCH_ACTUAL_CHECK_REQUIRED"):
        handoff.save(store, doc, "not-a-check")
    active = apply(activity, change(watch, registered, "resume", "2026-01-03T04:01:00Z"), "resume")
    check = query(store, watch, active, at="2026-01-03T04:02:00Z", price=v2_input())
    check["review_reasons"] = ["The fixed cash assumption requires economic review."]
    actual = activity.save_check(authorized("check", check),
        submission_id="managed-check")["reference"]
    result = activity.read(actual, include_current_use=False)["event"]["result"]
    doc["watch_adoptions"][0].update(check_ref=actual, result_sha256=digest(encoded(result)))
    receipt = handoff.save(store, doc, "managed-brief")
    opened = handoff.open_handoff(VersionedStore(GitStore(store.backend.root)), receipt["reference"])
    assert opened["watch_evidence_adoption"][0]["result"]["status"] == "review-required"


def test_saved_adoption_remains_historical_when_research_changes_and_watch_retires(retained):
    store, _, watch, research, _, _, doc = retained
    receipt = handoff.save(store, doc, "historical-brief")
    before = handoff.open_handoff(store, receipt["reference"])
    activity = WatchActivity(store)
    registered = apply(activity, change(watch), "register")
    apply(activity, change(watch, registered, "retire", "2026-01-03T04:02:00Z"), "retire")
    store.submit_revision("successor", RevisionInput("questions", "r2", "full", "Changed assumptions",
        "Synthetic", "synthetic", predecessor=BlobRef(**research), unresolved=("Synthetic successor",)))
    fresh = VersionedStore(GitStore(store.backend.root))
    head = fresh.backend.head()
    assert handoff.open_handoff(fresh, receipt["reference"]) == before
    assert fresh.backend.head() == head


@pytest.mark.parametrize("schema", [handoff.SCHEMA, handoff.WATCH_SCHEMA])
def test_same_day_schema_change_does_not_create_another_brief(retained, schema):
    store, _, _, _, _, _, doc = retained
    handoff.save(store, doc, "first")
    doc["body"] += " Updated wording."
    doc["schema"] = schema
    doc.pop("sources")
    if schema == handoff.SCHEMA: doc.pop("watch_adoptions")
    head = store.backend.head()
    # A pre-v3 Brief is only writable as an explicit replay, so the day guard
    # is asked to hold for the replay path too.
    with pytest.raises(Conflict, match="HOSTED_DAY_ALREADY_SAVED"):
        handoff.save(store, doc, "second", replay=True)
    assert store.backend.head() == head


def test_publication_identifies_only_known_shapes_and_the_exact_baseline(retained):
    store, _, _, _, _, _, doc = retained
    receipt = handoff.save(store, doc, "baseline")
    assert export_required(doc) and export_required({"document": doc})
    assert export_required({"kind": "brief", "items": [{"document": doc}]})
    assert not export_required({"source": {"watch_adoptions": ["untrusted"]}})
    next_doc = deepcopy(doc)
    next_doc.update(execution_date="2026-01-04", cutoff="2026-01-04T05:00:00Z",
                    watch_adoptions=[], baseline=receipt["reference"])
    assert not export_required(next_doc)
    assert handoff.publication_required(store, next_doc)
    next_doc["schema"] = handoff.SCHEMA
    next_doc.pop("watch_adoptions"); next_doc.pop("sources")
    assert handoff.publication_required(store, next_doc)
    assert handoff.publication_required(store, {"kind": "brief", "items": [{"document": next_doc}]})
    assert not handoff.publication_required(store, {"source": {"document": doc}})
