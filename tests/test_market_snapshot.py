"""Original SourceRefs through native Git, with one shared bounded GitHub API.

Only the remote responses and fetch transport are synthetic. Native Git owns
the actual commit graph, reference bodies and immutable predecessor history.
"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import base64
import subprocess

import pytest

from decision_kernel.application.market_collection import unit_request
from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.application.market_collection_custody import (
    retain_market_checkpoint, restore_market_checkpoint,
)
from decision_kernel.application.market_custody import market_read_scope, retain_market_query
from decision_kernel.application.research_sources import capture_registered
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure.market_snapshot import GitMarketReader, _fetch_market_branch
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from test_github_store import GitAPI
from test_market_collection_custody import collection, STAMP
from test_market_relay_inputs import REGISTRY, SyntheticOpener
from test_news_read_budget import transport


@pytest.fixture
def native(tmp_path):
    model = GitAPI(tmp_path / "private.git")
    model.binding = replace(model.binding, branch="records")
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    # Original local store only seeds synthetic fixtures. Production reads below
    # independently validate the real binding marker and exact native history.
    model.git.repository = model.binding.repository
    model.git.binding = model.binding
    return model, VersionedStore(model.git)


def local_fetch(model, calls):
    def fetch(binding, api, destination):
        assert binding == replace(model.binding, write_enabled=False)
        calls.append({"destination": destination, "api": api, "head": model.git.head()})
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
            "-c", "protocol.file.allow=always", "clone", "--bare", "--template=",
            str(model.git.root), str(destination)], env=GitStore._env(), capture_output=True,
            check=True, timeout=30)
    return fetch


def source(text):
    return SourceInput(source_id="native-market-source", kind="content", title="Synthetic market source",
                       origin="https://example.invalid/synthetic-market", text=text, custody="provided")


def test_lazy_nested_scope_keeps_original_refs_and_repins_only_on_next_outer_group(native):
    model, store = native
    first = store.put_source("first", source("original"))
    api, fetches = transport(model), []
    backend = GitMarketReader(replace(model.binding, write_enabled=False), api,
                              fetch=local_fetch(model, fetches))
    reader = VersionedStore(backend)
    assert api._requests == 0 and not fetches
    with market_read_scope(reader) as grouped:
        pinned = grouped.backend.head()
        budget = api._requests
        assert grouped.read_source(first)["data"] == "original"
        second = store.put_source("second", source("successor"))
        with market_read_scope(grouped) as nested:
            assert nested.backend.head() == pinned
            assert nested.read_source(first)["data"] == "original"
            with pytest.raises(RecordError, match="COMMIT_OUTSIDE_BINDING"):
                nested.read_source(second)
        assert api._requests == budget and len(fetches) == 1
    assert api._requests > budget and all(not c["destination"].exists() for c in fetches)
    with market_read_scope(reader) as grouped:
        assert grouped.backend.head() == second.manifest.commit
        assert grouped.read_source(second)["data"] == "successor"
        assert grouped.read_source(first)["data"] == "original"
    assert len(fetches) == 2 and api._requests == len(model.requests) < 64
    assert all(c["api"] is api and not c["destination"].exists() for c in fetches)


@pytest.mark.parametrize("drift", ["visibility", "reachability"])
def test_final_live_binding_drift_prevents_a_scoped_result_from_returning(native, drift):
    model, store = native
    reference = store.put_source("original", source("private rows"))
    api, fetches = transport(model), []
    backend = GitMarketReader(replace(model.binding, write_enabled=False), api,
                              fetch=local_fetch(model, fetches))
    released = []

    def obtain():
        with market_read_scope(VersionedStore(backend)) as reader:
            value = reader.read_source(reference)
            if drift == "visibility":
                model.meta["private"] = False
            else:
                model.git._run("update-ref", "refs/heads/records", model.before_anchor, model.git.head())
            return value

    with pytest.raises(RecordError):
        released.append(obtain())
    assert not released and backend._depth == 0 and backend._reader is None
    assert all(not call["destination"].exists() for call in fetches)


def test_source_only_reader_refuses_writes_foreign_refs_and_research_paths(native):
    model, store = native
    reference = store.put_source("one", source("provided source"))
    api, fetches = transport(model), []
    backend = GitMarketReader(replace(model.binding, write_enabled=False), api,
                              fetch=local_fetch(model, fetches))
    before = model.git.head()
    with pytest.raises(RecordError, match="READ_ONLY"):
        backend.commit({"submissions/arbitrary.json": b"{}"}, expected=before)
    with pytest.raises(RecordError, match="SOURCE_ONLY"):
        backend.paths(before, "research/cases/")
    with pytest.raises(RecordError, match="SOURCE_ONLY"):
        backend.read(before, "research/cases/case/current.json")
    with pytest.raises(RecordError, match="SOURCE_ONLY"):
        backend.reference(before, "submissions/arbitrary.json")
    assert api._requests == 0 and not fetches and model.git.head() == before
    foreign = replace(reference, manifest=replace(reference.manifest, repository="another/store"))
    with market_read_scope(VersionedStore(backend)) as reader:
        with pytest.raises(RecordError, match="FOREIGN_REPOSITORY"):
            reader.read_source(foreign)
        corrupted = replace(reference, manifest=replace(reference.manifest, sha256="0" * 64))
        with pytest.raises(RecordError, match="BLOB_DIGEST_MISMATCH"):
            reader.read_source(corrupted)
        assert reader.read_source(reference)["data"] == "provided source"
    assert model.git.head() == before and model.writes == 0


def test_native_capacity_counts_one_pinned_snapshot_and_rechecks_visibility(native):
    model, store = native
    store.put_source("one", source("private source rows"))
    api, fetches = transport(model), []
    backend = GitMarketReader(replace(model.binding, write_enabled=False), api,
                              fetch=local_fetch(model, fetches))
    reader = VersionedStore(backend)
    result = market_capacity(reader)
    assert result["tree_entries"] == 9 and result["version_count"] == 1
    assert result["native_bytes"] > 0 and result["admit_work_unit"] is True
    assert result["tree_response_applicability"] == "required"
    assert 0 < result["tree_response_bytes_estimate"] < (
        result["limits"]["tree_response_bytes_estimate"] - result["work_unit_reserve"]["tree_response_bytes_estimate"])
    assert len(fetches) == 1 and api._requests == len(model.requests) < 32
    assert b"private source rows" not in encoded(result) and b"source-views" not in encoded(result)
    original = backend.market_inventory
    def revoked():
        inventory = original()
        model.meta["private"] = False
        return inventory
    backend.market_inventory = revoked
    released = []
    with pytest.raises(RecordError, match="PRIVATE_BINDING_MISMATCH"):
        released.append(market_capacity(reader))
    assert not released and backend._depth == 0 and backend._reader is None


def test_fetch_credentials_stay_in_child_environment_and_failure_prose_is_not_returned(native, tmp_path, monkeypatch, capsys):
    model, _ = native
    api = transport(model)
    readonly = replace(model.binding, write_enabled=False)
    calls = []
    token = api._token
    header = "Authorization: Basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
    destination = tmp_path / "fetched.git"

    def run(command, **kwargs):
        calls.append((command, kwargs))
        destination.mkdir()
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("decision_kernel.infrastructure.market_snapshot.subprocess.run", run)
    _fetch_market_branch(readonly, api, destination)
    command, options = calls[0]
    assert token not in repr(command) and header not in repr(command)
    assert command[-2:] == ["https://github.com/" + readonly.repository + ".git", str(destination)]
    assert "http.followRedirects=false" in command and "credential.helper=" in command
    assert options["env"]["GIT_CONFIG_VALUE_0"] == header
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert options["capture_output"] is True and options["timeout"] == 120
    assert not capsys.readouterr().out and not capsys.readouterr().err

    def rejected(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout=token.encode(), stderr=(header + " private server prose").encode())

    monkeypatch.setattr("decision_kernel.infrastructure.market_snapshot.subprocess.run", rejected)
    with pytest.raises(RecordError, match="^MARKET_SNAPSHOT_FETCH_FAILED$") as failure:
        _fetch_market_branch(readonly, api, tmp_path / "failed.git")
    assert token not in str(failure.value) and header not in str(failure.value)
    assert api._requests == 0 and not capsys.readouterr().out


def test_twenty_original_master_pages_restore_in_one_bounded_native_scope(native, monkeypatch):
    model, store = native
    use, max_pages = unit_request(collection()["plan"], "master:SSE", REGISTRY)
    fields = use["parameters"]["fields"].split(",")
    rows = []
    for n in range(9501):
        row = {"ts_code": f"{600000+n:06}.SH", "symbol": f"{600000+n:06}", "name": "合成证券" + str(n),
            "exchange": "SSE", "curr_type": "CNY", "list_status": "L", "list_date": "20000101", "delist_date": None}
        rows.append([row[field] for field in fields])
    opener = SyntheticOpener("stock_basic", fields, rows)
    client = RelayHTTP(opener=opener, credential="synthetic-key-only")
    checkpoints = []
    capture = capture_registered(use, REGISTRY, RetryingRelay(client, clock=lambda: STAMP),
        max_pages=max_pages, checkpoint_writer=lambda s: checkpoints.append(deepcopy(s)))
    assert capture["complete"] and len(capture["pages"]) == client.requests == 20
    # Seed one final complete checkpoint through the original versioned store.
    # This is not a claim that its 21 original source writes fit a remote unit.
    api, fetches = transport(model), []
    fresh = lambda: VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
                                                   fetch=local_fetch(model, fetches)))
    saved = retain_market_checkpoint(checkpoints[-1], REGISTRY, store, checkpoint_id="twenty-page-master",
                                     fresh_reader=fresh, custody_authorized=True)
    before = api._requests
    assert before < 32 and len(fetches) == 1
    reader = fresh()
    def no_source(*args, **kwargs):
        raise AssertionError("restoration must not construct a Relay client")
    monkeypatch.setattr("decision_kernel.infrastructure.relay_http.RelayHTTP.__init__", no_source)
    restored = restore_market_checkpoint(saved["reference"], REGISTRY, reader)
    assert digest(encoded(restored["state"])) == digest(encoded(checkpoints[-1]))
    assert len(restored["state"]["capture"]["pages"]) == 20
    assert len(fetches) == 2 and api._requests == len(model.requests) < 64
    assert api._requests - before < 32
    assert client.requests == 20 and model.writes == 0
    assert all(call["api"] is api and not call["destination"].exists() for call in fetches)
    # Complete checkpoint promotion writes only the query manifest, without
    # replaying twenty saved pages against the writer's same finite API budget.
    writer = VersionedStore(GitHubStore(model.binding, api))
    before = api._requests
    promoted = retain_market_query({"case_id": "synthetic-market", "use": use,
        "status": "captured", "capture": capture}, REGISTRY, writer,
        fresh_reader=fresh, checkpoint_reference=saved["reference"], custody_authorized=True)
    assert promoted["restored"]["page_references"] == restored["page_references"]
    assert model.writes == 1 and api._requests == len(model.requests) <= 512
    assert api._requests - before < 64 and client.requests == 20
