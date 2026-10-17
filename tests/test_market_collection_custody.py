"""Durable market control/query identity, private custody and bounded real Git I/O."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from decision_kernel.application.market_collection import plan_collection, unit_request, checkpoint_id
from decision_kernel.application.market_collection_custody import (
    CHECKPOINT_SCHEMA, collection_source_id,
    retain_market_checkpoint, restore_market_checkpoint, retain_market_collection, restore_market_collection,
)
from decision_kernel.application.market_custody import (
    authorize_market_custody, market_read_scope, retain_market_query, restore_market_query,
)
from decision_kernel.application.relay_capture_checkpoint import CheckpointWriteError
from decision_kernel.application.research_sources import capture_registered, validate_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.observations.market_daily import MarketInputError
from test_github_store import GitAPI
from test_market_custody import MemoryBackend, raw_payload
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR, CODE, PRICES, SyntheticOpener
from test_news_read_budget import transport

STAMP = "2026-10-17T10:00:00+00:00"


def memory_store():
    backend = MemoryBackend()
    return VersionedStore(backend), lambda: VersionedStore(MemoryBackend(backend.shared))


def capture_states(*, limit=2, writer=None, pause_requested=None, rows=None):
    use = deepcopy(raw_payload()["stock_results"][CODE]["use"])
    use = validate_plan({"schema": "research-source-plan-v1", "case_id": "synthetic-market",
        "requests": [{k: v for k, v in use.items() if k in ("stage", "purpose", "api", "parameters")}]}, REGISTRY)["requests"][0]
    raw = {k: v for k, v in use.items() if k in ("stage", "purpose", "api", "parameters")}
    raw["parameters"]["limit"] = str(limit)
    use = validate_plan({"schema": "research-source-plan-v1", "case_id": "synthetic-market",
                        "requests": [raw]}, REGISTRY)["requests"][0]
    opener = SyntheticOpener("daily", ["ts_code", "trade_date", "close"], PRICES if rows is None else rows)
    client = RelayHTTP(opener=opener, credential="synthetic-key-only")
    session = RetryingRelay(client, clock=lambda: STAMP)
    states = []

    def checkpoint(state):
        states.append(deepcopy(state))
        if writer is not None:
            writer(state)

    result = capture_registered(use, REGISTRY, session, max_pages=20,
                               checkpoint_writer=checkpoint, pause_requested=pause_requested)
    return result, states, opener


def collection(generation="initial"):
    plan = plan_collection(collection_id="synthetic-market", generation=generation,
        start="20261012", end="20261016", calendar_end="20261016", registry=REGISTRY)
    return {"plan": plan, "units": {k + ":" + v:
        {"query": None, "checkpoint": None, "status": "pending", "reason_code": None}
        for k in ("calendar", "master") for v in ("SSE", "SZSE")}, "last_qualified": None,
        "attempt": {"id": "one", "status": "planned", "reason_code": None,
                    "started_at": STAMP, "updated_at": STAMP, "source_requests": 0}}


def test_single_query_uses_original_manifest_and_rejects_incomplete_before_write():
    store, fresh = memory_store()
    result = raw_payload()["stock_results"][CODE]
    retained = retain_market_query(result, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    restored = restore_market_query(retained["reference"], REGISTRY, fresh())
    assert retained["restored"] == restored
    assert restored["original_http_restored"] is False
    assert all(not p["attempts"] for p in restored["result"]["capture"]["pages"])
    assert restored["result"]["capture"]["pages"][0]["table"] == result["capture"]["pages"][0]["table"]
    writes = store.backend.shared["count"]
    bad = deepcopy(result); bad["capture"]["complete"] = False
    with pytest.raises((RecordError, MarketInputError)):
        retain_market_query(bad, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    assert store.backend.shared["count"] == writes


def test_private_query_retention_shares_one_real_api_budget_without_public_declaration(tmp_path):
    model = GitAPI(tmp_path / "private.git")
    api = transport(model)
    store = VersionedStore(GitHubStore(model.binding, api))
    readers = []

    def fresh():
        reader = VersionedStore(GitHubStore(replace(model.binding, write_enabled=False), api))
        readers.append(reader)
        return reader

    before = api._requests
    with pytest.raises(RecordError, match="AUTHORIZATION_REQUIRED"):
        retain_market_query(raw_payload()["stock_results"][CODE], REGISTRY, store, fresh_reader=fresh)
    assert api._requests == before and model.writes == 0
    saved = retain_market_query(raw_payload()["stock_results"][CODE], REGISTRY, store,
                                fresh_reader=fresh, custody_authorized=True)
    assert saved["reference"]["manifest"]["repository"] == model.binding.repository
    assert model.writes == 2
    assert len(readers) == 1 and readers[0].backend.api is api
    assert readers[0].backend is not store.backend and readers[0].backend.binding.write_enabled is False
    assert api._requests == len(model.requests) <= 512


def test_public_target_still_requires_public_rights_and_new_api_reader_is_rejected(tmp_path):
    model = GitAPI(tmp_path / "public.git")
    model.binding = replace(model.binding, visibility="public")
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.meta["private"] = False
    api = transport(model)
    store = VersionedStore(GitHubStore(model.binding, api))
    with pytest.raises(RecordError, match="PUBLIC_RIGHTS_REQUIRED"):
        authorize_market_custody(store, custody_authorized=True)
    assert model.writes == 0
    with pytest.raises(RecordError, match="SHARED_OPERATION_BUDGET_REQUIRED"):
        retain_market_query(raw_payload()["stock_results"][CODE], REGISTRY, store,
            public_rights_confirmed=True,
            fresh_reader=lambda: VersionedStore(GitHubStore(replace(model.binding, write_enabled=False), transport(model))))
    assert model.writes == 0
    _, states, _ = capture_states()
    unrelated = lambda: VersionedStore(GitHubStore(replace(model.binding, write_enabled=False), transport(model)))
    with pytest.raises(RecordError, match="SHARED_OPERATION_BUDGET_REQUIRED"):
        retain_market_checkpoint(states[0], REGISTRY, store, checkpoint_id="synthetic-query",
                                 fresh_reader=unrelated, public_rights_confirmed=True)
    with pytest.raises(RecordError, match="SHARED_OPERATION_BUDGET_REQUIRED"):
        retain_market_collection(collection(), REGISTRY, store,
                                 fresh_reader=unrelated, public_rights_confirmed=True)
    assert model.writes == 0


def test_each_checkpoint_preserves_original_control_and_deduplicates_accepted_pages():
    store, fresh = memory_store()
    previous, saved = None, []

    def keep(state):
        nonlocal previous
        result = retain_market_checkpoint(state, REGISTRY, store, checkpoint_id="synthetic-query",
            predecessor=previous, fresh_reader=fresh, custody_authorized=True)
        assert result["restored"]["state"] == state
        saved.append(result)
        previous = result["reference"]

    capture, states, opener = capture_states(writer=keep)
    assert capture["complete"] and len(opener.calls) == 3
    assert len(saved) == len(states)
    assert restore_market_checkpoint(saved[-1]["reference"], REGISTRY, fresh())["state"] == states[-1]
    root = json.loads(store.read_source(SourceRef.from_dict(saved[-1]["reference"]))["data"])
    assert root["schema"] == CHECKPOINT_SCHEMA and "pages" not in root["capture"]
    assert all(set(p) == {"reference", "source_sha256", "attempts"} for p in root["pages"])
    assert root["pages"][0]["attempts"] == capture["pages"][0]["attempts"]
    # One immutable source version per accepted original page; control versions
    # are a separate source family and never copy supplier table rows.
    page_paths = [p for p in store.backend.paths(store.backend.head())
                  if p.startswith("source-views/relay-query-") and p.endswith("/manifest.json")]
    assert len(page_paths) == 3
    before = store.backend.shared["count"]
    unchanged = retain_market_checkpoint(states[-1], REGISTRY, store, checkpoint_id="synthetic-query",
        predecessor=saved[-1]["reference"], fresh_reader=fresh, custody_authorized=True)
    assert unchanged["reference"] == saved[-1]["reference"] and store.backend.shared["count"] == before
    old = next(i for i, state in enumerate(states) if state["control"]["state"] == "ready"
               and len(state["capture"]["pages"]) == 1)
    assert restore_market_checkpoint(saved[old]["reference"], REGISTRY, fresh())["state"] == states[old]


def test_query_promotion_reuses_only_independently_restored_matching_checkpoint_pages():
    capture, states, _ = capture_states()
    store, fresh = memory_store()
    checkpoint = retain_market_checkpoint(states[-1], REGISTRY, store, checkpoint_id="promoted-query",
                                         fresh_reader=fresh, custody_authorized=True)
    result = {"case_id": "synthetic-market", "use": states[-1]["control"]["request"],
              "status": "captured", "capture": capture}
    writes = store.backend.shared["count"]
    query = retain_market_query(result, REGISTRY, store, fresh_reader=fresh,
        checkpoint_reference=checkpoint["reference"], custody_authorized=True)
    assert store.backend.shared["count"] == writes + 1
    assert query["restored"]["page_references"] == checkpoint["restored"]["page_references"]
    changed = deepcopy(result); changed["capture"]["pages"][0]["table"]["items"][0][2] = "999"
    writes = store.backend.shared["count"]
    with pytest.raises(RecordError, match="CHECKPOINT_MISMATCH"):
        retain_market_query(changed, REGISTRY, store, fresh_reader=fresh,
            checkpoint_reference=checkpoint["reference"], custody_authorized=True)
    assert store.backend.shared["count"] == writes


def test_checkpoint_predecessor_cannot_rewrite_accepted_page_or_be_reading_envelope():
    _, states, _ = capture_states()
    state = next(s for s in states if s["control"]["state"] == "ready" and len(s["capture"]["pages"]) == 1)
    store, fresh = memory_store()
    saved = retain_market_checkpoint(state, REGISTRY, store, checkpoint_id="synthetic-query",
                                    fresh_reader=fresh, custody_authorized=True)
    changed = deepcopy(state)
    changed["capture"]["pages"][0]["table"]["items"][0][2] = "999"
    writes = store.backend.shared["count"]
    with pytest.raises(RecordError, match="PAGE_HISTORY_CHANGED"):
        retain_market_checkpoint(changed, REGISTRY, store, checkpoint_id="synthetic-query",
            predecessor=saved["reference"], fresh_reader=fresh, custody_authorized=True)
    assert store.backend.shared["count"] == writes
    query = retain_market_query(raw_payload()["stock_results"][CODE], REGISTRY, store,
                               fresh_reader=fresh, custody_authorized=True)
    with pytest.raises(RecordError, match="CUSTODY_SCHEMA"):
        restore_market_checkpoint(query["reference"], REGISTRY, fresh())


@pytest.mark.parametrize("kind", ["three-failures", "unknown-reservation"])
def test_standalone_fresh_control_cannot_reset_a_retained_predecessor(kind):
    from test_relay_capture_checkpoint import run, busy, fresh_looking_state
    if kind == "three-failures":
        _, states, _, _, _ = run(*(busy(504) for _ in range(3)), budget=100)
    else:
        states = []
        with pytest.raises(RuntimeError, match="synthetic response lost"):
            run(RuntimeError("synthetic response lost"), writer=lambda s: states.append(deepcopy(s)))
    previous = states[-1]
    store, fresh = memory_store()
    retained = retain_market_checkpoint(previous, REGISTRY, store, checkpoint_id="original-budget",
                                        fresh_reader=fresh, custody_authorized=True)
    writes = store.backend.shared["count"]
    with pytest.raises(RecordError, match="BUDGET_RESET"):
        retain_market_checkpoint(fresh_looking_state(previous), REGISTRY, store,
            checkpoint_id="original-budget", fresh_reader=fresh,
            predecessor=retained["reference"], custody_authorized=True)
    assert store.backend.shared["count"] == writes
    assert restore_market_checkpoint(retained["reference"], REGISTRY, fresh())["state"] == previous


def test_public_restores_share_nested_read_scope_and_propagate_exit_failure():
    store, fresh = memory_store()
    query = retain_market_query(raw_payload()["stock_results"][CODE], REGISTRY, store,
                               fresh_reader=fresh, custody_authorized=True)
    _, states, _ = capture_states()
    checkpoint = retain_market_checkpoint(states[-1], REGISTRY, store, checkpoint_id="scope-query",
                                         fresh_reader=fresh, custody_authorized=True)
    root = retain_market_collection(collection(), REGISTRY, store, fresh_reader=fresh, custody_authorized=True)

    class ScopedBackend(MemoryBackend):
        depth, groups, fail_exit = 0, 0, False

        @contextmanager
        def market_read_scope(self):
            if not self.depth:
                self.groups += 1
            self.depth += 1
            try:
                yield self
            finally:
                self.depth -= 1
                if not self.depth and self.fail_exit:
                    raise RecordError("SYNTHETIC_FINAL_BINDING_REVOKED")

        def read(self, commit, path):
            assert self.depth > 0
            return super().read(commit, path)

    backend = ScopedBackend(store.backend.shared)
    with market_read_scope(VersionedStore(backend)) as reader:
        restore_market_query(query["reference"], REGISTRY, reader)
        restore_market_checkpoint(checkpoint["reference"], REGISTRY, reader)
        restore_market_collection(root["reference"], REGISTRY, reader)
    assert backend.groups == 1 and backend.depth == 0
    backend.fail_exit = True
    with pytest.raises(RecordError, match="FINAL_BINDING_REVOKED"):
        restore_market_query(query["reference"], REGISTRY, VersionedStore(backend))
    assert backend.groups == 2 and backend.depth == 0


def test_checkpoint_unknown_write_prevents_first_source_attempt():
    store, fresh = memory_store()
    calls = []

    def unknown(files, *, expected):
        calls.append(files)
        raise UnknownWrite("SYNTHETIC_WRITE_UNKNOWN")

    store.backend.commit = unknown
    with pytest.raises(CheckpointWriteError):
        capture_states(writer=lambda s: retain_market_checkpoint(s, REGISTRY, store,
            checkpoint_id="synthetic-query", fresh_reader=fresh, custody_authorized=True))
    assert len(calls) == 1


def test_collection_root_is_reference_only_and_preserves_successor_generation():
    store, fresh = memory_store()
    original = collection()
    first = retain_market_collection(original, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    changed = collection("successor")
    second = retain_market_collection(changed, REGISTRY, store, fresh_reader=fresh,
        predecessor=first["reference"], custody_authorized=True)
    assert first["source_id"] == collection_source_id(original["plan"])
    assert first["source_id"] != second["source_id"]
    assert restore_market_collection(first["reference"], REGISTRY, fresh())["collection"] == original
    restored = restore_market_collection(second["reference"], REGISTRY, fresh())
    assert restored["predecessor"] == first["reference"] and restored["queries_restored"] is False
    same = retain_market_collection(changed, REGISTRY, store, fresh_reader=fresh,
        predecessor=first["reference"], custody_authorized=True)
    assert same["reference"] == second["reference"]
    another = collection(); another["plan"]["collection_id"] = "different"
    with pytest.raises(RecordError):
        retain_market_collection(another, REGISTRY, store, fresh_reader=fresh,
            predecessor=first["reference"], custody_authorized=True)


def test_collection_foreign_query_and_clock_reject_before_any_write():
    store, fresh = memory_store()
    query = retain_market_query(raw_payload()["stock_results"][CODE], REGISTRY, store,
                               fresh_reader=fresh, custody_authorized=True)
    bad = collection()
    bad["units"]["calendar:SSE"].update(status="complete", query=deepcopy(query["reference"]))
    bad["units"]["calendar:SSE"]["query"]["manifest"]["repository"] = "another/store"
    writes = store.backend.shared["count"]
    with pytest.raises(RecordError, match="EXACT_LOCAL_REFERENCE_REQUIRED"):
        retain_market_collection(bad, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    bad = collection(); bad["attempt"]["updated_at"] = "2026-10-16T10:00:00+00:00"
    with pytest.raises(RecordError, match="CLOCK"):
        retain_market_collection(bad, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    assert store.backend.shared["count"] == writes


def test_new_process_restores_checkpoint_after_capture_file_removed(tmp_path):
    root = tmp_path / "synthetic.git"
    store = VersionedStore(GitStore.init_synthetic(root))
    fresh = lambda: VersionedStore(GitStore(root))
    _, states, _ = capture_states()
    original = states[-1]
    capture_file = tmp_path / "capture.json"
    capture_file.write_bytes(encoded(original))
    saved = retain_market_checkpoint(original, REGISTRY, store, checkpoint_id="independent-query",
                                    fresh_reader=fresh, custody_authorized=True)
    reference_file = tmp_path / "reference.json"
    reference_file.write_bytes(encoded(saved["reference"]))
    capture_file.unlink()
    script = """import json,sys
