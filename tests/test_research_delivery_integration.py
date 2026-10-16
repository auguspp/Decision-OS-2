"""Actual local Git and installed processes; host transport is synthetic, not stock evidence."""
import base64
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess

import pytest

from decision_kernel.application.research_delivery import ResearchDelivery, delivery_manifest
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, Conflict, MissingObject, RecordError, UnknownWrite, encoded
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure.git_snapshot import bundle_from_bare, verify_bundle
from decision_kernel.interfaces import research_runtime as runtime
from decision_kernel.interfaces.delivery_pack import pack
from decision_kernel.interfaces.research_payload import pack_payload
from test_research_delivery import delivery_payload, continued
from test_research_runtime import host, CODE


def invoke(host, out, op, payload, submission="", expected=None):
    args = {"operation": op, "payload": pack_payload(encoded(payload)), "code_sha": CODE,
            "submission_id": submission, "expected_head": expected or
            (host.git.head() if op in runtime.WRITE_OPERATIONS else "")}
    return runtime.run_command(host.binding, api=host, inputs=args, code_sha=CODE,
                               output=out, run_id="456", attempt="1")


def test_public_host_delivery_fresh_read_and_readonly_open(host, tmp_path):
    payload = delivery_payload()
    saved = invoke(host, tmp_path / "saved", "deliver", payload, "delivery-one")
    result = saved["result"]
    assert saved["status"] == "completed" and saved["write"] == "published"
    assert len(host.requests) <= 512  # Match the real transport budget for this use case.
    assert len(saved["published_stages"]) == 5  # intent, HTML, XLSX, source, revision
    assert result["readback"] == "fresh-reader-verified" and result["research_assessment"] == "not-performed"
    assert result["delivery_verification"]["status"] == "verified-declared-inventory"
    assert json.loads((tmp_path / "saved/report.json").read_text())["result"] == result
    head, writes = host.git.head(), host.writes
    opened = invoke(host, tmp_path / "opened", "open-delivery", {"reference": result["reference"]})
    assert opened["result"]["context"]["research"]["reference"] == result["reference"]
    assert opened["result"]["continuation"]["actions"][1]["question_id"] == "value"
    ref = result["delivery_verification"]["items"][1]["reference"]
    file = invoke(host, tmp_path / "file", "read-artifact", ref)
    assert file["result"]["data_base64"] == payload["items"][1]["artifact"]["data_base64"]
    assert file["write"] == "not-requested" and host.git.head() == head and host.writes == writes
    again = invoke(host, tmp_path / "reconciled", "reconcile-delivery", payload, "delivery-one")
    assert again["result"]["reference"] == result["reference"]
    assert again["write"] == "reconciled-without-write" and host.writes == writes


def test_partial_delivery_preserves_receipt_but_is_not_completed(host, tmp_path):
    payload = delivery_payload()
    payload["items"][1].pop("artifact")
    payload["items"][1].update(status="missing", reason="Original bytes not available.")
    saved = invoke(host, tmp_path / "partial", "deliver", payload, "partial")
    assert saved["status"] == "delivery-not-complete" and saved["write"] == "published"
    assert saved["result"]["reference"] and saved["result"]["delivery_verification"]["status"] == "partial"


def test_refusal_precedes_public_input_retention_and_token_use(host, tmp_path):
    payload = delivery_payload(); payload["rights_confirmed"] = False
    with pytest.raises(RecordError, match="RIGHTS"):
        invoke(host, tmp_path / "refused", "deliver", payload, "no")
    assert host.writes == 0 and not (tmp_path / "refused").exists()


def test_reconcile_missing_intent_never_stages_a_file(host, tmp_path):
    with pytest.raises(MissingObject, match="NO_WRITE"):
        invoke(host, tmp_path / "missing", "reconcile-delivery", delivery_payload(), "missing")
    assert host.writes == 0


def test_unknown_partial_save_reconciles_without_writing_then_resumes_original(host, tmp_path):
    payload = delivery_payload()
    count = 0
    def lose(_):
        nonlocal count
        count += 1
        if count == 2:
            raise UnknownWrite("SIMULATED_LOST_RESPONSE")
        return _
    host.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        invoke(host, tmp_path / "lost", "deliver", payload, "one")
    host.after_write = None
    before = host.git.head(); writes = host.writes
    with pytest.raises(MissingObject, match="NO_WRITE"):
        invoke(host, tmp_path / "audit", "reconcile-delivery", payload, "one")
    assert host.writes == writes and host.git.head() == before
    changed = deepcopy(payload); changed["progress"]["body"] += " different"
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        invoke(host, tmp_path / "conflict", "deliver", changed, "one")
    assert host.git.head() == before
    saved = invoke(host, tmp_path / "resume", "deliver", payload, "one")
    assert saved["status"] == "completed" and host.writes == 5


def test_interleaved_remote_write_does_not_advance_delivery_permission(host, tmp_path):
    count = 0
    def interleave(_):
        nonlocal count
        count += 1
        if count == 1:
            host.git.commit({"submissions/unrelated.json": b"{}\n"}, expected=host.git.head())
        return _
    host.after_write = interleave
    with pytest.raises((Conflict, UnknownWrite)):
        invoke(host, tmp_path / "interleaved", "deliver", delivery_payload(), "one")
    assert host.git.read(host.git.head(), "submissions/unrelated.json") == b"{}\n"
    report = json.loads((tmp_path / "interleaved/report.json").read_text())
    assert report["status"] != "completed" and "result" not in report


