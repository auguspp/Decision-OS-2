"""Original standard store/CAS and independent recovery of supplied originals."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from decision_kernel.application.global_public_custody import _body_source, read_public_root
from decision_kernel.application.global_public_inputs import read_global_public_payload
from decision_kernel.application.market_custody import market_reading_projection, read_stored_market, retain_market_reading
from decision_kernel.application.reading_market import read_market_reference
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MAX_BYTES, MissingObject, RecordError, SourceRef, StoreUnavailable, UnknownWrite, encoded
from decision_kernel.infrastructure.git_store import GitStore
from test_global_public_inputs import public_input, replace_body
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY_PATH, ROOT_DIR


@pytest.fixture
def custody():
    backend = MemoryBackend()
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    return store, fresh


def save(custody, payload, **changes):
    store, fresh = custody
    return retain_market_reading(payload, None, store,
        **{"fresh_reader": fresh, "public_rights_confirmed": True, **changes})


@pytest.mark.parametrize("family,count", [("treasury", 1), ("fx", 1), ("commodities", 3), ("crypto", 2)])
def test_standard_sources_independent_readback_and_exact_replay(custody, family, count):
    value = public_input(family)
    before = encoded(value)
    assert market_reading_projection(value, None) == value
    saved = save(custody, value)
    store, fresh = custody
    assert saved["status"] == "stored-and-independently-restored"
    assert saved["page_count"] == saved["query_count"] == count
    assert saved["restored"]["view"] == read_global_public_payload(value)
    assert saved["restored"]["original_bodies_restored"] is True
    assert saved["restored"]["original_http_restored"] is False
    ref, root = read_public_root(fresh(), saved["reference"])
    for original, retained in zip(value["records"], root["sources"], strict=True):
        body = fresh().read_source(SourceRef.from_dict(retained["reference"]))
        assert body["data"].encode() == original["body"].encode()
        assert body["manifest"]["input"]["origin"] == original["spec"]["url"]
        assert body["manifest"]["input"]["acquired_at"] == original["received_at"]
        assert body["manifest"]["input"]["published_at"] is None
        assert set(body["reference"]) == {"manifest", "selection"}
    head, writes = store.backend.head(), store.backend.shared["count"]
    assert save(custody, value)["reference"] == saved["reference"]
    assert read_market_reference(ref, None, fresh()) == saved["restored"]
    assert store.backend.head() == head and store.backend.shared["count"] == writes
    assert encoded(value) == before


def test_source_envelope_bound_and_authorization_fail_before_any_write(custody):
    value = public_input("crypto")
    # The raw text is less than 2 MiB, but its complete encoded SourceInput is
    # larger. It cannot use the old raw-body allowance to enlarge storage.
    text = "x" * (MAX_BYTES - 20)
    assert len(text.encode()) < MAX_BYTES
    with pytest.raises(RecordError, match="SOURCE_TOO_LARGE"):
        _body_source(value, value["records"][0], text)
    oversized = replace_body(deepcopy(value), 0, text)
    with pytest.raises(RecordError, match="MARKET_CUSTODY_INPUT_BOUND"):
        save(custody, oversized)
    with pytest.raises(RecordError, match="MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED"):
        save(custody, value, public_rights_confirmed=False)
    forged = deepcopy(value); forged["public_rights_confirmed"] = True
    with pytest.raises(RecordError, match="GP_INPUT_SCHEMA"):
        save(custody, forged)
    assert custody[0].backend.shared["count"] == 0


def test_unknown_write_stops_and_identical_original_reconciles_without_duplicate():
    failure = UnknownWrite("WRITE_OUTCOME_UNKNOWN")
    class Uncertain(MemoryBackend):
        uncertain = True
        def commit(self, files, *, expected):
            head = super().commit(files, expected=expected)
            if self.uncertain:
                self.uncertain = False
                raise failure
            return head
    backend = Uncertain()
    custody = VersionedStore(backend), lambda: VersionedStore(MemoryBackend(backend.shared))
    value = public_input("crypto")
    with pytest.raises(UnknownWrite) as caught:
        save(custody, value)
    assert caught.value is failure and backend.shared["count"] == 1
    result = save(custody, value)
    assert result["status"] == "stored-and-independently-restored"
    assert backend.shared["count"] == 3  # Two originals and one root, no second first write.


@pytest.mark.parametrize("failure", [StoreUnavailable("REMOTE_REQUEST_LIMIT"), RecordError("READING_SCOPE_CLOSED")])
def test_same_operation_failure_stops_before_any_later_original(custody, failure):
    saved = save(custody, public_input("crypto"))
    store, _ = custody
    _, root = read_public_root(store, saved["reference"])
    target = SourceRef.from_dict(root["sources"][0]["reference"]).manifest
    class Failing(MemoryBackend):
        def __init__(self):
            super().__init__(store.backend.shared); self.trace = []
        def resolve(self, ref):
            self.trace.append(ref)
            if ref == target:
                raise failure
            return super().resolve(ref)
    backend = Failing()
    with pytest.raises(type(failure)) as caught:
        read_stored_market(saved["reference"], None, VersionedStore(backend))
    assert caught.value is failure and backend.trace[-1] == target
    assert len(backend.trace) == 3  # Root manifest, root text, first original manifest.


def test_missing_original_keeps_full_denominator_and_other_product(custody):
    saved = save(custody, public_input("crypto"))
    store, fresh = custody
    _, root = read_public_root(store, saved["reference"])
    target = SourceRef.from_dict(root["sources"][0]["reference"]).manifest
    store.backend.shared["missing"].add((target.commit, target.path))
    restored = read_stored_market(saved["reference"], None, fresh(), entity_id="ETH-USD", limit=1)
    assert restored["status"] == "partial-restoration" and restored["unavailable_queries"] == 1
    view = restored["view"]
    assert view["scope_count"] == 2 and view["available_values"] == view["missing_values"] == 1
    assert view["entries"][0]["entity_id"] == "ETH-USD" and view["entries"][0]["value"] == "105"
    assert view["outcomes"][0]["status"] == "RETAINED_ORIGINAL_UNAVAILABLE"
    # A source cannot be substituted merely because another body is readable.
    root["sources"][0]["reference"] = root["sources"][1]["reference"]
    from decision_kernel.application.global_public_custody import _root_source
    tampered = store.put_source("synthetic-rebound-original", _root_source(root))
    wrong = read_stored_market(tampered, None, fresh())
    assert wrong["status"] == "partial-restoration" and wrong["sources"][0]["reason_code"] == "GP_ORIGINAL_CHANGED"


def test_ashare_guard_does_not_restore_any_public_original(custody):
    saved = save(custody, public_input("fx"))
    store, _ = custody
    class Traced(MemoryBackend):
        def __init__(self): super().__init__(store.backend.shared); self.refs = []
        def resolve(self, ref): self.refs.append(ref); return super().resolve(ref)
    backend = Traced()
    with pytest.raises(RecordError, match="MARKET_CUSTODY_SCHEMA"):
        read_market_reference(saved["reference"], None, VersionedStore(backend), ashare_only=True)
    assert len(backend.refs) == 2
    assert all("/market-global-" in ref.path for ref in backend.refs)


def test_existing_market_custody_cli_saves_then_separate_process_restores(tmp_path):
    path = tmp_path / "sources.git"
    GitStore.init_synthetic(path)
    value = public_input("fx")
    source = tmp_path / "input.json"; source.write_bytes(encoded(value))
    env = {**os.environ, "PYTHONPATH": str(ROOT_DIR / "src")}
    command = [sys.executable, "-m", "decision_kernel.interfaces.market_custody_cli"]
    saved = subprocess.run(command + ["save", "--path", str(path), "--registry", str(REGISTRY_PATH),
        "--input", str(source), "--public-rights-confirmed"], cwd=ROOT_DIR, env=env,
        capture_output=True, text=True, timeout=30)
    assert saved.returncode == 0, saved.stderr
    first = json.loads(saved.stdout)
    source.unlink()
    reference = tmp_path / "reference.json"; reference.write_bytes(encoded(first["reference"]))
    read = subprocess.run(command + ["read", "--path", str(path), "--registry", str(REGISTRY_PATH),
        "--input", str(reference)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert read.returncode == 0, read.stderr
    assert json.loads(read.stdout) == first["restored"]
