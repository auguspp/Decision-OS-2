"""News operation reuse over real GitHubStore + bounded GitHubAPI + real Git.

Only the wire response is synthetic; no production credentials, source fetches,
second store implementation, or lowered/raised 512-request budget.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from decision_kernel.application import news_rolling_index as index
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, encoded
from decision_kernel.infrastructure import github_store as remote
from decision_kernel.interfaces import news_rolling_cli as host, research_runtime
from decision_kernel.interfaces.research_payload import decode_payload
from test_github_store import GitAPI
from test_github_transport import Response
from test_news_rolling import END, save, window


class ModelWire:
    def __init__(self, model):
        self.model = model

    def open(self, request, timeout):
        assert timeout == 30
        path = request.full_url.removeprefix(remote.API_ROOT)
        value = self.model.request(request.method, path,
                                   json.loads(request.data) if request.data else None)
        return Response(encoded(value), request.full_url)


def transport(model):
    api = remote.GitHubAPI("synthetic-news-budget-token")
    api._opener = ModelWire(model)
    return api


@pytest.fixture
def sources(tmp_path, request):
    model = GitAPI(tmp_path / "news-model.git")
    model.binding = replace(model.binding, repository=host.REPOSITORY,
                            repository_id=1420041744, branch="records",
                            visibility="public", write_enabled=False)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.meta.update(id=1420041744, full_name=host.REPOSITORY, private=False)
    # Seed the fixture through the original local store; hosted readers below
    # independently enforce the public marker, exact references and Git bytes.
    model.git.repository = host.REPOSITORY
    store = VersionedStore(model.git)
    refs = [save(store, "batch-" + str(n), window("2026-10-16T10:00:00Z",
             item=str(n), title="合成新闻" + str(n), failed=n == 0)) for n in range(getattr(request, "param", 2))]
    previous = index.rolling_index(store.read_source, refs, as_of=END)
    prior = save(store, "prior", previous)
    new = [save(store, "new-" + str(n), window("2026-10-16T11:00:00Z",
            item=str(n), title="合成新增" + str(n))) for n in range(4)]
    plan = {"as_of": END, "batch_references": [asdict(r) for r in new], "predecessor": asdict(prior)}
    expected = index.rolling_index(store.read_source, new, as_of=END, predecessor=prior)
    model.requests.clear()
    return model, store, refs, plan, expected


def setup(monkeypatch, tmp_path, model, plan, *, saving=True, operation="build"):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config/store-binding.json").write_bytes(encoded(asdict(model.binding)))
    inputs = {"code_sha": "a" * 40, "operation": operation,
              "save": "true" if saving else "false", "plan": encoded(plan).decode(),
              "submission_id": "budget-window" if saving else "",
              "expected_head": model.git.head() if saving else "",
              "public_metadata_custody_confirmed": "true" if saving else "false"}
    event = tmp_path / "event.json"
    event.write_bytes(encoded({"inputs": inputs}))
    monkeypatch.chdir(tmp_path)
    for key, value in {"GITHUB_REPOSITORY": host.REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW_REF": host.WORKFLOW,
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_RUN_ID": "123", "GITHUB_SHA": "a" * 40,
        "GITHUB_EVENT_PATH": str(event), "DOS2_NEWS_MODE": "write" if saving else "read"}.items():
        monkeypatch.setenv(key, value)
    api = transport(model)
    monkeypatch.setattr(host.GitHubAPI, "from_environment", lambda: api)
    return api, inputs


def test_exact_ref_reuse_detaches_results_and_is_operation_local(sources):
    model, _, refs, _, _ = sources
    api = transport(model)
    reader = VersionedStore(remote.GitHubStore(model.binding, api)).read_source
    reuse = host._SourceReads(reader)
    original = deepcopy(reuse(refs[0]))
    count = api._requests
    a = reuse(asdict(refs[0])); a["manifest"]["input"]["title"] = "must not poison"
    a["data"] = "must not replace stored bytes"
    assert reuse(refs[0]) == original and api._requests == count
    assert reuse.usage()["reused_source_reads"] == 2
    assert host._SourceReads(reader)(refs[0]) == original and api._requests > count
    assert reuse(refs[1]) != original


@pytest.mark.parametrize("field", ["sha256", "commit", "path", "repository"])
def test_full_reference_key_never_aliases_corrupt_or_foreign_source(sources, field):
    model, _, refs, _, _ = sources
    reader = host._SourceReads(VersionedStore(remote.GitHubStore(model.binding, transport(model))).read_source)
    valid = reader(refs[0])
    bad = asdict(refs[0])
    bad["manifest"][field] = {"sha256": "0" * 64, "commit": model.binding.anchor,
        "path": refs[1].manifest.path, "repository": "example/foreign"}[field]
    with pytest.raises(RecordError): reader(bad)
    assert reader(refs[0]) == valid
    assert reader.usage()["cached_references"] == 1


def test_failed_read_not_cached_and_memory_bound_falls_back_without_losing_sources(sources, monkeypatch):
    _, store, refs, _, _ = sources
    calls = []
    def transient(ref):
        calls.append(ref)
        if len(calls) == 1: raise RecordError("SYNTHETIC_UNAVAILABLE")
        return store.read_source(ref)
    reader = host._SourceReads(transient)
    with pytest.raises(RecordError): reader(refs[0])
    assert reader.usage()["cached_references"] == 0
    result = reader(refs[0]); assert len(calls) == 2
    assert reader(refs[0]) == result and len(calls) == 2
    monkeypatch.setattr(host, "MAX_SOURCE_CACHE_BYTES", 1)
    bounded = host._SourceReads(store.read_source)
    assert bounded(refs[0]) == bounded(refs[0]) == result
    assert bounded.usage()["serialized_bytes"] == 0 and bounded.usage()["verified_source_reads"] == 2
    assert bounded(refs[1])["data"] != result["data"]


@pytest.mark.parametrize("sources", [12], indirect=True)
def test_production_request_limit_is_not_reset_and_cached_host_saves_then_cold_reads(sources, tmp_path, monkeypatch):
    model, _, _, plan, expected = sources
    before = model.git.head()
    # Protect the current consumer's complete save/read budget. Shared immutable
    # Git proof reuse can make the former host's fixed-cost failure disappear.
    start = len(model.requests)
    api, _ = setup(monkeypatch, tmp_path, model, plan)
    out = tmp_path / "saved"
    assert host.main(["build", "--out", str(out)]) == 0
    assert json.loads((out / "view.json").read_bytes()) == expected
    report = json.loads((out / "result/report.json").read_bytes())
    assert report["status"] == "completed" and report["write"] == "published" and model.writes == 1
    assert model.git.head() != before
    assert 0 < api._requests == len(model.requests) - start < remote.MAX_REQUESTS == 512
    usage = json.loads((out / "read-usage.json").read_bytes())
    assert usage["verified_source_reads"] == 17 and usage["reused_source_reads"] > 0
    assert usage["serialized_bytes"] < usage["byte_limit"] == 8 * 1024 * 1024
    assert usage["storage_api_requests"] == api._requests and usage["binding_rechecked"]
    ref = report["result"]["reference"]
    start = len(model.requests)
    fresh_api, _ = setup(monkeypatch, tmp_path, model, {"reference": ref, "source": "none"},
                         saving=False, operation="read")
    fresh = tmp_path / "cold"
    assert host.main(["build", "--out", str(fresh)]) == 0
    assert json.loads((fresh / "view.json").read_bytes()) == expected
    page = json.loads((fresh / "reading.json").read_bytes())
    assert page["rows"] == [] and page["coverage"]["retained_versions"] == 16
    assert page["coverage"]["failed_source_windows"] == 1 and len(page["source_outcomes"]) == 17
    assert 0 < fresh_api._requests == len(model.requests) - start < remote.MAX_REQUESTS
    assert model.writes == 1


@pytest.mark.parametrize("change", ["privacy", "marker"])
def test_end_of_operation_rechecks_mutable_binding_before_export_or_save(sources, tmp_path, monkeypatch, change):
    model, _, _, plan, _ = sources
    setup(monkeypatch, tmp_path, model, plan)
    original = host.read_index
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "privacy": model.meta["private"] = True
        else: model.git.commit({"store.json": b'{}'}, expected=model.git.head())
        return result
    monkeypatch.setattr(host, "read_index", changed)
    out = tmp_path / "blocked"
    assert host.main(["build", "--out", str(out)]) == 2
    assert not (out / "view.json").exists() and not (out / "result").exists() and model.writes == 0


def test_new_operation_does_not_hide_corrupt_source_bytes(sources, tmp_path, monkeypatch):
    model, _, _, plan, expected = sources
    setup(monkeypatch, tmp_path, model, plan)
    out = tmp_path / "first"
    assert host.main(["build", "--out", str(out)]) == 0
    ref = json.loads((out / "result/report.json").read_bytes())["result"]["reference"]
    setup(monkeypatch, tmp_path, model, {"reference": ref}, saving=False, operation="read")
    def corrupt(path, value):
        if "/git/blobs/" in path: value["content"] = "eA=="
        return value
    model.corrupt_response = corrupt
    out2 = tmp_path / "corrupt"
    assert host.main(["build", "--out", str(out2)]) == 2
    assert not (out2 / "reading.json").exists() and model.writes == 1


def test_original_writer_unknown_effect_and_same_intent_readonly_reconciliation(sources, tmp_path, monkeypatch):
    model, _, _, plan, expected = sources
    setup(monkeypatch, tmp_path, model, plan)
    model.after_write = lambda _: (_ for _ in ()).throw(TimeoutError("synthetic lost response"))
    out = tmp_path / "unknown"
    assert host.main(["build", "--out", str(out)]) == 2
    report = json.loads((out / "result/report.json").read_bytes())
    assert report["status"] == "unknown-write" and report["write"] == "unconfirmed"
    original = json.loads((out / "result/inputs.json").read_bytes())
    assert json.loads(decode_payload(original["payload"])["text"]) == expected
    model.after_write = None
    original["operation"] = "reconcile-source"
    receipt = research_runtime.run_command(model.binding, api=transport(model), inputs=original,
                code_sha="a" * 40, output=tmp_path / "reconcile", run_id="124", attempt="1")
    assert receipt["write"] == "reconciled-without-write" and model.writes == 1
    cold = VersionedStore(remote.GitHubStore(model.binding, transport(model)))
    assert json.loads(cold.read_source(SourceRef.from_dict(receipt["result"]["reference"]))["data"]) == expected
