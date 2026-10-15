"""Real local Git transactions using synthetic records; no remote/runtime acceptance."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import json
import shutil
from threading import Barrier

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    BlobRef, Conflict, Entity, IntegrityError, MissingObject, RecordError,
    RevisionInput, Selection, SourceInput, SourceRef, StoreUnavailable,
    UnknownWrite, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "synthetic.git"))


def source(**changes):
    data = dict(source_id="copper", kind="content", title="合成铜价观察", origin="synthetic:fixture",
                text="仅为合成数据\n铜价变化待验证\n", published_at="2026-10-01", report_period="2026-Q3",
                entities=(Entity("copper-cn", "commodity", "铜", ("电解铜",), (("unit", "元/吨"),)),))
    data.update(changes)
    return SourceInput(**data)


def revision(ref=None, **changes):
    data = dict(case_id="case-one", revision_id="r1", kind="quick", body="合成判断：仍需更多证据。",
                mandate="synthetic:mandate", method="synthetic:method", sources=(ref,) if ref else (),
                unresolved=() if ref else ("暂无来源；仅保留未完成问题。",))
    data.update(changes)
    return RevisionInput(**data)


def test_exact_read_reopen_replay_and_date_precision(store):
    original = source()
    ref = store.put_source("receive-one", original)
    view = VersionedStore(GitStore(store.backend.root)).read_source(ref)
    assert view["data"] == original.text
    assert view["manifest"]["input"]["published_at"] == "2026-10-01"
    assert view["manifest"]["input"]["acquired_at"] is None
    assert view["manifest"]["input"]["report_period"] == "2026-Q3"
    assert view["manifest"]["stored_at"].endswith("+00:00")
    assert view["original_verified"] is False
    later = store.put_source("receive-two", source(text="第二版合成材料"))
    head = store.backend.head()
    assert store.put_source("receive-one", original) == ref
    assert store.backend.head() == head
    assert store.read_source(ref)["data"] == original.text
    assert store.read_source(later)["data"] == "第二版合成材料"
    assert ref.manifest.commit != later.manifest.commit


def test_idempotency_conflicts_and_source_kind_identity(store):
    ref = store.put_source("intent", source())
    head = store.backend.head()
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        store.put_source("intent", source(text="不是相同意图"))
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        store.submit_revision("intent", revision(ref))
    with pytest.raises(Conflict, match="SOURCE_KIND_CONFLICT"):
        store.put_source("another", source(kind="dataset", text="series,value\ncopper,1\n"))
    assert store.backend.head() == head


def test_same_source_version_new_intent_preserves_first_metadata(store):
    first = store.put_source("first", source())
    second = store.put_source("second", source())
    assert second.manifest.commit != first.manifest.commit
    assert first.manifest.path == second.manifest.path
    assert store.read_source(first)["manifest"] == store.read_source(second)["manifest"]
    assert store.search("铜价")["total"] == 1
    assert store.put_source("second", source()) == second


def test_research_revision_chain_replay_and_references(store):
    src = store.put_source("src", source())
    first = store.submit_revision("research-one", revision(src))
    second_input = revision(src, revision_id="r2", predecessor=first, body="新增合成挑战；仍无最终结论。")
    second = store.submit_revision("research-two", second_input)
    reader = VersionedStore(GitStore(store.backend.root))
    head = reader.backend.head()
    assert reader.current("case-one") == second
    assert reader.current("case-one", snapshot=first.commit) == first
    assert reader.submit_revision("research-one", revision(src)) == first
    assert reader.backend.head() == head
    assert reader.read_revision(first)["body"] == revision(src).body
    saved = reader.read_revision(second)["record"]
    assert saved["human_acceptance"] == "not-confirmed"
    assert saved["input"]["predecessor"] == asdict(first)
    assert saved["input"]["sources"] == json.loads(encoded([asdict(src)]))
    frozen = SourceRef.from_dict(saved["input"]["sources"][0])
    assert reader.read_source(frozen)["data"] == source().text


def test_stale_wrong_case_and_immutable_revision_rejected(store):
    first = store.submit_revision("one", revision())
    second = store.submit_revision("two", revision(revision_id="r2", predecessor=first))
    head = store.backend.head()
    for bad in (revision(revision_id="r3", predecessor=first),
                revision(case_id="case-other", predecessor=second),
                revision(revision_id="r3")):
        with pytest.raises(Conflict, match="STALE_PREDECESSOR"):
            store.submit_revision("three", bad)
    with pytest.raises(Conflict, match="REVISION_ALREADY_EXISTS"):
        store.submit_revision("reuse-id", revision(revision_id="r1", predecessor=second))
    assert store.backend.head() == head
    assert store.current("case-one") == second
    assert store.current("absent") is None


def test_partial_unreferenced_research_not_forced_to_valuation_or_decision(store):
    receipt = store.submit_revision("partial", revision(kind="full"))
    result = store.read_revision(receipt)
    assert result["record"]["input"]["sources"] == []
    assert result["record"]["input"]["unresolved"]
    with pytest.raises(RecordError, match="UNSOURCED_LIMITATION_REQUIRED"):
        revision(unresolved=())
    with pytest.raises(RecordError, match="NOT_A_RESEARCH_KIND"):
        revision(kind="decision")


@pytest.mark.parametrize("change,error", [
    ({"repository": "synthetic-git:foreign"}, "FOREIGN_REPOSITORY"),
    ({"sha256": "0" * 64}, "BLOB_DIGEST_MISMATCH"),
    ({"commit": "0" * 40}, "COMMIT_NOT_REACHABLE"),
    ({"path": "source-views/missing/" + "0" * 64 + "/manifest.json"}, "OBJECT_MISSING"),
])
def test_invalid_source_ref_blocks_research_before_commit(store, change, error):
    src = store.put_source("src", source())
    invalid = SourceRef(replace(src.manifest, **change))
    head = store.backend.head()
    with pytest.raises(RecordError, match=error):
        store.submit_revision("bad", revision(invalid))
    assert store.backend.head() == head
    assert store.current("case-one") is None


def test_modified_worktree_or_new_blob_cannot_rewrite_frozen_evidence(store):
    src = store.put_source("src", source())
    body_path = src.manifest.path.rsplit("/", 1)[0] + "/content.txt"
    corrupted = store.backend.commit({body_path: b"changed by local owner"}, expected=store.backend.head())
    assert store.read_source(src)["data"] == source().text
    ref_now = SourceRef(store.backend.reference(corrupted, src.manifest.path))
    with pytest.raises(IntegrityError, match="BLOB_DIGEST_MISMATCH"):
        store.read_source(ref_now)
    head = store.backend.head()
    with pytest.raises(IntegrityError, match="BLOB_DIGEST_MISMATCH"):
        store.put_source("new-intent", source())
    assert store.backend.head() == head


def test_text_selection_and_partial_custody(store):
    src = store.put_source("excerpt", source(custody="excerpt", limitations=("仅保存获准摘录。",)))
    selection = replace(src, selection=Selection("lines", 2, 2))
    view = store.read_source(selection)
    assert view["data"] == "铜价变化待验证\n"
    assert view["limitations"] == ["仅保存获准摘录。"]
    assert view["original_verified"] is False
    with pytest.raises(RecordError, match="RANGE_OUT_OF_BOUNDS"):
        store.read_source(replace(src, selection=Selection("lines", 1, 9)))
    with pytest.raises(RecordError, match="CONTENT_LINES_REQUIRED"):
        store.read_source(replace(src, selection=Selection("rows", 1, 1)))


def test_external_locator_does_not_fake_stored_body(store):
    src = store.put_source("external", source(text=None, custody="external", origin="https://example.org/report",
                                              limitations=("原文未保管。",)))
    assert src.selection.kind == "locator"
    view = store.read_source(src)
    assert view["data"] is None and view["availability"] == "external-only"
    assert view["manifest"]["representation"] is None
    with pytest.raises(RecordError, match="BODY_NOT_STORED"):
        store.read_source(replace(src, selection=Selection()))
    receipt = store.submit_revision("limited", revision(src, unresolved=("仅有外部定位，不表述已读正文。",)))
    assert store.read_revision(receipt)["record"]["input"]["sources"][0]["selection"]["kind"] == "locator"


def test_csv_logical_rows_columns_strings_and_multiline_fields(store):
    data = '公司,值,备注\n合成甲,001,"第一行\n第二行"\n合成乙,unknown,待证\n'
    src = store.put_source("csv", source(kind="dataset", text=data))
    selected = replace(src, selection=Selection("rows", 1, 1, ("值", "备注")))
    assert store.read_source(selected)["data"] == [{"值": "001", "备注": "第一行\n第二行"}]
    assert store.read_source(src)["data"][1]["值"] == "unknown"
    with pytest.raises(RecordError, match="COLUMN_MISSING"):
        store.read_source(replace(src, selection=Selection("rows", 1, 1, ("利润",))))
    with pytest.raises(RecordError, match="DATASET_ROWS_REQUIRED"):
        store.read_source(replace(src, selection=Selection("lines", 1, 1)))


@pytest.mark.parametrize("text", ["a,a\n1,2\n", "a,b\n1\n", ',b\n1,2\n', 'a\n"unfinished'])
def test_invalid_dataset_never_gets_receipt(store, text):
    head = store.backend.head()
    with pytest.raises(RecordError, match="CSV"):
        store.put_source("bad", source(kind="dataset", text=text))
    assert store.backend.head() == head


def test_concurrent_native_cas_has_one_writer_without_lost_update(store, monkeypatch):
    base = store.backend.head()
    barrier = Barrier(2)
    advance = GitStore._advance

    def synchronized(self, commit, expected):
        barrier.wait(timeout=10)
        return advance(self, commit, expected)

    monkeypatch.setattr(GitStore, "_advance", synchronized)
    def write(number):
        own = VersionedStore(GitStore(store.backend.root))
        try:
            return own.put_source(f"writer-{number}", source(source_id=f"source-{number}"))
        except Conflict as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, (1, 2)))
    assert sum(isinstance(r, SourceRef) for r in results) == 1
    assert sum(isinstance(r, Conflict) for r in results) == 1
    assert store.search("")["total"] == 1
    parent = store.backend._run("rev-parse", store.backend.head() + "^").stdout.decode().strip()
    assert parent == base


def test_unknown_after_actual_commit_recovers_same_intent(store, monkeypatch):
    advance = store.backend._advance
    def commit_then_lose_reply(commit, expected):
        advance(commit, expected)
        raise UnknownWrite("REF_UPDATE_UNCONFIRMED")
    monkeypatch.setattr(store.backend, "_advance", commit_then_lose_reply)
    with pytest.raises(UnknownWrite):
        store.put_source("same-intent", source())
    committed = store.backend.head()
    monkeypatch.setattr(store.backend, "_advance", advance)
    receipt = store.put_source("same-intent", source())
    assert receipt.manifest.commit == committed
    assert store.backend.head() == committed
    assert store.read_source(receipt)["data"] == source().text


def test_prepublication_failure_does_not_make_partial_records_visible(store, monkeypatch):
    base = store.backend.head()
    def fail_before_commit(*args):
        raise StoreUnavailable("REF_UPDATE_FAILED")
    monkeypatch.setattr(store.backend, "_advance", fail_before_commit)
    with pytest.raises(StoreUnavailable):
        store.put_source("one", source())
    assert store.backend.head() == base
    assert store.search("")["total"] == 0
    with pytest.raises(MissingObject):
        store.backend.read(base, "submissions/one.json")


def test_short_chinese_codes_aliases_and_distinct_entity_identity(store):
    a = Entity("security-one", "security", "同名", ("ABC001",))
    b = Entity("company-one", "company", "同名", ("别名乙",))
    store.put_source("a", source(source_id="a", entities=(a,), text="甲的合成资料"))
    store.put_source("b", source(source_id="b", entities=(b,), text="乙的合成资料"))
    assert store.search("同名")["total"] == 2
    assert store.search("同名", entity_id="security-one")["total"] == 1
    assert store.search("ａｂｃ００１")["items"][0]["source_id"] == "a"
    assert store.search("别名乙")["items"][0]["source_id"] == "b"
    assert store.search("铜价")["total"] == 2
    assert store.search("source_id")["total"] == 0  # Do not match JSON field names.


def test_pagination_pins_actual_snapshot_and_query(store):
    for name in ("a", "b", "c"):
        store.put_source(name, source(source_id=name))
    first = store.search("铜价", limit=1)
    store.put_source("d", source(source_id="d"))
    second = store.search("铜价", limit=2, cursor=first["next_cursor"])
    assert first["total"] == second["total"] == 3
    assert second["snapshot"] == first["snapshot"] and second["next_cursor"] is None
    assert len({item["source_id"] for item in first["items"] + second["items"]}) == 3
    assert store.search("铜价")["total"] == 4
    with pytest.raises(RecordError, match="INVALID_CURSOR"):
        store.search("别的问题", cursor=first["next_cursor"])
    with pytest.raises(RecordError, match="CURSOR_OUT_OF_BOUNDS"):
        store.search("铜价", cursor={**first["next_cursor"], "offset": 99})


@pytest.mark.parametrize("value", [0, -1, 101, True])
def test_paging_rejects_invalid_limits(store, value):
    with pytest.raises(RecordError, match="INVALID_PAGE_LIMIT"):
        store.search("", limit=value)


def test_search_bound_is_explicit_not_silent_partial(store, monkeypatch):
    import decision_kernel.application.versioned_store as module
    store.put_source("a", source())
    monkeypatch.setattr(module, "MAX_QUERY_VERSIONS", 0)
    with pytest.raises(RecordError, match="QUERY_VERSION_LIMIT_EXCEEDED"):
        store.search("")


def test_restore_copy_is_readable_and_logical_repository_identity_survives(store, tmp_path):
    src = store.put_source("src", source())
    record = store.submit_revision("research", revision(src))
    replica = tmp_path / "restored.git"
    shutil.copytree(store.backend.root, replica)
    restored = VersionedStore(GitStore(replica))
    assert restored.backend.repository == store.backend.repository
    assert restored.read_revision(record)["body"] == revision(src).body
    assert restored.read_source(src)["data"] == source().text


def test_initialization_never_overwrites_and_binding_is_not_private_authorization(store, tmp_path):
    with pytest.raises(Conflict, match="STORE_PATH_EXISTS"):
        GitStore.init_synthetic(store.backend.root)
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(Conflict):
        GitStore.init_synthetic(existing)
    assert sentinel.read_text() == "keep"
    head = store.backend.head()
    store.backend.commit({"store.json": encoded({"schema": "store-binding-v1", "mode": "private",
                                                 "repository": "synthetic-git:claimed"})}, expected=head)
    with pytest.raises(RecordError, match="UNSUPPORTED_STORE_BINDING"):
        GitStore(store.backend.root)


@pytest.mark.parametrize("binding", [[], [1], 1, None])
def test_malformed_binding_is_structured_failure(store, binding):
    store.backend.commit({"store.json": encoded(binding)}, expected=store.backend.head())
    with pytest.raises(RecordError, match="UNSUPPORTED_STORE_BINDING"):
        GitStore(store.backend.root)


def test_git_process_environment_does_not_inherit_credentials_or_overrides(monkeypatch):
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "GIT_DIR", "GIT_INDEX_FILE",
                "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND", "GIT_AUTHOR_NAME"):
        monkeypatch.setenv(key, "synthetic-secret-or-override")
    env = GitStore._env()
    assert not any(value == "synthetic-secret-or-override" for value in env.values())
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("kwargs", [
    {"source_id": "../escape"}, {"origin": "https://user:password@example.org/private"},
    {"origin": "https://[bad"}, {"published_at": "2026-10-15T09:00:00"},
    {"published_at": "2026-02-31"}, {"custody": "external", "text": None},
    {"custody": "excerpt"}, {"text": "contains\0null"},
])
def test_source_input_failures_are_bounded_codes(kwargs):
    with pytest.raises(RecordError):
        source(**kwargs)


@pytest.mark.parametrize("path", ["../x", "/x", "a//b", "a/./b", "a\\b"])
def test_path_references_reject_traversal(path):
    with pytest.raises(RecordError, match="INVALID_PATH"):
        BlobRef("synthetic:test", "0" * 40, path, "0" * 64)


def test_malformed_predecessor_does_not_silently_become_first_revision():
    payload = asdict(revision())
    payload["predecessor"] = {}
    with pytest.raises(RecordError, match="INVALID_REVISION_INPUT"):
        RevisionInput.from_dict(payload)


def test_symlink_blob_is_not_followed_as_source(store):
    src = store.put_source("src", source())
    git = store.backend
    target = git._run("hash-object", "-w", "--stdin", data=b"/etc/passwd").stdout.decode().strip()
    tree = git._run("mktree", data=f"120000 blob {target}\tlink.txt\n".encode()).stdout.decode().strip()
    commit = git._run("commit-tree", tree, "-p", git.head(), data=b"synthetic unsafe tree\n").stdout.decode().strip()
    git._advance(commit, git.head())
    with pytest.raises(IntegrityError, match="REGULAR_BLOB_REQUIRED"):
        git.read(commit, "link.txt")
    # Historical source stays readable even when a local owner damages current layout.
    assert store.read_source(src)["data"] == source().text


def test_missing_representation_is_not_reconstructed_as_new_source(store):
    src = store.put_source("src", source())
    raw_read = store.backend.read
    body_path = src.manifest.path.rsplit("/", 1)[0] + "/content.txt"
    # Inject a missing-object read at the byte boundary, not at the validator.
    def missing_body(commit, path):
        if path == body_path:
            raise MissingObject("OBJECT_MISSING")
        return raw_read(commit, path)
    store.backend.read = missing_body
    head = store.backend.head()
    with pytest.raises(MissingObject, match="OBJECT_MISSING"):
        store.put_source("new-intent", source())
    assert store.backend.head() == head
