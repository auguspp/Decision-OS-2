"""Private market custody authorization stays separate from publication rights."""
from dataclasses import replace
import json
import subprocess

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import market_custody_cli, market_discovery_cli
import test_market_custody as custody_fixture
from test_market_custody import raw_payload, REGISTRY_PATH
from test_market_discovery import AS_OF, REGISTRY, retain, rising, storage
from test_market_snapshot import local_fetch, native
from test_news_read_budget import transport


@pytest.mark.parametrize("operation", ["save", "discovery-save"])
def test_runtime_forwards_private_authorization_without_claiming_public_rights(tmp_path, operation):
    fixture = custody_fixture.RuntimeTests(); fixture.setUp()
    payload = raw_payload() if operation == "save" else {
        "schema": "market-discovery-plan-v1", "as_of": AS_OF,
        "surfaces": {"sector": {"reference": fixture.ref}}}
    event = fixture.event(operation, payload)
    event["inputs"].update(custody_authorized="true", public_rights_confirmed="false")
    env = {**fixture.env(tmp_path), "FTSHARE_API_KEY": "synthetic-secret-must-not-reach-child"}
    out = tmp_path / "private-custody"

    def child(command, **kwargs):
        assert "--custody-authorized" in command and "--public-rights-confirmed" not in command
        assert "FTSHARE_API_KEY" not in kwargs["env"] and "TUSHARE_PROXY_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["DECISION_OS_STORE_TOKEN"] == env["DECISION_OS_STORE_TOKEN"]
        assert json.loads((out / "binding.json").read_bytes())["write_enabled"] is True
        assert ("--submission-id" in command) == (operation == "discovery-save")
        return subprocess.CompletedProcess(command, 0, "{}", "")

    assert fixture.invoke(event, env, out, child) == 0
    receipt = json.loads((out / "result.json").read_bytes())
    assert receipt["custody_authorized"] is True and receipt["public_rights_confirmed"] is False
    assert receipt["source_requests"] == 0


@pytest.mark.parametrize("authorization", [False, 1, "yes", {}, []])
def test_invalid_private_authorization_cannot_launch_save(tmp_path, authorization):
    fixture = custody_fixture.RuntimeTests(); fixture.setUp()
    event = fixture.event("save", raw_payload())
    event["inputs"].update(custody_authorized=authorization, public_rights_confirmed=False)
    out = tmp_path / "unauthorized"
    with pytest.raises(RecordError, match="RUNTIME_AUTHORIZATION"):
        fixture.invoke(event, fixture.env(tmp_path), out,
                       lambda *args, **kwargs: pytest.fail("unauthorized child launched"))
    assert not out.exists()


def test_discovery_private_save_and_exact_read_do_not_need_public_permission(storage):
    source = retain(storage, rising(30))
    plan = {"schema": "market-discovery-plan-v1", "as_of": AS_OF,
            "surfaces": {"sector": {"reference": source}}}
    saved = market_discovery_cli.execute("save", plan, storage[0], registry=REGISTRY,
        submission_id="explicit-private-state", custody_authorized=True, rights_confirmed=False)
    head = storage[0].backend.head()
    restored = market_discovery_cli.execute("read", saved["reference"], storage[1](), registry=REGISTRY)
    assert restored["view_sha256"] == saved["result"]["view_sha256"]
    assert storage[0].backend.head() == head and restored["source_requests"] == 0


@pytest.mark.parametrize("authorization", [1, "true", None])
def test_discovery_save_rejects_nonboolean_authorization_before_write(storage, authorization):
    source = retain(storage, rising(30))
    plan = {"schema": "market-discovery-plan-v1", "as_of": AS_OF,
            "surfaces": {"sector": {"reference": source}}}
    head = storage[0].backend.head()
    with pytest.raises(RecordError, match="SAVE_NOT_AUTHORIZED"):
        market_discovery_cli.execute("save", plan, storage[0], registry=REGISTRY,
            submission_id="invalid-authorization", custody_authorized=authorization)
    assert storage[0].backend.head() == head


def test_discovery_private_authorization_cannot_write_a_public_native_target(native):
    model, _ = native
    api = transport(model)
    store = VersionedStore(GitHubStore(model.binding, api))
    model.meta["private"] = False
    fixture = custody_fixture.RuntimeTests(); fixture.setUp()
    plan = {"schema": "market-discovery-plan-v1", "as_of": AS_OF,
            "surfaces": {"sector": {"reference": fixture.ref}}}
    before = model.git.head()
    with pytest.raises(RecordError, match="PRIVATE_BINDING"):
        market_discovery_cli.execute("save", plan, store, registry=REGISTRY,
            submission_id="public-target-denied", custody_authorized=True)
    assert model.git.head() == before and model.writes == 0


def test_native_custody_cli_uses_independent_git_readers_and_one_api(native, monkeypatch, capsys):
    model, _ = native
    api, fetches = transport(model), []
    monkeypatch.setattr("decision_kernel.infrastructure.market_snapshot._fetch_market_branch",
                        local_fetch(model, fetches))
    monkeypatch.setattr(market_custody_cli.GitHubAPI, "from_environment", lambda: api)
    monkeypatch.setattr(market_custody_cli, "_load", lambda path:
        replace(model.binding, write_enabled=True).__dict__ if path.name == "binding.json" else raw_payload())
    code = market_custody_cli.main(["save", "--binding", "binding.json", "--input", "payload.json",
        "--registry", str(REGISTRY_PATH), "--custody-authorized"])
    output = capsys.readouterr()
    assert code == 0, output.err
    result = json.loads(output.out)
    assert result["status"] == "stored-and-independently-restored"
    assert result["restored"]["view"]["scope_count"] == 2
    assert len(fetches) >= 2 and all(call["api"] is api for call in fetches)
    assert 0 < api._requests == len(model.requests) < 512 and model.writes > 0