def test_old_body_sources_and_binary_originals_survive_native_bundle_restore(host, tmp_path):
    # Generic API fixture aliases its branch; native bundles require the actual ref.
    host.binding = replace(host.binding, branch="records")
    anchor = host.git.commit({"store.json": host.binding.marker()}, expected=host.git.head())
    host.binding = replace(host.binding, anchor=anchor)
    payload = delivery_payload()
    receipt = invoke(host, tmp_path / "save", "deliver", payload, "one")["result"]
    store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    next_payload = continued(store, payload, receipt)
    invoke(host, tmp_path / "second", "deliver", next_payload, "two")
    snapshot = host.git.head()
    path = tmp_path / "records.bundle"
    exported = bundle_from_bare(host.git.root, host.binding, snapshot, path)
    verified = verify_bundle(path, host.binding, snapshot)
    assert exported == verified and verified["artifact_versions"] == 4
    assert verified["research_revisions"] == 2 and verified["source_versions"] == 1


def local_inventory(root, payload):
    value = deepcopy(payload)
    for item in value["items"]:
        if "artifact" in item:
            artifact = item.pop("artifact")
            path = item["id"] + ".original"
            (root / path).write_bytes(base64.b64decode(artifact["data_base64"]))
            item["artifact_file"] = {"path": path, **{k: artifact[k] for k in ("artifact_id", "name", "media_type")}}
    body = value["progress"].pop("body")
    (root / "body.md").write_bytes(body.encode())
    value["progress"]["body_file"] = "body.md"
    input_file = root / "inventory.json"
    input_file.write_bytes(encoded(value))
    return input_file


def test_offline_packer_preserves_html_css_crlf_and_xlsx_formula_bytes(tmp_path):
    payload = delivery_payload()
    file = local_inventory(tmp_path, payload)
    result = pack(file)
    assert result == payload
    wire = pack_payload(encoded(result))
    assert len(wire.encode()) < 60 * 1024 and len(encoded(payload)) > 60 * 1024


def test_packer_missing_original_reports_missing_never_recreates(tmp_path):
    file = local_inventory(tmp_path, delivery_payload())
    (tmp_path / "model.original").unlink()
    result = pack(file)
    assert result["items"][1]["status"] == "missing" and "artifact" not in result["items"][1]


def test_packer_never_opens_withheld_file_or_transmits_its_local_path(tmp_path):
    file = local_inventory(tmp_path, delivery_payload())
    value = json.loads(file.read_bytes())
    value["items"][1].update(status="rights-excluded", reason="No publication rights")
    value["items"][1]["artifact_file"]["path"] = "../private.xlsx"
    file.write_bytes(encoded(value))
    result = pack(file)
    assert "artifact_file" not in result["items"][1] and "../private.xlsx" not in encoded(result).decode()


@pytest.mark.parametrize("path", ["../private", "/etc/passwd", "folder/../../private"])
def test_packer_rejects_path_escape(tmp_path, path):
    file = local_inventory(tmp_path, delivery_payload())
    value = json.loads(file.read_bytes()); value["items"][0]["artifact_file"]["path"] = path
    file.write_bytes(encoded(value))
    with pytest.raises(RecordError, match="PATH_INVALID"):
        pack(file)


def test_packer_rejects_symlink_without_reading_target(tmp_path):
    file = local_inventory(tmp_path, delivery_payload())
    (tmp_path / "html.original").unlink()
    (tmp_path / "html.original").symlink_to(tmp_path / "model.original")
    with pytest.raises(RecordError, match="SYMLINK"):
        pack(file)


def exercise_installed_delivery(command, outside, python, env, cli, store):
    """Extend the existing one-wheel consumer, not a separate build/install test."""
    payload = delivery_payload()
    root = outside / "delivery-originals"; root.mkdir()
    inventory = local_inventory(root, payload)
    packed = subprocess.run([str(python), "-I", "-m", "decision_kernel.interfaces.delivery_pack",
                             "--input", str(inventory)], cwd=outside, env=env,
                             check=True, capture_output=True)
    assert json.loads(packed.stdout) == payload
    input_file = root / "delivery.json"; input_file.write_bytes(packed.stdout)
    saved = command("deliver-research", "--input", str(input_file), "--submission-id", "installed-delivery")
    assert saved["readback"] == "fresh-reader-verified"
    reference = root / "revision.json"; reference.write_bytes(encoded(saved["reference"]))
    opened = command("open-delivery", "--ref", str(reference))
    assert opened["context"]["research"]["reference"] == saved["reference"]
    assert {a["question_id"] for a in opened["continuation"]["actions"]} == {"cash", "value"}
    for original, retained in zip(payload["items"][:2], opened["delivery"]["items"][:2]):
        file_ref = root / "file-ref.json"; file_ref.write_bytes(encoded(retained["reference"]))
        actual = command("read-artifact", "--ref", str(file_ref))
        assert actual["data_base64"] == original["artifact"]["data_base64"]
    partial = deepcopy(payload)
    partial["request"]["case_id"] = "installed-partial"
    partial["items"][1].pop("artifact")
    partial["items"][1].update(status="missing", reason="Original absent")
    input_file.write_bytes(encoded(partial))
    run = subprocess.run([str(cli), "store", "--path", str(store), "deliver-research", "--input", str(input_file),
                          "--submission-id", "installed-partial"], cwd=outside, env=env, capture_output=True)
    assert run.returncode == 2
    result = json.loads(run.stdout)
    assert result["reference"] and result["delivery_verification"]["status"] == "partial"