from pathlib import Path
from decision_kernel.application.market_collection_custody import restore_market_checkpoint
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.domain.records import digest,encoded
out=restore_market_checkpoint(json.loads(Path(sys.argv[2]).read_bytes()),RelayRegistry(Path(sys.argv[3])),VersionedStore(GitStore(Path(sys.argv[1]))))
print(json.dumps({'state_sha256':digest(encoded(out['state'])),'checkpoint_id':out['checkpoint_id'],'source_requests':0}))
"""
    completed = subprocess.run([sys.executable, "-S", "-c", script, str(root), str(reference_file), str(REGISTRY_PATH)],
        check=True, capture_output=True, text=True,
        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT_DIR / "src"), "LANG": "C.UTF-8"})
    assert json.loads(completed.stdout) == {"state_sha256": digest(encoded(original)),
                                           "checkpoint_id": "independent-query", "source_requests": 0}
    assert not capture_file.exists()


def test_native_git_daily_layout_accounts_for_checkpoint_and_directory_growth(tmp_path):
    """The 61-day capacity model counts native directories, not only file paths."""
    backend = GitStore.init_synthetic(tmp_path / "capacity.git")
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(GitStore(backend.root))
    root = collection()
    root["plan"]["sessions"] = ["20261016"]
    for api in ("daily", "adj_factor", "suspend_d"):
        root["units"]["20261016:" + api] = {
            "query": None, "checkpoint": None, "status": "pending", "reason_code": None}

    def size():
        rows = backend._run("ls-tree", "-r", "-t", "-z", backend.head()).stdout.split(b"\0")
        paths = backend.paths(backend.head())
        versions = sum(p.startswith("source-views/") and p.endswith("/manifest.json") for p in paths)
        return len([r for r in rows if r]), len(paths), versions

    reference = retain_market_collection(root, REGISTRY, store,
        fresh_reader=fresh, custody_authorized=True)["reference"]
    assert size() == (9, 5, 1)
    deltas = []
    for api in ("daily", "adj_factor", "suspend_d"):
        slot = "20261016:" + api
        use, max_pages = unit_request(root["plan"], slot, REGISTRY)
        fields = use["parameters"]["fields"].split(",")
        rows = []
        for n in range(0 if api == "suspend_d" else 5001):
            row = {"ts_code": f"{600000+n:06}.SH", "trade_date": "20261016", "close": "10", "adj_factor": "1"}
            rows.append([row[field] for field in fields])
        opener = SyntheticOpener(api, fields, rows)
        session = RetryingRelay(RelayHTTP(opener=opener, credential="synthetic-key-only"), clock=lambda: STAMP)
        latest = None

        def keep(state):
            nonlocal latest
            latest = retain_market_checkpoint(state, REGISTRY, store,
                checkpoint_id=checkpoint_id(root["plan"], slot), predecessor=latest,
                fresh_reader=fresh, custody_authorized=True)["reference"]

        before = size()
        capture = capture_registered(use, REGISTRY, session, max_pages=max_pages, checkpoint_writer=keep)
        query = retain_market_query({"case_id": root["plan"]["collection_id"], "use": use,
            "status": "captured", "capture": capture}, REGISTRY, store,
            fresh_reader=fresh, custody_authorized=True)
        root["units"][slot].update(query=query["reference"], checkpoint=latest, status="complete")
        root["attempt"].update(status="partial", source_requests=root["attempt"]["source_requests"] + len(opener.calls))
        reference = retain_market_collection(root, REGISTRY, store, predecessor=reference,
            fresh_reader=fresh, custody_authorized=True)["reference"]
        deltas.append(tuple(b - a for a, b in zip(before, size())))
    assert deltas == [(40, 28, 8), (40, 28, 8), (26, 18, 5)]
    assert size() == (115, 79, 22)
