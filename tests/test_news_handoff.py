"""Frozen native handoff identity and read-only recovery on a real Git store."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application import news_handoff as handoff, news_rolling_index as index
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    Conflict, MissingObject, RecordError, SourceInput, UnknownWrite, digest, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.news_capture import capture_newsnow
from decision_kernel.interfaces.news_custody import source_from_archive
from test_news_live_custody import fixture_archive
from test_news_windows import NOW, newsnow

AS_OF = "2026-10-16T01:00:00Z"
HANDOFF_CODE = "b" * 40
HANDOFF_RUN = "789"


def native(reading=None):
    raw, run, artifact, selected, reading = fixture_archive(reading)
    source = source_from_archive(raw, run, artifact, selected)
    return {**selected, "as_of": AS_OF, "predecessor": None}, source, reading


def frozen(plan, source, head, **changes):
    args = {"source_code_sha": "a" * 40, "expected_head": head,
            "handoff_code_sha": HANDOFF_CODE, "handoff_run_id": HANDOFF_RUN}
    args.update(changes)
    return handoff.make_intent(plan, source, **args)


def jref(ref):
    return json.loads(encoded(asdict(ref)))


def exact(source_id="news-roll-prior"):
    return {"manifest": {"repository": handoff.REPOSITORY, "commit": "c" * 40,
            "path": "source-views/" + source_id + "/" + "d" * 64 + "/manifest.json",
            "sha256": "e" * 64},
            "selection": {"kind": "all", "start": None, "end": None, "columns": []}}


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "handoff.git"))


def test_plan_freezes_exact_clock_and_has_only_bounded_native_identity():
    plan, _, _ = native()
    plan["as_of"] = "2026-10-16T09:00:00+08:00"
    actual = handoff.plan_input(plan)
    assert actual == plan and actual is not plan
    assert handoff.keys("123") == {"intent": "news-handoff-123-intent",
        "batch": "news-window-123", "window": "news-handoff-123-window"}
    assert set(handoff.keys("123").values()).isdisjoint(handoff.keys("124").values())
    for invalid in (123, True, "0", "0123", "-1", "1/../2", "1" * 21):
        with pytest.raises(RecordError, match="NATIVE_ID_REQUIRED"):
            handoff.keys(invalid)
    with pytest.raises(RecordError, match="PLAN_REQUIRED"):
        handoff.plan_input({**plan, "research": {"start": True}})
    with pytest.raises(RecordError, match="PLAN_BOUND"):
        handoff.plan_input({**plan, "artifact_sha256": "a" * (8 * 1024)})
    with pytest.raises(RecordError, match="NEWS_ROLL_CLOCK"):
        handoff.plan_input({**plan, "as_of": "2026-10-16T01:00:00"})
    with pytest.raises(RecordError, match="NATIVE_ID_REQUIRED"):
        handoff.plan_input({**plan, "artifact_id": 456})


def test_plan_requires_complete_same_repository_whole_source_predecessor():
    plan, _, _ = native()
    prior = exact()
    plan["predecessor"] = prior
    result = handoff.plan_input(plan)
    prior["manifest"]["commit"] = "f" * 40
    assert result["predecessor"]["manifest"]["commit"] == "c" * 40
    wrong = [exact() for _ in range(5)]
    wrong[0].pop("selection")
    wrong[1]["manifest"]["repository"] = "other/repository"
    wrong[2]["selection"].update(kind="lines", start=1, end=1)
    wrong[3]["manifest"]["path"] = "source-views/../manifest.json"
    wrong[4]["manifest"]["latest"] = True
    for ref in wrong:
        with pytest.raises(RecordError):
            handoff.plan_input({**plan, "predecessor": ref})


def test_intent_binds_source_bytes_capture_code_and_original_handoff_host():
    plan, source, reading = native()
    prior = exact("news-window-123")
    prior["manifest"]["path"] = "source-views/news-window-123/" + digest(encoded(asdict(source))) + "/manifest.json"
    intent = frozen(plan, source, "c" * 40, prior_batch_reference=prior)
    assert intent["batch_source_sha256"] == digest(encoded(asdict(source)))
    assert intent["source_code_sha"] == "a" * 40
    assert intent["handoff_code_sha"] == HANDOFF_CODE
    assert intent["handoff_run_id"] == HANDOFF_RUN
    assert intent["prior_batch_reference"] == prior
    assert intent["as_of"] == AS_OF and intent["expected_head"] == "c" * 40
    saved = handoff.intent_source(intent)
    assert saved.source_id == handoff.keys("123")["intent"]
    assert json.loads(saved.text) == intent and len(saved.text.encode()) < 8 * 1024
    assert reading["observations"][0]["title"] not in saved.text
    assert "observations" not in intent and "source_outcomes" not in intent
    with pytest.raises(TypeError):
        handoff.make_intent(plan, source, "a" * 40, "c" * 40)
    with pytest.raises(RecordError, match="PRIOR_BATCH_IDENTITY"):
        frozen(plan, source, "c" * 40, prior_batch_reference=exact("news-window-124"))
    with pytest.raises(RecordError, match="PRIOR_BATCH_IDENTITY"):
        frozen(plan, source, "c" * 40, prior_batch_reference=exact("news-window-123"))
    for bad in ({**intent, "handoff_code_sha": "B" * 40},
                {**intent, "handoff_run_id": 789},
                {**intent, "handoff_run_id": "0"},
                {**intent, "source_code_sha": "a" * 39},
                {**intent, "body": "unexpected authority"}):
        with pytest.raises(RecordError):
            handoff.validate_intent(bad)


def test_intent_rejects_source_rebinding_and_cutoff_before_final_capture_receipt():
    def partial(source):
        if source == "jin10":
            raise RecordError("NEWSNOW_HTTP_503")
        return newsnow()
    times = iter((NOW, NOW, "2026-10-16T00:59:59Z", "2026-10-16T01:00:01Z"))
    reading = capture_newsnow(sources=("cls", "jin10"), fetcher=partial, clock=lambda: next(times))
    plan, source, _ = native(reading)
    assert len(reading["observations"]) == 1 and reading["source_outcomes"][1]["status"] == "unavailable"
    with pytest.raises(RecordError, match="CUTOFF_BEFORE_CAPTURE"):
        frozen(plan, source, "c" * 40)
    later = {**plan, "as_of": "2026-10-16T01:00:02Z"}
    assert frozen(later, source, "c" * 40)["batch_source_sha256"] == digest(encoded(asdict(source)))
    with pytest.raises(RecordError, match="CUTOFF_BEFORE_CAPTURE"):
        frozen({**plan, "as_of": "2026-10-15T23:59:59Z"}, source, "c" * 40)
    with pytest.raises(RecordError, match="BATCH_IDENTITY"):
        frozen(later, replace(source, source_id="news-window-124"), "c" * 40)
    with pytest.raises(RecordError, match="BATCH_IDENTITY"):
        frozen(later, source, "c" * 40, source_code_sha="d" * 40)


def test_frozen_intent_recovers_original_reference_after_records_advance(store, monkeypatch):
    plan, source, _ = native()
    intent = frozen(plan, source, store.backend.head())
    saved_source = handoff.intent_source(intent)
    key = handoff.keys("123")["intent"]
    original = store.put_source(key, saved_source)
    store.put_source("unrelated", replace(source, source_id="unrelated"))
    fresh = VersionedStore(GitStore(store.backend.root))
    head = fresh.backend.head()
    monkeypatch.setattr(fresh.backend, "paths", lambda *a, **kw: pytest.fail("must not scan"))
    monkeypatch.setattr(fresh.backend, "commit", lambda *a, **kw: pytest.fail("read-only recovery"))
    found = handoff.retained_source(fresh, key, key)
    assert found["reference"] == jref(original)
    assert found["reference"]["manifest"]["commit"] != head
    assert found["source"] == saved_source == handoff.intent_source(json.loads(found["data"]["data"]))
    assert fresh.backend.head() == head
    changed = {**intent, "as_of": "2026-10-16T02:00:00Z"}
    handoff.validate_intent(changed)  # Valid shape does not permit replacing this frozen request.
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        store.put_source(key, handoff.intent_source(changed))
    assert store.backend.head() == head


def test_missing_journal_is_distinct_from_bad_journal_and_permission_error(store, monkeypatch):
    key = handoff.keys("123")["intent"]
    assert handoff.retained_source(store, key, key) is None
    path = "submissions/" + key + ".json"
    store.backend.commit({path: b"{broken"}, expected=store.backend.head())
    with pytest.raises(RecordError, match="JOURNAL_INVALID"):
        handoff.retained_source(store, key, key)
    def forbidden(*args):
        raise PermissionError("synthetic denied read")
    monkeypatch.setattr(store.backend, "read", forbidden)
    with pytest.raises(PermissionError):
        handoff.retained_source(store, key, key)


def test_present_journal_with_missing_source_is_not_an_absent_stage(store):
    key = handoff.keys("123")["batch"]
    journal = {"schema": "submission-v1", "intent_sha256": "a" * 64,
               "result_path": "source-views/" + key + "/" + "b" * 64 + "/manifest.json"}
    store.backend.commit({"submissions/" + key + ".json": encoded(journal)}, expected=store.backend.head())
    with pytest.raises(MissingObject, match="OBJECT_MISSING"):
        handoff.retained_source(store, key, key)


def test_retained_source_rechecks_submission_hash_and_expected_source_identity(store):
    _, source, _ = native()
    key = handoff.keys("123")["batch"]
    original = store.put_source(key, source)
    with pytest.raises(RecordError, match="JOURNAL_INVALID"):
        handoff.retained_source(store, key, "news-window-124")
    path = "submissions/" + key + ".json"
    journal = json.loads(store.backend.read(store.backend.head(), path))
    journal["intent_sha256"] = "0" * 64
    store.backend.commit({path: encoded(journal)}, expected=store.backend.head())
    assert VersionedStore(GitStore(store.backend.root)).read_source(original)["data"] == source.text
    with pytest.raises(RecordError, match="SUBMISSION_INTENT_MISMATCH"):
        handoff.retained_source(store, key, key)


def test_retained_source_revalidates_manifest_and_representation_bytes(store):
    _, source, _ = native()
    key = handoff.keys("123")["batch"]
    original = store.put_source(key, source)
    manifest_path = original.manifest.path
    journal_path = "submissions/" + key + ".json"
    manifest = json.loads(store.backend.read(store.backend.head(), manifest_path))
    journal = json.loads(store.backend.read(store.backend.head(), journal_path))
    # A later damaged journal must not bless changed source bytes. Whitespace
    # changes the journal commit while keeping its valid submission fields.
    store.backend.commit({manifest["representation"]["path"]: source.text.encode() + b" ",
                          journal_path: encoded(journal) + b" "}, expected=store.backend.head())
    with pytest.raises(RecordError, match="BLOB_DIGEST_MISMATCH"):
        handoff.retained_source(store, key, key)
    assert store.read_source(original)["data"] == source.text


def test_unknown_commit_acknowledgement_recovers_only_the_original_batch(store, monkeypatch):
    _, source, _ = native()
    key = handoff.keys("123")["batch"]
    commit = store.backend.commit
    calls = []
    def commit_without_receipt(files, *, expected):
        calls.append(commit(files, expected=expected))
        raise UnknownWrite("SYNTHETIC_ACK_LOST")
    monkeypatch.setattr(store.backend, "commit", commit_without_receipt)
    with pytest.raises(UnknownWrite, match="ACK_LOST"):
        store.put_source(key, source)
    fresh = VersionedStore(GitStore(store.backend.root))
    head = fresh.backend.head()
    monkeypatch.setattr(fresh.backend, "commit", lambda *a, **kw: pytest.fail("must not resubmit"))
    recovered = handoff.retained_source(fresh, key, key)
    assert recovered["reference"]["manifest"]["commit"] == calls[0] == head
    assert recovered["source"] == source and len(calls) == 1
    assert fresh.backend.head() == head


def test_existing_batch_submission_replays_exact_reference_and_rejects_changed_payload(store):
    _, source, _ = native()
    key = handoff.keys("123")["batch"]
    original = store.put_source(key, source)
    store.put_source("another", replace(source, source_id="another"))
    head = store.backend.head()
    assert store.put_source(key, source, expected_head=original.manifest.commit) == original
    assert handoff.retained_source(store, key, key)["reference"] == jref(original)
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        store.put_source(key, replace(source, title="changed request"))
    assert store.backend.head() == head


def test_shared_window_source_preserves_original_payload_and_failed_source_semantics(store):
    def partial(source):
        if source == "jin10":
            raise RecordError("NEWSNOW_HTTP_503")
        return newsnow()
    reading = capture_newsnow(sources=("cls", "jin10"), fetcher=partial, observed_at=NOW)
    _, source, _ = native(reading)
    batch = store.put_source(handoff.keys("123")["batch"], source)
    value = index.rolling_index(store.read_source, [batch], as_of=AS_OF)
    key = handoff.keys("123")["window"]
    actual = index.index_source(value, key)
    legacy = SourceInput(source_id="news-roll-" + key, kind="content",
        title="24小时新闻观察 / " + value["as_of"],
        origin="https://github.com/auguspp/Decision-OS-2/issues/48",
        text=encoded(value).decode("utf-8"), custody="provided",
        limitations=("DERIVED_NEWS_METADATA_NOT_ORIGINAL_ARTICLES",
                     "OBSERVATION_TIME_NOT_PUBLICATION_TIME",
                     "BOUNDED_SELECTION_NOT_COMPLETE_NEWS_COVERAGE",
                     "DERIVED_NEWS_INDEX_REQUIRES_RETAINED_SOURCE_VERSIONS"))
    assert encoded(asdict(actual)) == encoded(asdict(legacy))
    retained = store.put_source(key, actual)
    fresh = VersionedStore(GitStore(store.backend.root))
    assert handoff.retained_source(fresh, key, "news-roll-" + key)["reference"] == jref(retained)
    opened = index.open_index(fresh.read_source, retained)
    page = index.read_index(fresh.read_source, opened)
    assert page["selected_count"] == 1
    assert page["coverage"]["failed_source_windows"] == 1
    assert any(row["outcome"]["source"] == "jin10" and row["outcome"]["status"] == "unavailable"
               for row in page["source_outcomes"])
    damaged = deepcopy(value)
    damaged["entry_count"] = 0
    with pytest.raises(RecordError):
        index.index_source(damaged, key)
