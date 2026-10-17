"""ZIP custody preserves original bytes; member names never become local paths."""
import base64
from copy import deepcopy
from dataclasses import asdict, replace
import io
import json
import zipfile

import pytest

from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_delivery import ResearchDelivery
from decision_kernel.application.store_audit import audit_store
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain import research_artifacts as artifact_domain
from decision_kernel.domain.records import BlobRef, Conflict, RecordError, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.git_snapshot import GitSnapshot, bundle_from_bare, git, verify_bundle
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import MAX_REQUESTS
from decision_kernel.interfaces import research_runtime as runtime
from decision_kernel.interfaces.delivery_pack import pack
from decision_kernel.interfaces.research_payload import pack_payload
from test_news_read_budget import transport
from test_research_delivery import continued, delivery_payload, workbook_bytes
from test_research_delivery_integration import local_inventory
from test_research_runtime import CODE, host


def _zip(parts):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in parts:
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            archive.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


def zip_bytes():
    """A deterministic synthetic archive, constructed only in test code."""
    inner = _zip([("旧版/正文.md", "# 合成旧正文\r\n保留原格式。\r\n".encode())])
    return _zip([
        ("说明/完整历史.txt", "合成计算与完整历史\r\n中文文件名与 CRLF。\r\n".encode()),
        ("数据/原表.csv", b"metric,value\r\nrevenue,3\r\ncash,2\r\n"),
        ("计算/重算.py", b"raise RuntimeError('synthetic archive must never execute')\r\n"),
        ("计算/工作簿.xlsx", workbook_bytes()),
        ("研究/旧报告.html", b"<!doctype html>\r\n<p>Synthetic original history</p>\r\n"),
        ("旧版/原包.zip", inner),
        ("../outside-synthetic.txt", b"A ZIP member name is opaque metadata, never an output path.\r\n"),
    ])


def zip_item(raw=None):
    return {"id": "archive", "role": "supporting-file", "status": "include", "required": True, "reason": "",
            "artifact": asdict(ArtifactInput.from_bytes("research-archive", "研究计算与完整历史.zip",
                "application/zip", zip_bytes() if raw is None else raw))}


def _native_binding(host):
    # The API fixture normally aliases its ref; a native bundle uses the actual
    # bare Git branch and an explicit matching marker/anchor.
    host.binding = replace(host.binding, branch="records")
    anchor = host.git.commit({"store.json": host.binding.marker()}, expected=host.git.head())
    host.binding = replace(host.binding, anchor=anchor)
    host.git.repository = host.binding.repository


def _host_operation(host, output, operation, payload, request, submission=""):
    api, start = transport(host), len(host.requests)
    inputs = {"operation": operation, "payload": pack_payload(encoded(payload)), "code_sha": CODE,
              "submission_id": submission,
              "expected_head": host.git.head() if operation in runtime.WRITE_OPERATIONS else ""}
    result = runtime.run_command(host.binding, api=api, inputs=inputs, code_sha=CODE,
                                 output=output, run_id="789", attempt="1")
    calls = len(host.requests) - start
    assert 0 < api._requests == calls <= MAX_REQUESTS == 512
    request.node.user_properties.append((operation + "-" + output.name,
                                         json.dumps({"wire_requests": calls, "api_requests": api._requests})))
    return result


def _assert_zip(readback, raw):
    expected = zip_item(raw)["artifact"]
    assert readback["metadata"] == {key: value for key, value in expected.items() if key != "data_base64"}
    assert base64.b64decode(readback["data_base64"], validate=True) == raw
    assert readback["bytes_verified"] is True and readback["executed"] is False
    assert readback["content_assessment"] == "not-performed"


def test_zip_pack_host_delta_and_native_restore_preserve_original_bytes(host, tmp_path, monkeypatch, request):
    _native_binding(host)
    raw = zip_bytes()
    payload = delivery_payload()
    payload["items"].append(zip_item(raw))
    inventory = local_inventory(tmp_path, payload)
    assert (tmp_path / "archive.original").read_bytes() == raw

    def no_container_parser(*_args, **_kwargs):
        pytest.fail("ZIP custody must not parse, unpack, or inspect member files")

    # The fixture is already built. Neither file packing, custody nor native Git
    # snapshot recovery needs a ZIP parser, including for the nested workbook.
    monkeypatch.setattr(zipfile, "ZipFile", no_container_parser)
    packed = pack(inventory)
    assert packed == payload
    first = _host_operation(host, tmp_path / "first", "deliver", packed, request, "zip-first")
    assert first["status"] == "completed" and first["write"] == "published"
    assert len(first["published_stages"]) == host.writes == 6
    first_result = first["result"]
    check = first_result["delivery_verification"]
    assert check["status"] == "verified-declared-inventory" and check["errors"] == []
    retained_zip = check["items"][-1]
    assert retained_zip["id"] == "archive" and retained_zip["readback"] == "verified"
    archive_ref = retained_zip["reference"]

    local = VersionedStore(host.git)
    delta = continued(local, packed, first_result)
    assert delta["body_mode"] == "delta" and delta["items"][-1]["artifact_ref"] == archive_ref
    assert all("artifact" not in item and "source" not in item for item in delta["items"])
    second = _host_operation(host, tmp_path / "delta", "deliver", delta, request, "zip-delta")
    assert second["status"] == "completed" and len(second["published_stages"]) == 2
    assert host.writes == 8
    assert second["result"]["delivery_verification"]["base_revision"] == first_result["reference"]
    assert second["result"]["delivery_verification"]["items"][-1]["reference"] == archive_ref

    snapshot, writes = host.git.head(), host.writes
    read = _host_operation(host, tmp_path / "read", "read-artifact", archive_ref, request)
    assert read["write"] == "not-requested"
    _assert_zip(read["result"], raw)
    assert host.git.head() == snapshot and host.writes == writes
    calls = len(host.requests)
    reader = VersionedStore(GitSnapshot(host.git.root, host.binding, snapshot))
    audit = audit_store(reader, snapshot)
    bundle = tmp_path / "zip-records.bundle"
    assert bundle_from_bare(host.git.root, host.binding, snapshot, bundle) == audit
    restored_root = tmp_path / "restored.git"
    git("clone", "--bare", "--single-branch", "--branch", host.binding.branch, "--template=",
        str(bundle), str(restored_root))
    restored = VersionedStore(GitSnapshot(restored_root, host.binding, snapshot))
    assert audit_store(restored, snapshot) == audit
    assert archive_ref in audit["artifacts"]
    _assert_zip(ResearchArtifacts(restored).read(archive_ref), raw)
    for receipt in (first_result, second["result"]):
        reopened = ResearchDelivery(restored).verify(BlobRef(**receipt["reference"]))
        assert reopened == receipt["delivery_verification"]
    assert len(host.requests) == calls and host.git.head() == snapshot and host.writes == writes
    with pytest.raises(RecordError, match="SNAPSHOT_READ_ONLY"):
        restored.backend.commit({}, expected=snapshot)


