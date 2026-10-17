"""Actual store CLI over real GitHubAPI budgets and a real-Git response wire."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_delivery import ResearchDelivery, delivery_manifest
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, RecordError, RevisionInput, SourceInput, UnknownWrite, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure import github_store as remote
from decision_kernel.interfaces import delivery_commands, odds_commands
from decision_kernel.interfaces.cli import main
from decision_kernel.methods.research import research_method
from test_conditional_odds import authorized, spec_payload
from test_news_read_budget import transport
from test_odds_watch import watch_payload
from test_research_delivery import continued, delivery_payload
from test_research_runtime import host


class WireCLI:
    """Observe real factories and calls without replacing saves or verification."""

    def __init__(self, model, tmp_path, monkeypatch, capsys, properties):
        self.model, self.tmp_path, self.capsys = model, tmp_path, capsys
        self.properties = properties
        self.apis, self.stages = [], []
        self.before_fresh = None
        self.invocations = 0
        self.binding = tmp_path / "binding.json"
        self.binding.write_bytes(encoded(asdict(replace(model.binding, write_enabled=True))))

        def api_factory():
            api = transport(model)
            self.apis.append(api)
            return api

        monkeypatch.setattr(remote.GitHubAPI, "from_environment", api_factory)
        for handler in (delivery_commands, odds_commands):
            self._observe(handler, monkeypatch)

    def _observe(self, handler, monkeypatch):
        original = handler.execute

        def execute(args, store, load, fresh_reader):
            stage = {"command": args.store_command, "writer": store.backend}
            self.stages.append(stage)

            def observed_fresh():
                stage["wire_before_fresh"] = len(self.model.requests)
                stage["writer_requests"] = store.backend.api._requests
                if self.before_fresh is not None:
                    self.before_fresh()
                fresh = fresh_reader()
                stage["fresh"] = fresh.backend
                stage["initial_fresh_blobs"] = set(fresh.backend._blobs)
                return fresh

            return original(args, store, load, observed_fresh)

        monkeypatch.setattr(handler, "execute", execute)

    def run(self, command, payload, *, submission=None, reference=False):
        self.invocations += 1
        path = self.tmp_path / f"input-{self.invocations}.json"
        path.write_bytes(encoded(payload))
        binding = self.binding
        if reference:
            binding = self.tmp_path / "readonly-binding.json"
            binding.write_bytes(encoded(asdict(replace(self.model.binding, write_enabled=False))))
        args = ["store", "--binding", str(binding), command,
                "--ref" if reference else "--input", str(path)]
        if submission is not None:
            args.extend(["--submission-id", submission])
        calls_before, apis_before = len(self.model.requests), len(self.apis)
        code = main(args)
        captured = self.capsys.readouterr()
        self.properties.append((f"cli-{self.invocations}-{command}", json.dumps({
            "code": code, "wire_requests": len(self.model.requests) - calls_before,
            "api_requests": [api._requests for api in self.apis[apis_before:]],
            "writer_requests": self.stages[-1].get("writer_requests")})))
        return {"code": code, "result": json.loads(captured.out) if captured.out else None,
                "error": json.loads(captured.err) if captured.err else None,
                "calls": len(self.model.requests) - calls_before,
                "calls_before": calls_before,
                "apis": self.apis[apis_before:], "stage": self.stages[-1]}


@pytest.fixture
def cli(host, tmp_path, monkeypatch, capsys, request):
    return WireCLI(host, tmp_path, monkeypatch, capsys, request.node.user_properties)


def artifact_payload():
    return {"artifact": asdict(ArtifactInput.from_bytes(
                "cli-original", "original.bin", "application/octet-stream", b"original\x00\xff\r\n")),
            "rights_confirmed": True, "rights_basis": "Synthetic engineering fixture only."}


def assert_fresh_transport(outcome, model):
    stage = outcome["stage"]
    writer, fresh = stage["writer"], stage["fresh"]
    assert len(outcome["apis"]) == 1
    assert writer is not fresh and writer.api is fresh.api is outcome["apis"][0]
    assert fresh.binding == replace(writer.binding, write_enabled=False)
    assert 0 < stage["writer_requests"] < fresh.api._requests == outcome["calls"] <= remote.MAX_REQUESTS == 512
    for cache in ("_trees", "_blobs", "_ancestor_proofs"):
        assert getattr(writer, cache) is not getattr(fresh, cache)
    # Constructor validation may populate marker bytes, but originals must be
    # fetched again through the wire instead of inheriting the writer's cache.
    uncached = set(writer._blobs) - stage["initial_fresh_blobs"]
    assert uncached
    reread = {path.rsplit("/", 1)[-1] for method, path, _ in
              model.requests[stage["wire_before_fresh"]:] if method == "GET" and "/git/blobs/" in path}
    assert uncached & reread
    calls = len(model.requests)
    with pytest.raises(RecordError, match="REMOTE_READ_ONLY"):
        fresh.commit({"research/never-written.json": b"{}"}, expected=model.git.head())
    assert len(model.requests) == calls


def test_delivery_cli_uses_one_budget_and_independent_readonly_fresh_store(cli, host):
    payload = delivery_payload()
    outcome = cli.run("deliver-research", payload, submission="cli-delivery")
    assert outcome["code"] == 0 and outcome["error"] is None
    result = outcome["result"]
    assert result["readback"] == "fresh-reader-verified"
    check = result["delivery_verification"]
    assert check["status"] == "verified-declared-inventory" and check["errors"] == []
    assert len(check["items"]) == 3 and all(item["readback"] == "verified" for item in check["items"])
    assert_fresh_transport(outcome, host)
    assert host.writes == 5  # Intent, two originals, source, revision; no fresh write.
    host.git.repository = host.binding.repository
    local = VersionedStore(host.git)
    manifest = delivery_manifest(local.read_revision(BlobRef(**result["reference"]))["body"])
    for original, retained in zip(payload["items"][:2], manifest["items"][:2]):
        readback = ResearchArtifacts(local).read(retained["reference"])
        assert readback["data_base64"] == original["artifact"]["data_base64"]


def large_ref_only_delta(host):
    host.git.repository = host.binding.repository
    local = VersionedStore(host.git)
    payload = delivery_payload()
    payload["request"]["kind"] = "quick"
    payload["method_sha256"] = research_method("quick")["sha256"]
    originals = deepcopy(payload["items"])
    payload["items"] = originals[:2]
    for number in range(2, 20):
        payload["items"].append({"id": "artifact-" + str(number), "role": "supporting-file",
            "status": "include", "required": True, "reason": "",
            "artifact": asdict(ArtifactInput.from_bytes("original-" + str(number),
                "original-" + str(number) + ".html", "text/html",
                ("<!doctype html>\r\n<p>Synthetic original " + str(number) + "</p>").encode()))})
    for number in range(3):
        item = deepcopy(originals[2])
        item["id"] = "source-" + str(number)
        item["source"]["source_id"] = "synthetic-source-" + str(number)
        item["source"]["title"] += " " + str(number)
        payload["items"].append(item)
    first = ResearchDelivery(local).deliver(payload, submission_id="cli-budget-base")
    delta = continued(local, payload, first)
    assert delta["body_mode"] == "delta" and delta["request"]["kind"] == "quick"
    assert len(delta["items"]) == 23
    assert len({item["artifact_ref"]["commit"] for item in delta["items"] if "artifact_ref" in item}) == 20
    assert all("artifact" not in item and "source" not in item for item in delta["items"])
    return local, first, delta


def test_ref_only_delta_exhausts_one_cli_budget_and_later_read_can_verify(cli, host):
    local, first, payload = large_ref_only_delta(host)
    outcome = cli.run("deliver-research", payload, submission="cli-budget-delta")
    stats = {"code": outcome["code"], "wire": outcome["calls"],
             "writer_requests": outcome["stage"].get("writer_requests"),
             "api_requests": [api._requests for api in outcome["apis"]]}
    assert host.writes == 2, stats  # All material refs pre-exist; intent and delta only.
    assert 0 < outcome["stage"]["writer_requests"] < remote.MAX_REQUESTS == 512, stats
    assert outcome["code"] == 2 and outcome["error"] is None, stats
    result = outcome["result"]
    assert result["reference"] and result["delivery_verification"]["status"] == "failed", stats
    check = result["delivery_verification"]
    assert any(item.get("error") == "REMOTE_REQUEST_LIMIT" for item in check["items"])
    assert len(outcome["apis"]) == 1 and outcome["calls"] == 512, stats
    assert outcome["apis"][0]._requests > 512
    assert outcome["stage"]["fresh"].api is outcome["stage"]["writer"].api
    assert outcome["stage"]["fresh"] is not outcome["stage"]["writer"]
    assert outcome["stage"]["fresh"].binding.write_enabled is False
    manifest = delivery_manifest(local.read_revision(BlobRef(**result["reference"]))["body"])
    assert manifest["base_revision"] == first["reference"] and manifest["body_mode"] == "delta"
    assert [item["reference"] for item in manifest["items"]] == [
        item.get("artifact_ref", item.get("source_ref")) for item in payload["items"]]

    # A separately invoked read starts a new operation budget, verifies the
    # published reference and writes nothing. The prior ledger stays exhausted.
    exhausted_count = outcome["apis"][0]._requests
    later = cli.run("verify-delivery", result["reference"], reference=True)
    assert later["code"] == 0 and later["error"] is None
    assert later["result"]["reference"] == result["reference"]
    assert later["result"]["status"] == "verified-declared-inventory" and later["result"]["errors"] == []
    assert len(later["result"]["items"]) == 23
    assert all(item["readback"] == "verified" for item in later["result"]["items"])
    assert len(later["apis"]) == 1 and later["apis"][0] is not outcome["apis"][0]
    assert 0 < later["apis"][0]._requests == later["calls"] <= 512
    assert later["stage"]["writer"].binding.write_enabled is False
    assert outcome["apis"][0]._requests == exhausted_count and host.writes == 2


def test_put_artifact_success_preserves_shape_bytes_and_fresh_budget(cli, host):
    payload = artifact_payload()
    outcome = cli.run("put-artifact", payload, submission="cli-artifact")
    assert outcome["code"] == 0 and outcome["error"] is None
    assert set(outcome["result"]) == {"reference", "readback"}
    readback = outcome["result"]["readback"]
    assert set(readback) == {"reference", "metadata", "bytes_verified", "content_assessment", "executed"}
    assert readback["reference"] == outcome["result"]["reference"]
    assert readback["metadata"] == {key: value for key, value in payload["artifact"].items() if key != "data_base64"}
    assert readback["bytes_verified"] is True and readback["executed"] is False
    assert readback["content_assessment"] == "not-performed" and host.writes == 1
    assert_fresh_transport(outcome, host)


@pytest.mark.parametrize("command", ["freeze-valuation", "freeze-odds-watch"])
def test_odds_and_research_watch_share_the_cli_fresh_budget(cli, host, command, monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00Z")
    host.git.repository = host.binding.repository
    local = VersionedStore(host.git)
    source = local.put_source("cli-source", SourceInput(
        "synthetic", "content", "Synthetic source", "synthetic:cli", "Synthetic evidence"))
    research = local.submit_revision("cli-research", RevisionInput(
        "case", "r1", "full", "Synthetic partial research", "Synthetic mandate", "synthetic-method", (source,)))
    payload = authorized("spec", spec_payload(asdict(research))) if command == "freeze-valuation" else \
        authorized("watch", watch_payload(asdict(research), "research"))
    outcome = cli.run(command, payload, submission="cli-odds")
    assert outcome["code"] == 0 and outcome["error"] is None, outcome
    assert outcome["result"]["reference"] and outcome["result"]["readback"] == {
        "status": "verified", "new_reader": True, "economic_assessment": "not-performed"}
    assert_fresh_transport(outcome, host)
    assert host.writes == 1


def test_put_artifact_fresh_permission_failure_preserves_published_reference(cli, host):
    payload = artifact_payload()
    cli.before_fresh = lambda: host.meta.update(private=True)
    outcome = cli.run("put-artifact", payload, submission="cli-artifact")
    journal = json.loads(host.git.read(host.git.head(), "submissions/cli-artifact.json"))
    expected = asdict(host.git.reference(host.git.head(), journal["result_path"]))
    expected["repository"] = host.binding.repository
    assert outcome["code"] == 2 and host.writes == 1
    assert outcome["result"] is not None, outcome
    assert outcome["result"]["reference"] == expected
    assert outcome["result"]["readback"] == {
        "status": "failed", "error": "FRESH_ARTIFACT_READBACK_FAILED", "bytes_verified": False,
        "content_assessment": "not-performed", "executed": False,
    }
    assert outcome["error"] is None
    assert len(outcome["apis"]) == 1
    assert outcome["apis"][0]._requests == outcome["calls"]
    assert host.requests[-1][:2] == ("GET", "/repos/" + host.binding.repository)
    # A later, independent read may succeed after visibility is restored. It
    # reads the already published exact bytes and does not repeat the write.
    host.meta["private"] = False
    cli.before_fresh = None
    readback = cli.run("read-artifact", expected, reference=True)
    assert readback["code"] == 0 and readback["error"] is None
    assert readback["result"]["data_base64"] == payload["artifact"]["data_base64"]
    assert readback["result"]["bytes_verified"] is True and host.writes == 1
    assert readback["apis"][0] is not outcome["apis"][0]
    assert readback["apis"][0]._requests == readback["calls"]
    assert readback["stage"]["writer"].binding.write_enabled is False


def test_put_artifact_unknown_write_does_not_invent_a_reference_or_fresh_read(cli, host):
    def lose_response(_value):
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")

    host.after_write = lose_response
    outcome = cli.run("put-artifact", artifact_payload(), submission="cli-unknown-write")
    assert outcome["code"] == 2 and outcome["result"] is None
    assert outcome["error"] == {"error": "REMOTE_WRITE_UNCONFIRMED"}
    assert "wire_before_fresh" not in outcome["stage"] and "fresh" not in outcome["stage"]
    assert host.writes == 1 and len(outcome["apis"]) == 1
    assert outcome["apis"][0]._requests == outcome["calls"] <= 512
    journal = json.loads(host.git.read(host.git.head(), "submissions/cli-unknown-write.json"))
    assert host.git.read(host.git.head(), journal["result_path"])
