"""Native full-bundle restores protect current store references, not production SLA."""
from dataclasses import asdict, replace
from pathlib import Path
import json

import pytest

from decision_kernel.application.store_audit import audit_store
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, Conflict, RecordError, RevisionInput, SourceInput, encoded
from decision_kernel.infrastructure.git_snapshot import GitSnapshot, bundle_from_bare, git, verify_bundle
from decision_kernel.infrastructure.github_store import GitHubStore
from test_github_visibility import public_api as base_public_api
from test_github_store import source


@pytest.fixture
def public_api(base_public_api):
    api = base_public_api
    # The generic HTTP fixture aliases records/dos2 to its local records ref.
    # Native bundle tests require the binding to name the real local ref.
    api.binding = replace(api.binding, branch="records")
    anchor = api.git.commit({"store.json": api.binding.marker()}, expected=api.git.head())
    api.binding = replace(api.binding, anchor=anchor)
    return api


def populated(api):
    store = VersionedStore(GitHubStore(api.binding, api))
    first = store.put_source("src1", source())
    old = store.submit_revision("r1", RevisionInput("case", "r1", "quick", "synthetic-one", "m", "method", (first,)))
    store.put_source("src2", source("updated synthetic source"))
    new = store.submit_revision("r2", RevisionInput("case", "r2", "full", "synthetic-two", "m", "method", (first,), old))
    return store, first, old, new


def test_native_full_bundle_restores_history_and_exact_reference_graph(public_api, tmp_path):
    live, first, old, new = populated(public_api)
    snapshot = public_api.git.head()
    binding = replace(public_api.binding, write_enabled=False)
    reader = VersionedStore(GitSnapshot(public_api.git.root, binding, snapshot))
    audit = audit_store(live, snapshot)
    assert audit_store(reader, snapshot) == audit
    output = tmp_path / "snapshot.bundle"
    assert bundle_from_bare(public_api.git.root, binding, snapshot, output) == audit
    assert verify_bundle(output, binding, snapshot) == audit
    assert audit["source_versions"] == 2 and audit["research_revisions"] == 2
    assert audit["current"]["case"] == asdict(new)
    assert audit["retained_source_uses"] == 2
    assert reader.read_revision(old)["body"] == "synthetic-one"
    assert reader.read_source(first)["data"] == source().text
    with pytest.raises(RecordError, match="SNAPSHOT_READ_ONLY"):
        reader.backend.commit({"submissions/illegal.json": b"{}"}, expected=snapshot)
    with pytest.raises(Conflict, match="ALREADY_EXISTS"):
        bundle_from_bare(public_api.git.root, binding, snapshot, output)


def test_snapshot_blocks_pre_anchor_commit_and_wrong_marker(public_api):
    snapshot = public_api.git.head()
    binding = replace(public_api.binding, write_enabled=False)
    reader = GitSnapshot(public_api.git.root, binding, snapshot)
    with pytest.raises(RecordError, match="COMMIT_OUTSIDE_BINDING"):
        reader.read(public_api.before_anchor, "store.json")
    with pytest.raises(RecordError, match="STORE_MARKER_MISMATCH"):
        GitSnapshot(public_api.git.root, replace(binding, store_id="other"), snapshot)
    with pytest.raises(RecordError, match="READ_ONLY_BINDING"):
        GitSnapshot(public_api.git.root, public_api.binding, snapshot)


def test_full_bundle_has_no_prerequisites_and_wrong_ref_or_snapshot_rejected(public_api, tmp_path):
    populated(public_api)
    snapshot = public_api.git.head()
    binding = replace(public_api.binding, write_enabled=False)
    bundle = tmp_path / "full.bundle"
    bundle_from_bare(public_api.git.root, binding, snapshot, bundle)
    with pytest.raises(RecordError, match="BUNDLE_REF_MISMATCH"):
        verify_bundle(bundle, binding, binding.anchor)
    thin = tmp_path / "thin.bundle"
    git("--git-dir=" + str(public_api.git.root), "bundle", "create", str(thin),
        "refs/heads/records", "^" + binding.anchor)
    with pytest.raises(RecordError, match="SNAPSHOT_GIT_FAILED"):
        verify_bundle(thin, binding, snapshot)
    broken = tmp_path / "broken.bundle"
    broken.write_bytes(bundle.read_bytes()[:90])
    with pytest.raises(RecordError):
        verify_bundle(broken, binding, snapshot)


@pytest.mark.parametrize("damage", ["source-bytes", "current", "missing-source", "unknown-path"])
def test_restoration_does_not_accept_partial_or_damaged_store(public_api, damage):
    store, first, old, new = populated(public_api)
    if damage == "source-bytes":
        path = first.manifest.path.rsplit("/", 1)[0] + "/content.txt"
        files = {path: b"changed"}
    elif damage == "current":
        files = {"research/cases/case/current.json": encoded({"revision_path": "research/cases/case/revisions/gone/record.json"})}
    elif damage == "unknown-path":
        files = {".github/workflows/evil.yml": b"not an allowed record"}
    else:
        record = store.read_revision(new)["record"]
        record["input"]["sources"][0]["manifest"]["sha256"] = "0" * 64
        files = {new.path: encoded(record)}
    snapshot = public_api.git.commit(files, expected=public_api.git.head())
    with pytest.raises(RecordError):
        reader = VersionedStore(GitSnapshot(public_api.git.root, replace(public_api.binding, write_enabled=False), snapshot))
        audit_store(reader, snapshot)


def test_empty_store_is_explicitly_zero_not_a_research_acceptance(public_api):
    snapshot = public_api.git.head()
    reader = VersionedStore(GitSnapshot(public_api.git.root, replace(public_api.binding, write_enabled=False), snapshot))
    audit = audit_store(reader, snapshot)
    assert audit["source_versions"] == audit["research_revisions"] == 0
    assert audit["research_quality"] == "not-evaluated"
    assert audit["live_authorization"] == "not-certified-by-audit"


def test_external_locator_remains_not_custodied_after_bundle_restore(public_api, tmp_path):
    store = VersionedStore(GitHubStore(public_api.binding, public_api))
    item = SourceInput("locator", "content", "synthetic external", "https://example.org/x", None,
                       custody="external", limitations=("body not retained",))
    ref = store.put_source("locator", item)
    snapshot = public_api.git.head()
    binding = replace(public_api.binding, write_enabled=False)
    audit = bundle_from_bare(public_api.git.root, binding, snapshot, tmp_path / "external.bundle")
    assert audit["sources"][0]["selection"]["kind"] == "locator"
    assert audit["originals_authenticated"] is False


@pytest.mark.parametrize("path", ["objects/info/alternates", "info/grafts"])
def test_external_object_sources_are_refused(public_api, path):
    target = public_api.git.root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n")
    with pytest.raises(RecordError, match="EXTERNAL_OBJECTS_FORBIDDEN"):
        GitSnapshot(public_api.git.root, replace(public_api.binding, write_enabled=False), public_api.git.head())