def test_supported_zip_does_not_rewrite_old_omissions_or_their_intent(tmp_path):
    local = VersionedStore(GitStore.init_synthetic(tmp_path / "records.git"))
    raw = zip_bytes()
    payload = delivery_payload()
    omissions = [
        {"id": name, "role": "supporting-file", "status": status, "required": True,
         "reason": "Explicit original declaration: " + status}
        for name, status in (("archive", "unsupported"), ("private-archive", "rights-excluded"),
                             ("absent-archive", "missing"))]
    payload["items"].extend(omissions)
    inventory = local_inventory(tmp_path, payload)
    (tmp_path / "available.zip").write_bytes(raw)
    selected = json.loads(inventory.read_bytes())
    for row in selected["items"][-3:]:
        row["artifact_file"] = {"path": "available.zip", "artifact_id": "research-archive",
                                "name": "研究计算与完整历史.zip", "media_type": "application/zip"}
    inventory.write_bytes(encoded(selected))
    assert pack(inventory) == payload  # Even an available supported file cannot override a declaration.
    first = ResearchDelivery(local).deliver(payload, submission_id="declared-omissions")
    before = local.backend.head()
    changed = deepcopy(payload)
    changed["items"][-3] = zip_item(raw)
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        ResearchDelivery(local).deliver(changed, submission_id="declared-omissions")
    assert local.backend.head() == before

    supplement = continued(local, payload, first)
    supplement["items"][-3] = zip_item(raw)
    second = ResearchDelivery(local).deliver(supplement, submission_id="new-zip-supplement")
    old_check = ResearchDelivery(local).verify(BlobRef(**first["reference"]))
    new_check = ResearchDelivery(local).verify(BlobRef(**second["reference"]))
    assert old_check["status"] == new_check["status"] == "partial"
    assert old_check["errors"] == new_check["errors"] == []
    assert [{key: item[key] for key in omissions[0]} for item in old_check["items"][-3:]] == omissions
    assert all(item["readback"] == "not-retained" for item in old_check["items"][-3:])
    assert new_check["base_revision"] == first["reference"]
    assert new_check["items"][-3]["readback"] == "verified"
    assert new_check["items"][-2:] == old_check["items"][-2:]
    _assert_zip(ResearchArtifacts(local).read(new_check["items"][-3]["reference"]), raw)


def test_pre_zip_strict_reader_and_bundle_audit_reject_the_new_media_type(host, tmp_path, monkeypatch):
    _native_binding(host)
    local = VersionedStore(host.git)
    ordinary = ResearchArtifacts(local).put("ordinary", ArtifactInput.from_bytes(
        "ordinary", "ordinary.txt", "text/plain", b"A previously supported artifact."))
    raw = zip_bytes()
    archive = ResearchArtifacts(local).put("archive", ArtifactInput.from_dict(zip_item(raw)["artifact"]))
    snapshot = host.git.head()
    bundle = tmp_path / "compatibility.bundle"
    expected = bundle_from_bare(host.git.root, host.binding, snapshot, bundle)
    calls = len(host.requests)

    # This is the exact pre-ZIP policy, not a lax compatibility reader. The
    # unchanged artifact schema still revalidates its media type on every read.
    with monkeypatch.context() as old:
        old.setattr(artifact_domain, "MEDIA_TYPES", artifact_domain.MEDIA_TYPES - {"application/zip"})
        reader = VersionedStore(GitSnapshot(host.git.root, host.binding, snapshot))
        assert ResearchArtifacts(reader).read(ordinary)["bytes_verified"] is True
        with pytest.raises(RecordError, match="UNSUPPORTED_ARTIFACT_TYPE"):
            ResearchArtifacts(reader).read(archive)
        with pytest.raises(RecordError, match="UNSUPPORTED_ARTIFACT_TYPE"):
            audit_store(reader, snapshot)
        with pytest.raises(RecordError, match="UNSUPPORTED_ARTIFACT_TYPE"):
            verify_bundle(bundle, host.binding, snapshot)

    assert verify_bundle(bundle, host.binding, snapshot) == expected
    _assert_zip(ResearchArtifacts(VersionedStore(GitSnapshot(host.git.root, host.binding, snapshot))).read(archive), raw)
    assert len(host.requests) == calls and host.git.head() == snapshot
